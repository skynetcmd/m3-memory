"""Eligible-group SQL + conv-list loader — _query_eligible_groups, _load_conv_list."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

# These are set in m3_enrich.py; we don't import them to avoid cycles
from enrich import ALWAYS_SKIP_TYPES  # single source of truth (package leaf)


def _load_conv_list(path: Path) -> set[str]:
    """Read a list of group_keys from FILE. Accepts either:
      • newline-delimited text (one group_key per line, blank lines + #-comments ignored)
      • a JSON array of strings

    Returns a deduplicated set; raises SystemExit on malformed input.
    """
    if not path.exists():
        sys.exit(f"ERROR: --source-conv-list path not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        sys.exit(f"ERROR: --source-conv-list is empty: {path}")
    if raw.lstrip().startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: --source-conv-list JSON parse failed: {e}")
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            sys.exit("ERROR: --source-conv-list JSON must be an array of strings.")
        return {x for x in data if x}
    out: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line)
    if not out:
        sys.exit(f"ERROR: --source-conv-list contained no usable entries: {path}")
    return out


def _query_eligible_groups(
    db_path: Path,
    type_allowlist: tuple[str, ...],
    limit: int | None,
    source_variant: str | None = None,
    conv_filter: set[str] | None = None,
) -> list[tuple[str, str, list[tuple]]]:
    """Group eligible memory_items rows into (user_id, conversation_id, [turns]).

    Conversation grouping rule:
      1. row.conversation_id column if non-NULL
      2. else metadata_json.session_id
      3. else row.id (one-row group — Observer will treat as single turn)

    source_variant filter:
      None         → no filter (original behavior; pulls every variant)
      "__none__"   → variant IS NULL (true core memory only)
      "<name>"     → variant = '<name>' (single bench/test variant)

    Returns a list of (user_id, conv_id, turns_list) where each turns_list
    contains (id, content, role, turn_index, created_at, metadata_json) tuples
    sorted by turn_index ASC. Same shape run_observer.process_conversation expects.
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

    # Push the conv_filter to SQL. On the bench DB this turns a 128s full
    # scan (2.4M rows) into a ~3s scoped scan when the filter is small.
    # SQLite's parameter limit is 999 by default, so chunk the IN-list if
    # the filter is large.
    conv_chunks: list[list[str]] = []
    if conv_filter is not None:
        # Convert set to deterministic list for IN-list parameters
        cf_list = list(conv_filter)
        # SQLite default SQLITE_MAX_VARIABLE_NUMBER is 999 (older builds) or
        # 32766 (3.32+). We chunk at 800 to be safe and union-merge results.
        chunk_size = 800
        conv_chunks = [cf_list[i:i + chunk_size] for i in range(0, len(cf_list), chunk_size)]

    if conv_chunks:
        # Two-path approach. The earlier single-query form wrapped
        # `conversation_id` in COALESCE inside the WHERE, which prevents
        # SQLite from using `idx_mi_conversation_id` — measured at 106s
        # per 800-key chunk on a 50GB DB, fully scanning every variant
        # row. Splitting into two queries lets each one hit the right
        # index:
        #
        #   Path A — direct conversation_id IN (...) — uses
        #     idx_mi_conversation_id for an index seek per key
        #     (handles >99% of rows on production-shape data).
        #
        #   Path B — fallback for rows where conversation_id IS NULL
        #     and group_key was derived from metadata_json.$.session_id
        #     or row.id. This path still scans variant rows but only
        #     the NULL-conversation_id slice (typically tiny for fresh
        #     ingest, larger for legacy data).
        #
        # Path B is opt-in via a one-shot pre-flight: if the variant has
        # zero rows with NULL conversation_id, we skip Path B entirely
        # (saves ~25s/chunk on bench-style corpora that always populate
        # conversation_id). The pre-flight uses the partial-index path
        # too, so it's effectively free.
        #
        # Net effect on the 19,287-key bench enumeration: 60+ minutes of
        # full scans → seconds-to-tens-of-seconds of indexed seeks plus
        # one small fallback scan.
        need_path_b = True
        if variant_clause:
            # Pre-flight: does this variant have ANY rows with NULL
            # conversation_id? If not, Path B will return 0 rows for
            # every chunk and we can skip it.
            preflight_sql = f"""
                SELECT 1 FROM memory_items
                WHERE is_deleted=0
                  {variant_clause}
                  AND conversation_id IS NULL
                LIMIT 1
            """
            preflight_rows = conn.execute(preflight_sql, variant_params).fetchall()
            need_path_b = bool(preflight_rows)
        rows: list = []
        for chunk in conv_chunks:
            ph_chunk = ",".join("?" * len(chunk))
            # Path A — direct conversation_id IN, index-using.
            # NB: WHERE uses raw `is_deleted=0` (not COALESCE) because the
            # partial index `idx_mi_conversation_id` is defined with predicate
            # `WHERE is_deleted=0`. SQLite's planner only matches partial-
            # index predicates by literal expression — `COALESCE(is_deleted,0)=0`
            # disqualifies the index even though it's logically equivalent.
            # Verified 2026-05-05: COALESCE form took 106s/chunk, raw form
            # takes 3.4s/chunk on the bench DB. is_deleted distribution on
            # this corpus has 0 NULLs (4.9M zeros + 67K ones), so dropping
            # the COALESCE is safe; rows with NULL is_deleted (if any future
            # writer creates them) would be excluded — which is the intended
            # is_deleted=0 semantic anyway.
            sql_a = f"""
                SELECT id,
                       content,
                       COALESCE(json_extract(metadata_json,'$.role'),
                                title,
                                'user') AS role,
                       COALESCE(json_extract(metadata_json,'$.turn_index'), 0) AS turn_index,
                       created_at,
                       metadata_json,
                       conversation_id AS group_key,
                       COALESCE(user_id, '') AS user_id
                FROM memory_items
                WHERE is_deleted=0
                  AND type IN ({placeholders})
                  AND type NOT IN ({excl_placeholders})
                  {variant_clause}
                  AND conversation_id IN ({ph_chunk})
            """
            params_a = list(type_allowlist) + list(ALWAYS_SKIP_TYPES) + variant_params + list(chunk)
            rows.extend(conn.execute(sql_a, params_a).fetchall())
            if not need_path_b:
                continue
            # Path B — fallback for NULL conversation_id rows whose group_key
            # comes from metadata.session_id or row.id. Most ingest pipelines
            # populate conversation_id, so this path returns 0 rows on bench
            # variants. The pre-flight check above sets need_path_b=False on
            # those variants, skipping this scan entirely.
            sql_b = f"""
                SELECT id,
                       content,
                       COALESCE(json_extract(metadata_json,'$.role'),
                                title,
                                'user') AS role,
                       COALESCE(json_extract(metadata_json,'$.turn_index'), 0) AS turn_index,
                       created_at,
                       metadata_json,
                       COALESCE(json_extract(metadata_json,'$.session_id'), id) AS group_key,
                       COALESCE(user_id, '') AS user_id
                FROM memory_items
                WHERE is_deleted=0
                  AND type IN ({placeholders})
                  AND type NOT IN ({excl_placeholders})
                  {variant_clause}
                  AND conversation_id IS NULL
                  AND COALESCE(json_extract(metadata_json,'$.session_id'), id) IN ({ph_chunk})
            """
            params_b = list(type_allowlist) + list(ALWAYS_SKIP_TYPES) + variant_params + list(chunk)
            rows.extend(conn.execute(sql_b, params_b).fetchall())
    else:
        # No conv_filter: must scan everything (caller will paginate via
        # --limit downstream).
        sql = f"""
            SELECT id,
                   content,
                   COALESCE(json_extract(metadata_json,'$.role'),
                            title,
                            'user') AS role,
                   COALESCE(json_extract(metadata_json,'$.turn_index'), 0) AS turn_index,
                   created_at,
                   metadata_json,
                   COALESCE(conversation_id,
                            json_extract(metadata_json,'$.session_id'),
                            id) AS group_key,
                   COALESCE(user_id, '') AS user_id
            FROM memory_items
            WHERE COALESCE(is_deleted,0)=0
              AND type IN ({placeholders})
              AND type NOT IN ({excl_placeholders})
              {variant_clause}
            ORDER BY user_id, group_key, turn_index ASC, created_at ASC
        """
        params = list(type_allowlist) + list(ALWAYS_SKIP_TYPES) + variant_params
        rows = conn.execute(sql, params).fetchall()
    conn.close()

    # Group ALL rows first, THEN apply --limit at the conversation-group
    # level (not the row level). This ensures --limit N gives N full
    # conversations, not N orphan turns scattered across N conversations.
    groups: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in rows:
        # row layout: id, content, role, turn_index, created_at, metadata_json, group_key, user_id
        groups[(r[7], r[6])].append((r[0], r[1], r[2], r[3], r[4], r[5]))

    # Sort each group's turns now that the SQL-level ORDER BY only applies
    # in the no-filter path. With the chunked-IN path turns can come back
    # interleaved across chunks — sort defensively.
    if conv_chunks:
        for k, turns in groups.items():
            turns.sort(key=lambda t: (t[3], t[4]))  # turn_index, created_at

    out = [(uid, cid, turns) for (uid, cid), turns in groups.items()]
    # Belt-and-suspenders: if conv_filter was provided, this is already enforced
    # at SQL but a Python-side filter catches any drift between the IN-list
    # and the conv_filter set (e.g. casing differences).
    if conv_filter is not None:
        out = [g for g in out if g[1] in conv_filter]
    # Sort by group size descending so --limit picks the BIGGEST conversations
    # first — most likely to contain extractable facts. Single-turn groups
    # (acks, status checks) sort to the bottom and only hit the cap last.
    out.sort(key=lambda g: -len(g[2]))
    if limit:
        out = out[:limit]
    return out
