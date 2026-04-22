"""Generate per-tool inventory entries under docs/tools/.

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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
SCRIPTS_DIR = BASE_DIR / "scripts"
BENCHMARKS_DIR = BASE_DIR / "benchmarks"
# Flat dirs scanned with *.py (top level). benchmarks/ is walked recursively
# below because it has harness subfolders (longmemeval/, locomo/).
SOURCE_DIRS = [BIN_DIR, SCRIPTS_DIR]
RECURSIVE_SOURCE_DIRS = [BENCHMARKS_DIR]
# Top-level files at the repo root — a small set of user-facing entry
# points that live at the root because install docs and runbooks point
# users there (e.g. `python install_os.py` from the project root).
# Scanned individually rather than via a glob so we never accidentally
# pick up ad-hoc diagnostic scripts that land at the root during
# development. Add new files here explicitly only after verifying they
# belong in the published inventory.
ROOT_FILES = [
    BASE_DIR / "install_os.py",
    BASE_DIR / "run_tests.py",
    BASE_DIR / "scan_repo_v7.py",
    BASE_DIR / "validate_env.py",
]
OUT_DIR = BASE_DIR / "docs" / "tools"

SKIP = {"gen_tool_inventory.py", "__init__.py"}
PRIVATE = {
    "discord_bot.py", "status_api.py", "embed_server_gpu.py",
    # macOS-oriented — runs on Win/Linux too but primary UX is a macOS pulse
    # dashboard. Inventoried so imports show up in the call graph but flagged
    # in INDEX.md's "Private" column to dampen general visibility.
    "mission_control.py",
    # Homepage dashboard endpoint — runs on a MacBook serving /status JSON.
    # Mentions the device name in its docstring; mark private so the inventory
    # table doesn't surface it in the default listing.
    "macbook_status_server.py",
}

# Core library modules worth auditing even though they lack a CLI surface —
# central enough that other tools import them, so they belong in the graph.
CORE_LIBRARIES = {
    "memory_core.py", "memory_bridge.py", "mcp_tool_catalog.py", "mcp_proxy.py",
    "m3_sdk.py", "auth_utils.py", "temporal_utils.py", "agent_protocol.py",
    "embedding_utils.py", "custom_tool_bridge.py", "debug_agent_bridge.py",
    # Post-2026-04-21 refactor additions: chatlog + maintenance + sync + LLM
    # failover are load-bearing even without a CLI surface.
    "chatlog_config.py", "chatlog_core.py", "chatlog_redaction.py",
    "chatlog_status.py", "memory_maintenance.py", "memory_sync.py",
    "llm_failover.py",
}

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


def _tracked_files() -> set[Path]:
    """Absolute paths of every file currently tracked by git.

    Used to filter gitignored / locally-added files out of the inventory pass
    before we ever read their source. The motivation: some scripts under
    bin/ are gitignored (like discord_bot.py) because they hardcode internal
    homelab IPs or device names; the inventory generator reads the working
    tree, so without this filter it would happily regenerate inventory
    entries that leak those details into docs/tools/. Running git once per
    invocation is cheap and the fallback (returning an empty set, which
    disables filtering) keeps the tool usable in worktrees without git —
    with a warning so the operator knows the gate is off.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(BASE_DIR), "ls-files"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"warn: git ls-files failed ({type(e).__name__}); "
              f"generator will NOT filter untracked files. "
              f"Check for leaks in output if this host is shared.",
              flush=True)
        return set()
    return {(BASE_DIR / line).resolve() for line in result.stdout.splitlines() if line.strip()}


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
    uses_database_helper = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Detect the standardized add_database_arg(parser) helper from m3_sdk.
        # The helper wraps parser.add_argument under the hood, so the direct
        # walk would miss the --database flag without this fallback.
        if isinstance(func, ast.Name) and func.id == "add_database_arg":
            uses_database_helper = True
            continue
        if isinstance(func, ast.Attribute) and func.attr == "add_database_arg":
            uses_database_helper = True
            continue
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
            "default_passed": "default" in kw,
            "type": _literal(kw.get("type")),
            "action": _literal(kw.get("action")),
            "required": _literal(kw.get("required")),
            "choices": _literal(kw.get("choices")),
        })
    if uses_database_helper and not any("--database" in a["names"] for a in args):
        # Synthesize the canonical row so the inventory reflects the actual
        # CLI surface. Matches m3_sdk.add_database_arg's signature.
        args.append({
            "names": ["--database"],
            "help": (
                "SQLite database path. Env: M3_DATABASE. "
                "Default: memory/agent_memory.db."
            ),
            "default": None,
            "default_passed": True,
            "type": None,
            "action": None,
            "required": None,
            "choices": None,
        })
    return args


