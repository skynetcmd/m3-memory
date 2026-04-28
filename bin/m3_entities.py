#!/usr/bin/env python3
"""m3_entities — build entity-graph rows from your core/chatlog DBs.

Phase I driver. Mirrors `bin/m3_enrich.py`'s shape: profile-based,
generic source-variant filter, --core / --chatlog scope flags, --dry-run
preview, smoke-then-full-pass workflow.

What it does:
  - Walks eligible memory_items rows (filtered by type-allowlist +
    --source-variant).
  - For each row, calls the extractor (default: qwen/qwen3-8b:2 via
    LM Studio Anthropic /v1/messages) with the m3-tuned vocab and
    tightened prompt.
  - Resolves entities via memory_core helpers (idempotent UPSERT into
    `entities` table; INSERT OR IGNORE on `memory_item_entities`).
  - Writes relationships into `entity_relationships` (delete-then-insert
    keyed on source_memory_id; idempotent re-extraction).
  - Records partial-failure metrics so re-running picks up where the
    last call left off.

What it does NOT do:
  - It is not a daemon; one-shot pass over the eligible set.
  - It does not re-extract rows that already have entities linked
    UNLESS --force is passed.

Usage examples:
  # Preview
  python bin/m3_entities.py --core --source-variant __none__ --dry-run

  # Smoke 10 rows
  python bin/m3_entities.py --core --source-variant __none__ \
      --limit 10 --skip-preflight --yes

  # Full core pass
  python bin/m3_entities.py --core --source-variant __none__ \
      --concurrency 4 --skip-preflight --yes

The default vocab is `config/lists/entity_graph_m3.yaml` (m3-tuned).
Override via --entity-vocab-yaml or M3_ENTITY_VOCAB_YAML.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import httpx  # noqa: E402

import memory_core as mc  # noqa: E402
from slm_intent import load_profile, Profile  # noqa: E402
from auth_utils import get_api_key  # noqa: E402

DEFAULT_PROFILE = "entities_local_qwen"
DEFAULT_VOCAB_YAML = REPO_ROOT / "config" / "lists" / "entity_graph_m3.yaml"
BACKUP_DIR = Path(os.path.expanduser("~/.m3-memory/backups/entities"))

# By default, only types where named-entity extraction is sensible.
# Curated content (note/decision/knowledge/reference/fact/plan/document/
# infrastructure/network_config/local_device/home_automation) is INCLUDED
# here because — unlike Observer/user-fact extraction — entity extraction
# DOES work on these. They're full of named tools, hosts, files, etc.
DEFAULT_TYPES = (
    "message", "conversation", "chat_log",
    "note", "decision", "knowledge", "reference", "fact", "plan",
    "document", "observation",
    "project", "config", "infrastructure", "network_config",
    "local_device", "home_automation", "log", "preference",
)
ALWAYS_SKIP_TYPES = ("auto", "scratchpad", "summary")  # summary excluded; deleted leak shape

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ── Helpers ────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _backup_db(db_path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    dst = BACKUP_DIR / f"{db_path.stem}.pre-entities.{stamp}{db_path.suffix}"
    shutil.copy2(str(db_path), str(dst))
    return dst


def _load_vocab(path: Optional[Path]) -> tuple[frozenset[str], frozenset[str]]:
    """Load entity_types + entity_predicates from a YAML at path. If None,
    defer to memory_core.load_entity_vocab(None) which honors
    M3_ENTITY_VOCAB_YAML / falls back to entity_graph_default.yaml."""
    if path is None:
        return mc.load_entity_vocab(None)
    return mc.load_entity_vocab(str(path))


def _build_extractor(
    profile: Profile,
    token: str,
    valid_types: frozenset[str],
    valid_predicates: frozenset[str],
    client: httpx.AsyncClient,
):
    """Return an async callable that takes content text and returns
    {entities: [...], relationships: [...]}. Closure captures profile +
    token + client so the caller doesn't have to plumb them through.

    The returned callable is exactly the shape memory_core's
    _run_entity_extractor expects.
    """

    async def call(content: str) -> dict:
        if not content or len(content.strip()) < 8:
            return {"entities": [], "relationships": []}

        # Truncate to profile.input_max_chars to keep qwen3-8b under the
        # parse-failure threshold the smoke surfaced (parse errors above
        # ~2k chars of content).
        body = content[: profile.input_max_chars] if profile.input_max_chars else content[:4000]

        # Anthropic /v1/messages payload (same shape as observer profile).
        payload = {
            "model": profile.model,
            "max_tokens": profile.max_tokens,
            "messages": [
                {"role": "user", "content": body},
            ],
        }
        # System prompt goes in the top-level `system` field for Anthropic.
        if profile.system:
            payload["system"] = profile.system
        if profile.temperature is not None:
            payload["temperature"] = profile.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": getattr(profile, "anthropic_version", "2023-06-01"),
        }

        try:
            r = await client.post(profile.url, json=payload, headers=headers, timeout=profile.timeout_s)
        except (httpx.HTTPError, TimeoutError) as e:
            raise RuntimeError(f"http error: {type(e).__name__}: {e}")
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}: {r.text[:300]}")

        data = r.json()
        # Anthropic content blocks
        text = ""
        for blk in data.get("content") or []:
            if isinstance(blk, dict) and blk.get("type") == "text":
                text += blk.get("text", "")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        m = JSON_RE.search(text)
        if not m:
            return {"entities": [], "relationships": []}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"entities": [], "relationships": []}

        entities_in = obj.get("entities", []) if isinstance(obj, dict) else []
        relationships_in = obj.get("relationships", []) if isinstance(obj, dict) else []
        if not isinstance(entities_in, list):
            entities_in = []
        if not isinstance(relationships_in, list):
            relationships_in = []

        # Filter against the active vocab. memory_core's _run_entity_extractor
        # validates again, but pre-filtering here lets us count rejections
        # for telemetry.
        entities_clean: list[dict] = []
        emitted_canonicals: set[str] = set()
        for ent in entities_in[:25]:  # honor prompt's hard cap
            if not isinstance(ent, dict):
                continue
            cname = str(ent.get("canonical_name", "")).strip()
            etype = str(ent.get("entity_type", "")).strip()
            mention = str(ent.get("mention_text", "")).strip() or cname

            # Post-process: smoke surfaced the model emitting 'memory_id_<hex>'
            # canonicals when type=memory_id. Strip the prefix so we end up
            # with the bare 8-hex which downstream `references` edges expect.
            if etype == "memory_id" and cname.startswith("memory_id_"):
                cname = cname[len("memory_id_"):]
                mention = cname

            if not cname or etype not in valid_types:
                continue
            try:
                conf = float(ent.get("confidence", 0.85))
            except (TypeError, ValueError):
                conf = 0.85
            if conf < 0.6:
                continue
            entities_clean.append({
                "canonical_name": cname,
                "entity_type": etype,
                "mention_text": mention,
                "confidence": max(0.0, min(1.0, conf)),
            })
            emitted_canonicals.add(cname)

        relationships_clean: list[dict] = []
        for rel in relationships_in[:25]:
            if not isinstance(rel, dict):
                continue
            f = str(rel.get("from_entity", "")).strip()
            t = str(rel.get("to_entity", "")).strip()
            p = str(rel.get("predicate", "")).strip()
            if not (f and t and p):
                continue
            if p not in valid_predicates:
                continue
            # Mirror the memory_id_ stripping done on entities so cross-refs
            # match emitted_canonicals.
            if f.startswith("memory_id_"):
                f = f[len("memory_id_"):]
            if t.startswith("memory_id_"):
                t = t[len("memory_id_"):]
            # Both endpoints must have appeared in entities_clean.
            if f not in emitted_canonicals or t not in emitted_canonicals:
                continue
            # Drop trivial self-loops (smoke saw `defined_in self->self`).
            if f == t:
                continue
            try:
                conf = float(rel.get("confidence", 0.85))
            except (TypeError, ValueError):
                conf = 0.85
            if conf < 0.6:
                continue
            relationships_clean.append({
                "from_entity": f,
                "to_entity": t,
                "predicate": p,
                "confidence": max(0.0, min(1.0, conf)),
            })

        return {"entities": entities_clean, "relationships": relationships_clean}

    return call


# ── Eligibility ────────────────────────────────────────────────────────────

def _build_type_allowlist(
    args: argparse.Namespace,
) -> tuple[str, ...]:
    if args.types:
        return tuple(t.strip() for t in args.types.split(",") if t.strip())
    return DEFAULT_TYPES


def _query_eligible_rows(
    db_path: Path,
    type_allowlist: tuple[str, ...],
    source_variant: Optional[str],
    limit: Optional[int],
    skip_already_extracted: bool,
) -> list[tuple[str, str]]:
    """Return [(memory_id, content)] for rows the extractor should process.

    skip_already_extracted=True (the default) excludes rows that already
    have at least one row in memory_item_entities — re-running the driver
    incrementally picks up only new rows.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    placeholders = ",".join("?" * len(type_allowlist))
    excl_placeholders = ",".join("?" * len(ALWAYS_SKIP_TYPES))
    variant_clause = ""
    variant_params: list = []
    if source_variant == "__none__":
        variant_clause = " AND variant IS NULL"
    elif source_variant:
        variant_clause = " AND variant = ?"
        variant_params = [source_variant]

    extracted_clause = ""
    if skip_already_extracted:
        extracted_clause = (
            " AND id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)"
        )

    sql = f"""
        SELECT id, COALESCE(title, '') || CASE WHEN COALESCE(title,'') != '' THEN '\n\n' ELSE '' END || COALESCE(content, '')
        FROM memory_items
        WHERE COALESCE(is_deleted, 0) = 0
          AND type IN ({placeholders})
          AND type NOT IN ({excl_placeholders})
          {variant_clause}
          {extracted_clause}
        ORDER BY LENGTH(content) DESC
    """
    params = list(type_allowlist) + list(ALWAYS_SKIP_TYPES) + variant_params
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if limit is not None:
        rows = rows[:limit]
    return [(r[0], r[1]) for r in rows]


