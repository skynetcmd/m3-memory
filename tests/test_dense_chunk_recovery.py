"""Unit tests for _subdivide_dense_chunk — the dense-content recovery helper.

Pure Python; no DB, no embedder.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


class SubdivideDenseChunkTests(unittest.TestCase):
    def setUp(self):
        import memory_core
        self.mc = memory_core

    def test_empty_or_zero_tokens_returns_input_unchanged(self):
        self.assertEqual(self.mc._subdivide_dense_chunk("", 0), [""])
        self.assertEqual(self.mc._subdivide_dense_chunk("hello", 0), ["hello"])
        self.assertEqual(self.mc._subdivide_dense_chunk("", 100), [""])

    def test_chars_per_token_drives_sub_chunk_size(self):
        # Simulate the 778e7500 case: 28000 chars, 16875 tokens => 1.66 c/t.
        # Sub-chunks should target 7000 tokens * 1.66 c/t * 0.9 = ~10460 chars.
        text = "x" * 28000
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=16875)
        # Every sub-chunk must be <= the targeted safe size
        max_sub = max(len(s) for s in subs)
        chars_per_token = 28000 / 16875
        expected_target = int(self.mc.DENSE_TARGET_TOKENS * chars_per_token * 0.90)
        self.assertLessEqual(max_sub, expected_target,
            f"sub-chunk exceeds target: {max_sub} > {expected_target}")
        # Should produce at least 2 sub-chunks for an over-ceiling input
        self.assertGreaterEqual(len(subs), 2)

    def test_qwen3_style_case_higher_ratio(self):
        # 7127bb1e case: 28000 chars, 9735 tokens => 2.88 c/t.
        # Sub-chunks: 7000 * 2.88 * 0.9 = ~18100 chars => 2 sub-chunks
        text = "y" * 28000
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=9735)
        max_sub = max(len(s) for s in subs)
        chars_per_token = 28000 / 9735
        expected_target = int(self.mc.DENSE_TARGET_TOKENS * chars_per_token * 0.90)
        self.assertLessEqual(max_sub, expected_target)
        # Lighter density => fewer sub-chunks
        self.assertLess(len(subs), 4)
        self.assertGreaterEqual(len(subs), 2)

    def test_min_sub_chars_floor_prevents_infinite_subdivision(self):
        # Pathologically dense input: chars_per_token=0.1 (10x denser than
        # CJK). Computed target = 7000 * 0.1 * 0.9 = 630 chars. Without a
        # floor, we'd produce ~16 sub-chunks each tiny — each still likely
        # to overflow because the density assumption itself is wrong.
        # The floor at DENSE_MIN_SUB_CHARS=2000 caps subdivision: we accept
        # that a truly pathological row may still partially fail at the
        # embedder level (caught by the second-level except in the caller)
        # rather than fragment into useless 600-char shreds.
        text = "z" * 10000
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=100000)
        # All non-tail sub-chunks are exactly DENSE_MIN_SUB_CHARS (floor applied)
        for s in subs[:-1]:
            self.assertEqual(len(s), self.mc.DENSE_MIN_SUB_CHARS,
                f"floor not applied to non-tail sub-chunk: {len(s)} != {self.mc.DENSE_MIN_SUB_CHARS}")
        # Tail can be smaller (just the leftover after striding)
        self.assertGreater(len(subs), 1, "expected multiple sub-chunks with floor active")

    def test_sub_chunks_cover_full_input(self):
        # All sub-chunks concatenated (after stripping overlap) should cover
        # every char of the input. Verify by checking the union of position
        # ranges spans [0, len(text)).
        text = "abcdefghij" * 3000  # 30000 chars, deterministic content
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=12000)
        # Every char of text must appear in at least one sub-chunk
        for i in range(0, len(text), 1000):
            sample = text[i:i+50]
            found = any(sample in s for s in subs)
            self.assertTrue(found, f"char range {i}..{i+50} missing from sub-chunks")

    def test_consecutive_sub_chunks_have_overlap(self):
        text = "p" * 30000
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=12000)
        if len(subs) < 2:
            self.skipTest("too few sub-chunks to verify overlap")
        # We can't know positions from the strings alone (all 'p'), but we
        # can verify by checking total chars > len(text) (overlap implies
        # double-coverage of some chars).
        total = sum(len(s) for s in subs)
        self.assertGreater(total, len(text),
            "sub-chunks have no overlap (sum of lengths == text length)")

    def test_light_density_returns_single_sub_chunk(self):
        # If observed_tokens is small enough that sub_chars >= len(text),
        # the function returns the original unchanged (guard branch).
        text = "abc" * 1000  # 3000 chars
        # observed_tokens=500 => 6 c/t => sub_chars = 7000 * 6 * 0.9 = 37800,
        # way bigger than text. Function returns [text].
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=500)
        self.assertEqual(subs, [text])

    def test_regex_extracts_token_count_from_llama_error(self):
        # Sanity: the _DENSE_ERR_RE regex must match the actual llama.cpp
        # error message format.
        err = "backend error: input too long: 16875 tokens > n_ctx 8192"
        m = self.mc._DENSE_ERR_RE.search(err)
        self.assertIsNotNone(m, "regex failed to match the error message")
        self.assertEqual(int(m.group(1)), 16875)

        # Variant: bare error without prefix
        err2 = "input too long: 9735 tokens > n_ctx 8192"
        m2 = self.mc._DENSE_ERR_RE.search(err2)
        self.assertIsNotNone(m2)
        self.assertEqual(int(m2.group(1)), 9735)


if __name__ == "__main__":
    unittest.main()
