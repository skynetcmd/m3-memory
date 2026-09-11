"""Differential search check: does this tree still answer like a known-good m3?

The unit suite can be entirely green while search returns DIFFERENT ROWS. It
happened on 2026-09-10: a bm25 pre-filter optimisation ranked inside a CTE that
could not see ``is_deleted``/tenancy, so filtered rows ate ``LIMIT`` slots and a
20-row request came back with 8. 3,920 tests passed. The regression was only
visible by asking two builds the same questions and diffing the answers --
which is exactly what this test automates.

**This is a comparison, not an assertion about quality.** It says "the working
tree returns what a reference build returns", so it catches silent retrieval
drift from refactors and optimisations.

WHAT IT CANNOT CATCH, stated plainly because it was counter-tested and failed:

* A bug both builds share. The diff is relative; identical wrongness reads as
  identical. The absolute fill-to-k check below covers one important case of
  this, but only that one.
* A backend-layer truncation, now that search fills to k. Restoring the
  2026-09-10 CTE bug (``_FTS_OVERFETCH=1``) makes
  ``keyword_search_with_row_data(limit=20)`` return 0 rows -- and this tool
  still reports a clean run, because the semantic top-up supplies whatever the
  truncated candidate query failed to. The defect is real and invisible HERE by
  construction.

  That class belongs in a unit test against the backend itself, below the fill
  layer: ``tests/test_candidate_sql_builder.py::
  test_keyword_search_returns_the_full_limit_despite_deleted_rows`` fails on it
  (verified). Do not rely on this script for it.

Reference build, in order of preference -- explicit beats auto-detected, so a
caller who names a reference always gets it:
  1. ``M3_DIFF_REF_PYTHON`` + ``M3_DIFF_REF_BIN`` -- any interpreter + bin dir
  2. a git worktree at ``M3_DIFF_REF_REV`` (exact, and always schema-matched)
  3. the pipx-installed m3 (its own venv interpreter, so its deps resolve)

A pinned git rev is the most reliable reference: an INSTALLED m3 can be old
enough that its schema expectations differ from a store the working tree seeded,
in which case it returns nothing and this test skips rather than lying.

Skips rather than fails when no reference exists: a fresh clone or CI box has
no installed m3, and a test that cannot run must not look like a passing one.

    python -m pytest tests/test_search_differential.py -v
    M3_DIFF_REF_REV=v2026.9.8.0 python -m pytest tests/test_search_differential.py

By default it runs against a THROWAWAY store seeded from this repo's own docs,
so it is self-contained and deterministic. Point it at a real store with
``M3_DIFF_STORE=/path/to/engine_root`` for a higher-signal (but read-only) run --
that is how the 2026-09-10 bug was found, against a 5,203-memory store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]

# Deliberately mixed: single tokens, multi-word phrases, and a phrase chosen to
# sit on the exact-substring short-circuit -- the path the 2026-09-10 bug broke.
QUERIES = [
    "postgres",
    "embedding",
    "contradiction detection",
    "storage backend",
    "cognitive loop",
    "deferred enrichment",
    "sync watermark",
]

# Runs in a subprocess against one build. Prints one JSON line.
_PROBE = r'''
import sys, os, json, asyncio, contextlib, io

# FIRST, before any m3 import: conftest's autouse sandbox exports its own
# M3_ENGINE_ROOT / M3_CONFIG_ROOT / M3_MEMORY_ROOT (and M3_MEMORY_ROOT is the
# MASTER override, from which the others derive when unset). A child inherits
# all three, so the probe pins the pair it wants and drops the master.
os.environ["M3_ENGINE_ROOT"] = sys.argv[1]
os.environ["M3_CONFIG_ROOT"] = sys.argv[2]
os.environ.pop("M3_MEMORY_ROOT", None)
os.environ["M3_FIPS_MODE"] = "0"
os.environ["M3_FIPS_STRICT"] = "0"
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        import memory_core as mc

        async def run():
            out = {{}}
            for q in {queries!r}:
                rows = await mc.memory_search_scored_impl(q, k={k})
                out[q] = [[round(float(s), 5), it["id"]] for s, it in rows]
            return out

        res = asyncio.run(run())
except Exception as exc:  # surfaced by the caller, never swallowed
    print(json.dumps({{"__error__": "%s: %s" % (type(exc).__name__, exc)}}))
else:
    print(json.dumps(res))
'''


def _run_probe(python: str, bin_dir: str, engine: str, config: str, k: int = 8) -> dict:
    code = _PROBE.format(queries=QUERIES, k=k)
    env = dict(os.environ)
    # PYTHONPATH rather than a sys.path.insert inside the script: a venv's
    # startup can reorder sys.path and win over the insert, which is exactly how
    # the pipx-installed payload failed to import while its files sat right
    # there. PYTHONPATH is applied before any of that.
    env["PYTHONPATH"] = bin_dir
    env["M3_ENGINE_ROOT"] = engine
    env["M3_CONFIG_ROOT"] = config
    env["M3_FIPS_MODE"] = "0"
    env["M3_FIPS_STRICT"] = "0"
    env.pop("M3_MEMORY_ROOT", None)  # never let a caller's root redirect a probe
    proc = subprocess.run(
        [python, "-c", code, engine, config],
        capture_output=True, text=True, timeout=900, env=env,
        # A neutral cwd: running from the repo root lets the dev tree's own
        # m3_memory/ shadow an installed one, so the "reference" build would
        # silently be the tree under test.
        cwd=tempfile.gettempdir(),
    )
    line = next(
        (ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("{")), ""
    )
    if not line:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return {"__error__": "no output: " + " / ".join(t[:120] for t in tail)}
    return json.loads(line)


def _reference_build() -> tuple[str, str, str] | None:
    """(label, python, bin_dir) for a known-good m3, or None."""
    py = os.environ.get("M3_DIFF_REF_PYTHON")
    bin_dir = os.environ.get("M3_DIFF_REF_BIN")
    if py and bin_dir and Path(bin_dir).is_dir():
        return ("env-specified", py, bin_dir)

    rev = os.environ.get("M3_DIFF_REF_REV")
    if rev:
        wt = Path(tempfile.gettempdir()) / f"m3-diff-{re.sub(r'[^A-Za-z0-9_.-]', '_', rev)}"
        if not wt.exists():
            rc = subprocess.run(
                ["git", "worktree", "add", "-q", "--detach", str(wt), rev],
                cwd=REPO, capture_output=True, text=True, timeout=600,
            )
            if rc.returncode != 0:
                return None
        if (wt / "bin" / "memory_core.py").exists():
            return (f"worktree@{rev}", sys.executable, str(wt / "bin"))
    # The pipx install, run with ITS OWN interpreter so its dependencies
    # resolve. Importing it with the dev interpreter does not work: the package
    # lives under m3_memory/ and its internal imports are bare (`import
    # memory_core`), which only resolve with its bin/ on sys.path.
    venv_py = Path.home() / "pipx" / "venvs" / "m3-memory" / "Scripts" / "python.exe"
    if not venv_py.exists():
        venv_py = Path.home() / ".local" / "pipx" / "venvs" / "m3-memory" / "bin" / "python"
    if venv_py.exists():
        # Locate it on disk. Asking the interpreter to `import m3_memory`
        # resolved to the DEV tree whenever the repo was reachable, which would
        # have made the "reference" build a copy of the tree under test -- a
        # test that always passes.
        for libdir in ("Lib/site-packages", "lib/site-packages"):
            cand = venv_py.parent.parent / libdir / "m3_memory" / "bin"
            if (cand / "memory_core.py").exists():
                return ("pipx-installed", str(venv_py), str(cand))
        for cand in (venv_py.parent.parent / "lib").glob("python*/site-packages/m3_memory/bin"):
            if (cand / "memory_core.py").exists():
                return ("pipx-installed", str(venv_py), str(cand))

    return None



_SEED = r'''
import sys, os, json, asyncio, contextlib, io
os.environ["M3_ENGINE_ROOT"] = sys.argv[1]
os.environ["M3_CONFIG_ROOT"] = sys.argv[2]
os.environ.pop("M3_MEMORY_ROOT", None)
os.environ["M3_FIPS_MODE"] = "0"
os.environ["M3_FIPS_STRICT"] = "0"
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    import memory_core as mc
    items = json.load(open(sys.argv[3], encoding="utf-8"))
    for i in range(0, len(items), 100):
        asyncio.run(mc.memory_write_bulk_impl(items[i:i + 100]))
print("seeded", len(items))
'''


def _seed_store(tmp: Path) -> tuple[str, str]:
    """Seed a throwaway store from this repo's own docs.

    Real prose, deterministic, and present in every checkout. Synthetic filler
    where every row shares the same words would make every query match
    everything -- a pathological case that measures the wrong thing.
    """
    engine, config = tmp / "engine", tmp / "config"
    engine.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)

    para = re.compile(chr(10) + r"\s*" + chr(10))
    chunks: list[str] = []
    for path in sorted((REPO / "docs").glob("*.md"))[:25]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        chunks += [c.strip() for c in para.split(text) if 120 <= len(c.strip()) <= 1200]
    if len(chunks) < 50:
        raise SystemExit("not enough doc prose in this checkout to seed a store")

    items = [
        {"type": "note", "title": c.splitlines()[0][:60] or f"note {i}", "content": c}
        for i, c in enumerate(chunks[:400])
    ]
    # The corpus goes in a FILE, not on the command line: Windows caps a command
    # line near 32 KB and 400 rows blew straight past it (WinError 206).
    items_path = tmp / "seed_items.json"
    items_path.write_text(json.dumps(items), encoding="utf-8")

    env = dict(os.environ, PYTHONPATH=str(REPO / "bin"))
    proc = subprocess.run(
        [sys.executable, "-c", _SEED, str(engine), str(config), str(items_path)],
        capture_output=True, text=True, timeout=1800,
        cwd=tempfile.gettempdir(), env=env,
    )
    if "seeded" not in (proc.stdout or ""):
        raise SystemExit("could not seed a store: "
                         + (proc.stderr or proc.stdout or "")[-300:])

    # Soft-delete a third of the rows. A store with NOTHING filtered cannot
    # exercise a filter-vs-LIMIT bug -- and that is not hypothetical: with the
    # original defect restored (_FTS_OVERFETCH=1) and no deletions, this check
    # reported IDENTICAL and missed the regression it exists to catch. Deleted
    # rows must never reach results, so both builds should still agree; what
    # changes is that a build which lets them consume LIMIT slots now returns
    # fewer rows than the reference.
    import sqlite3

    conn = sqlite3.connect(str(engine / "agent_memory.db"))
    try:
        conn.execute(
            "UPDATE memory_items SET is_deleted = 1 "
            "WHERE rowid % 3 = 0"
        )
        conn.commit()
        n_del = conn.execute(
            "SELECT count(*) FROM memory_items WHERE is_deleted = 1").fetchone()[0]
        n_all = conn.execute("SELECT count(*) FROM memory_items").fetchone()[0]
    finally:
        conn.close()
    print(f"seeded {n_all} rows ({n_del} soft-deleted, to exercise the filter path)")
    return str(engine), str(config)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=os.environ.get("M3_DIFF_STORE", ""),
                    help="existing engine root to query READ-ONLY "
                         "(default: seed a throwaway one from docs/)")
    ap.add_argument("--k", type=int, default=8)
    args = ap.parse_args()

    ref = _reference_build()
    if ref is None:
        print("no reference m3 found. Install one with pipx, or set "
              "M3_DIFF_REF_REV=<git-rev>, or M3_DIFF_REF_PYTHON + M3_DIFF_REF_BIN.")
        return 2
    label_ref, ref_python, ref_bin = ref

    tmp = None
    if args.store:
        root = Path(args.store)
        if not (root / "agent_memory.db").exists() and (root / "engine" / "agent_memory.db").exists():
            root = root / "engine"
        if not (root / "agent_memory.db").exists():
            print(f"no agent_memory.db under {args.store}")
            return 2
        engine, config = str(root), str(root.parent / "config")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="m3diff_"))
        engine, config = _seed_store(tmp)

    try:
        current = _run_probe(sys.executable, str(REPO / "bin"), engine, config, args.k)
        reference = _run_probe(ref_python, ref_bin, engine, config, args.k)
        for who, res in (("working tree", current), (f"reference ({label_ref})", reference)):
            if "__error__" in res:
                print(f"{who} could not run: {res['__error__']}")
                return 2
        if not any(reference.get(q) for q in QUERIES):
            # A reference that answers nothing proves nothing -- most likely it
            # could not open the store. Reporting "identical" here would be a lie.
            print(f"reference ({label_ref}) returned no results for any query; "
                  "cannot compare. Try M3_DIFF_REF_REV=<git-rev> instead.")
            return 2

        # Live row count: the absolute check below needs to know whether the
        # store COULD supply k, so a small corpus is reported as such instead of
        # looking like a truncation bug.
        import sqlite3 as _sq

        try:
            _c = _sq.connect(f"file:{Path(engine) / 'agent_memory.db'}?mode=ro", uri=True)
            live_rows = _c.execute(
                "SELECT count(*) FROM memory_items WHERE is_deleted = 0").fetchone()[0]
            _c.close()
        except Exception:
            live_rows = 0  # unknown -> the fill check below simply does not fire

        print(f"store     : {engine} ({live_rows} live rows)")
        print(f"reference : {label_ref}")
        print(f"{'':4}{'query':<26}{'tree':>6}{'ref':>6}")
        problems = []
        gained = []
        for q in QUERIES:
            cur, old = current.get(q, []), reference.get(q, [])
            ci, oi = [r[1] for r in cur], [r[1] for r in old]
            if ci == oi:
                status = "OK "
            elif set(ci) == set(oi):
                by_id = {r[1]: r[0] for r in old}
                if len({round(by_id[i], 5) for i in ci}) == 1:
                    status = "OK "          # reordered within a score tie
                else:
                    status, _ = "DIFF", problems.append(f"{q!r}: reordered at distinct scores")
            elif set(oi).issubset(set(ci)):
                # Pure ADDITION: every reference row is still present. That is a
                # fill (more rows for the same k), not drift -- losing a row is
                # the dangerous direction, gaining one is not.
                #
                # Order is NOT required to match here. A filled result re-ranks:
                # 'postgres' gained one row that sorted into the middle rather
                # than the end, which a prefix check wrongly called drift. What
                # matters is that nothing the reference found went missing.
                status = "FILL"
                gained.append(f"{q!r}: +{len(ci) - len(oi)} (n {len(oi)} -> {len(ci)})")
            else:
                status = "DIFF"
                _lost = len(set(oi) - set(ci))
                problems.append(
                    f"{q!r}: {_lost} lost / {len(set(ci) - set(oi))} gained "
                    f"(n {len(oi)} -> {len(ci)})"
                    + ("  <- ROWS LOST" if _lost else ""))
            print(f"{status:<4}{q:<26}{len(ci):>6}{len(oi):>6}")

        if problems:
            print()
            print("DIVERGES from the reference build:")
            for p in problems:
                print("  " + p)
            print()
            print("This is retrieval drift, not a perf regression. Diff the "
                  "candidate SQL and the ranking path before shipping.")
            return 1
        # ABSOLUTE check, not a comparison. Everything above is relative: it
        # asks "does this tree answer like the reference?", so a truncation bug
        # present in BOTH builds is invisible to it. That is not hypothetical --
        # this tool was counter-tested by restoring the 2026-09-10 CTE bug
        # (_FTS_OVERFETCH=1) and reported IDENTICAL, because the reference was
        # short by exactly the same rows.
        #
        # The absolute expectation: a store holding at least k live rows must
        # answer a k-request with k rows. Applied to BOTH builds, so it catches
        # a shared defect that no diff can see.
        # Only the TREE can fail this. A short REFERENCE is expected whenever the
        # tree carries a fill/limit fix the reference predates -- failing on that
        # would make every such improvement look like a regression.
        short = [
            f"{q!r}: {len(current.get(q, []))} of k={args.k} "
            f"({live_rows} live rows available)"
            for q in QUERIES
            if live_rows >= args.k and len(current.get(q, [])) < args.k
        ]
        ref_short = sum(
            1 for q in QUERIES
            if live_rows >= args.k and len(reference.get(q, [])) < args.k
        )
        if short:
            print()
            print("SHORT of k -- this tree caps results below what was asked:")
            for line in short:
                print("  " + line)
            print()
            print("A store with enough rows must fill k. This is the shape of the "
                  "2026-09-10 bug: a LIMIT consumed by rows filtered out later.")
            return 1
        if ref_short:
            print()
            print(f"note: the reference build is short of k on {ref_short} "
                  f"quer{'y' if ref_short == 1 else 'ies'}; this tree fills all "
                  f"{len(QUERIES)}.")

        print()
        if gained:
            print(f"MATCHES the reference build, with MORE rows for the same k "
                  f"(the reference was short):")
            for g in gained:
                print("  " + g)
            print()
            print("No reference row was lost or displaced. This is what a "
                  "fill-to-k change looks like; a regression would show losses.")
        else:
            print(f"IDENTICAL - search results match the reference build, "
                  f"and both fill k={args.k}.")
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
