"""Generate per-tool inventory memory entries under memory/tool_inventory/.

Parses each Python file in bin/ that looks like a CLI tool (has argparse or
__main__). Extracts, per tool:
  - module docstring (purpose)
  - argparse options (flags, help, defaults)
  - env vars read via os.environ.get / os.getenv
  - entry points (main/async main + __main__ guard)
  - calls INTO this repo (imports from sibling bin/*.py or project packages)
  - calls OUT to external systems (subprocess, urllib/requests/httpx, sqlite,
    os.system, file writes/opens with string constants) — helps surface
    dependencies on side-channel tools or filesystem paths.
  - file dependencies (string constants that reference files under the repo)
  - file mtime + sha1 (so stale entries can be detected)

Writes one markdown file per tool plus an index. Re-run after code changes;
a diff in the 'sha1' field tells you which entries need re-validation.
"""
from __future__ import annotations

import ast
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
OUT_DIR = BASE_DIR / "memory" / "tool_inventory"

SKIP = {"gen_tool_inventory.py", "__init__.py"}
PRIVATE = {"discord_bot.py", "status_api.py", "embed_server_gpu.py"}

_ENV_RE = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*['"]([A-Z0-9_]+)['"]""")

# External-call surface we care about. Maps attribute-call pattern → bucket.
_EXTERNAL_BUCKETS: dict[tuple[str, ...], str] = {
    ("subprocess", "run"): "subprocess",
    ("subprocess", "Popen"): "subprocess",
    ("subprocess", "check_call"): "subprocess",
    ("subprocess", "check_output"): "subprocess",
    ("subprocess", "call"): "subprocess",
    ("os", "system"): "subprocess",
    ("os", "execv"): "subprocess",
    ("os", "execvp"): "subprocess",
    ("requests", "get"): "http",
    ("requests", "post"): "http",
    ("requests", "put"): "http",
    ("requests", "delete"): "http",
    ("requests", "request"): "http",
    ("httpx", "get"): "http",
    ("httpx", "post"): "http",
    ("httpx", "AsyncClient"): "http",
    ("httpx", "Client"): "http",
    ("urllib", "request"): "http",
    ("sqlite3", "connect"): "sqlite",
    ("aiosqlite", "connect"): "sqlite",
}


