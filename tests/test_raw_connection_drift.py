"""Guard: feature code opens databases through the seam, not `sqlite3.connect`.

DESIGN_PHILOSOPHIES §1 (local-first / backend seam) and §10a. A raw
`sqlite3.connect(path)` is not merely untidy — it is a site that CANNOT work on
any non-SQLite backend, and PostgreSQL is already implemented while MariaDB is
planned. Every raw connection is therefore a place the seam does not reach, and
a place a future backend silently returns wrong or dies.

Three concrete things a raw connection bypasses:

  * **Backend routing** — `M3Context.for_db(path)` resolves WHICH backend owns
    that path. A raw connect assumes SQLite and is simply wrong on PostgreSQL.
  * **The connection pool** — raw connects open and drop a handle per call. The
    context pools them.
  * **The pragma stack** — `busy_timeout`, WAL, `foreign_keys`. Hand-rolled
    copies drift; two files in this repo had different busy_timeouts for the
    same database.

Why a guard and not just a conversion: converting the existing sites does not
stop the NEXT author adding site N+1, and the count has grown back before. This
test pins the CURRENT number and fails when it rises, so the debt can only
shrink. That is deliberately weaker than "zero raw connects" — a hard zero is
not reachable today (migrations must bootstrap a schema before a seam exists;
the seam's own backends must open something) and a guard nobody can make pass
gets deleted, which is worse than a guard that ratchets.

To LOWER the budget after converting sites: run this file, take the number it
reports, and lower `_BUDGET`. That is the intended workflow — the test tells you
the new floor.

To ADD a legitimate raw connect: put the file in `_EXEMPT` with a one-line
reason. That is a visible, reviewable edit rather than a silent new copy.
"""
from __future__ import annotations

import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories whose raw connections are out of scope for the seam.
_SKIP_DIRS = {
    "build",            # packaging copy of the tree
    "tests",            # test fixtures legitimately build throwaway DBs
    "to_be_deleted",
    ".venv",
    ".git",
    ".claude",          # nested worktrees from Claude Code
    "examples",         # standalone samples, shipped to be read not run
    "scripts",          # one-off operator scripts, not the product
    "benchmarks",
}

