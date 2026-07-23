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


def _scan(db_path: str):
    """Return [(vector_kind, embed_model, family, dim, count)] for the store."""
    from doctor.embed_space_probe import _family
    from memory.backends import active_backend

    out = []
    with active_backend().open_readonly(db_path) as conn:
        cur = conn.execute(
            "SELECT COALESCE(vector_kind, 'default'), COALESCE(embed_model, ''), "
            "       COALESCE(dim, 0), COUNT(*) "
            "FROM memory_embeddings GROUP BY 1, 2, 3"
        )
        for kind, tag, dim, n in cur.fetchall():
            out.append((str(kind), str(tag), _family(str(tag)), int(dim), int(n)))
    return out


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
        from m3_core.paths import resolve_engine_file
    except Exception as e:  # noqa: BLE001
        print(f"error: could not load path seam: {type(e).__name__}: {e}")
        return 1

    db_path = args.db or resolve_engine_file("agent_memory.db")
    if not os.path.exists(db_path):
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

    if not args.no_backup:
        try:
            dest = _backup(db_path)
            print(f"backup  : {dest}")
        except Exception as e:  # noqa: BLE001
            print(f"error: backup failed ({type(e).__name__}: {e}); aborting. "
                  f"Pass --no-backup to override.")
            return 1

    # Path-scoped write. active_backend().connection() takes NO path — it opens
    # the DEFAULT store — so using it here would silently ignore --db and delete
    # from the wrong database. bin/embed_backfill.py, the sibling tool this one
    # hands off to, connects directly for the same reason; match it.
    import sqlite3
    deleted = 0
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        for kind, tag, _fam, dim, _n in doomed:
            cur = conn.execute(
                "DELETE FROM memory_embeddings WHERE COALESCE(vector_kind,'default')=? "
                "AND COALESCE(embed_model,'')=? AND COALESCE(dim,0)=?",
                (kind, tag, dim),
            )
            deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    finally:
        conn.close()
    print(f"deleted : {deleted:,} embedding rows")

    if args.no_backfill:
        print()
        print("Next    : run `python bin/embed_backfill.py` to regenerate them.")
        return 0

    print()
    print("Regenerating via embed_backfill.py ...")
    import subprocess
    cmd = [sys.executable, os.path.join(_BIN, "embed_backfill.py")]
    if args.db:
        cmd += ["--db", args.db]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
