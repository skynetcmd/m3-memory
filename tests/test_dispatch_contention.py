"""The correctness half of the pre-registered budget: nothing lost, nothing twice.

The latency number is only meaningful alongside this. A dispatcher that hits
p50 3 s by dropping messages passes the latency budget and fails the point, so
both were registered together before the code was written.

Kept as a test rather than a one-off benchmark because concurrency regressions
do not announce themselves: a lost message looks exactly like an idle queue.
"""
from __future__ import annotations

import collections
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_core import paths as P  # noqa: E402
from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

_MESSAGES = 60      # enough to interleave; small enough for a fast suite
_WORKERS = 8        # the concurrency the claim contract was measured at


class TestNothingIsLostOrDoubleDelivered(unittest.TestCase):
    def setUp(self):
        self.store = str(pathlib.Path(tempfile.mkdtemp()) / "agent_dispatch.db")
        boot = (pathlib.Path(P.dispatch_migrations_dir())
                / "001_bootstrap.up.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(self.store)
        conn.executescript(boot)
        conn.executemany(
            "INSERT INTO notification_dispatch (agent_id, kind) VALUES (?,?)",
            [("worker@1", "handoff")] * _MESSAGES)
        conn.commit()
        conn.close()

    def test_concurrent_claimants_each_get_a_distinct_message(self):
        claimed: dict = collections.defaultdict(list)
        lock = threading.Lock()
        errors: list = []

        def work(wid: int) -> None:
            d = SqliteDialect()
            conn = sqlite3.connect(self.store, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Hard bound on the loop. A claim that stops excluding
            # already-claimed rows re-claims the same row forever, so an
            # unbounded `while True` turns that defect into a HANG rather than
            # a failure -- and a hanging test in CI is barely better than a
            # blind one. Measured while building this: removing the
            # `claimed_by IS NULL` guard made the suite hang for 120s at
            # thread join instead of reporting anything.
            budget = _MESSAGES * 4
            try:
                for _ in range(budget):
                    got = d.claim_message(
                        conn, table="notification_dispatch",
                        where_sql="agent_id = ?", where_params=("worker@1",),
                        claimant=f"w{wid}", lease_ttl=300)
                    if got is None:
                        return
                    row_id, token = got
                    with lock:
                        claimed[row_id].append(wid)
                    # CLAIM THEN RELEASE: no work is held inside the claim's
                    # transaction, which is the contract claim_message states.
                    d.complete_message(conn, table="notification_dispatch",
                                       row_id=row_id, lease_token=token)
                errors.append(
                    f"w{wid}: claimed {budget} times without the queue "
                    f"draining -- claim_message is handing out the same row "
                    f"repeatedly, which means it stopped excluding "
                    f"already-claimed rows")
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert
                errors.append(f"w{wid}: {type(exc).__name__}: {exc}")
            finally:
                conn.close()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        stuck = [t.name for t in threads if t.is_alive()]
        self.assertEqual(
            stuck, [],
            "worker threads did not finish.\n"
            f"observed: still running={stuck}\n"
            "possible: claim_message never returns None, so the queue never "
            "drains -- check that it still excludes already-claimed rows\n"
            "inspect: SqliteDialect.claim_message")

        self.assertEqual(errors, [], f"observed: worker errors={errors}")

        doubles = {k: v for k, v in claimed.items() if len(v) > 1}
        self.assertEqual(
            doubles, {},
            "a message was handed to more than one worker.\n"
            f"observed: {len(doubles)} double-delivered, sample={list(doubles.items())[:3]}\n"
            "cause: the claim is no longer atomic, or the lease fence stopped "
            "excluding an already-claimed row\n"
            "possible impact: two agents do the same work and one reports "
            "another's result as its own\n"
            "inspect: Dialect.claim_message and lease_fence_predicate",
        )

        conn = sqlite3.connect(self.store)
        unclaimed = conn.execute(
            "SELECT COUNT(*) FROM notification_dispatch WHERE read_at IS NULL"
        ).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM notification_dispatch WHERE read_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(
            unclaimed, 0,
            "messages were left undelivered.\n"
            f"observed: {unclaimed} of {_MESSAGES} still unread, {completed} completed\n"
            "cause: a claimant exited while rows remained claimable\n"
            "possible impact: mail sits in the queue that nothing will pick up, "
            "and an idle queue looks identical to an empty one\n"
            "inspect: Dialect.claim_message",
        )
        self.assertEqual(completed, _MESSAGES)
        self.assertEqual(len(claimed), _MESSAGES)


if __name__ == "__main__":
    unittest.main()
