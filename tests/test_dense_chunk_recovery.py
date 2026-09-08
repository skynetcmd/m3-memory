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
        """7127bb1e case: 28000 chars reported as 9735 tokens (2.88 c/t).

        ⚠ CONTRACT CHANGE (2026-09-07). This used to assert `len(subs) < 4`,
        expecting ~2 pieces of ~18,100 chars from the reported ratio. That
        expectation encoded the trust-the-report bug: 18,100 ASCII characters
        estimate ~18,102 tokens, well past the 7,000 budget, so those "correct"
        pieces would each still overflow.

        Sizing from a single reported ratio is a HINT, not a guarantee — density
        is not uniform within a chunk (the measured spread across content types
        is 4x). Every piece is now verified against the real budget and halved
        until it fits, so the honest assertion is "every piece fits", not "the
        arithmetic produced N pieces".
        """
        from memory.tokens import TOKEN_BUDGET, estimate_tokens
        text = "y" * 28000
        subs = self.mc._subdivide_dense_chunk(text, observed_tokens=9735)
        self.assertGreaterEqual(len(subs), 2)
        for s in subs:
            self.assertLessEqual(
                estimate_tokens(s), TOKEN_BUDGET,
                "a sub-chunk still exceeds the token budget")
        # Coverage, not equality: consecutive sub-chunks OVERLAP by design, so
        # the concatenation is longer than the input. What must hold is that no
        # content is lost.
        self.assertGreaterEqual("".join(subs).count("y"), 28000,
                                "subdivision lost content")

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


class MeanPoolTests(unittest.TestCase):
    """_mean_pool averages an oversized row's sub-chunk vectors into one vector
    AND L2-normalizes it (standard long-doc embedding) — used by
    _embedded_bulk_with_subdivide so an over-n_ctx row is handled in-process.
    The normalize step is load-bearing: bge-m3 vectors are unit-length and the
    store / cosine paths assume that, so a raw mean (norm < 1) is incomparable."""

    def setUp(self):
        import math

        import memory.embed as me
        self.me = me
        self.math = math

    def _norm(self, v):
        return self.math.sqrt(sum(x * x for x in v))

    def _assert_unit(self, v):
        self.assertAlmostEqual(self._norm(v), 1.0, places=5,
                               msg=f"pooled vector not unit-length: norm={self._norm(v)}")

    def test_pooled_vector_is_unit_length(self):
        # Mean of [1,1] and [3,3] points along [1,1]; normalized => [0.707, 0.707].
        out = self.me._mean_pool([[1.0, 1.0], [3.0, 3.0]])
        self._assert_unit(out)
        self.assertAlmostEqual(out[0], out[1], places=6)  # symmetric input
        self.assertAlmostEqual(out[0], 1.0 / self.math.sqrt(2), places=5)

    def test_direction_preserved(self):
        # Pooling preserves direction (the mean of the inputs), only rescales.
        out = self.me._mean_pool([[0.0, 6.0], [2.0, 0.0], [4.0, 3.0]])  # mean [2,3]
        self._assert_unit(out)
        # [2,3] normalized
        self.assertAlmostEqual(out[0] / out[1], 2.0 / 3.0, places=5)

    def test_single_vector_passthrough(self):
        # A single (already-normalized) model output is returned as-is.
        self.assertEqual(self.me._mean_pool([[5.0, 6.0]]), [5.0, 6.0])

    def test_empty_returns_none(self):
        self.assertIsNone(self.me._mean_pool([]))

    def test_skips_malformed_subvector(self):
        # A sub-vector of the wrong dim is skipped, not crashed on; result still unit.
        out = self.me._mean_pool([[1.0, 1.0], [9.0], [3.0, 3.0]])
        self._assert_unit(out)
        self.assertAlmostEqual(out[0], out[1], places=6)

    def test_opposing_vectors_degenerate_to_zero(self):
        # Mean of opposing vectors is the zero vector — must not divide by zero.
        self.assertEqual(self.me._mean_pool([[1.0, 0.0], [-1.0, 0.0]]), [0.0, 0.0])