def file_sha1(path: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    h.update(path.read_bytes())
    return h.hexdigest()[:12]


def extract_docstring(tree: ast.Module) -> str:
    return (ast.get_docstring(tree) or "").strip()


def extract_env_vars(source: str) -> list[str]:
    return sorted(set(_ENV_RE.findall(source)))


def _literal(n):
    if n is None:
        return None
    if isinstance(n, ast.Constant):
        return n.value
    try:
        return ast.unparse(n)
    except Exception:
        return None


def extract_argparse(tree: ast.Module) -> list[dict]:
    args: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        arg_names: list[str] = []
        for pos in node.args:
            if isinstance(pos, ast.Constant) and isinstance(pos.value, str):
                arg_names.append(pos.value)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        args.append({
            "names": arg_names,
            "help": _literal(kw.get("help")) or "",
            "default": _literal(kw.get("default")),
            "type": _literal(kw.get("type")),
            "action": _literal(kw.get("action")),
            "required": _literal(kw.get("required")),
            "choices": _literal(kw.get("choices")),
        })
    return args


def is_cli_tool(tree: ast.Module, source: str) -> bool:
    if "argparse.ArgumentParser" in source:
        return True
    if "__main__" in source and ("def main" in source or "async def main" in source):
        return True
    return False


def _repo_module_set() -> set[str]:
    """Every top-level module name reachable as a repo import.

    Covers bin/*.py (stem), top-level packages (any directory with
    __init__.py directly under the repo root), and a small set of known
    project packages we ship without __init__.py.
    """
    mods: set[str] = set()
    for p in BIN_DIR.glob("*.py"):
        mods.add(p.stem)
    for pkg_dir in BASE_DIR.iterdir():
        if pkg_dir.is_dir() and (pkg_dir / "__init__.py").exists():
            mods.add(pkg_dir.name)
    # Known project-internal packages (kept small and explicit).
    mods.update({"m3_sdk", "memory_core", "auth_utils", "embedding_utils",
                 "temporal_utils", "memory_bridge", "mcp_proxy"})
    return mods


def extract_entry_points(tree: ast.Module, source: str) -> list[str]:
    eps: list[str] = []
    # Top-level defs named main / run / cli / entrypoint etc.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {"main", "run", "cli", "entrypoint"}:
                kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                eps.append(f"`{kind} {node.name}()` (line {node.lineno})")
    if re.search(r"""if\s+__name__\s*==\s*['"]__main__['"]""", source):
        eps.append("`if __name__ == \"__main__\"` guard")
    return eps


def extract_imports(tree: ast.Module, repo_mods: set[str], self_name: str
                    ) -> tuple[list[str], list[str]]:
    """Return (intra_repo_imports, external_imports_of_interest)."""
    intra: set[str] = set()
    external: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top == self_name:
                    continue
                (intra if top in repo_mods else external).add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            top = node.module.split(".", 1)[0]
            if top == self_name:
                continue
            # Record "from X import a, b" as "X (a, b)" for readability.
            names = ", ".join(a.name for a in node.names)
            entry = f"{node.module} ({names})" if names else node.module
            (intra if top in repo_mods else external).add(entry)
    return sorted(intra), sorted(external)


def extract_external_calls(tree: ast.Module) -> dict[str, list[str]]:
    """Classify external-surface calls into buckets (subprocess/http/sqlite)."""
    buckets: dict[str, set[str]] = {"subprocess": set(), "http": set(), "sqlite": set()}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Flatten attribute chain: a.b.c -> ("a","b","c")
        parts: list[str] = []
        cur = func
        while isinstance(cur, ast.Attribute):
            parts.insert(0, cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.insert(0, cur.id)
        if len(parts) < 2:
            continue
        key = (parts[0], parts[-1])
        bucket = _EXTERNAL_BUCKETS.get(key)
        if bucket is None:
            continue
        pretty = ".".join(parts) + "()"
        # Try to capture first string arg as hint of target (e.g. URL, cmd)
        for a in node.args[:2]:
            lit = _literal(a)
            if isinstance(lit, str) and len(lit) < 80:
                pretty += f"  → `{lit}`"
                break
            if isinstance(lit, list) and lit and isinstance(lit[0], str):
                pretty += f"  → `{lit[0]}`"
                break
        buckets[bucket].add(f"`{pretty}` (line {node.lineno})")
    return {k: sorted(v) for k, v in buckets.items() if v}


def extract_file_deps(source: str) -> list[str]:
    """Find string literals that look like repo-relative paths or known files.

    We keep this deliberately narrow to avoid false positives:
      - Strings containing a path separator AND a known extension.
      - Strings matching known config/template file names.
    """
    known_files = {
        "AGENT_INSTRUCTIONS.md", "CLAUDE.md", "GEMINI.md", ".mcp.json",
        "MEMORY.md", "pyproject.toml", "requirements.txt",
    }
    exts = (".sql", ".md", ".json", ".yaml", ".yml", ".toml", ".db",
            ".txt", ".sh", ".template", ".entitlements", ".ini", ".conf")
    found: set[str] = set()
    # Match Python string literals (single or double quoted, no escape handling).
    for m in re.finditer(r"""['"]([^'"\n]{2,200})['"]""", source):
        s = m.group(1)
        base = s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in known_files:
            found.add(s)
            continue
        if not s.endswith(exts):
            continue
        # Heuristic: needs to look like a path (slash or sensible basename).
        if "/" in s or "\\" in s or re.match(r"^[A-Za-z0-9_.\-]+$", s):
            # Drop obvious non-paths (URLs, mime types, etc).
            if "://" in s or s.startswith(("http", "Content-", "application/")):
                continue
            found.add(s)
    # Prefer repo-relative paths; truncate very long lists.
    return sorted(found)[:40]


def render_entry(relpath: str, sha1: str, mtime: str, doc: str,
                 args: list[dict], env_vars: list[str],
                 entry_points: list[str], intra_imports: list[str],
                 external_imports: list[str], external_calls: dict[str, list[str]],
                 file_deps: list[str], private: bool) -> str:
    L: list[str] = []
    L += ["---",
          f"tool: {relpath}",
          f"sha1: {sha1}",
          f"mtime_utc: {mtime}",
          f"generated_utc: {datetime.now(timezone.utc).isoformat()}",
          f"private: {'true' if private else 'false'}",
          "---",
          "",
          f"# {relpath}",
          "",
          "## Purpose",
          "",
          doc if doc else "_(no module docstring — update the source file.)_",
          "",
          "## Entry points",
          ""]
    if entry_points:
        for ep in entry_points:
            L.append(f"- {ep}")
    else:
        L.append("_(no conventional entry point detected)_")
    L += ["", "## CLI flags / arguments", ""]
    if not args:
        L.append("_(no argparse arguments detected)_")
    else:
        L.append("| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |")
        L.append("|---|---|---|---|---|---|")
        for a in args:
            names = ", ".join(f"`{n}`" for n in a["names"]) or "_positional_"
            help_ = (a["help"] or "").replace("|", "\\|").replace("\n", " ")
            default = a["default"]
            default_s = "—" if default is None else f"`{default}`"
            ta = a.get("type") or a.get("action") or ""
            L.append(f"| {names} | {help_} | {default_s} |  | {ta} |  |")
    L += ["", "## Environment variables read", ""]
    if not env_vars:
        L.append("_(none detected)_")
    else:
        for v in env_vars:
            L.append(f"- `{v}`")
    L += ["", "## Calls INTO this repo (intra-repo imports)", ""]
    if intra_imports:
        for imp in intra_imports:
            L.append(f"- `{imp}`")
    else:
        L.append("_(none detected)_")
    L += ["", "## Calls OUT (external side-channels)", ""]
    if external_calls:
        for bucket, calls in external_calls.items():
            L.append(f"**{bucket}**")
            L.append("")
            for c in calls:
                L.append(f"- {c}")
            L.append("")
    else:
        L.append("_(no subprocess / http / sqlite calls detected)_")
    L += ["", "## Notable external imports", ""]
    if external_imports:
        interesting = [i for i in external_imports
                       if not i.split(" ", 1)[0].split(".", 1)[0] in {
                           "os", "sys", "re", "json", "time", "datetime",
                           "pathlib", "typing", "argparse", "collections",
                           "contextlib", "functools", "itertools", "asyncio",
                           "logging", "io", "hashlib", "uuid", "random",
                           "shutil", "tempfile", "enum", "dataclasses", "math",
                           "traceback", "warnings", "subprocess", "urllib",
                           "textwrap", "threading", "signal", "queue", "socket",
                           "struct", "string", "copy", "ast", "inspect", "glob",
                           "zipfile", "tarfile", "sqlite3", "__future__"}]
        if interesting:
            for i in interesting[:30]:
                L.append(f"- `{i}`")
        else:
            L.append("_(only stdlib)_")
    else:
        L.append("_(only stdlib)_")
    L += ["", "## File dependencies (repo paths referenced)", ""]
    if file_deps:
        for f in file_deps:
            L.append(f"- `{f}`")
    else:
        L.append("_(none detected)_")
    L += ["", "## Re-validation", "",
          "If the `sha1` above differs from the current file's sha1, the inventory "
          "is stale — re-read the tool, confirm flags/env vars/entry-points/calls "
          "still match, and regenerate via `python bin/gen_tool_inventory.py`.",
          ""]
    return "\n".join(L)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    repo_mods = _repo_module_set()
    index_entries: list[tuple[str, str, bool]] = []

    for src in sorted(BIN_DIR.glob("*.py")):
        if src.name in SKIP:
            continue
        try:
            source = src.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError) as e:
            print(f"skip {src.name}: {e}")
            continue

        if not is_cli_tool(tree, source):
            continue

        doc = extract_docstring(tree)
        args = extract_argparse(tree)
        env_vars = extract_env_vars(source)
        entry_points = extract_entry_points(tree, source)
        intra, external = extract_imports(tree, repo_mods, src.stem)
        external_calls = extract_external_calls(tree)
        file_deps = extract_file_deps(source)
        sha1 = file_sha1(src)
        mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat()
        private = src.name in PRIVATE

        rel = f"bin/{src.name}"
        out_path = OUT_DIR / (src.stem + ".md")
        out_path.write_text(
            render_entry(rel, sha1, mtime, doc, args, env_vars,
                         entry_points, intra, external, external_calls,
                         file_deps, private),
            encoding="utf-8",
        )
        index_entries.append((rel, doc.split("\n", 1)[0] or "(no docstring)", private))
        print(f"wrote {out_path.relative_to(BASE_DIR)}")

    idx_lines = ["# Tool inventory index", ""]
    idx_lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}._")
    idx_lines.append("")
    idx_lines.append("Re-run `python bin/gen_tool_inventory.py` after changing any tool.")
    idx_lines.append("Entries whose `sha1` no longer matches the live file need re-validation.")
    idx_lines.append("")
    idx_lines.append("| Tool | Summary | Private |")
    idx_lines.append("|---|---|---|")
    for rel, summary, private in sorted(index_entries):
        name = rel.split("/")[-1].replace(".py", "")
        summary_clean = summary.replace("|", "\\|")[:120]
        idx_lines.append(f"| [{rel}]({name}.md) | {summary_clean} | {'yes' if private else ''} |")
    (OUT_DIR / "INDEX.md").write_text("\n".join(idx_lines) + "\n", encoding="utf-8")
    print(f"wrote {(OUT_DIR / 'INDEX.md').relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
