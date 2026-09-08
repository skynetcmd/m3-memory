"""The sweep's abort breaker must fire on INFRASTRUCTURE, never on CONTENT.

Three bugs lived in one control loop in embed_sweep_lib.py, all from conflating
"did the call raise" with "did work happen":

1. A CONTENT failure (a row the embedder refused as too long) incremented the
   same counter as a connection refusal, so five dense CJK batches aborted the
   whole sweep with "Check embedder availability". The loop re-fetched the same
   rows next pass, failed again, and never drained -- which is why issue #139
   looked like a deployment problem and sent its reporter to inspect a healthy
   server.

2. `consecutive_fails = 0` ran unconditionally on the success path, so a batch
   where EVERY row came back vec=None -- an embedder returning None rather than
   raising -- CLEARED the failure counter. The breaker could never trip against
   that embedder, and the sweep would spin forever. Opposite failure mode, same
   root confusion, and no test covered it.

3. The oversize guard measured BYTES against a TOKEN ceiling.

Hermetic: the loop takes `embed_many` as a callback, so these drive the real
control flow with stub embedders. No GPU, no model, no network.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import embed_sweep_lib as L  # noqa: E402


def _rows(n, text="hello world"):
    return [(f"id{i}", text, "", None) for i in range(n)]


def _run(embed_many, rows, **kw):
    """Drive one full sweep with a single fetch, returning (counters, logs)."""
    counters, logs, served = L.Counters(), [], []

    def fetch(_after, _limit):
        if served:
            return []
        served.append(True)
        return rows

    asyncio.run(L.run_embed_loop(
        fetch_candidates=fetch,
        write_embedding=lambda *_a: True,
        counters=counters,
        embed_many=embed_many,
        content_hash_fn=lambda t: f"h{len(t)}",
        batch_size=10, concurrency=1, expected_dim=None,
        log=logs.append, **kw,
    ))
    return counters, logs


async def _raise_content(_texts):
    raise RuntimeError("input too long: 9999 tokens > n_ctx 8192")


async def _raise_infra(_texts):
    raise ConnectionError("connection refused")


async def _all_none(texts):
    return [(None, "model") for _ in texts]


async def _ok(texts):
    return [([0.1], "model") for _ in texts]


class TestFailureClassification(unittest.TestCase):
    def test_overflow_is_content_not_infrastructure(self):
        self.assertFalse(L._is_infrastructure_failure(
            RuntimeError("input too long: 16875 tokens > n_ctx 8192")))

    def test_typed_context_error_is_content(self):
        from memory.embed import ContextLengthExceeded
        self.assertFalse(L._is_infrastructure_failure(
            ContextLengthExceeded("too long", 16875, 8192)))

    def test_connection_error_is_infrastructure(self):
        self.assertTrue(L._is_infrastructure_failure(ConnectionError("refused")))

    def test_unknown_failure_defaults_to_infrastructure(self):
        """Conservative by design: a mis-classified content failure costs one
        retry; a mis-classified infra failure costs an infinite spin."""
        self.assertTrue(L._is_infrastructure_failure(RuntimeError("who knows")))


class TestBreakerFiresOnTheRightThing(unittest.TestCase):
    def test_content_failures_never_abort_the_sweep(self):
        """THE #139 BUG. Five oversized batches used to abort everything."""
        c, logs = _run(_raise_content, _rows(50))
        self.assertEqual(c.consecutive_fails, 0)
        self.assertGreater(c.content_failures, 0)
        self.assertNotIn("ABORT", " ".join(logs))

    def test_infrastructure_failures_still_abort(self):
        """The breaker must keep working -- this is the case it exists for."""
        c, logs = _run(_raise_infra, _rows(50))
        self.assertGreaterEqual(c.consecutive_fails, 5)
        self.assertIn("ABORT", " ".join(logs))

    def test_abort_message_does_not_assert_an_unverified_cause(self):
        """'Check embedder availability' stated a GUESS as a diagnosis. The
        message now separates what was observed from what is merely possible."""
        _c, logs = _run(_raise_infra, _rows(50))
        abort = next(line for line in logs if "ABORT" in line)
        self.assertIn("possible:", abort)
        self.assertIn("observed:", abort)
        self.assertNotIn("Check embedder availability", abort)

    def test_all_none_batches_trip_the_breaker(self):
        """REGRESSION, previously uncovered: an embedder returning None instead
        of raising cleared the counter on every batch, so the sweep spun
        forever against a dead backend."""
        c, logs = _run(_all_none, _rows(80))
        self.assertGreaterEqual(c.consecutive_fails, 5)
        self.assertIn("ABORT", " ".join(logs))
        self.assertTrue(any("BATCH_EMPTY" in line for line in logs))

    def test_progress_resets_the_breaker(self):
        c, _logs = _run(_ok, _rows(30))
        self.assertEqual(c.consecutive_fails, 0)
        self.assertGreater(c.embedded, 0)


class TestOversizeGuardMeasuresTokens(unittest.TestCase):
    def test_cjk_row_under_the_byte_cap_is_still_routed_alone(self):
        """A CJK row can sit UNDER max_row_bytes (32768 == 8192*4, the English
        ratio) while being ~9,900 tokens. It used to pass the guard, skip the
        subdivide path, and blow n_ctx inside a shared batch."""
        import random
        random.seed(1)
        zh = ["数据库", "连接", "失败", "重试", "记录", "详细", "错误", "信息"]
        big = "".join(random.choice(zh) + random.choice("，。；、") for _ in range(2800))
        self.assertLessEqual(len(big.encode("utf-8")), 32_768,
                             "fixture must pass the BYTE guard to be meaningful")
        self.assertGreater(L._token_budget(big), L._TOKEN_BUDGET,
                           "fixture must exceed the TOKEN budget")

        sent = []

        async def spy(texts):
            sent.append(len(texts))
            return [([0.1], "model") for _ in texts]

        rows = [("big", big, "", None)] + _rows(3, "short")
        c, _logs = _run(spy, rows, oversize_mode="subdivide")
        self.assertIn(1, sent, "oversized row must be sent in its own batch")
        self.assertEqual(c.oversize_subdivided, 1)


if __name__ == "__main__":
    unittest.main()
