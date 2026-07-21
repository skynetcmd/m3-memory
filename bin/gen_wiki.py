#!/usr/bin/env python3
"""gen_wiki.py — compile a browsable wiki from core memories + the files corpus.

Thin CLI shell around the pure builder in `bin/wiki/`. Reads agent_memory.db and
(optionally) files_database.db, renders deterministic Markdown, and writes an
Obsidian-ready vault. Default output is <engine_root>/wiki.

    python bin/gen_wiki.py generate [--out DIR] [--check] [--no-files]
                                    [--importance-threshold F]
    python bin/gen_wiki.py status  [--out DIR]

Invoked by `m3 wiki generate` / `m3 wiki status` (see m3_memory/cli.py). The heavy
clustering dep (networkx) is optional: `pip install "m3-memory[wiki]"` upgrades
cluster quality; without it a pure-Python fallback is used.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Make bin/ importable (mirrors consolidate_beliefs.py et al.).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wiki.build import WikiOptions, build_wiki  # noqa: E402


def _engine_root() -> str:
    try:
        from m3_core.paths import get_m3_engine_root
        return get_m3_engine_root()
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".m3", "engine")


def _default_out() -> str:
    return os.path.join(_engine_root(), "wiki")


def _memory_db_path() -> str:
    try:
        from files_memory.config import memory_db_path
        return memory_db_path()
    except Exception:
        return os.path.join(_engine_root(), "agent_memory.db")


def _files_db_path() -> str:
    try:
        from files_memory.config import FILES_DB_PATH
        return FILES_DB_PATH
    except Exception:
        return os.path.join(_engine_root(), "files_database.db")


def _open_ro(path: str) -> sqlite3.Connection:
    """Open a sqlite DB read-only (URI mode) so the generator never mutates it."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _build_vault(args: argparse.Namespace) -> dict[str, str]:
    mem_path = _memory_db_path()
    if not os.path.isfile(mem_path):
        print(f"memory DB not found at {mem_path} — run `m3 setup` first.", file=sys.stderr)
        raise SystemExit(2)
    mem_conn = _open_ro(mem_path)

    files_conn = None
    if not args.no_files:
        fpath = _files_db_path()
        if os.path.isfile(fpath):
            files_conn = _open_ro(fpath)

    opts = WikiOptions(
        importance_threshold=(args.importance_threshold
                              if args.importance_threshold is not None else 0.6),
        include_files=not args.no_files,
        use_networkx=not args.no_networkx,
    )
    try:
        return build_wiki(mem_conn, files_conn, opts)
    finally:
        mem_conn.close()
        if files_conn is not None:
            files_conn.close()


def _write_vault(vault: dict[str, str], out_dir: str) -> int:
    written = 0
    for relpath, text in sorted(vault.items()):
        dest = os.path.join(out_dir, relpath)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written += 1
    try:
        shown = os.path.relpath(out_dir)
    except ValueError:
        shown = out_dir
    print(f"wrote {written} pages to {shown}")
    return 0


def _check_vault(vault: dict[str, str], out_dir: str) -> int:
    drifted: list[str] = []
    for relpath, text in sorted(vault.items()):
        dest = os.path.join(out_dir, relpath)
        if not os.path.isfile(dest):
            drifted.append(f"missing: {relpath}")
            continue
        with open(dest, encoding="utf-8") as f:
            if f.read() != text:
                drifted.append(f"changed: {relpath}")
    if drifted:
        print("wiki is stale — run `m3 wiki generate`:", file=sys.stderr)
        for d in drifted:
            print(f"  {d}", file=sys.stderr)
        return 1
    print(f"wiki up to date ({len(vault)} pages)")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    out_dir = args.out or _default_out()
    vault = _build_vault(args)
    if args.check:
        return _check_vault(vault, out_dir)
    return _write_vault(vault, out_dir)


def _cmd_status(args: argparse.Namespace) -> int:
    out_dir = args.out or _default_out()
    if not os.path.isdir(out_dir):
        print(f"no wiki at {out_dir} — run `m3 wiki generate`.")
        return 0
    pages = []
    for root, _dirs, filenames in os.walk(out_dir):
        for fn in filenames:
            if fn.endswith(".md"):
                pages.append(os.path.join(root, fn))
    if not pages:
        print(f"wiki dir {out_dir} exists but has no pages — run `m3 wiki generate`.")
        return 0
    latest = max(os.path.getmtime(p) for p in pages)
    import datetime
    ts = datetime.datetime.fromtimestamp(latest).isoformat(timespec="seconds")
    print(f"wiki: {out_dir}")
    print(f"pages: {len(pages)}")
    print(f"last build: {ts}")
    return 0


def _add_generate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default=None, help="Output vault dir (default <engine_root>/wiki).")
    p.add_argument("--check", action="store_true",
                   help="Exit non-zero if the on-disk vault differs from a fresh build.")
    p.add_argument("--no-files", action="store_true",
                   help="Skip the files corpus (memory-only vault).")
    p.add_argument("--no-networkx", action="store_true",
                   help="Force the pure-Python clustering fallback even if networkx is present.")
    p.add_argument("--importance-threshold", type=float, default=None,
                   help="Min importance for a memory to count as 'core' (default 0.6).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gen_wiki.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    p_gen = sub.add_parser("generate", help="Compile the wiki vault.")
    _add_generate_args(p_gen)
    p_gen.set_defaults(func=_cmd_generate)

    p_status = sub.add_parser("status", help="Report vault location, page count, last build.")
    p_status.add_argument("--out", default=None)
    p_status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        # Default to generate for a bare invocation.
        args = parser.parse_args(["generate"] + (argv or []))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
