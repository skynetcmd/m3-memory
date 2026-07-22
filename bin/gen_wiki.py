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


def _open_ro_sqlite(path: str) -> sqlite3.Connection:
    """Open a local sqlite DB read-only (URI mode) so the generator never mutates
    it. Used for the FILES corpus, which is a local SQLite sidecar on every backend
    (files_database.db) — the memory store, by contrast, goes through the backend
    seam (see _build_vault) so the wiki works on PostgreSQL too."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _build_vault(args: argparse.Namespace, out_dir: str) -> dict[str, str]:
    opts = WikiOptions(
        importance_threshold=(args.importance_threshold
                              if args.importance_threshold is not None else 0.6),
        include_files=not args.no_files,
        use_networkx=not args.no_networkx,
        exclude_regex=getattr(args, "exclude", None),
        obsidian=getattr(args, "obsidian", False),
    )

    synthesizer = None
    if getattr(args, "synthesize", False):
        from wiki.synth import SynthConfig, Synthesizer
        cache_dir = os.path.join(out_dir, ".synth-cache")
        synthesizer = Synthesizer(SynthConfig.from_env(cache_dir))

    # Files corpus: always a local SQLite sidecar (files_database.db), on every
    # backend — open it read-only directly. Absent → memory-only vault.
    files_conn = None
    if not args.no_files:
        fpath = _files_db_path()
        if os.path.isfile(fpath):
            files_conn = _open_ro_sqlite(fpath)

    # Memory store: route through m3's backend seam so the wiki reads the ACTIVE
    # backend (SQLite default, or PostgreSQL) rather than assuming a local .db
    # file. The seam yields a live connection; build inside the `with`.
    try:
        from memory.db import _db as _memory_seam
    except Exception:
        _memory_seam = None

    try:
        if _memory_seam is not None:
            with _memory_seam() as mem_conn:
                vault = build_wiki(mem_conn, files_conn, opts, synthesizer=synthesizer)
        else:
            # Fallback (payload not importable): the legacy local-SQLite path.
            mem_path = _memory_db_path()
            if not os.path.isfile(mem_path):
                print(f"memory DB not found at {mem_path} — run `m3 setup` first.",
                      file=sys.stderr)
                raise SystemExit(2)
            mem_conn = _open_ro_sqlite(mem_path)
            try:
                vault = build_wiki(mem_conn, files_conn, opts, synthesizer=synthesizer)
            finally:
                mem_conn.close()
    finally:
        if files_conn is not None:
            files_conn.close()

    if synthesizer is not None:
        print(synthesizer.summary(), file=sys.stderr)
    return vault


# Manifest of the files m3 generated, so a later regen can prune ONLY its own
# stale pages and never touch user-authored notes / .obsidian config / caches.
_MANIFEST = ".m3-wiki-manifest.json"


def _read_manifest(out_dir: str) -> list[str]:
    path = os.path.join(out_dir, _MANIFEST)
    try:
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("generated", [])
        return [p for p in files if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def _write_manifest(out_dir: str, relpaths: list[str]) -> None:
    import json
    path = os.path.join(out_dir, _MANIFEST)
    payload = {"generated": sorted(relpaths)}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _prune_stale(vault: dict[str, str], out_dir: str) -> list[str]:
    """Delete files m3 generated on a PRIOR run that it no longer generates.

    Compliance-relevant: a GDPR/soft-deleted memory drops out of the vault, so its
    page must be removed from disk — a lingering page would retain 'forgotten'
    content. We only ever delete paths recorded in our own manifest and absent from
    the new vault, so user notes / .obsidian/ / .synth-cache are never touched.
    """
    prev = set(_read_manifest(out_dir))
    now = set(vault.keys())
    stale = sorted(prev - now)
    removed: list[str] = []
    for relpath in stale:
        # Defense in depth: never delete outside out_dir, never touch dotfiles/dirs
        # we don't own (a manifest entry is always a relative page path we wrote).
        dest = os.path.normpath(os.path.join(out_dir, relpath))
        if not dest.startswith(os.path.normpath(out_dir) + os.sep):
            continue
        try:
            if os.path.isfile(dest):
                os.remove(dest)
                removed.append(relpath)
        except OSError:
            pass
    # Best-effort: clean up now-empty generated subdirs (topics/, sources/).
    for sub in ("topics", "sources"):
        d = os.path.join(out_dir, sub)
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return removed


def _write_vault(vault: dict[str, str], out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    removed = _prune_stale(vault, out_dir)
    written = 0
    for relpath, text in sorted(vault.items()):
        dest = os.path.join(out_dir, relpath)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        written += 1
    _write_manifest(out_dir, list(vault.keys()))
    try:
        shown = os.path.relpath(out_dir)
    except ValueError:
        shown = out_dir
    msg = f"wrote {written} pages to {shown}"
    if removed:
        msg += f" · pruned {len(removed)} stale page(s)"
    print(msg)
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
    if args.check and getattr(args, "synthesize", False):
        print("--check and --synthesize are mutually exclusive: LLM ledes are not "
              "bit-reproducible, so the drift check runs on the deterministic vault "
              "only (drop --synthesize).", file=sys.stderr)
        return 2
    vault = _build_vault(args, out_dir)
    if args.check:
        return _check_vault(vault, out_dir)
    rc = _write_vault(vault, out_dir)
    if getattr(args, "html", False):
        _write_html(vault, out_dir)
    return rc


def _write_html(vault: dict[str, str], out_dir: str) -> None:
    from wiki.html_view import build_html
    html = build_html(vault)
    dest = os.path.join(out_dir, "wiki.html")
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    try:
        shown = os.path.relpath(dest)
    except ValueError:
        shown = dest
    print(f"wrote self-contained viewer to {shown} (open it in a browser)")


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
    p.add_argument("--synthesize", action="store_true",
                   help="Write an LLM prose lede per topic via a local chat endpoint "
                        "(opt-in; cached; degrades to member-lists if no model). "
                        "Mutually exclusive with --check.")
    p.add_argument("--importance-threshold", type=float, default=None,
                   help="Min importance for a memory to count as 'core' (default 0.6).")
    p.add_argument("--exclude", default=None, metavar="REGEX",
                   help="Drop any memory whose title/content matches this regex "
                        "(case-insensitive) — e.g. to keep private/bench notes out "
                        "of a shareable vault.")
    p.add_argument("--html", action="store_true",
                   help="Also write a single self-contained wiki.html viewer — open "
                        "it in any browser to click through the vault offline "
                        "(no server, no dependencies).")
    p.add_argument("--obsidian", action="store_true",
                   help="Emit [[wikilinks]] instead of standard Markdown links so "
                        "Obsidian's graph view and backlinks work. (Wikilinks show "
                        "as literal text outside Obsidian, so this is opt-in.)")


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