# Files where a raw sqlite3.connect is CORRECT, with the reason. These are not
# debt: converting them would be wrong.
_EXEMPT = {
    # --- The seam itself. It has to open something. ---
    "bin/memory/backends/sqlite_backend.py":
        "IS the SQLite backend — the thing every other site routes through.",
    # (bin/memory/backends/base.py is deliberately NOT exempt: it is the
    # ABSTRACT contract and contains no connect call. Exempting it would have
    # silently licensed a future raw connect in the one file that defines what
    # every backend must implement — caught by the staleness check below on this
    # guard's first run.)
    "bin/m3_core/context.py":
        "Owns the pool that get_sqlite_conn() hands out.",
    "bin/m3_core/paths.py":
        "Resolves and validates DB paths before any context exists.",
    "bin/sqlite_pragmas.py":
        "Defines the pragma stack itself; applying it needs a connection.",
    "bin/chatlog_config.py":
        "BUILDS the chatlog connection pool (same role as m3_core/context.py) -- "
        "the thing other code borrows from.",
    "bin/reembed_space.py":
        "Already branches on `is_file`; the raw connect is the SQLite arm and "
        "applies the shared pragma stack.",

    # --- Foreign stores: another product's file, not an m3 store. ---
    "bin/chatlog_ingest.py":
        "Reads OpenCode's OWN database (opencode.db) to import its transcripts. "
        "The seam is deliberately wrong here: backend.open_readonly() ignores "
        "db_path on PostgreSQL and would hand back m3's store instead of the "
        "file asked for, and the file is SQLite because OpenCode chose SQLite — "
        "not because m3 did. Adding a backend would not change this line, which "
        "is DESIGN section 10a's own test for what belongs in the dialect. "
        "Opened read-only; m3 never writes to it.",

    # --- Bootstrap: runs BEFORE a usable schema/seam exists. ---
    "bin/migrate_memory.py":
        "Creates and versions the schema the seam later assumes.",
    "bin/homecoming.py":
        "Relocates database FILES between roots; operates on paths, not rows.",
    "bin/split_chatlog_from_core.py":
        "One-time topology split: copies between two physical SQLite files.",
    "bin/backfill_content_hash.py":
        "Schema-era backfill that runs as part of migration.",
    "bin/setup_memory.py":
        "Bootstraps the store from nothing; there is no seam to route through "
        "until it has run.",
    "bin/migrate_entity_vocab.py":
        "One-shot vocabulary migration over the SQLite migration chain; exits "
        "if the file is absent.",
    "bin/migrate_flat_memory.py":
        "One-way ETL from an EXTERNAL SQLite DB (OpenClaw), opened "
        "?mode=ro&immutable=1. A foreign file, not m3's store.",

    # --- Self-test harnesses that must NOT share the app's pool. ---
    "bin/test_memory_bridge.py":
        "Bridge self-test: deliberately isolates from the live pool.",
    "bin/test_bulk_parity.py":
        "Parity harness comparing raw vs seam results — needs the raw side.",
    "bin/setup_test_db.py":
        "Builds throwaway fixture databases.",

    # --- Correctly capability-gated already; converting would be wrong. ---
    # Each of these branches on `backend.name == "sqlite"` (the capability form
    # §10a asks for, not `!= "postgres"`) and takes a real PG path in the other
    # arm. The raw connect is the SQLite ARM of a working branch, not an
    # assumption that SQLite is all there is.
    "bin/chatlog_decay.py":
        "SQLite arm of `if backend.name == \"sqlite\"`; PG uses the pooled path.",
    "bin/chatlog_prune.py":
        "SQLite arm of an explicit backend branch; dialect drives the SQL.",
    "bin/m3_enrich.py":
        "SQLite arm of `if _backend.name == \"sqlite\"`.",
    "bin/curator_apply.py":
        "SQLite arm of an explicit branch; its docstring records the stale-file "
        "write on PG that this branch FIXED.",
    "bin/m3_cognitive_loop.py":
        "Comment records the raw connect was already replaced; remaining use is "
        "backend-appropriate.",
    "bin/memory/db.py":
        "Both sites are inside _lazy_init holding _init_lock; "
        "backend.connection() delegates back to _db() -> _lazy_init, so a raw "
        "handle is REQUIRED to avoid deadlock. Both are gated on "
        "resolve_backend_name() == \"sqlite\"; PG gets these from its pg_* chain.",
    "bin/chatlog_status.py":
        "All sites are SQLite arms of real branches: the main/chatlog counts sit "
        "under `_primary_is_sqlite`, the files count under `_pg_files`, and "
        "_recent_write_count now returns its -1 unknown sentinel on non-SQLite "
        "rather than walking candidate FILE paths that cannot exist on PG.",
    "bin/memory/backends/selector.py":
        "Defines require_sqlite_backend(), the fail-loud guard FOR this pattern.",
    "bin/enrich/prep.py":
        "Replays a SQLite-DIALECT migration file (executescript + sqlite_master). "
        "Returns early on any non-sqlite backend; PG gets these tables from its "
        "own pg_040 migration. Documented in the function's own docstring.",

    # --- FILES: the raw connect is the SQLite ARM of a real backend branch. ---
    # FILES fully supports PostgreSQL. The sidecar file (files_database.db)
    # exists ONLY on SQLite; on PG the files tables live in a `files` SCHEMA of
    # the primary DB (tasks #9-#11) and are reached through the seam. Each file
    # below already branches on that, so the raw connect is the correct SQLite
    # half of a working pair -- NOT an assumption that FILES is SQLite-only.
    "bin/gen_wiki.py":
        "Branches on backend: sidecar file on SQLite, files_memory.db._db() on "
        "PG. Its own comment records that this routing is what keeps the files "
        "corpus VISIBLE on PG (it previously rendered memory-only there).",
    "bin/files_memory/db.py":
        "Owns that branch: _is_postgres() routes to the seam on PG and to the "
        "local sidecar only on SQLite.",
    "bin/files_memory/entities.py":
        "Default path goes through the seam (both backends); the raw connect is "
        "the explicit-file escape hatch for tests and isolated mutators.",

    # --- Benchmarks measure the raw baseline on purpose. ---
    "bin/bench_memory.py":
        "Benchmarks the pure-SQLite baseline; routing it through the pool would "
        "measure the pool instead of the thing under test.",

    # --- SQLite FILE-level operations; no cross-backend meaning. ---
    "m3_memory/install/fs.py":
        "PRAGMA wal_checkpoint(TRUNCATE) and Connection.backup() are SQLite "
        "file APIs -- this copies/shrinks .db FILES during install, it does not "
        "query a store. PG has no file to copy.",

    # --- Self-test harnesses that must NOT share the app's pool. ---
    "bin/test_mcp_proxy.py":
        "Self-test harness for the MCP proxy.",

    # --- Standalone-execution fallbacks and schema repair. ---
    "bin/embed_backfill.py":
        "One site: the documented pure-Python fallback for running this sweeper "
        "standalone, without the payload on sys.path. Every query path goes "
        "through _bound_db()/active_database().",
    "bin/ai_mechanic.py":
        "Schema REPAIR: DROP TABLE / CREATE TABLE to rebuild a corrupted "
        "SQLite store. Bootstrap class, like migrate_memory.",
    "bin/chatlog_embed_sweeper.py":
        "Sweeps a SQLite chatlog FILE by path; the PG chatlog is swept through "
        "the seam by the cognitive loop's embed pass (see the branch above it).",
    "bin/m3_enrich_assign.py":
        "CLI helper over an explicit --db SQLite file; applies the shared "
        "pragma stack and exits if the file is absent.",
    "bin/m3_enrich_batch.py":
        "Enrich state handle held across a long batch body with four existing "
        "close() sites; converting needs the call-site rework tracked with the "
        "remaining L3 writers.",

    # --- Must never fail on a seam import error. ---
    "bin/auth_utils.py":
        "Every vault read/write now goes through the seam; the one remaining "
        "connect is the SQLite-only bootstrap shim in _backend(), reached only "
        "when the seam is not yet importable (installer bootstrap).",
    "bin/hooks/chatlog/session_start_capture_check.py":
        "Session-start hook: a seam import error must not break session start.",

    # --- Test harnesses that BUILD a throwaway store, like tests/ itself. ---
    "bin/search_differential.py":
        "Differential harness: seeds and soft-deletes rows in a throwaway "
        "fixture store to diff two search builds. Same category as the tests/ "
        "skip -- it is constructing the fixture, not reading product data. "
        "Surfaced 2026-09-12 when _code_lines stopped dropping whole lines "
        "containing a string literal; it had been invisible, not absent.",
}


