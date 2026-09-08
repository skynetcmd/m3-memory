"""The stale-guidance probe must catch known-wrong text without crying wolf.

m3 never writes CLAUDE.md / GEMINI.md — they are the user's files. The cost of
that (correct) boundary is that guidance users COPIED out of
docs/AGENT_INSTRUCTIONS.md never updates when we correct it. This probe closes
the gap by detection only.

Two failure modes matter equally here:
  - MISSING stale text leaves a user with a warning that fires when nothing is
    wrong, which is the exact trust problem the probe exists to fix.
  - FALSE-POSITIVING on a user's own prose would make the probe itself a
    wolf-crier. So the needles are specific, and a test pins that.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from doctor import agent_guidance_probe as P  # noqa: E402

_STALE_DISCONNECT = (
    'WARNING: The m3 memory MCP server is not reachable. '
    'Chatlog is NOT being captured. Design decisions made this session '
    'will NOT be preserved across sessions.'
)
_STALE_FLAG = "If `hook.enabled = false` OR `last_write` is null → warn"

_CORRECTED = (
    "m3's MCP tools are unreachable, so I can't search or write memory right "
    "now. Chatlog capture is unaffected — it writes to the DB directly, "
    "independent of the MCP connection. Reconnect with `/mcp`."
)


class _Fixture:
    """Point the probe at a temp file instead of the real home."""

    def __init__(self, tmpdir: pathlib.Path, text: str):
        self.path = tmpdir / "CLAUDE.md"
        self.path.write_text(text, encoding="utf-8")
        self._saved = P._home_files

    def __enter__(self):
        P._home_files = lambda: [self.path]
        return self

    def __exit__(self, *exc):
        P._home_files = self._saved
        return False


class TestStaleGuidanceDetection(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_detects_the_mcp_disconnect_false_alarm(self):
        with _Fixture(self.tmp, _STALE_DISCONNECT):
            ids = [f["id"] for f in P.check()]
        self.assertIn("mcp-disconnect-implies-loss", ids)

    def test_detects_the_hooks_enabled_false_positive(self):
        with _Fixture(self.tmp, _STALE_FLAG):
            ids = [f["id"] for f in P.check()]
        self.assertIn("hooks-enabled-false-alarm", ids)

    def test_corrected_guidance_is_clean(self):
        """The text we now ship must NOT trip the probe -- otherwise every
        user who applies the fix gets warned about having applied it."""
        with _Fixture(self.tmp, _CORRECTED):
            self.assertEqual(P.check(), [])

    def test_does_not_fire_on_unrelated_prose(self):
        """Guard against the probe becoming a wolf-crier itself."""
        text = (
            "# My notes\n"
            "m3 captures chatlog automatically. If the MCP server drops, "
            "reconnect and carry on; capture is independent.\n"
            "Remember to check capture.healthy in chatlog_status.\n"
        )
        with _Fixture(self.tmp, text):
            self.assertEqual(P.check(), [])

    def test_matching_is_case_insensitive(self):
        with _Fixture(self.tmp, _STALE_DISCONNECT.upper()):
            self.assertTrue(P.check())

    def test_findings_carry_a_path_why_and_fix(self):
        """A finding a user cannot act on is noise."""
        with _Fixture(self.tmp, _STALE_DISCONNECT):
            for f in P.check():
                self.assertTrue(f["path"])
                self.assertTrue(f["why"])
                self.assertTrue(f["fix"])


class TestProbeContract(unittest.TestCase):
    def test_run_is_exit_code_neutral_even_when_stale(self):
        """m3 does not own these files, so stale text is a correctness issue in
        the user's own notes -- not a broken install. Failing the doctor's exit
        code would break CI over something m3 cannot fix itself."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with _Fixture(pathlib.Path(d), _STALE_DISCONNECT):
                self.assertEqual(P.run(brief=True), 0)
                self.assertEqual(P.run(brief=False), 0)

    def test_run_survives_a_broken_check(self):
        """A probe must never take the doctor down."""
        saved = P.check
        try:
            P.check = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            self.assertEqual(P.run(brief=False), 0)
        finally:
            P.check = saved

    def test_missing_files_are_not_an_error(self):
        """Most users have no CLAUDE.md at all; that is not a finding."""
        saved = P._home_files
        try:
            P._home_files = lambda: [pathlib.Path("/nonexistent-xyz/CLAUDE.md")]
            self.assertEqual(P.check(), [])
        finally:
            P._home_files = saved

    def test_probe_never_writes(self):
        """Detection only. The whole design rests on m3 not touching these."""
        import inspect
        src = inspect.getsource(P)
        for forbidden in ("write_text(", "open(", "unlink(", "rmtree("):
            self.assertNotIn(forbidden, src, f"probe must not {forbidden}")


if __name__ == "__main__":
    unittest.main()