def is_cli_tool(tree: ast.Module, source: str, name: str = "") -> bool:
    """Decide whether a source file belongs in the inventory.

    Previous logic required either an ``argparse.ArgumentParser`` literal
    or a ``def main`` / ``async def main`` paired with a ``__main__`` guard.
    That missed files like mission_control.py (entry point is
    ``run_dashboard``) and benchmarks/locomo/probe_issues.py (entry is
    ``probe_dataset`` + sibling probes). Relaxed to: any ``__main__`` guard
    qualifies — the inventory's value (env vars, file deps, intra-repo
    imports, call-out buckets) is useful even without argparse.
    """
    if name in CORE_LIBRARIES:
        return True
    if "argparse.ArgumentParser" in source:
        return True
    # A __main__ guard is sufficient. Regex matches both single- and
    # double-quoted guards and tolerates whitespace variance.
    if re.search(r"""if\s+__name__\s*==\s*['"]__main__['"]""", source):
        return True
    return False


def _repo_module_set() -> set[str]:
    """Every top-level module name reachable as a repo import.

    Covers bin/*.py (stem), top-level packages (any directory with
    __init__.py directly under the repo root), and a small set of known
    project packages we ship without __init__.py.
    """
    mods: set[str] = set()
    for d in SOURCE_DIRS:
        if d.is_dir():
            for p in d.glob("*.py"):
                mods.add(p.stem)
    for d in RECURSIVE_SOURCE_DIRS:
        if d.is_dir():
            for p in d.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
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


_FLAG_OVERRIDES_KEY = tuple[str, str]  # (default_behavior, impact_when_set)

_GEN_UTC_RE = re.compile(r"^generated_utc:\s*(\S+)\s*$", re.MULTILINE)
_MTIME_RE = re.compile(r"^mtime_utc:\s*(\S+)\s*$", re.MULTILINE)
_SHA1_RE = re.compile(r"^sha1:\s*(\S+)\s*$", re.MULTILINE)


def _read_front_field(path: Path, field_re: re.Pattern[str]) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = field_re.search(text)
    return m.group(1) if m else None


def _body_without_volatile_fields(path: Path, mtime_placeholder: str,
                                  gen_placeholder: str) -> str | None:
    """Load prior file with generated_utc AND mtime_utc replaced by
    placeholders. Lets us detect whether the *content* changed while ignoring
    non-content metadata that might shift between runs (e.g. filesystem
    touches that bump mtime without changing sha1). Returns None if missing.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = _GEN_UTC_RE.sub(f"generated_utc: {gen_placeholder}", text, count=1)
    text = _MTIME_RE.sub(f"mtime_utc: {mtime_placeholder}", text, count=1)
    return text


def parse_existing_flag_overrides(path: Path) -> dict[tuple[str, int], tuple[str, str, str]]:
    """Read hand-curated cells from a prior inventory file's CLI flag table.
    Keyed by (first_flag_name, ordinal_occurrence). Value is a triple of
    (help_text, default_behavior, impact_when_set).

    Ordinal disambiguates files that define the same flag multiple times
    (e.g. migrate_memory.py has `--target` in several subparsers with
    different semantics). AST walk order is stable, so ordinals are stable
    across regens as long as argparse calls aren't reordered in source.

    Columns 4 (Default behavior) and 6 (Impact when set) are human-written
    and always preserved when non-empty. Column 2 (Help) is preserved only
    as a fallback when the source has no `help=` kwarg — if the source
    later adds one, the source wins. Returns empty dict if file doesn't
    exist, lacks the flag table, or the table is malformed.
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    # Find the CLI flag table. The header row locks in column order.
    header_re = re.compile(
        r"^\|\s*Flag\(s\)\s*\|.*Default behavior.*Impact when set\s*\|\s*$",
        re.MULTILINE,
    )
    m = header_re.search(text)
    if not m:
        return {}
    # Scan table body lines until we hit a non-table line.
    out: dict[tuple[str, int], tuple[str, str, str]] = {}
    seen_count: dict[str, int] = {}
    start = text.find("\n", m.end()) + 1  # skip to line after the `|---|...|` separator
    start = text.find("\n", start) + 1    # now past the separator
    for line in text[start:].splitlines():
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        flag_cell, help_text, _default, default_behavior, _type, impact = cells[:6]
        # First flag name, stripped of `` and commas.
        first_flag = flag_cell.split(",", 1)[0].strip().strip("`").strip()
        if not first_flag or first_flag == "_positional_":
            continue
        ordinal = seen_count.get(first_flag, 0)
        seen_count[first_flag] = ordinal + 1
        # Only keep if any column was filled by hand.
        if help_text or default_behavior or impact:
            out[(first_flag, ordinal)] = (help_text, default_behavior, impact)
    return out