# ── Pre-flight ─────────────────────────────────────────────────────────────

async def _smoke_profile(profile: Profile, token: str, valid_types, valid_predicates) -> None:
    """Quick smoke: send a tiny payload through the extractor and confirm
    we get well-formed JSON back. Catches misconfigured endpoints before
    we touch the DB."""
    print(f"[m3-entities] smoke profile {profile.name!r} ...", flush=True)
    async with httpx.AsyncClient() as client:
        extractor = _build_extractor(profile, token, valid_types, valid_predicates, client)
        try:
            out = await extractor(
                "Test sentence: bin/memory_core.py defines memory_search_scored_impl. "
                "It runs on SkyPC at 10.21.40.2."
            )
        except Exception as e:
            sys.exit(f"ERROR: profile smoke failed: {type(e).__name__}: {e}")
    n_ents = len(out.get("entities", []))
    n_rels = len(out.get("relationships", []))
    print(f"[m3-entities] smoke OK: {n_ents} entities, {n_rels} relationships", flush=True)
    if n_ents == 0:
        print("  [warning] smoke produced 0 entities. Profile prompt may be misconfigured.", flush=True)


# ── Main loop ──────────────────────────────────────────────────────────────

async def _run_db(
    db_path: Path,
    profile: Profile,
    token: str,
    valid_types: frozenset[str],
    valid_predicates: frozenset[str],
    type_allowlist: tuple[str, ...],
    source_variant: Optional[str],
    concurrency: int,
    limit: Optional[int],
    skip_already_extracted: bool,
    counters: dict,
) -> None:
    """Drive the extractor across all eligible rows in one DB."""
    os.environ["M3_DATABASE"] = str(db_path)
    # Make sure entity-graph code-path is enabled in this process.
    os.environ.setdefault("M3_ENABLE_ENTITY_GRAPH", "1")

    rows = _query_eligible_rows(
        db_path, type_allowlist, source_variant, limit, skip_already_extracted
    )
    counters["eligible"] = len(rows)
    print(f"[m3-entities] {db_path.name}: {len(rows)} eligible rows", flush=True)
    if not rows:
        return

    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    async with httpx.AsyncClient() as client:
        extractor = _build_extractor(profile, token, valid_types, valid_predicates, client)

        async def gated(memory_id: str, text: str) -> None:
            async with sem:
                # Retry-on-failure with backoff. First attempt uses full
                # truncation (profile.input_max_chars). On timeout / 500,
                # retry once with halved input to clear the long-input
                # tail and back off briefly to let LM Studio settle (the
                # OOM-induced "Model reloaded" 500s burn one full reload
                # cycle before the model is ready again).
                out = None
                last_err = None
                for attempt, slice_ratio, backoff_s in ((1, 1.0, 0.0), (2, 0.5, 30.0)):
                    if backoff_s:
                        await asyncio.sleep(backoff_s)
                    try:
                        body = text[: int(profile.input_max_chars * slice_ratio)] if profile.input_max_chars else text
                        out = await extractor(body)
                        break
                    except Exception as e:
                        last_err = e
                        if attempt == 1:
                            print(f"[m3-entities] retry {memory_id[:8]}: {type(e).__name__}", flush=True)
                if out is None:
                    counters["failed"] += 1
                    print(f"[m3-entities] FAIL {memory_id[:8]}: {type(last_err).__name__}: {last_err}", flush=True)
                    return
                ents = out.get("entities", [])
                rels = out.get("relationships", [])
                counters["processed"] += 1
                if not ents:
                    counters["empty"] += 1
                    return
                counters["entities_emitted"] += len(ents)
                counters["relationships_emitted"] += len(rels)
                # Hand off to memory_core's writer — it does entity resolve,
                # link insert, relationship UPSERT (delete-then-insert per
                # source_memory_id).
                try:
                    async def _passthrough(_text: str) -> dict:
                        return out
                    await mc._run_entity_extractor(
                        memory_id, text, _passthrough,
                        valid_types=valid_types,
                        valid_predicates=valid_predicates,
                    )
                except Exception as e:
                    counters["write_failed"] += 1
                    print(f"[m3-entities] WRITE-FAIL {memory_id[:8]}: {type(e).__name__}: {e}", flush=True)

        await asyncio.gather(*(gated(mid, txt) for mid, txt in rows))

    elapsed = time.monotonic() - started
    print(
        f"[m3-entities] {db_path.name} done in {elapsed/60:.1f}m: "
        f"{counters['processed']} processed, "
        f"{counters['entities_emitted']} entities, "
        f"{counters['relationships_emitted']} relationships, "
        f"{counters['empty']} empty, "
        f"{counters['failed']} HTTP-fail, "
        f"{counters['write_failed']} write-fail",
        flush=True,
    )


