# Agent dispatch — receipt latency

Pre-registered before the code was written, per the requirement that a feature
ships with a measured outcome rather than a plausible one:

| Budget | Threshold |
|---|---|
| Cross-agent message receipt + ack, **waiter running** | **P50 ≤ 20 s, ceiling 30 s** |
| Correctness under contention | 0 lost, 0 double-delivered |

"Waiter running" is part of the threshold, not a footnote. A test that kills the
waiter measures *availability*, a different number bounded by the self-heal
cadence (Windows PT10M worst case; launchd/systemd seconds).

## End-to-end: send → a real waiter reports the hit

Measured 2026-09-17 on Windows, against a store built by the migration runner,
with a real `m3_notification_waiter.py` subprocess at the poll interval the
installed services use.

```
n=5  poll interval = 5s
  p50 = 3.07 s      max = 3.08 s
  P50 PASS (budget 20 s)    CEILING PASS (budget 30 s)
```

Every trial reported `new_notifications`, so these are genuine detections.
Roughly 6.5× headroom on P50.

⚠ **The first version of this benchmark measured nothing.** It reported 0.01 s —
implausibly fast for a 5 s poll — because the hand-built fixture database lacked
the full main schema, so the waiter crashed at startup and the harness timed the
crash. A measurement far better than the mechanism allows is a measurement to
distrust. Build the store with the migration runner.

## Store operations, per OS

The dispatch store is the half that moved, so its behaviour is measured on every
supported platform rather than inferred from one.

| OS | write p50 | read p50 | read max |
|---|---|---|---|
| Windows | 1.394 ms | 0.045 ms | 0.124 ms |
| Linux | 0.026 ms | 0.005 ms | 7.769 ms |
| macOS (Darwin) | 0.227 ms | 0.008 ms | 0.019 ms |

n=200 per OS. The write spread is `fsync` cost per filesystem, not logic. All
are three to five orders of magnitude inside the budget, which is the point: the
receipt SLA is bounded by the POLL INTERVAL, not by the database.

That is why the interval is the tuning knob and the store is not. At 5 s the
poll is 17% of the 30 s ceiling while costing a fifth of the `stat()` calls of a
1 s interval (720/hr vs 3,600/hr).

## What is not measured here

* **Multi-node PostgreSQL receipt.** The PG path polls the shared table rather
  than watching a WAL file, so its latency is bounded by the same interval, but
  it has not been timed across two hosts.
* **Agent-turn latency.** The budget covers detection and ack. When a peer
  *acts* on the message depends on its turn boundary, which no code here bounds.