def render_entry(relpath: str, sha1: str, mtime: str, doc: str,
                 args: list[dict], env_vars: list[str],
                 entry_points: list[str], intra_imports: list[str],
                 external_imports: list[str], external_calls: dict[str, list[str]],
                 file_deps: list[str], private: bool,
                 flag_overrides: dict[tuple[str, int], tuple[str, str, str]] | None = None,
                 generated_utc: str | None = None) -> str:
    flag_overrides = flag_overrides or {}
    if generated_utc is None:
        generated_utc = datetime.now(timezone.utc).isoformat()
    L: list[str] = []
    L += ["---",
          f"tool: {relpath}",
          f"sha1: {sha1}",
          f"mtime_utc: {mtime}",
          f"generated_utc: {generated_utc}",
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
        ordinal_counter: dict[str, int] = {}
        for a in args:
            names = ", ".join(f"`{n}`" for n in a["names"]) or "_positional_"
            help_ = (a["help"] or "").replace("|", "\\|").replace("\n", " ")
            default = a["default"]
            if a.get("default_passed"):
                # Explicit default= was supplied (even if the value is None).
                default_s = "None" if default is None else f"`{default}`"
            elif a.get("action") == "store_true":
                default_s = "`False`"
            elif a.get("action") == "store_false":
                default_s = "`True`"
            else:
                default_s = "—"
            # argparse defaults unspecified type to str for flags that take a
            # value. store_true / store_false supply action instead. Preserve
            # that implicit info rather than leaving the column blank.
            ta = a.get("type") or a.get("action")
            if not ta:
                ta = "" if a.get("action") else "str"
            first_flag = a["names"][0] if a["names"] else ""
            ordinal = ordinal_counter.get(first_flag, 0)
            ordinal_counter[first_flag] = ordinal + 1
            prior_help, default_behavior, impact = flag_overrides.get(
                (first_flag, ordinal), ("", "", ""))
            # Help column: source wins when present; fall back to prior
            # hand-curated help when source has none.
            if not help_ and prior_help:
                help_ = prior_help
            L.append(f"| {names} | {help_} | {default_s} | {default_behavior} | {ta} | {impact} |")
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
                       if i.split(" ", 1)[0].split(".", 1)[0] not in {
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

    # Tracked-files filter: only generate inventory for files under version
    # control. Untracked files in bin/ (gitignored WIP, machine-local
    # experiments, leaks-by-accident) are silently ignored so their contents
    # never reach docs/tools/. Empty set disables filtering — see the warning
    # in _tracked_files() for when that happens.
    tracked = _tracked_files()
    untracked_skipped: list[str] = []

    sources: list[Path] = []
    for d in SOURCE_DIRS:
        if d.is_dir():
            sources.extend(sorted(d.glob("*.py")))
    # Recursive dirs (e.g. benchmarks/) have harness subfolders — walk them
    # but skip __pycache__ and site-packages-like trees.
    for d in RECURSIVE_SOURCE_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            sources.append(p)
    # Explicit root-level files (no glob — avoids accidentally picking up
    # throwaway scripts that land at the root during development).
    for p in ROOT_FILES:
        if p.is_file():
            sources.append(p)

    for src in sources:
        if src.name in SKIP:
            continue
        if tracked and src.resolve() not in tracked:
            untracked_skipped.append(str(src.relative_to(BASE_DIR)))
            continue
        try:
            source = src.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError) as e:
            print(f"skip {src.name}: {e}")
            continue

        if not is_cli_tool(tree, source, src.name):
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

        # Relative path rooted at repo root, with forward slashes for portability
        # in markdown links.
        try:
            rel = src.relative_to(BASE_DIR).as_posix()
        except ValueError:
            rel = f"{src.parent.name}/{src.name}"
        out_path = OUT_DIR / (src.stem + ".md")

        # Preserve hand-curated flag columns from the prior file.
        flag_overrides = parse_existing_flag_overrides(out_path)
        # Build the live (flag, ordinal) set so we can warn about orphans
        # (overrides pointing at flags that were removed from source).
        live_keys: set[tuple[str, int]] = set()
        _live_counter: dict[str, int] = {}
        for a in args:
            if not a["names"]:
                continue
            fn = a["names"][0]
            ord_ = _live_counter.get(fn, 0)
            _live_counter[fn] = ord_ + 1
            live_keys.add((fn, ord_))
        orphans = [k for k in flag_overrides if k not in live_keys]
        for orphan in orphans:
            name, ord_ = orphan
            suffix = f" (occurrence #{ord_ + 1})" if ord_ else ""
            print(f"  warn: {rel} — dropping override for removed flag {name!r}{suffix}")
            flag_overrides.pop(orphan, None)

        # Preserve generated_utc + mtime_utc when content is unchanged, to
        # avoid spurious git diffs. Policy:
        #   - If prior sha1 matches AND the rendered body (minus volatile
        #     metadata) matches, keep the prior mtime and generated_utc.
        #   - If sha1 matches but body differs (flag override edited by hand,
        #     docstring reparsed differently, etc.), refresh generated_utc
        #     but keep the prior mtime.
        #   - If sha1 differs (real source change), refresh both.
        prior_sha1 = _read_front_field(out_path, _SHA1_RE)
        prior_mtime = _read_front_field(out_path, _MTIME_RE)
        prior_generated_utc = _read_front_field(out_path, _GEN_UTC_RE)

        gen_placeholder = "__GEN_UTC_PLACEHOLDER__"
        mtime_placeholder = "__MTIME_PLACEHOLDER__"
        candidate = render_entry(rel, sha1, mtime_placeholder, doc, args, env_vars,
                                 entry_points, intra, external, external_calls,
                                 file_deps, private, flag_overrides,
                                 generated_utc=gen_placeholder)
        prior_body = _body_without_volatile_fields(
            out_path, mtime_placeholder, gen_placeholder)
        now_iso = datetime.now(timezone.utc).isoformat()

        if prior_sha1 == sha1 and prior_body == candidate:
            # Fully unchanged — keep prior metadata verbatim.
            mtime_to_write = prior_mtime or mtime
            generated_utc = prior_generated_utc or now_iso
        elif prior_sha1 == sha1:
            # Source content unchanged but rendered body differs — keep
            # prior mtime (it still describes the source), refresh gen time.
            mtime_to_write = prior_mtime or mtime
            generated_utc = now_iso
        else:
            # Real source change.
            mtime_to_write = mtime
            generated_utc = now_iso

        rendered = (candidate
                    .replace(gen_placeholder, generated_utc, 1)
                    .replace(mtime_placeholder, mtime_to_write, 1))
        out_path.write_text(rendered, encoding="utf-8")
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

    if untracked_skipped:
        print(f"skipped {len(untracked_skipped)} untracked file(s) "
              f"(not in git ls-files — intentional if gitignored):")
        for path in untracked_skipped:
            print(f"  - {path}")


if __name__ == "__main__":
    main()
