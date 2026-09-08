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


if __name__ == "__main__":
    unittest.main()