# ── CLI ────────────────────────────────────────────────────────────────────

def _print_dry_run(plan: dict) -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-entities DRY RUN — no writes will happen")
    print("══════════════════════════════════════════════════════════════")
    print()
    print(f"  Profile:             {plan['profile_name']}")
    print(f"  Model:               {plan['model']}")
    print(f"  Endpoint:            {plan['url']}")
    print(f"  Vocab YAML:          {plan['vocab_yaml']}")
    print(f"  Entity types ({len(plan['entity_types'])}):  {sorted(plan['entity_types'])}")
    print(f"  Predicates ({len(plan['predicates'])}):     {sorted(plan['predicates'])}")
    src = plan.get("source_variant") or "(all)"
    if src == "__none__":
        src = "__none__ (variant IS NULL)"
    print(f"  Source variant:      {src}")
    print(f"  Type allowlist:      {plan['types']}")
    print(f"  Skip already-extracted: {plan['skip_already_extracted']}")
    print()
    for db_label, info in plan["dbs"].items():
        print(f"  ── {db_label} ─────────────")
        print(f"     path:        {info['path']}")
        print(f"     eligible:    {info['n_rows']} rows")
        print(f"     est wall:    ~{info['n_rows'] * 3 / 60:.1f} min @ concurrency=4 (local model)")
        print(f"     est cost:    $0 (local)")
        print()
    print("To run for real, drop --dry-run.")
    print("══════════════════════════════════════════════════════════════")


