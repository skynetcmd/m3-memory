"""Tests for the _chunk_for_sliding_window function in memory_core.

Pure-Python tests — no DB, no embedder, no Rust core required. Covers the
invariants documented in the function docstring.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


class SlidingWindowTests(unittest.TestCase):
    def setUp(self):
        # Reimport under each test so env-var overrides take effect.
        # In practice the constants are read at module import; we set defaults
        # here and rely on the module being imported once with those defaults.
        import memory_core
        self.mc = memory_core
        self.MAX = memory_core.MAX_CHARS_PER_CHUNK
        self.OVL = memory_core.MIN_OVERLAP_CHARS
        self.STRIDE = memory_core.STRIDE_CHARS

    def test_short_text_returns_single_chunk(self):
        text = "x" * (self.MAX - 1)
        result = self.mc._chunk_for_sliding_window(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (text, 0))

    def test_exact_boundary_returns_single_chunk(self):
        text = "x" * self.MAX
        result = self.mc._chunk_for_sliding_window(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], 0)
        self.assertEqual(len(result[0][0]), self.MAX)

    def test_empty_returns_single_empty_chunk(self):
        result = self.mc._chunk_for_sliding_window("")
        self.assertEqual(result, [("", 0)])

    def test_just_over_boundary_produces_two_windows(self):
        # MAX + 1 char => 2 windows. First is full-size MAX; second is the
        # remaining tail (~OVL+1 chars). No shift fires because tail >= OVL.
        text = "".join(chr(ord("a") + (i % 26)) for i in range(self.MAX + 1))
        result = self.mc._chunk_for_sliding_window(text)
        self.assertEqual(len(result), 2)
        # First window is full-size MAX chars, starts at 0
        self.assertEqual(len(result[0][0]), self.MAX)
        self.assertTrue(text.startswith(result[0][0]))
        # Last window covers the tail (no shift — tail is OVL+1, above the threshold)
        self.assertTrue(text.endswith(result[1][0]))
        # Last window length is at least MIN_OVERLAP_CHARS (the invariant)
        self.assertGreaterEqual(len(result[1][0]), self.OVL)

    def test_overlap_invariant(self):
        # For any two consecutive windows, overlap is at least MIN_OVERLAP_CHARS.
        text = "".join(chr(ord("a") + (i % 26)) for i in range(self.MAX * 3))
        result = self.mc._chunk_for_sliding_window(text)
        self.assertGreaterEqual(len(result), 2)
        for prev, curr in zip(result, result[1:]):
            # Position of prev's end in text:
            # find prev's content in text, get its end index
            prev_text, _ = prev
            curr_text, _ = curr
            prev_end_in_text = text.index(prev_text) + len(prev_text)
            curr_start_in_text = text.index(curr_text)
            overlap = prev_end_in_text - curr_start_in_text
            self.assertGreaterEqual(
                overlap, self.OVL,
                f"overlap {overlap} below MIN_OVERLAP_CHARS {self.OVL}"
            )

    def test_ceiling_invariant_every_chunk_under_max(self):
        # No chunk should ever exceed MAX_CHARS_PER_CHUNK.
        for n in [self.MAX + 1, self.MAX * 2, self.MAX * 3 + 7, self.STRIDE * 4 + 1]:
            text = "y" * n
            result = self.mc._chunk_for_sliding_window(text)
            for chunk_text, _ in result:
                self.assertLessEqual(
                    len(chunk_text), self.MAX,
                    f"chunk over ceiling for n={n}: {len(chunk_text)} > {self.MAX}",
                )

    def test_last_window_min_size_invariant(self):
        # Last window is always at least MIN_OVERLAP_CHARS, OR the entire text
        # is shorter than MIN_OVERLAP_CHARS (single-chunk case).
        for n in [self.MAX + 1, self.STRIDE + 1, self.STRIDE * 2 + 1, self.STRIDE * 3 + 100]:
            text = "z" * n
            result = self.mc._chunk_for_sliding_window(text)
            last_text, _ = result[-1]
            if n > self.MAX:
                self.assertGreaterEqual(
                    len(last_text), self.OVL,
                    f"last window too thin for n={n}: {len(last_text)} < {self.OVL}",
                )

    def test_tail_coverage_invariant_last_char_present(self):
        # The very last char of the input must appear in some window.
        for n in [self.MAX + 1, self.STRIDE * 2, self.STRIDE * 3 + 50]:
            text = "p" * (n - 1) + "Q"  # sentinel
            result = self.mc._chunk_for_sliding_window(text)
            self.assertTrue(
                any(c.endswith("Q") for c, _ in result),
                f"last char missing for n={n}",
            )

    def test_window_indices_are_sequential(self):
        text = "a" * (self.MAX * 4)
        result = self.mc._chunk_for_sliding_window(text)
        for i, (_, idx) in enumerate(result):
            self.assertEqual(idx, i)

    def test_first_window_starts_at_zero(self):
        for n in [self.MAX + 1, self.MAX * 5, self.STRIDE * 7]:
            text = "K" + "x" * (n - 1)
            result = self.mc._chunk_for_sliding_window(text)
            self.assertTrue(result[0][0].startswith("K"))

    def test_min_tail_invariant_holds_without_explicit_shift(self):
        # Verify the min-tail-size invariant holds for a wide range of
        # input lengths, even though no explicit shift-back code exists.
        # The invariant follows from STRIDE = MAX - OVL: whenever a naive
        # tail would be < OVL, the previous iteration would already have
        # been the last (because it extends past the tail's start by OVL
        # chars). Therefore the last window is always >= OVL chars long.
        for n in [
            self.MAX,
            self.MAX + 1,
            self.MAX + self.OVL // 2,
            self.MAX + self.OVL - 1,
            self.MAX + self.OVL,
            self.MAX + self.OVL + 1,
            self.MAX + self.STRIDE,
            self.STRIDE * 2 + 1,
            self.STRIDE * 2 + self.OVL - 1,
            self.STRIDE * 2 + self.OVL,
            self.STRIDE * 3,
            self.STRIDE * 3 + 1,
            self.STRIDE * 5 + 17,
        ]:
            text = "u" * n
            result = self.mc._chunk_for_sliding_window(text)
            last_text, _ = result[-1]
            if n > self.MAX:
                self.assertGreaterEqual(
                    len(last_text), self.OVL,
                    f"last-window-size invariant violated for n={n}: {len(last_text)} < {self.OVL}",
                )


if __name__ == "__main__":
    unittest.main()
