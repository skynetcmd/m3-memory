#!/usr/bin/env python3
"""
migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory
into the m3-memory MCP server.

Supported sources:
    claude    ~/.claude/projects/<slug>/memory/*.md  (YAML frontmatter)
    gemini    ~/.gemini/GEMINI.md                    (## Gemini Added Memories bullets)
    openclaw  ~/.openclaw/memory/main.sqlite         (read-only, chunks table)
    rules     CLAUDE.md / GEMINI.md / AGENTS.md / CONVENTIONS.md  (opt-in)

Idempotent: each source item gets a stable `source_key` stored in metadata.
Re-runs skip items whose `source_key` already exists in m3-memory.

Verification: after writing, each new memory is round-tripped through
memory_get_impl — content and SHA-256 hash must match before it counts as
migrated. Verified items are listed for manual cleanup at the end; this
script never deletes source files.

Usage:
    python bin/migrate_flat_memory.py --dry-run
    python bin/migrate_flat_memory.py
    python bin/migrate_flat_memory.py --sources claude,gemini
    python bin/migrate_flat_memory.py --include-rules
    python bin/migrate_flat_memory.py --claude-project-slug C--Users-bhaba-m3-memory
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

BIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = BIN_DIR.parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import memory_core  # noqa: E402  — after sys.path tweak

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [migrate_flat_memory] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("migrate_flat_memory")

HOME = Path.home()

# ── Source roots (all absolute, all real) ──────────────────────────────────────
CLAUDE_PROJECTS_ROOT = HOME / ".claude" / "projects"
CLAUDE_GLOBAL_RULES = HOME / ".claude" / "CLAUDE.md"
GEMINI_GLOBAL_MD = HOME / ".gemini" / "GEMINI.md"
OPENCLAW_DB = HOME / ".openclaw" / "memory" / "main.sqlite"

ALLOWED_SOURCE_ROOTS = (
    HOME / ".claude",
    HOME / ".gemini",
    HOME / ".openclaw",
    HOME / ".config" / "opencode",
    HOME / ".aider.conf.yml",
    REPO_ROOT,
)

# ── Type mappings ──────────────────────────────────────────────────────────────
# Claude Code uses its own taxonomy in frontmatter; map to m3 types.
CLAUDE_TYPE_MAP = {
    "user": "user_fact",
    "feedback": "preference",
    "project": "plan",
    "reference": "reference",
    # passthrough for anything already m3-native
    "note": "note", "fact": "fact", "decision": "decision",
    "preference": "preference", "task": "task", "observation": "observation",
    "plan": "plan", "summary": "summary", "code": "code", "config": "config",
    "snippet": "snippet", "knowledge": "knowledge", "log": "log",
    "home": "home", "user_fact": "user_fact", "scratchpad": "scratchpad",
}

VALID_M3_TYPES = {
    "auto", "code", "config", "conversation", "decision", "fact", "home",
    "knowledge", "log", "message", "note", "observation", "plan", "preference",
    "reference", "scratchpad", "snippet", "summary", "task", "user_fact",
    "chat_log",
}

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
GEMINI_SECTION_HEADER = "## Gemini Added Memories"

# ── Data model ─────────────────────────────────────────────────────────────────
@dataclass
class SourceItem:
    """One unit of memory extracted from a source, ready to write."""
    source_agent: str                  # "claude" | "gemini" | "openclaw" | "rules"
    source_path: str                   # absolute path on disk
    source_locator: str                # e.g. filename, "line:42", "chunk:<id>"
    source_key: str                    # stable dedup key (sha256 of source_agent+locator+content)
    m3_type: str
    title: str
    content: str
    extra_metadata: dict = field(default_factory=dict)

@dataclass
class WriteResult:
    item: SourceItem
    status: str                        # "migrated" | "skipped" | "failed" | "dry-run"
    memory_id: str | None = None
    error: str | None = None
    verified: bool = False

# ── Safety: path containment ───────────────────────────────────────────────────
def _inside_allowed_root(p: Path) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for root in ALLOWED_SOURCE_ROOTS:
        try:
            rp.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False

def _assert_safe_path(p: Path) -> None:
    if not _inside_allowed_root(p):
        raise PermissionError(f"Refusing to read path outside allowed roots: {p}")

# ── Idempotency: stable source_key ─────────────────────────────────────────────
def _source_key(source_agent: str, locator: str, content: str) -> str:
    h = hashlib.sha256()
    h.update(source_agent.encode("utf-8"))
    h.update(b"\x00")
    h.update(locator.encode("utf-8"))
    h.update(b"\x00")
    h.update(content.strip().encode("utf-8"))
    return f"{source_agent}:{h.hexdigest()[:32]}"

# ── Dedup lookup: query m3 SQLite directly for an existing source_key ──────────
def existing_memory_id_by_source_key(source_key: str) -> str | None:
    """Returns the id of a non-deleted memory whose metadata.source_key matches, else None."""
    from memory_core import _db  # lazy — pool inits on first use
    with _db() as db:
        # metadata_json is stored as a JSON string; substring match is safe because
        # source_key is a prefixed hex hash with no JSON-unsafe characters. We
        # additionally verify with json.loads on the matched row.
        needle = f'"source_key": "{source_key}"'
        rows = db.execute(
            "SELECT id, metadata_json FROM memory_items "
            "WHERE is_deleted = 0 AND metadata_json LIKE ? LIMIT 5",
            (f"%{needle}%",),
        ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if meta.get("source_key") == source_key:
            return row["id"]
    return None

# ── Claude Code reader ─────────────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in raw.splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body

def iter_claude_items(project_slug: str | None) -> Iterable[SourceItem]:
    if not CLAUDE_PROJECTS_ROOT.exists():
        log.info("claude: no projects root at %s", CLAUDE_PROJECTS_ROOT)
        return
    if project_slug:
        project_dirs = [CLAUDE_PROJECTS_ROOT / project_slug]
    else:
        project_dirs = [d for d in CLAUDE_PROJECTS_ROOT.iterdir() if d.is_dir()]

    for proj in project_dirs:
        mem_dir = proj / "memory"
        if not mem_dir.is_dir():
            continue
        _assert_safe_path(mem_dir)
        for md in sorted(mem_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue  # index, not a memory
            try:
                raw = md.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as e:
                log.warning("claude: cannot read %s: %s", md, e)
                continue
            meta, body = _parse_frontmatter(raw)
            body = body.strip()
            if not body:
                continue
            claude_type = (meta.get("type") or "note").strip().lower()
            m3_type = CLAUDE_TYPE_MAP.get(claude_type, "note")
            if m3_type not in VALID_M3_TYPES:
                m3_type = "note"
            title = meta.get("name") or meta.get("description") or md.stem
            locator = f"{proj.name}/{md.name}"
            yield SourceItem(
                source_agent="claude",
                source_path=str(md),
                source_locator=locator,
                source_key=_source_key("claude", locator, body),
                m3_type=m3_type,
                title=title[:200],
                content=body,
                extra_metadata={
                    "source": "claude_code_flat_memory",
                    "claude_type": claude_type,
                    "claude_description": meta.get("description", ""),
                    "claude_project": proj.name,
                },
            )

# ── Gemini CLI reader ──────────────────────────────────────────────────────────
def iter_gemini_items() -> Iterable[SourceItem]:
    if not GEMINI_GLOBAL_MD.exists():
        log.info("gemini: no file at %s", GEMINI_GLOBAL_MD)
        return
    _assert_safe_path(GEMINI_GLOBAL_MD)
    try:
        lines = GEMINI_GLOBAL_MD.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        log.warning("gemini: cannot read %s: %s", GEMINI_GLOBAL_MD, e)
        return

    in_section = False
    current: list[str] = []
    start_line = 0

    def flush(start: int, buf: list[str]) -> SourceItem | None:
        content = "\n".join(buf).strip()
        if not content:
            return None
        locator = f"line:{start}"
        return SourceItem(
            source_agent="gemini",
            source_path=str(GEMINI_GLOBAL_MD),
            source_locator=locator,
            source_key=_source_key("gemini", locator, content),
            m3_type="note",
            title=content.split(".")[0][:120] or f"Gemini memory @ line {start}",
            content=content,
            extra_metadata={
                "source": "gemini_cli",
                "gemini_section": "Gemini Added Memories",
                "gemini_start_line": start,
            },
        )

    for i, line in enumerate(lines, start=1):
        if not in_section:
            if line.strip() == GEMINI_SECTION_HEADER:
                in_section = True
            continue
        # in section
        if line.lstrip().startswith("- "):
            # flush previous
            if current:
                item = flush(start_line, current)
                if item:
                    yield item
            current = [line.lstrip()[2:]]
            start_line = i
        elif line.startswith("  ") and current:
            # continuation of previous bullet
            current.append(line.strip())
        elif line.strip().startswith("#"):
            # new section starts, stop
            if current:
                item = flush(start_line, current)
                if item:
                    yield item
            current = []
            in_section = False
        elif not line.strip():
            continue
    if current:
        item = flush(start_line, current)
        if item:
            yield item

# ── Openclaw reader (read-only SQLite) ─────────────────────────────────────────
def iter_openclaw_items() -> Iterable[SourceItem]:
    if not OPENCLAW_DB.exists():
        log.info("openclaw: no db at %s", OPENCLAW_DB)
        return
    _assert_safe_path(OPENCLAW_DB)
    uri = f"file:{OPENCLAW_DB.as_posix()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        log.warning("openclaw: cannot open db: %s", e)
        return
    try:
        # Verify expected schema
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "chunks" not in tables:
            log.warning("openclaw: 'chunks' table missing — schema changed?")
            return
        rows = conn.execute(
            "SELECT id, path, source, start_line, end_line, text, updated_at "
            "FROM chunks ORDER BY updated_at"
        ).fetchall()
        for row in rows:
            text = (row["text"] or "").strip()
            if not text:
                continue
            locator = f"chunk:{row['id']}"
            title_bits = []
            if row["path"]:
                title_bits.append(Path(row["path"]).name)
            if row["start_line"] is not None:
                title_bits.append(f"L{row['start_line']}-{row['end_line']}")
            title = " ".join(title_bits) or f"openclaw chunk {row['id'][:8]}"
            yield SourceItem(
                source_agent="openclaw",
                source_path=str(OPENCLAW_DB),
                source_locator=locator,
                source_key=_source_key("openclaw", locator, text),
                m3_type="snippet",
                title=title[:200],
                content=text[:50000],
                extra_metadata={
                    "source": "openclaw_sqlite",
                    "openclaw_chunk_id": row["id"],
                    "openclaw_path": row["path"],
                    "openclaw_source_kind": row["source"],
                    "openclaw_start_line": row["start_line"],
                    "openclaw_end_line": row["end_line"],
                    "openclaw_updated_at": row["updated_at"],
                },
            )
    finally:
        conn.close()

# ── Rules reader (opt-in) ──────────────────────────────────────────────────────
RULES_CANDIDATES = [
    (HOME / ".claude" / "CLAUDE.md", "claude_global"),
    (HOME / ".gemini" / "GEMINI.md", "gemini_global_rules"),  # section-free body
    (REPO_ROOT / "CLAUDE.md", "claude_project"),
    (REPO_ROOT / "AGENTS.md", "opencode_agents"),
    (REPO_ROOT / "GEMINI.md", "gemini_project"),
    (REPO_ROOT / "CONVENTIONS.md", "aider_conventions"),
]

def iter_rules_items() -> Iterable[SourceItem]:
    for path, kind in RULES_CANDIDATES:
        if not path.exists():
            continue
        try:
            _assert_safe_path(path)
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, PermissionError, UnicodeDecodeError) as e:
            log.warning("rules: cannot read %s: %s", path, e)
            continue
        if not text:
            continue
        # For the rules variant of GEMINI.md, drop the ## Gemini Added Memories
        # section so we don't double-migrate those bullets.
        if kind == "gemini_global_rules":
            cut = text.find(GEMINI_SECTION_HEADER)
            if cut >= 0:
                text = text[:cut].strip()
            if not text:
                continue
        locator = f"rules:{path.name}:{kind}"
        yield SourceItem(
            source_agent="rules",
            source_path=str(path),
            source_locator=locator,
            source_key=_source_key("rules", locator, text),
            m3_type="preference",
            title=f"{kind}: {path.name}",
            content=text[:50000],
            extra_metadata={
                "source": "agent_rules_file",
                "rules_kind": kind,
            },
        )

# ── Writer ─────────────────────────────────────────────────────────────────────
async def write_item(item: SourceItem, dry_run: bool) -> WriteResult:
    existing = existing_memory_id_by_source_key(item.source_key)
    if existing:
        return WriteResult(item=item, status="skipped", memory_id=existing)

    if dry_run:
        return WriteResult(item=item, status="dry-run")

    metadata = dict(item.extra_metadata)
    metadata.update({
        "migrated_from": item.source_path,
        "source_key": item.source_key,
        "source_agent": item.source_agent,
        "source_locator": item.source_locator,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "migrator": "migrate_flat_memory.py",
    })

    try:
        res = await memory_core.memory_write_impl(
            type=item.m3_type,
            content=item.content,
            title=item.title,
            metadata=json.dumps(metadata),
            agent_id=f"migrate-{item.source_agent}",
            source="agent",
            importance=0.5,
            embed=True,
        )
    except Exception as e:
        return WriteResult(item=item, status="failed", error=f"write: {e!r}")

    if not isinstance(res, str) or not res.startswith("Created:"):
        return WriteResult(item=item, status="failed", error=f"unexpected write result: {res!r}")

    mem_id = res.split()[1]

    # Verification: round-trip via memory_get_impl + hash check
    try:
        got = memory_core.memory_get_impl(mem_id)
        got_obj = json.loads(got) if isinstance(got, str) else {}
        if got_obj.get("id") != mem_id:
            return WriteResult(item=item, status="failed", memory_id=mem_id,
                               error=f"verify: id mismatch {got_obj.get('id')!r}")
        stored_content = got_obj.get("content") or ""
        if stored_content.strip() != item.content.strip():
            return WriteResult(item=item, status="failed", memory_id=mem_id,
                               error="verify: content round-trip mismatch")
        # Hash integrity
        verify_out = memory_core.memory_verify_impl(mem_id)
        if "Integrity OK" not in verify_out:
            return WriteResult(item=item, status="failed", memory_id=mem_id,
                               error=f"verify: {verify_out}")
    except Exception as e:
        return WriteResult(item=item, status="failed", memory_id=mem_id,
                           error=f"verify: {e!r}")

    return WriteResult(item=item, status="migrated", memory_id=mem_id, verified=True)

# ── Orchestration ──────────────────────────────────────────────────────────────
SOURCE_READERS: dict[str, callable] = {
    "claude": lambda args: iter_claude_items(args.claude_project_slug),
    "gemini": lambda args: iter_gemini_items(),
    "openclaw": lambda args: iter_openclaw_items(),
    "rules": lambda args: iter_rules_items(),
}

async def run(args) -> int:
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.include_rules and "rules" not in sources:
        sources.append("rules")

    unknown = [s for s in sources if s not in SOURCE_READERS]
    if unknown:
        log.error("unknown source(s): %s (valid: %s)", unknown, list(SOURCE_READERS))
        return 2

    all_items: list[SourceItem] = []
    for src in sources:
        try:
            items = list(SOURCE_READERS[src](args))
        except PermissionError as e:
            log.error("%s: %s", src, e)
            continue
        except Exception as e:
            log.exception("%s: reader failed: %s", src, e)
            continue
        log.info("%s: %d item(s) discovered", src, len(items))
        all_items.extend(items)

    if not all_items:
        log.info("nothing to migrate")
        return 0

    # Dedup by source_key within this run too (defensive — a malformed source could emit dupes)
    seen_keys: set[str] = set()
    deduped: list[SourceItem] = []
    for it in all_items:
        if it.source_key in seen_keys:
            continue
        seen_keys.add(it.source_key)
        deduped.append(it)
    if len(deduped) != len(all_items):
        log.info("deduped intra-run duplicates: %d -> %d", len(all_items), len(deduped))

    print(f"\nDiscovered {len(deduped)} item(s) across sources: {sources}")
    if args.dry_run:
        print("Dry-run mode — no writes will be performed.")
    elif not args.yes:
        ans = input(f"Migrate {len(deduped)} item(s) into m3-memory? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    results: list[WriteResult] = []
    for it in deduped:
        r = await write_item(it, dry_run=args.dry_run)
        results.append(r)
        tag = {"migrated": "+", "skipped": "=", "failed": "!", "dry-run": "?"}[r.status]
        extra = f" id={r.memory_id}" if r.memory_id else ""
        err = f" error={r.error}" if r.error else ""
        log.info("%s %-8s %-40s%s%s", tag, r.status, f"{r.item.source_agent}:{r.item.source_locator}", extra, err)

    # ── Summary ───────────────────────────────────────────────────────────────
    by_src_status: dict[tuple[str, str], int] = {}
    for r in results:
        key = (r.item.source_agent, r.status)
        by_src_status[key] = by_src_status.get(key, 0) + 1

    print("\n" + "=" * 60)
    print("Migration summary")
    print("=" * 60)
    for (src, status), n in sorted(by_src_status.items()):
        print(f"  {src:10s}  {status:10s}  {n}")
    total_migrated = sum(1 for r in results if r.status == "migrated")
    total_failed = sum(1 for r in results if r.status == "failed")
    total_skipped = sum(1 for r in results if r.status == "skipped")
    total_dry = sum(1 for r in results if r.status == "dry-run")
    print("-" * 60)
    print(f"  total      migrated={total_migrated}  skipped={total_skipped}  failed={total_failed}  dry-run={total_dry}")

    # ── Manual cleanup hints (only for fully-verified migrations) ─────────────
    if total_migrated and not args.dry_run:
        verified = [r for r in results if r.status == "migrated" and r.verified]
        # Group distinct source files
        by_path: dict[str, list[WriteResult]] = {}
        for r in verified:
            by_path.setdefault(r.item.source_path, []).append(r)

        print("\n" + "=" * 60)
        print("Verified in m3-memory. You may now delete / clean these sources:")
        print("=" * 60)
        for path, items in sorted(by_path.items()):
            src = items[0].item.source_agent
            if src == "gemini":
                # We can't safely delete individual bullets — tell the user to edit
                print(f"  {path}")
                print("    (gemini: edit file manually — remove the following bullets:)")
                for r in items:
                    print(f"      - {r.item.source_locator}: {r.item.title[:80]}")
            elif src == "claude":
                print(f"  rm \"{path}\"")
            elif src == "openclaw":
                print(f"  (openclaw chunk {r.item.source_locator} — managed by openclaw, leave in place)")
            elif src == "rules":
                print(f"  (rules file {path} — DO NOT delete; still loaded into agent system prompt)")
            else:
                print(f"  {path}")
        print("\nThe migration script NEVER deletes source files. Clean up manually after review.")

    if total_failed:
        print("\nFailures:")
        for r in results:
            if r.status == "failed":
                print(f"  [{r.item.source_agent}] {r.item.source_locator}: {r.error}")
        return 1
    return 0

# ── CLI ────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Migrate flat-file / SQLite agent memory into m3-memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--sources", default="claude,gemini,openclaw",
        help="Comma-separated list of sources. Valid: claude, gemini, openclaw, rules. "
             "Default: claude,gemini,openclaw",
    )
    p.add_argument(
        "--include-rules", action="store_true",
        help="Also import CLAUDE.md / GEMINI.md / AGENTS.md / CONVENTIONS.md as type=preference. "
             "These are behavioral rules loaded by each agent's system prompt — importing them "
             "makes them searchable in m3-memory but does NOT replace the source files.",
    )
    p.add_argument(
        "--claude-project-slug", default=None,
        help="Restrict Claude source to a single project slug under ~/.claude/projects/. "
             "Default: all projects.",
    )
    p.add_argument("--dry-run", action="store_true", help="Discover + plan but don't write.")
    p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging.")
    return p

def main() -> int:
    # UTF-8 console on Windows — the default cp1252 mangles em-dashes etc.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    # Silence the sibling migrate_memory.py logger — it imports through our chain
    # and prints a misleading "Database is up to date" line on every run.
    logging.getLogger("migrate_memory").setLevel(logging.WARNING)

    parser = build_parser()
    args = parser.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

if __name__ == "__main__":
    sys.exit(main())
