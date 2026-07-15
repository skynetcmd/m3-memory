#!/usr/bin/env python3
"""
mem0_scan.py — Scan a codebase for mem0 usage and report the m3 swap.

The m3 LangChain surface (``m3_memory.langchain.Memory``) is a drop-in for
``mem0.Memory``: the import line changes, and mem0-identical calls
(``.add()``/``.search()``/``.get()``/``.delete()``/…) keep working byte-for-byte.
This tool finds every mem0 import + call site in a target tree, tells you which
calls are drop-in vs. which map to an m3-native extra vs. which have no
equivalent, and (with ``--fix``) rewrites the import line in place.

It is AST-based, so it only flags real mem0 usage — not the substring "mem0" in
a comment or unrelated string.

Usage:
    python bin/mem0_scan.py PATH [PATH ...]      # report only
    python bin/mem0_scan.py PATH --fix           # also rewrite import lines
    python bin/mem0_scan.py PATH --json          # machine-readable report

Exit code: 0 if no mem0 usage found, 1 if any found (report), 0 after --fix.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# mem0 module names whose ``Memory`` / ``AsyncMemory`` / ``MemoryClient`` we treat
# as swappable. ``mem0ai`` is the PyPI dist; ``mem0`` the import package.
MEM0_MODULES = {"mem0", "mem0ai"}
MEM0_CLASSES = {"Memory", "AsyncMemory", "MemoryClient"}

# The m3 mem0-compat surface (m3_memory/integrations/langchain/mem0_compat.py).
# Keep in sync with the public methods on that ``Memory`` class.
M3_DROP_IN = {
    "add", "search", "get", "get_all", "delete", "delete_all", "from_config",
}
# mem0 verbs that m3 exposes under a first-class extra (works, but the m3-native
# name is better) — reported so a migrant knows the stronger tool exists.
M3_MAPPED = {
    "update": "supersede(old_id, new_content)  # targeted, bi-temporal — not a flat overwrite",
    "history": "history(memory_id)             # supersession chain over time",
    "reset": "forget(user_id=...)              # GDPR Art.17 hard-erase (scoped to a user)",
}
# mem0 verbs with no m3 equivalent — a migrant must handle these by hand.
M3_UNSUPPORTED = {
    "chat": "no m3 equivalent — m3 is a memory store, not a chat wrapper; call your LLM directly.",
}

SKIP_DIRS = {
    ".git", ".hg", "__pycache__", ".venv", "venv", "env", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", ".eggs",
    "site-packages",
}

M3_IMPORT = "from m3_memory.langchain import Memory"


@dataclass
class Finding:
    line: int
    col: int
    kind: str          # "import" | "drop-in" | "mapped" | "unsupported"
    text: str          # the source snippet or method name
    note: str = ""     # guidance


@dataclass
class FileReport:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    import_lines: list[int] = field(default_factory=list)  # 1-based, to rewrite

    @property
    def has_mem0(self) -> bool:
        return bool(self.findings)


class _Visitor(ast.NodeVisitor):
    """Collect mem0 imports, the local names bound to mem0 classes, and calls on
    instances constructed from those classes."""

    def __init__(self) -> None:
        # local name -> mem0 class it refers to (from `from mem0 import Memory`
        # or `from mem0 import Memory as M`)
        self.class_aliases: dict[str, str] = {}
        # module aliases (`import mem0` / `import mem0 as m`): local -> "mem0"
        self.module_aliases: dict[str, str] = {}
        # instance var name -> True (constructed from a mem0 class)
        self.instances: set[str] = set()
        self.findings: list[Finding] = []
        self.import_lines: list[int] = []

    # --- imports -----------------------------------------------------------
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root in MEM0_MODULES:
            for alias in node.names:
                if alias.name in MEM0_CLASSES:
                    local = alias.asname or alias.name
                    self.class_aliases[local] = alias.name
            self.import_lines.append(node.lineno)
            self.findings.append(Finding(
                node.lineno, node.col_offset, "import",
                f"from {node.module} import ...",
                note=f"swap → {M3_IMPORT}",
            ))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in MEM0_MODULES:
                self.module_aliases[alias.asname or alias.name] = "mem0"
                self.import_lines.append(node.lineno)
                self.findings.append(Finding(
                    node.lineno, node.col_offset, "import",
                    f"import {alias.name}"
                    + (f" as {alias.asname}" if alias.asname else ""),
                    note="m3 has no top-level `mem0` module import; use "
                         f"`{M3_IMPORT}` and construct Memory() directly.",
                ))
        self.generic_visit(node)

    # --- construction: track instances -------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        cls = self._constructed_mem0_class(node.value)
        if cls and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.instances.add(node.targets[0].id)
        self.generic_visit(node)

    def _constructed_mem0_class(self, value: ast.AST) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        func = value.func
        # Memory(...) where Memory was `from mem0 import Memory`
        if isinstance(func, ast.Name) and func.id in self.class_aliases:
            return self.class_aliases[func.id]
        # mem0.Memory(...) where `import mem0`
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in self.module_aliases
                and func.attr in MEM0_CLASSES):
            return func.attr
        # Memory.from_config(...) classmethod construction
        if (isinstance(func, ast.Attribute) and func.attr == "from_config"
                and isinstance(func.value, ast.Name)
                and func.value.id in self.class_aliases):
            return self.class_aliases[func.value.id]
        return None

    # --- calls: classify method usage --------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            inst, method = func.value.id, func.attr
            if inst in self.instances:
                self._classify(method, node.lineno, node.col_offset)
        self.generic_visit(node)

    def _classify(self, method: str, line: int, col: int) -> None:
        if method in M3_DROP_IN:
            self.findings.append(Finding(
                line, col, "drop-in", method,
                note="identical on m3 — no code change.",
            ))
        elif method in M3_MAPPED:
            self.findings.append(Finding(
                line, col, "mapped", method,
                note="m3 extra → " + M3_MAPPED[method],
            ))
        elif method in M3_UNSUPPORTED:
            self.findings.append(Finding(
                line, col, "unsupported", method,
                note=M3_UNSUPPORTED[method],
            ))
        # methods not in any table are left unflagged: they're either not
        # memory calls or user code — we only speak to the mem0 surface.


def scan_file(path: Path) -> FileReport:
    report = FileReport(path)
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return report
    # Cheap prefilter: no "mem0" substring → skip the AST parse entirely.
    if "mem0" not in src:
        return report
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return report
    v = _Visitor()
    v.visit(tree)
    report.findings = v.findings
    report.import_lines = sorted(set(v.import_lines))
    return report


def iter_py_files(paths: list[Path]):
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                yield f


def apply_fix(report: FileReport) -> bool:
    """Rewrite mem0 import lines to the m3 import in place. Only touches
    `from mem0 import ...` lines that bind Memory-like classes; leaves other
    lines alone. Returns True if the file was modified."""
    if not report.import_lines:
        return False
    lines = report.path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    already_has_m3 = any(M3_IMPORT in ln for ln in lines)
    for ln_no in report.import_lines:
        idx = ln_no - 1
        if idx >= len(lines):
            continue
        original = lines[idx]
        stripped = original.lstrip()
        indent = original[: len(original) - len(stripped)]
        # Only auto-rewrite the common `from mem0 import Memory[ as X]` form.
        if stripped.startswith(("from mem0", "from mem0ai")):
            newline = "\n" if original.endswith("\n") else ""
            if already_has_m3:
                lines[idx] = f"{indent}# mem0_scan: removed — Memory now from m3_memory.langchain{newline}"
            else:
                lines[idx] = f"{indent}{M3_IMPORT}  # was: {stripped.rstrip()}{newline}"
                already_has_m3 = True
            changed = True
        # `import mem0` forms are left for manual fix (attribute access differs).
    if changed:
        report.path.write_text("".join(lines), encoding="utf-8")
    return changed


# ── rendering ──────────────────────────────────────────────────────────────
_ICON = {"import": "IMPORT", "drop-in": "  OK  ", "mapped": " MAP  ",
         "unsupported": " STOP "}


def render_text(reports: list[FileReport]) -> str:
    out: list[str] = []
    totals = {"import": 0, "drop-in": 0, "mapped": 0, "unsupported": 0}
    hit_reports = [r for r in reports if r.has_mem0]
    for r in hit_reports:
        out.append(f"\n{r.path}")
        for f in r.findings:
            totals[f.kind] += 1
            tag = _ICON.get(f.kind, f.kind)
            head = f"  {tag}  L{f.line}: "
            body = f.text if f.kind == "import" else f".{f.text}()"
            out.append(f"{head}{body}")
            if f.note:
                out.append(f"           ↳ {f.note}")
    if not hit_reports:
        return "No mem0 usage found. Nothing to migrate."
    out.append("\n" + "─" * 60)
    out.append(
        f"Summary: {len(hit_reports)} file(s) use mem0 · "
        f"{totals['import']} import(s) · {totals['drop-in']} drop-in call(s) · "
        f"{totals['mapped']} mapped · {totals['unsupported']} unsupported"
    )
    out.append(
        f"Swap the import to `{M3_IMPORT}`; drop-in calls need no change. "
        "MAP calls work but an m3-native verb is stronger; STOP calls need "
        "manual handling. Re-run with --fix to rewrite imports."
    )
    return "\n".join(out)


def render_json(reports: list[FileReport]) -> str:
    payload = [
        {
            "path": str(r.path),
            "findings": [
                {"line": f.line, "col": f.col, "kind": f.kind,
                 "text": f.text, "note": f.note}
                for f in r.findings
            ],
        }
        for r in reports if r.has_mem0
    ]
    return json.dumps({"files": payload}, indent=2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path, help="files or directories to scan")
    ap.add_argument("--fix", action="store_true",
                    help="rewrite `from mem0 import ...` lines to the m3 import in place")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    reports = [scan_file(f) for f in iter_py_files(args.paths)]

    if args.fix:
        fixed = [r for r in reports if r.has_mem0 and apply_fix(r)]
        for r in fixed:
            print(f"fixed import(s) in {r.path}")
        remaining = sum(1 for r in reports for f in r.findings
                        if f.kind in ("mapped", "unsupported"))
        print(f"\nRewrote imports in {len(fixed)} file(s). "
              f"{remaining} call site(s) still need review (MAP/STOP) — "
              "run without --fix to list them.")
        return 0

    if args.json:
        print(render_json(reports))
    else:
        print(render_text(reports))
    return 1 if any(r.has_mem0 for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
