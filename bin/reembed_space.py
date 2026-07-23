#!/usr/bin/env python3
"""reembed_space.py — retire vectors from the wrong embedding model.

Companion to the mixed-embed-space doctor probe (bin/doctor/embed_space_probe.py).
When a store has accumulated vectors from more than one embedding model, cosine
across those spaces is meaningless and the minority rows rank wrongly forever —
silently, with no error. This tool retires the offending vectors so they can be
regenerated with the current model.

DESIGN: this does NOT contain an embed loop. It deletes the stale
``memory_embeddings`` rows, which makes their parent items match the
``WHERE NOT EXISTS`` predicate that ``bin/embed_backfill.py`` already sweeps on.
That sweeper is hardened (batching, concurrency, timeouts, oversize/bad-dim
skips, resumability) and re-implementing it here would be a second code path to
keep correct. So the flow is:

    m3 embedder reembed --apply      # retire stale vectors (this tool)
    python bin/embed_backfill.py     # regenerate them (existing sweeper)

``--apply`` chains the sweeper automatically unless ``--no-backfill`` is given.

SAFETY: dry-run is the DEFAULT. The tool prints exactly what it would delete and
exits without touching anything until ``--apply`` is passed. A timestamped backup
of the target DB is taken before the first delete unless ``--no-backup`` is set.
Deleting an embedding is non-destructive to the MEMORY — content, metadata and
relationships are untouched; only the vector is dropped and regenerated.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

_BIN = os.path.dirname(os.path.abspath(__file__))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


def _mixed_kinds(rows) -> "set[str]":
    """vector_kinds that hold MORE THAN ONE model family.

    ``vector_kind`` partitions vectors that are SUPPOSED to live in separate
    spaces, so a different model under a different kind is by design, not a
    mix. Only a kind containing two families is a problem — and only rows in
    such a kind may be retired. Matches how the doctor probe decides.
    """
    fams_by_kind: "dict[str, set[str]]" = {}
    for kind, _tag, fam, _dim, _n in rows:
        fams_by_kind.setdefault(kind, set()).add(fam)
    return {k for k, f in fams_by_kind.items() if len(f) > 1}


def _keep_family(rows, keep: "str | None") -> str:
    """Choose which family to KEEP: the explicit --keep, else the largest.

    Defaulting to the majority family is the conservative choice — it minimises
    how many vectors have to be regenerated, and on a store polluted by a stray
    experiment the majority is nearly always the intended model. Only rows in a
    genuinely mixed vector_kind get a vote, so a single-family fallback space
    cannot swing the choice.
    """
    if keep:
        return keep
    mixed = _mixed_kinds(rows)
    totals: "dict[str, int]" = {}
    for kind, _tag, fam, _dim, n in rows:
        if kind in mixed:
            totals[fam] = totals.get(fam, 0) + n
    return max(totals.items(), key=lambda kv: kv[1])[0] if totals else ""


def _resolve_default_db() -> str:
    """The engine-root agent_memory.db. Separate seam so tests can point the
    default-path branch at a temp store."""
    from m3_core.paths import resolve_engine_file
    return resolve_engine_file("agent_memory.db")


def _scan(db_path: str):
    """Return [(vector_kind, embed_model, family, dim, count)] for the store."""
    from doctor.embed_space_probe import _family
    from memory.backends import active_backend

    out = []
    with active_backend().open_readonly(db_path) as conn:
        cur = conn.execute(
            "SELECT COALESCE(vector_kind, 'default'), COALESCE(embed_model, ''), "
            "       COALESCE(dim, 0), COUNT(*) "
            f"FROM {_embeddings_table()} GROUP BY 1, 2, 3"
        )
        for kind, tag, dim, n in cur.fetchall():
            out.append((str(kind), str(tag), _family(str(tag)), int(dim), int(n)))
    return out


def _is_file_backend() -> bool:
    """True when the active backend stores one DB per file (SQLite today).

    Keyed on ``!= "sqlite"`` like memory/db.py::_db, NOT on ``== "postgres"``,
    so a future SQL backend (MariaDB) routes through the pooled seam rather than
    silently falling into the file path and touching the wrong store.
    """
    try:
        from memory.backends import active_backend
        return active_backend().name == "sqlite"
    except Exception:  # noqa: BLE001 — no backend layer means the legacy file path
        return True


def _embeddings_table() -> str:
    """The CORE store's embeddings table — ``memory_embeddings`` on every backend.

    Note this is deliberately NOT ``chatlog_table("embeddings")``. That helper
    maps the CHATLOG store's tables, which on non-SQLite backends are namespaced
    ``chat_log_*`` so both logical stores can share one database. This tool
    operates on the core memory store, whose table keeps the same name
    everywhere (see postgres_backend's own JOIN on ``memory_embeddings``).
    Kept as a seam so a future backend that namespaces differently has one place
    to change rather than a literal scattered through the SQL.
    """
    return "memory_embeddings"


def _delete_doomed(db_path: str, doomed) -> int:
    """Delete the stale embedding rows, backend-blind.

    On a file backend (SQLite) the write must honor ``db_path`` — ``--db`` names
    a specific file and the seam's ``connection()`` takes no path, so it would
    open the DEFAULT store and delete from the wrong database. On a pooled
    backend (PostgreSQL, future MariaDB) there is exactly ONE store, ``db_path``
    is meaningless, and the seam's connection is the only correct route.

    Binds go through ``placeholder()`` so the same SQL works on ``?`` (SQLite)
    and ``%s`` (psycopg).
    """
    from contextlib import closing

    from memory.backends import active_backend

    backend = active_backend()
    ph = backend.dialect().param()
    # Branch on name == "sqlite" like chatlog_decay/chatlog_prune/curator_apply:
    # only a file backend has a per-file store to honor, and every other backend
    # (PG today, MariaDB later) routes through the pooled seam. One local
    # decision drives both the connection and the commit below, so they cannot
    # disagree.
    is_file = _is_file_backend()
    if is_file:
        import sqlite3
        _conn = sqlite3.connect(db_path, timeout=30.0)
        # §10 DB hygiene: every new SQLite connection applies the pragma stack
        # (WAL autocheckpoint, journal_size_limit, mmap/cache) via the shared
        # helper — never inline PRAGMAs. Best-effort: a missing helper must not
        # abort the delete. SQLite-only; pooled backends tune their own pool.
        try:
            from sqlite_pragmas import apply_pragmas, profile_for_db
            apply_pragmas(_conn, profile_for_db(db_path))
        except Exception:  # noqa: BLE001 — hygiene is best-effort, not a gate
            pass
        cm = closing(_conn)
    else:
        cm = backend.connection()

    sql = (
        f"DELETE FROM {_embeddings_table()} "
        f"WHERE COALESCE(vector_kind,'default')={ph} "
        f"AND COALESCE(embed_model,'')={ph} AND COALESCE(dim,0)={ph}"
    )
    deleted = 0
    with cm as conn:
        cur = conn.cursor() if hasattr(conn, "cursor") else conn
        for kind, tag, _fam, dim, _n in doomed:
            res = cur.execute(sql, (kind, tag, dim))
            rc = getattr(res, "rowcount", None)
            if rc is None:
                rc = getattr(cur, "rowcount", 0)
            deleted += rc if rc and rc > 0 else 0
        # The pooled seam commits on clean exit; a raw sqlite3 handle does not.
        if is_file:
            conn.commit()
            # §10: truncate the WAL at clean exit so a bulk delete doesn't leave
            # it bloated. Best-effort — never fail a completed delete on hygiene.
            try:
                from sqlite_pragmas import checkpoint_truncate
                checkpoint_truncate(conn)
            except Exception:  # noqa: BLE001
                pass
    return deleted


def _backup(db_path: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{db_path}.bak-{stamp}-prereembed"
    shutil.copy2(db_path, dest)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="reembed_space.py",
        description="Retire embeddings produced by a non-current model so they "
                    "can be regenerated. Dry-run unless --apply is given.",
    )
    ap.add_argument("--db", default=None,
                    help="Target DB (default: the resolved engine agent_memory.db).")
    ap.add_argument("--keep", default=None,
                    help="Model family to KEEP (e.g. 'bge-m3'). Default: the "
                         "family holding the most vectors.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Without this the tool only reports.")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip the pre-delete DB copy (not recommended).")
    ap.add_argument("--no-backfill", action="store_true",
                    help="Do not chain embed_backfill.py after deleting.")
    args = ap.parse_args(argv)

    try:
        db_path = args.db or _resolve_default_db()
    except Exception as e:  # noqa: BLE001
        print(f"error: could not load path seam: {type(e).__name__}: {e}")
        return 1

    # Only a file backend has a path to check; on a pooled store db_path is a
    # label, not a file, and this guard would abort every run.
    if _is_file_backend() and not os.path.exists(db_path):
        print(f"error: no such DB: {db_path}")
        return 1

    try:
        rows = _scan(db_path)
    except Exception as e:  # noqa: BLE001
        print(f"error: could not read memory_embeddings: {type(e).__name__}: {e}")
        return 1

    if not rows:
        print("No embeddings in this store — nothing to do.")
        return 0

    mixed = _mixed_kinds(rows)
    if not mixed:
        print(f"DB      : {db_path}")
        print("Status  : no vector_kind holds more than one model family — "
              "nothing to retire.")
        return 0

    keep = _keep_family(rows, args.keep)
    families = sorted({f for _k, _t, f, _d, _n in rows})
    if keep not in families:
        print(f"error: --keep '{keep}' is not present in this store. "
              f"Families found: {', '.join(families)}")
        return 1

    # Only rows inside a MIXED kind are candidates. A different model living
    # alone under its own vector_kind is a separate space by design and must
    # never be deleted.
    doomed = [r for r in rows if r[0] in mixed and r[2] != keep]
    total_doomed = sum(r[4] for r in doomed)
    total_keep = sum(r[4] for r in rows if r[2] == keep)

    print(f"DB      : {db_path}")
    print(f"Keeping : {keep}  ({total_keep:,} vectors)")
    if not doomed:
        print("Status  : single embedding space — nothing to retire.")
        return 0

    print(f"Retiring: {total_doomed:,} vectors from {len({r[2] for r in doomed})} "
          f"other famil{'y' if len({r[2] for r in doomed}) == 1 else 'ies'}:")
    for kind, tag, fam, dim, n in sorted(doomed, key=lambda r: -r[4]):
        print(f"  - {fam:22} tag={tag:38} kind={kind:10} dim={dim:<5} {n:>8,}")
    print()
    print("The parent memories are NOT touched — only their vectors are dropped, "
          "then regenerated by embed_backfill.py against the current model.")

    if not args.apply:
        print()
        print("DRY RUN — nothing deleted. Re-run with --apply to proceed.")
        return 0

    backup_path = ""
    if not args.no_backup:
        if not _is_file_backend():
            # A file copy is meaningless for a server-hosted store; taking one
            # silently would imply a rollback that does not exist. Say so and
            # make the operator opt in explicitly.
            print("error: --no-backup is required on a non-file backend. This tool "
                  "cannot snapshot a server-hosted store — take a dump first "
                  "(e.g. pg_dump) and re-run with --no-backup.")
            return 1
        try:
            backup_path = _backup(db_path)
            print(f"backup  : {backup_path}")
        except Exception as e:  # noqa: BLE001
            print(f"error: backup failed ({type(e).__name__}: {e}); aborting. "
                  f"Pass --no-backup to override.")
            return 1

    deleted = _delete_doomed(db_path, doomed)
    print(f"deleted : {deleted:,} embedding rows")

    if args.no_backfill:
        print()
        print("Next    : run `python bin/embed_backfill.py` to regenerate them.")
        return 0

    print()
    print("Regenerating via embed_backfill.py ...")
    import subprocess
    # ALWAYS pass the resolved db_path, never only when --db was given.
    # embed_backfill's own default is the pre-Homecoming repo-relative
    # `<repo>/memory/agent_memory.db`, which does not exist on a decoupled-roots
    # install — so omitting --db made it abort with "DB not found" AFTER this
    # tool had already committed the deletes, leaving the store with vectors
    # removed and nothing regenerating them. (Hit live 2026-07-23.)
    cmd = [sys.executable, os.path.join(_BIN, "embed_backfill.py"), "--db", db_path]
    rc = subprocess.call(cmd)
    if rc != 0:
        print()
        print(f"WARNING: embed_backfill.py exited {rc} — the stale vectors are "
              f"deleted but NOT yet regenerated.")
        print(f"         Re-run manually:  python bin/embed_backfill.py --db {db_path}")
        print(f"         Or restore:       {backup_path or '(no backup taken)'}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
