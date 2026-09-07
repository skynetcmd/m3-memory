"""FILES_MAX_LEAF_TOKENS must actually be enforced, and `truncated` must not lie.

`files_memory/config.py` declared FILES_MAX_LEAF_TOKENS = 7000 with a comment
promising that oversize leaves "are truncated with a warning and `truncated=true`
flag". It had ZERO references outside its own definition: nothing truncated,
nothing warned, and `Leaf.truncated` -- a real column written at ingest.py:420 --
was never set True by any code path. So `WHERE truncated=1` handed every operator
a false clean bill of health, and a user could set M3_FILES_MAX_LEAF_TOKENS to
any value with no effect whatsoever.

That mattered because the per-chunker caps count CHARACTERS. markdown.py's
MAX_SECTION_CHARS=16000 is ~9,639 tokens of Chinese, ~9,816 of JSON and 16,000
of base64 -- all past bge-m3's 8,192 ceiling -- and Markdown is the format most
likely to carry exactly that (fenced code blocks, JSON examples, CJK docs).
text.py's MAX_CHARS=2400 is safe only by accident.

Hermetic: pure chunker-level tests, no DB, no embedder, no filesystem.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from files_memory.chunkers import Leaf, _enforce_leaf_token_cap  # noqa: E402
from files_memory.config import FILES_MAX_LEAF_TOKENS  # noqa: E402
from memory.tokens import estimate_tokens  # noqa: E402


def _leaf(text: str) -> Leaf:
    return Leaf(
        text=text, division_type="heading", division_id="h1",
        char_range_start=0, char_range_end=len(text),
    )


class TestLeafTokenCap(unittest.TestCase):
    def test_small_leaf_passes_through_unchanged(self):
        leaf = _leaf("hello world " * 50)
        out = list(_enforce_leaf_token_cap(leaf))
        self.assertEqual(len(out), 1)
        self.assertIs(out[0], leaf)
        self.assertFalse(out[0].truncated)

    def test_oversize_cjk_section_is_split(self):
        """A 16,000-char markdown section of Chinese -- the exact shape
        MAX_SECTION_CHARS permits and n_ctx rejects."""
        text = "数据库连接失败。" * 2000
        out = list(_enforce_leaf_token_cap(_leaf(text)))
        self.assertGreater(len(out), 1)
        for piece in out:
            self.assertLessEqual(estimate_tokens(piece.text), FILES_MAX_LEAF_TOKENS)

    def test_oversize_json_and_base64_are_split(self):
        for text in ('{"id":"a","score":0.9}' * 800, "eyJhbGciOiJIUzI1NiJ9" * 900):
            out = list(_enforce_leaf_token_cap(_leaf(text)))
            self.assertGreater(len(out), 1)
            for piece in out:
                self.assertLessEqual(estimate_tokens(piece.text), FILES_MAX_LEAF_TOKENS)

    def test_split_loses_no_content_and_keeps_order(self):
        """Splitting is preferred over truncation precisely because it does not
        discard content -- so prove concatenation reproduces the input."""
        text = "数据库连接失败。" * 2000
        out = list(_enforce_leaf_token_cap(_leaf(text)))
        self.assertEqual("".join(p.text for p in out), text)

    def test_char_ranges_are_contiguous(self):
        text = "abcdefghij" * 3000
        out = list(_enforce_leaf_token_cap(_leaf(text)))
        self.assertGreater(len(out), 1)
        for prev, curr in zip(out, out[1:]):
            self.assertEqual(prev.char_range_end, curr.char_range_start)
        self.assertEqual(out[0].char_range_start, 0)
        self.assertEqual(out[-1].char_range_end, len(text))

    def test_division_ids_are_distinct(self):
        """The ingester treats division_id as unique within
        (file_node, division_type); duplicates would collide on write."""
        out = list(_enforce_leaf_token_cap(_leaf("x" * 40000)))
        ids = [p.division_id for p in out]
        self.assertEqual(len(ids), len(set(ids)))

    def test_forced_split_lowers_boundary_confidence(self):
        """A token-forced split is not a structural boundary; downstream
        rankers should not treat it as one."""
        out = list(_enforce_leaf_token_cap(_leaf("x" * 40000)))
        self.assertGreater(len(out), 1)
        for piece in out:
            self.assertLessEqual(piece.boundary_confidence, 0.5)

    def test_no_empty_pieces(self):
        for text in ("x" * 40000, "数据库。" * 4000):
            for piece in _enforce_leaf_token_cap(_leaf(text)):
                self.assertTrue(piece.text)

    def test_truncated_flag_now_carries_information(self):
        """`truncated` must be False on an ordinary split -- it means "content
        was lost", and a split loses nothing. Previously it was False on
        EVERYTHING, including rows that overflowed, so it meant nothing."""
        out = list(_enforce_leaf_token_cap(_leaf("数据库连接失败。" * 2000)))
        self.assertGreater(len(out), 1)
        self.assertFalse(any(p.truncated for p in out))

    def test_cap_is_actually_referenced(self):
        """The regression that started this: a config knob with no call site.
        If FILES_MAX_LEAF_TOKENS stops being read, this fails."""
        import inspect
        from files_memory import chunkers
        src = inspect.getsource(chunkers)
        self.assertIn("FILES_MAX_LEAF_TOKENS", src)

    def test_dispatch_applies_the_cap(self):
        """The cap lives at the single dispatch point, so every chunker --
        including any added later -- inherits it."""
        import inspect
        from files_memory import chunkers
        self.assertIn("_enforce_leaf_token_cap",
                      inspect.getsource(chunkers.chunk_file))


if __name__ == "__main__":
    unittest.main()