async def _main_async(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    if profile is None:
        sys.exit(f"ERROR: profile {args.profile!r} not found")

    vocab_path = Path(args.entity_vocab_yaml) if args.entity_vocab_yaml else DEFAULT_VOCAB_YAML
    if not vocab_path.exists():
        sys.exit(f"ERROR: vocab YAML not found: {vocab_path}")
    valid_types, valid_predicates = _load_vocab(vocab_path)

    type_allowlist = _build_type_allowlist(args)

    # DB targets
    db_targets: list[tuple[str, Path]] = []
    if not args.chatlog_only:
        core_db = Path(args.core_db) if args.core_db else Path(
            os.environ.get("M3_DATABASE") or (REPO_ROOT / "memory" / "agent_memory.db")
        )
        if core_db.exists():
            db_targets.append(("core", core_db))
    if not args.core_only:
        chatlog_db = Path(args.chatlog_db) if args.chatlog_db else (
            REPO_ROOT / "memory" / "agent_chatlog.db"
        )
        if chatlog_db.exists():
            db_targets.append(("chatlog", chatlog_db))
    if not db_targets:
        sys.exit("ERROR: no DBs found. Use --core-db / --chatlog-db or set M3_DATABASE.")

    # Build dry-run plan
    plan = {
        "profile_name": profile.name,
        "model": profile.model,
        "url": profile.url,
        "vocab_yaml": str(vocab_path),
        "entity_types": sorted(valid_types),
        "predicates": sorted(valid_predicates),
        "source_variant": args.source_variant,
        "types": list(type_allowlist),
        "skip_already_extracted": not args.force,
        "dbs": {},
    }
    for label, db_path in db_targets:
        rows = _query_eligible_rows(
            db_path, type_allowlist, args.source_variant, args.limit, not args.force
        )
        plan["dbs"][label] = {"path": str(db_path), "n_rows": len(rows)}

    if args.dry_run:
        _print_dry_run(plan)
        return 0

    _print_dry_run(plan)

    if not args.yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted (no changes made)")
            return 0

    # Token + smoke
    token = get_api_key(profile.api_key_service) or ""
    if not token:
        sys.exit(f"ERROR: no token resolved for service {profile.api_key_service!r}")

    if not args.skip_preflight:
        await _smoke_profile(profile, token, valid_types, valid_predicates)
        for label, db_path in db_targets:
            backup = _backup_db(db_path)
            print(f"[m3-entities] backup: {db_path.name} → {backup}", flush=True)

    counters_total = defaultdict(int)
    for label, db_path in db_targets:
        counters = defaultdict(int)
        await _run_db(
            db_path, profile, token, valid_types, valid_predicates,
            type_allowlist, args.source_variant, args.concurrency,
            args.limit, not args.force, counters,
        )
        for k, v in counters.items():
            counters_total[k] += v

    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-entities COMPLETE")
    print("══════════════════════════════════════════════════════════════")
    print(f"  rows processed:        {counters_total.get('processed', 0)}")
    print(f"  entities emitted:      {counters_total.get('entities_emitted', 0)}")
    print(f"  relationships emitted: {counters_total.get('relationships_emitted', 0)}")
    print(f"  empty (no entities):   {counters_total.get('empty', 0)}")
    print(f"  HTTP failures:         {counters_total.get('failed', 0)}")
    print(f"  write failures:        {counters_total.get('write_failed', 0)}")
    print()
    print("  inspect via:")
    print("    SELECT canonical_name, entity_type, COUNT(*) FROM entities e "
          "JOIN memory_item_entities mie ON mie.entity_id=e.id "
          "GROUP BY e.id ORDER BY COUNT(*) DESC LIMIT 30;")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="m3_entities — build entity-graph rows from your core/chatlog DBs."
    )
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help=f"Profile name in config/slm/. Default: {DEFAULT_PROFILE}.")
    ap.add_argument("--entity-vocab-yaml", default=None,
                    help=f"Vocab YAML path. Default: {DEFAULT_VOCAB_YAML}.")
    ap.add_argument("--core", action="store_true", dest="core_only",
                    help="Only enrich the core memory DB (skip chatlog).")
    ap.add_argument("--chatlog", action="store_true", dest="chatlog_only",
                    help="Only enrich the chatlog DB (skip core).")
    ap.add_argument("--core-db", default=None,
                    help="Explicit path to the core memory DB.")
    ap.add_argument("--chatlog-db", default=None,
                    help="Explicit path to the chatlog DB.")
    ap.add_argument("--source-variant", default=None,
                    help="Filter source rows by variant. '__none__' = true core memory only "
                         "(variant IS NULL). A name = single-variant scope. Default: no filter.")
    ap.add_argument("--types", default=None,
                    help="Comma-separated type allowlist override. Default: chat + curated.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap rows enriched per DB (smoke testing).")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="Concurrent SLM calls. Default 2 (single-host LM Studio "
                         "with two qwen3-8b instances was OOM-reloading at 4).")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract rows that already have memory_item_entities. "
                         "Default: skip already-extracted.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview what would happen without writing.")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip endpoint smoke + DB backup. Power-user only.")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Skip the interactive confirm prompt.")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
