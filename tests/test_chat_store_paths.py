"""`chat_store_paths()` must find EVERY store chat turns can land in.

THE TRAP. On a SPLIT topology (the default) turns live in `agent_chatlog.db`
while `agent_memory.db` is a different file. Any caller that checks only the
main DB sees ZERO chat_log rows on a perfectly healthy install. That has
produced the same bug three times, each in a caller that hand-rolled its own
answer:

  - the cognitive loop's embed gate (2026-07-25: 6,653 unembedded turns while
    the loop reported no work),
  - `m3 embedder backfill` before it reused `_embed_target_dbs`,
  - the SessionStart capture check (2026-09-07: it reported "capture NOT
    writing" while the chatlog store held 654 rows in the window -- a false
    alarm on every session start, which is exactly the cry-wolf failure that
    check exists to prevent).

The duplication is the defect. These tests pin the shared answer, and in
particular that the chatlog half comes from the CANONICAL resolver rather than
a sibling-of-the-main-DB guess -- a guess sees neither CHATLOG_DB_PATH, nor the
active-database ContextVar, nor a pinned `db_path`.
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import chatlog_config as C  # noqa: E402


class TestChatStorePaths(unittest.TestCase):
    def test_returns_at_least_one_store(self):
        self.assertTrue(C.chat_store_paths())

    def test_paths_are_absolute(self):
        for p in C.chat_store_paths():
            self.assertTrue(os.path.isabs(p), f"{p} is not absolute")

    def test_deduplicated(self):
        """A UNIFIED deployment resolves both halves to the same file; callers
        must not embed it twice and double-count."""
        paths = C.chat_store_paths()
        self.assertEqual(len(paths), len(set(paths)))

    def test_split_topology_includes_the_chatlog_store(self):
        """The regression itself: on a split install the chatlog store must be
        in the list, or every caller under-counts to zero."""
        chat = os.path.abspath(C.chatlog_db_path())
        paths = [os.path.abspath(p) for p in C.chat_store_paths()]
        self.assertIn(chat, paths)

    def test_honours_an_explicit_chatlog_db_env(self):
        """The reason to call the resolver instead of guessing a sibling: a
        relocated chatlog must still be found."""
        saved = os.environ.get("CHATLOG_DB_PATH")
        target = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "_relocated_chatlog.db"))
        try:
            os.environ["CHATLOG_DB_PATH"] = target
            C.invalidate_cache()
            self.assertIn(target, [os.path.abspath(p) for p in C.chat_store_paths()])
        finally:
            if saved is None:
                os.environ.pop("CHATLOG_DB_PATH", None)
            else:
                os.environ["CHATLOG_DB_PATH"] = saved
            C.invalidate_cache()

    def test_main_store_is_first(self):
        """Callers that stop at the first hit must stay correct on a unified
        deployment, so ordering is part of the contract."""
        paths = C.chat_store_paths()
        self.assertTrue(paths[0].endswith(".db"))

    def test_a_broken_chatlog_config_still_yields_the_main_store(self):
        """A caller that only needs one path must not be starved by a bad
        chatlog config -- fail soft, not silent-empty."""
        saved = C.chatlog_db_path
        try:
            C.chatlog_db_path = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertTrue(C.chat_store_paths())
        finally:
            C.chatlog_db_path = saved


class TestSessionStartHookUsesIt(unittest.TestCase):
    """The SessionStart hook is the ONLY failure signal that works when the MCP
    connection is down, so its store resolution must not regress."""

    def test_hook_prefers_the_shared_resolver(self):
        import inspect
        sys.path.insert(0, str(
            pathlib.Path(__file__).resolve().parents[1] / "bin" / "hooks" / "chatlog"))
        import session_start_capture_check as H
        src = inspect.getsource(H._candidate_dbs)
        self.assertIn("chat_store_paths", src,
                      "hook must use the shared resolver, not hand-roll paths")

    def test_hook_finds_both_stores(self):
        sys.path.insert(0, str(
            pathlib.Path(__file__).resolve().parents[1] / "bin" / "hooks" / "chatlog"))
        import session_start_capture_check as H
        self.assertIn(os.path.abspath(C.chatlog_db_path()),
                      [os.path.abspath(p) for p in H._candidate_dbs()])


class TestEnvPollutionCollapsesStores(unittest.TestCase):
    """The FOURTH instance of this bug class, and a new mechanism (#180).

    The three in the module docstring were all callers hand-rolling the store
    list. This one corrupts the SHARED resolver's own answer: `chat_store_paths`
    takes its "main store" entry from `resolve_db_path(None)`, which reads
    `M3_DATABASE`. A caller that sets that env var and does not restore it --
    while looping over core THEN chatlog, so the value left behind is the
    CHATLOG path -- makes the resolver return the chatlog store AS main and
    collapse to a single entry. `_embed_target_dbs` then substitutes its own
    main over that one entry and the chatlog store disappears entirely, so
    `has_embed_work()` reads a clean main DB, reports no work, and the
    unembedded chat backlog grows until the process restarts.

    Using the shared resolver is therefore NOT sufficient on its own: the env
    var it depends on has to be scoped. That is what `scoped_db_env` is for.
    """

    def setUp(self):
        self._prev = os.environ.get("M3_DATABASE")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("M3_DATABASE", None)
        else:
            os.environ["M3_DATABASE"] = self._prev
        C.invalidate_cache()

    def test_leaked_env_collapses_the_store_list(self):
        """Pins the MECHANISM: this is what a leak does, so it stays diagnosed."""
        chat = os.path.abspath(C.chatlog_db_path())
        os.environ["M3_DATABASE"] = chat
        C.invalidate_cache()
        paths = [os.path.abspath(p) for p in C.chat_store_paths()]
        self.assertEqual(
            paths, [chat],
            "a leaked M3_DATABASE should collapse the list to the chatlog store "
            "-- if this no longer holds, the #180 mechanism has changed and the "
            "scoped_db_env callers should be re-reviewed",
        )

    def test_scoped_db_env_restores_and_keeps_both_stores(self):
        from m3_sdk import scoped_db_env

        main = os.path.abspath(C._main_path_from_env() or C.MAIN_DB_PATH)
        chat = os.path.abspath(C.chatlog_db_path())
        if main == chat:
            self.skipTest("unified topology: one store, nothing to collapse")

        os.environ["M3_DATABASE"] = main
        C.invalidate_cache()

        # Iterate core THEN chatlog, as every affected caller does.
        for db in (main, chat):
            with scoped_db_env(db):
                pass

        self.assertEqual(os.environ.get("M3_DATABASE"), main,
                         "scoped_db_env must restore the prior value")
        C.invalidate_cache()
        paths = [os.path.abspath(p) for p in C.chat_store_paths()]
        self.assertIn(chat, paths)
        self.assertIn(main, paths)

    def test_scoped_db_env_restores_absence(self):
        from m3_sdk import scoped_db_env

        os.environ.pop("M3_DATABASE", None)
        with scoped_db_env(os.path.abspath(C.chatlog_db_path())):
            pass
        self.assertNotIn(
            "M3_DATABASE", os.environ,
            "absence must restore as absence: an empty string falls through "
            "resolve_db_path's `or`, but readers that test for the KEY see a "
            "variable that did not exist before",
        )


if __name__ == "__main__":
    unittest.main()