# Built at runtime, not written literally: a source file containing an
# f-string that mentions the idiom would be scanned by this very guard.
PROSE_FSTRING = 'x = f"Do not use ' + "sqlite3.connect" + ' here {var}"\n'
REAL_FSTRING = 'conn = ' + "sqlite3.connect" + '(f"{root}/a.db")\n'


def _iter_py():
    for p in _ROOT.rglob("*.py"):
        rel = p.relative_to(_ROOT).as_posix()
        if set(p.relative_to(_ROOT).parts) & _SKIP_DIRS:
            continue
        yield rel, p


def _code_lines(src: str):
    """Yield (lineno, text) for lines that are CODE, not prose about code.

    Skips `#` comments AND string literals — including docstrings. Both matter:
    this file's whole purpose is finding a call, and a module that *documents*
    why it no longer makes that call was being counted as still making it
    (bin/embed_backfill.py, whose docstring names the idiom it removed). That
    inflates the debt AND, worse, is the same class of blindness in reverse: a
    real connect could hide on a line the naive scan had already learned to
    ignore.

    Uses the tokenizer rather than a regex, so it cannot be fooled by nesting
    or quote style. Falls back to the comment-only filter if the file does not
    parse (a syntax error is someone else's test to fail).
    """
    import io as _io
    import tokenize

    lines = src.splitlines()

    try:
        toks = list(tokenize.generate_tokens(_io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        for i, line in enumerate(lines, 1):
            if not line.strip().startswith("#"):
                yield i, line
        return

    # Blank out the SPAN each comment/string occupies, rather than discarding
    # every line it touches.
    #
    # The earlier version added each such line to a `prose` set and skipped it
    # whole. That made the guard blind to its own majority case, because almost
    # every real raw connect passes a STRING path:
    #
    #     sqlite3.connect("/tmp/x.db")   -> line dropped, INVISIBLE
    #     sqlite3.connect(PATH)          -> line kept, correctly caught
    #
    # So the budget read 0 while uncounted violations sat in the tree
    # (query.py at the repo root, 2026-09-12). A guard that reports clean
    # because it cannot see is worse than no guard: it reads as coverage.
    # DESIGN_PHILOSOPHIES 12c -- plant a violation and watch it trip.
    #
    # Blanking the span preserves the rest of the line, so the call survives
    # while the literal's CONTENTS (which may legitimately mention the idiom in
    # prose) do not.
    # FSTRING_MIDDLE matters as much as STRING. Python 3.12 changed f-string
    # tokenization: `f"...{x}"` is no longer one STRING token but
    # FSTRING_START / FSTRING_MIDDLE / FSTRING_END, and the PROSE lives in
    # FSTRING_MIDDLE. Checking only STRING therefore leaves f-string text
    # unscrubbed, so a mention inside an f-string reads as a real call -- the
    # over-count this scrubbing exists to prevent, reappearing through a
    # tokenizer change. Resolved via getattr so the 3.11 floor (where these
    # token types do not exist) still runs.
    _fstring_mid = getattr(tokenize, "FSTRING_MIDDLE", None)
    _prose_types = {tokenize.COMMENT, tokenize.STRING}
    if _fstring_mid is not None:
        _prose_types.add(_fstring_mid)

    scrubbed = {}
    for tok in toks:
        if tok.type not in _prose_types:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for ln in range(srow, erow + 1):
            if ln - 1 >= len(lines):
                continue
            text = scrubbed.get(ln, lines[ln - 1])
            a = scol if ln == srow else 0
            b = ecol if ln == erow else len(text)
            scrubbed[ln] = text[:a] + " " * max(0, min(b, len(text)) - a) + text[b:]

    for i, line in enumerate(lines, 1):
        out = scrubbed.get(i, line)
        if out.strip():
            yield i, out




def _raw_sites():
    """(rel_path, lineno, line) for every non-exempt raw sqlite3.connect."""
    out = []
    for rel, p in _iter_py():
        if rel in _EXEMPT:
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in _code_lines(src):
            if "sqlite3.connect" in line:
                out.append((rel, i, line.strip()))
    return out


# The current count. LOWER THIS as sites are converted; it must never rise.
# 2026-09-08: 135 total in shipping code; the exempt set above is correct-as-is
# (seam, bootstrap, cross-backend, self-test), leaving this many to convert.
# Of the remainder, 6 are already capability-gated (`resolve_backend_name() ==
# "sqlite"` with an honest n/a on PG) and are fine; the rest assume SQLite
# unconditionally. Started at 71; auth_utils' vault probe was the first
# conversion (a real PG defect, not tidiness -- see that commit).
_BUDGET = 0


class TestRawConnectionDrift(unittest.TestCase):

    def test_no_new_raw_connections(self):
        sites = _raw_sites()
        n = len(sites)
        if n > _BUDGET:
            new = "\n  ".join(f"{f}:{ln}  {src}" for f, ln, src in sites[-12:])
            self.fail(
                f"Raw sqlite3.connect count ROSE to {n} (budget {_BUDGET}).\n"
                f"Open databases through the seam instead:\n"
                f"  read-only : backend.open_readonly(path)\n"
                f"  scoped    : with active_database(path): ... memory.db._db()\n"
                f"  pooled    : M3Context.for_db(path).get_sqlite_conn()\n"
                f"A raw connect cannot work on PostgreSQL/MariaDB.\n"
                f"If this site is legitimately raw, add it to _EXEMPT with a "
                f"reason.\nSome current sites:\n  {new}"
            )

    def test_budget_is_not_stale(self):
        """If the count dropped, lower the budget in the same change.

        Otherwise the guard silently loosens: someone converts 20 sites, the
        budget still says 72, and 20 new raw connects can appear without ever
        tripping it. A ratchet that does not tighten is not a ratchet.
        """
        n = len(_raw_sites())
        self.assertGreaterEqual(
            _BUDGET, n,
            "budget below actual — this should have failed the test above",
        )
        self.assertLessEqual(
            _BUDGET - n, 5,
            f"Raw-connect count is {n} but the budget is {_BUDGET}. "
            f"Sites were converted without tightening the ratchet — lower "
            f"_BUDGET to {n}.",
        )

    def test_exempt_entries_still_exist(self):
        """An exemption for a deleted/renamed file is dead weight that hides
        the fact the guard's coverage changed."""
        missing = [f for f in _EXEMPT if not (_ROOT / f).exists()]
        self.assertEqual(
            missing, [],
            f"_EXEMPT names files that no longer exist: {missing}. "
            f"Remove them so the exemption list reflects reality.",
        )

    def test_exempt_entries_actually_contain_the_idiom(self):
        """An exemption for a file that no longer has a raw connect is stale —
        it would silently permit a NEW one later."""
        pointless = []
        for f in _EXEMPT:
            p = _ROOT / f
            if not p.exists():
                continue
            if "sqlite3.connect" not in p.read_text(encoding="utf-8",
                                                    errors="replace"):
                pointless.append(f)
        self.assertEqual(
            pointless, [],
            f"_EXEMPT names files with no raw sqlite3.connect: {pointless}. "
            f"Drop the exemption so a future raw connect there is caught.",
        )

    def test_the_guard_can_actually_fail(self):
        """§12c: a guard that cannot demonstrate a catch is indistinguishable
        from one that is blind. Plant a violation and watch it trip.

        This test used to be a TAUTOLOGY. It asserted
        ``self.assertIn("sqlite3.connect", planted)`` against a string it had
        just written -- checking Python's ``in`` operator, never once calling
        ``_code_lines``. So it could not fail, and it did not: the detector
        spent that whole time blind to every connect whose path was a STRING
        literal, because ``_code_lines`` dropped any line a string token
        touched. The budget read 0 while real violations sat in the tree
        (query.py at the repo root; bin/search_differential.py in shipping code,
        now exempted with a reason).

        The fix is to run the DETECTOR over planted source, both forms, plus the
        prose cases it must NOT flag. A self-test that does not invoke the thing
        it certifies is decoration.
        """
        def seen(src):
            return [t for _, t in _code_lines(src)]

        # 1. The case that was invisible: a STRING path.
        hits = seen('conn = sqlite3.connect("/tmp/x.db")\n')
        self.assertTrue(
            any("sqlite3.connect" in h for h in hits),
            "a connect with a string-literal path is invisible to the detector "
            "-- this is the exact blindness that let the budget read 0",
        )

        # 2. The case that always worked: a VARIABLE path.
        hits = seen("conn = sqlite3.connect(path)\n")
        self.assertTrue(any("sqlite3.connect" in h for h in hits))

        # 3. Multi-line call with the literal on its own line.
        hits = seen('conn = sqlite3.connect(\n    "/tmp/x.db"\n)\n')
        self.assertTrue(any("sqlite3.connect" in h for h in hits))

        # 4. A COMMENT about the idiom must NOT count.
        hits = seen("# never call sqlite3.connect here\n")
        self.assertFalse(
            any("sqlite3.connect" in h for h in hits),
            "prose in a comment was counted as a use",
        )

        # 5. A DOCSTRING about the idiom must NOT count -- the false positive
        #    the string-stripping was introduced to fix. Both directions matter:
        #    fixing the blindness must not resurrect the over-count.
        hits = seen('"""We no longer call sqlite3.connect here."""\n')
        self.assertFalse(
            any("sqlite3.connect" in h for h in hits),
            "prose in a docstring was counted as a use",
        )

        # 6. F-STRING PROSE must not count. Python 3.12 split f-strings into
        #    FSTRING_START/MIDDLE/END, so scrubbing only STRING leaves the
        #    prose visible and the guard over-counts. Found in review by the
        #    other agent, on a fix that was otherwise correct -- a tokenizer
        #    change quietly reopening a closed hole is exactly why a
        #    self-test has to exercise the real detector.
        hits = seen(PROSE_FSTRING)
        self.assertFalse(
            any("sqlite3.connect" in h for h in hits),
            "prose inside an f-string was counted as a use (FSTRING_MIDDLE)",
        )

        # 7. ...but an f-string used as a REAL path must still be caught.
        #    Scrubbing f-strings must not become a new blind spot.
        hits = seen(REAL_FSTRING)
        self.assertTrue(
            any("sqlite3.connect" in h for h in hits),
            "a real connect with an f-string path went invisible",
        )

        # 8. A string MENTIONING the idiom on a line that also has real code.
        hits = seen('LOG = "do not sqlite3.connect"\nconn = sqlite3.connect(p)\n')
        self.assertEqual(
            sum("sqlite3.connect" in h for h in hits), 1,
            "must count the real call exactly once and ignore the mention",
        )




if __name__ == "__main__":
    unittest.main()