class HttpBulkSubdivideTests(unittest.IsolatedAsyncioTestCase):
    """_http_bulk_with_subdivide — tier-2 (HTTP) analogue of the in-process
    subdivide path. On the shared-embedder default (tier 1 off, everything
    defers to the :8082 server) an oversized row 500s the whole HTTP batch;
    this helper detects the "N tokens > n_ctx" body and subdivides+mean-pools
    over HTTP so the row still gets embedded instead of being dropped."""

    def setUp(self):
        import memory.embed as me
        self.me = me

    async def test_normal_rows_pass_through(self):
        async def post_one(texts):
            return [[1.0, 0.0] for _ in texts]
        out = await self.me._http_bulk_with_subdivide(post_one, ["a", "b"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out, [[1.0, 0.0], [1.0, 0.0]])

    async def test_oversized_row_is_subdivided_and_pooled(self):
        """Recovery works when the server DOES report the token count.

        ⚠ SCOPE. The error string below is FABRICATED, and for years this file's
        only overflow coverage was this shape — which is precisely how issue
        #139 stayed green in CI while broken in production. A standalone
        embed server did not emit this text at all: FastAPI's default handler
        replaced it with "Internal Server Error", so the `_DENSE_ERR_RE` gate
        this test exercises could never match in the deployment that mattered.

        A hand-fed string can only prove the parser handles a string someone
        already believed in. The real contract is covered by
        tests/test_embed_server_413.py, which drives the ACTUAL FastAPI app and
        asserts what it really returns; keep this test for the parser branch,
        not as evidence the seam works end to end.
        """
        big = "x" * 40000
        calls = {"n": 0}

        async def post_one(texts):
            calls["n"] += 1
            if len(texts) == 1 and len(texts[0]) > 30000:
                raise RuntimeError(
                    "CPU embedder HTTP 500: embed failed: backend error: "
                    "input too long: 24064 tokens > n_ctx 8192"
                )
            # sub-chunks (or normal rows) embed fine
            return [[1.0, 0.0] for _ in texts]

        out = await self.me._http_bulk_with_subdivide(post_one, [big])
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(out[0], "oversized row should be recovered via subdivide")
        # pooled + normalized -> unit length
        import math
        norm = math.sqrt(sum(x * x for x in out[0]))
        self.assertAlmostEqual(norm, 1.0, places=5)
        # subdivide actually happened (more than the single failing call)
        self.assertGreaterEqual(calls["n"], 2)

    async def test_non_overflow_error_yields_none(self):
        # An error that is NOT an n_ctx overflow must not trigger subdivision;
        # the row is left unembedded (None) to cascade to the next tier.
        async def post_one(texts):
            raise RuntimeError("connection refused")
        out = await self.me._http_bulk_with_subdivide(post_one, ["a"])
        self.assertEqual(out, [None])

    async def test_overflow_without_a_token_count_still_recovers(self):
        """The case the fabricated-string test could not reach.

        A server may report the CLASS of overflow without any numbers — a
        differently-worded llama.cpp build, or a proxy that rewrote the body.
        Recovery must depend on knowing the input is too long, NOT on the
        server having been chatty about it; otherwise the row is dropped for
        want of a digit.
        """
        big = "x" * 40000

        async def post_one(texts):
            if len(texts) == 1 and len(texts[0]) > 30000:
                raise RuntimeError("CPU embedder HTTP 413: maximum context length")
            return [[1.0, 0.0] for _ in texts]

        out = await self.me._http_bulk_with_subdivide(post_one, [big])
        self.assertIsNotNone(
            out[0], "an overflow with no reported count must still subdivide")

    async def test_typed_context_error_recovers_without_string_matching(self):
        """The structured path: a current server raises the typed error, so
        recovery must fire with no prose parsing at all."""
        from memory.embed import ContextLengthExceeded
        big = "x" * 40000

        async def post_one(texts):
            if len(texts) == 1 and len(texts[0]) > 30000:
                raise ContextLengthExceeded("too long", 24064, 8192)
            return [[1.0, 0.0] for _ in texts]

        out = await self.me._http_bulk_with_subdivide(post_one, [big])
        self.assertIsNotNone(out[0], "typed overflow must trigger subdivision")


class RecoverOversizedSingleTests(unittest.IsolatedAsyncioTestCase):
    """_recover_oversized_single — the shared lone-oversized-row recovery used
    by BOTH tier 2 (CPU HTTP) and tier 3 (primary/LM-Studio HTTP). A bisecting
    batch path isolates the big row but can't shrink it; this subdivides within
    the row (char-based, no token count) and mean-pools, so a single oversized
    row is never silently dropped on any remote tier."""

    def setUp(self):
        import memory.embed as me
        self.me = me

    async def test_non_oversized_row_returns_none(self):
        # A row at/under one window is not this helper's job — caller drops it.
        async def post_batch(texts):
            return [[1.0, 0.0] for _ in texts]
        out = await self.me._recover_oversized_single(post_batch, "short text")
        self.assertIsNone(out)

    async def test_oversized_row_subdivided_and_pooled(self):
        big = "x" * (self.me.MAX_CHARS_PER_CHUNK * 3)
        seen = {"batches": 0, "max_texts": 0}

        async def post_batch(texts):
            seen["batches"] += 1
            seen["max_texts"] = max(seen["max_texts"], len(texts))
            return [[1.0, 0.0] for _ in texts]

        out = await self.me._recover_oversized_single(post_batch, big)
        self.assertIsNotNone(out, "oversized row should be recovered")
        import math
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in out)), 1.0, places=5)
        # It subdivided into >1 sub-chunk (single post of multiple texts).
        self.assertGreater(seen["max_texts"], 1)

    async def test_sub_embed_failure_returns_none(self):
        big = "y" * (self.me.MAX_CHARS_PER_CHUNK * 3)

        async def post_batch(texts):
            return "TRANSIENT-SENTINEL"  # not a list -> failure
        out = await self.me._recover_oversized_single(post_batch, big)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
