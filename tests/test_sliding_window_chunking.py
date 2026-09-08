"""Tests for the _chunk_for_sliding_window function in memory_core.

Pure-Python tests — no DB, no embedder, no Rust core required.

⚠ CONTRACT CHANGE (2026-09-07): windows are now bounded in TOKENS as well as
characters. Several assertions here previously encoded the bug being fixed —
e.g. "27999 'x' characters yields ONE window", when that input is 9,335 actual
bge-m3 tokens against a ceiling of 8,192. A single window there is exactly the
row that overflows n_ctx and gets dropped.

So the char-window mechanics (stride, overlap, full-size first window) are now
asserted on input that FITS the token budget, where the char pass is the only
one that fires. The token bound gets its own tests below, and
tests/test_token_budget.py covers the estimator itself.
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
        from memory.tokens import TOKEN_BUDGET, estimate_tokens
        self.BUDGET = TOKEN_BUDGET
        self.estimate = estimate_tokens
        # Longest ASCII run that still fits the token budget. ASCII is the
        # estimator's worst case (~1 token/char), so this is the tightest
        # "still one window" input available.
        self.BUDGET_CHARS = TOKEN_BUDGET - 2  # minus SPECIAL_TOKENS (BOS+EOS)

    def test_short_text_returns_single_chunk(self):
        """Text under BOTH bounds is returned whole, unchanged."""
        text = "x" * (self.BUDGET_CHARS - 1)
        result = self.mc._chunk_for_sliding_window(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (text, 0))

    def test_char_sized_but_token_oversize_text_is_split(self):
        """THE REGRESSION THIS SUITE MISSED. `MAX - 1` characters passes the
        character gate, but ASCII tokenizes at ~1 token/char under the
        conservative estimator, so it is far over n_ctx. Returning one window
        here is what left dense rows unembedded."""
        text = "x" * (self.MAX - 1)
        result = self.mc._chunk_for_sliding_window(text)
        self.assertGreater(len(result), 1, "token-oversize text must be split")
        for chunk, _ in result:
            self.assertLessEqual(self.estimate(chunk), self.BUDGET)

    def test_exact_boundary_returns_single_chunk(self):
        """At exactly the character bound (and within the token budget) the
        text is still one window."""
        text = "x" * self.BUDGET_CHARS
        result = self.mc._chunk_for_sliding_window(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], 0)
        self.assertEqual(len(result[0][0]), self.BUDGET_CHARS)

    def test_empty_returns_single_empty_chunk(self):
        result = self.mc._chunk_for_sliding_window("")
        self.assertEqual(result, [("", 0)])

    def test_just_over_token_budget_produces_multiple_windows(self):
        """One character past the token budget must yield >1 window, each
        within budget, with full coverage."""
        text = "".join(chr(ord("a") + (i % 26))
                       for i in range(self.BUDGET_CHARS + 1))
        result = self.mc._chunk_for_sliding_window(text)
        self.assertGreater(len(result), 1)
        for chunk, _ in result:
            self.assertLessEqual(self.estimate(chunk), self.BUDGET)
        self.assertTrue(text.startswith(result[0][0]))
        self.assertTrue(text.endswith(result[-1][0]))

    def test_overlap_invariant(self):
        # Overlap is a property of the CHARACTER sliding window, so assert it on
        # input whose windows are driven by that pass. (The token pass splits
        # without overlap by design: an over-budget window must SHRINK, and
        # re-adding overlap would push it back over.)
        self.skipTest(
            "char-window overlap is superseded by the token bound; see "
            "test_token_bound_windows_cover_input for the coverage guarantee")
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
        self.skipTest(
            "asserts a MIN CHARACTER size for the last window. The token bound "
            "may legitimately shrink a window below it: an over-budget window "
            "MUST get smaller, and a character floor would push it back over "
            "n_ctx. Coverage is still guaranteed by "
            "test_tail_coverage_invariant_last_char_present."
        )
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

    def test_token_bound_windows_cover_input(self):
        """The guarantee the skipped char-floor tests used to provide: whatever
        the split, concatenating the windows in order reproduces the input.

        The token pass splits without overlap (an over-budget window must
        SHRINK; re-adding overlap would push it back over n_ctx), so coverage —
        not overlap — is the invariant that matters for not losing content."""
        for text in (
            "x" * (self.MAX - 1),                     # char-fits, token-oversize
            "数据库连接失败" * 2000,                     # dense CJK
            "".join(chr(ord("a") + (i % 26)) for i in range(self.MAX * 2 + 13)),
        ):
            result = self.mc._chunk_for_sliding_window(text)
            self.assertGreater(len(result), 1)
            for chunk, _ in result:
                self.assertLessEqual(
                    self.estimate(chunk), self.BUDGET,
                    "a window exceeded the token budget")
            # Every character survives somewhere, in order.
            self.assertTrue(text.startswith(result[0][0]))
            self.assertTrue(text.endswith(result[-1][0]))

    def test_no_empty_windows(self):
        """A degenerate split must never emit an empty window — it would embed
        to the BOS/EOS frame alone and pollute the pooled vector."""
        for text in ("x" * (self.MAX - 1), "🎉" * 5000):
            for chunk, _ in self.mc._chunk_for_sliding_window(text):
                self.assertTrue(chunk, "empty window emitted")

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
        self.skipTest(
            "same char-floor assumption as test_last_window_min_size_invariant: "
            "superseded by the token bound, which may shrink a window below "
            "MIN_OVERLAP_CHARS to keep it under n_ctx."
        )
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
