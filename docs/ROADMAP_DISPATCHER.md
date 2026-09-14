# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Message Dispatch Daemon — design

> **Status: proposed.** Nothing here is built. This records the design and the
> reasoning behind each choice so the decisions are reviewable before code
> exists.

---

## The problem

Every agent currently runs its own listener against the queue. That works for
two agents on one machine and breaks in three directions:

1. **Flooding.** If fifty background tasks finish at once, an agent receives
   fifty messages. Context is the visible cost; losing the thread of the current
   task is the real one.
2. **No cross-agent view.** An agent can pace itself. Only a central component
   can say "this one is saturated, route elsewhere".
3. **Remote agents.** Agents may run on their own machines. Giving every remote
   agent a raw SQL connection to a central Postgres — or sharing SQLite over a
   network — is not an option. A network boundary is required.

## Shape

```
  remote agent ──SSE (work)──▶ ┌──────────┐ ──▶ queue      (claim / lease / fence,
  remote agent ──POST (ack)──▶ │ dispatch │                  pruned on ack)
  local agent  ────────────────│  daemon  │ ──▶ archive    (payloads, bounded TTL)
                               └──────────┘ ──▶ registry   (agents, liveness)
```

The daemon holds the **only** database connection. Agents speak HTTP and never
SQL — which also removes their dependence on the backend, so SQLite, PostgreSQL
and a future MariaDB are invisible to them.

---

## Transport: SSE recommended, not mandated

Recommended default is **Server-Sent Events** for the server→agent direction,
with a plain `POST` for acks. Reasoning rather than decree:

| | Verdict |
|---|---|
| **SSE** | One-way over ordinary HTTP. Native reconnect/resume via `Last-Event-ID`. Nothing to supervise beyond the HTTP server already present. **Recommended.** |
| **Long-poll** | Works everywhere, but connection churn costs throughput and gains nothing SSE lacks. |
| **WebSocket** | Bidirectional, which we do not need. Adds connection state, ping/pong liveness and reconnect logic — three more things to supervise — and brings its own backpressure problem, which is the failure we are trying to prevent. |
| **MCP** | Already spoken by some agents, but not by all of them, and not by a plain script. Worth revisiting if the agent population narrows. |

The split matters: work flows down a stream the daemon controls, acks come back
as ordinary requests. The agent cannot be pushed faster than it acks, because
the daemon will not send more until it does.

## Pacing: rule in the data, buffer in the daemon

Two layers, deliberately:

- **Durable rule — in the queue.** The store refuses to hand out more than N
  unacked messages to one consumer. This is `claim_message` + `claim_expires_at`
  + `lease_token`, which already exist.
- **Transient buffer — in the daemon.** A bounded per-connection queue that
  blocks the producer when a consumer falls behind; rebuilt on reconnect.

**Why not put the rule in the daemon.** If the daemon owns the only copy, a
daemon crash leaves every agent unpaced *and* unmailed, with nothing able to
enforce the contract. With the rule in the data, a dead daemon degrades to what
we do today: agents poll for themselves, slower but correct. Same single-owner
reasoning as derived message state.

## Leases

No new mechanism. The existing lease primitives are already the documented
industry pattern — stamp a claim with an owner and an expiry, and let a reaper
return rows whose lease lapsed. The daemon is a consumer of that, not a
replacement for it.

---

## Security

**Assume the open internet.** The daemon must be safe to expose on an ordinary
network, because a private overlay network cannot be required: many
organizations block VPN clients outright, so any design that depends on one is
undeployable for those users. An overlay network remains a *recommended
operational pattern* — defence in depth — never the mechanism that makes the
application safe.

This holds even where such a network exists. It authenticates *devices*, not
requests; treating it as the boundary makes every host on it a trusted client.

**Credentials.** Tokens resolve through the existing chain — environment
variable → OS keyring → encrypted vault (AES-256, PBKDF2-HMAC-SHA256, 600K
iterations). Never a literal in a config file, and the resolver already exists
(`m3_sdk.get_secret`); this design does not add a second one.

**Outbound safety.** Every agent-side HTTP call carries an explicit timeout and
a circuit breaker that opens after three consecutive failures. An agent that
cannot reach the daemon must fail fast and say so, not hang — the failure mode
this codebase spent a release removing from its scheduler calls.

**Logging.** The daemon strips authorization material from its logs. A token in
a log file is a credential at rest in the one place nobody audits.

**Transport security.** HTTPS is required, not optional — an agent token
crossing an ordinary network in cleartext is a credential handed to anyone on
the path. The daemon terminates TLS itself rather than assuming a reverse proxy,
so a default install is safe without extra infrastructure.

**Request authentication** then needs to stand on its own. Three candidates,
undecided:

| | Trade-off |
|---|---|
| **API key / bearer** | Simplest, and the credential chain above already resolves it. Bearer alone means possession is authority — a leaked token is full access until revoked. |
| **JWT** | Carries expiry and scope, so a leak is time-bounded and a per-agent capability set becomes expressible. Needs a signing key and a rotation story. |
| **mTLS** | Strongest: the client proves possession of a private key that never crosses the wire, and it authenticates the *agent* rather than a string it holds. Costs certificate issuance and renewal on every agent machine — real operational weight for a small deployment. |

Recommendation is to ship bearer-plus-HTTPS first with the existing credential
resolver, design the token format so JWT is a drop-in later, and treat mTLS as
the enterprise option rather than the default. This section is the least
researched in this document and should be reviewed by someone with a security
background before implementation.

---

## Design constraints

Six boundary conditions. A proposal that fails any one of them is out, however
good it looks on the others.

1. **Light-weight.** No component that a single-laptop install must run just to
   send one message.
2. **Secure without security bloat.** Message encryption and ephemeral pairing
   are in scope; a persistent key-management burden is not.
3. **Not OS- or backend-dependent.** Three operating systems, and SQLite /
   PostgreSQL / a future MariaDB behind the existing dialect seam.
4. **Works in public, private, regulated and air-gapped environments.** No
   dependency on a hosted broker. This is what rules out SQS/Kafka-as-a-service
   and it is also why an overlay network cannot be mandatory.
5. **Performant.**
6. **Deliberation history must remain retrievable.** Agents reason with each
   other through this queue; discarding the reasoning discards the record of how
   a decision was reached.

Constraints 5 and 6 pull against each other — a fast queue wants to delete spent
rows, a history wants to keep them. The pointer pattern below is how they are
reconciled.

---

## Store separation — evaluate before building

The queue currently lives in `agent_memory.db` alongside long-term memory. Those
two have opposite access profiles: the queue is high-churn and disposable, the
memory store is low-churn and permanent. Sharing one file means queue polling
and deep memory search contend for the same write lock — acute on SQLite, which
has one writer per database — and it makes aggressive pruning or `VACUUM` of
spent messages a risk to durable data.

**m3 already proves the pattern.** The chatlog is a separate store
(`agent_chatlog.db`) for exactly this reason. Measured on this machine:
**133,220 chatlog rows against 5,203 in main.** Had those shared a file, every
memory search would queue behind chatlog writes.

A dedicated queue store would also let the two diverge where they should: the
queue wants short retention, cheap indexes and frequent compaction; memory wants
embeddings, FTS and permanence. Nothing the queue needs requires memory's
machinery.

The cost is a third root to configure, migrate and back up, and the split-brain
hazard already documented for the engine/config roots applies — pin both or
neither.

**The decisive measurement is the flow, not the size.** A snapshot of the
queue's share of the database is the wrong quantity: it measures a stock while
the question is about a rate. Rows created per day in this store:

| date | notifications | memory_items | ratio |
|---|---|---|---|
| 2026-09-14 | 210 | 23 | 9.1 : 1 |
| 2026-09-12 | 314 | 44 | 7.1 : 1 |
| April 2026 | 4–17 | — | roughly parity |

Single-agent operation held the two at parity. Multi-agent operation moved queue
churn to roughly nine times memory creation, and the ratio widens with the
number of agents.

Against that inflow there is **no retention at all**: `memory_items` has TTL
expiry, decay, auto-archival and pinning; `notifications` has none, and no
`DELETE FROM notifications` exists anywhere in `bin/`. Of 634 rows, 481 (76%)
are terminal state, and the oldest acked row still resident was created five
months earlier. One store therefore holds one governed table and one that grows
monotonically, and every whole-file backup copies the accumulated remainder.

Note that replication is *not* part of this argument: `notifications` is absent
from the sync manifest, so queue state never reaches the warehouse. The case for
separation rests on growth, retention and lock contention alone.

**Backend-neutral, not SQLite-only.** A dispatch store must stay behind the
dialect seam. PostgreSQL claims with `FOR UPDATE SKIP LOCKED`, which SQLite
cannot express — concurrent claimers divide the queue instead of serialising —
so a SQLite-exclusive store would hand PostgreSQL deployments the weaker queue
on the one workload where the difference is largest.

---

## Pointer pattern — a fast queue and a durable record

Constraint 6 appears to forbid the aggressive pruning constraint 5 wants. It
does not, because the payload and the delivery are separable.

The queue row carries a **pointer**, not prose: the delivery state (addressing,
lease, attempt count) plus the id of an archived body. On ack the queue row is
prunable immediately — it holds nothing that is not reconstructable — while the
reasoning stays addressable.

This mirrors the decision already taken for handoffs: the notification carries
`memory_id` rather than a copy of the payload, so the text keeps one owner. The
same rule, applied to a different pair of tables.

**The archive belongs in the dispatch store, in its own table** — not in the
memory store and not in the chatlog. Both alternatives were considered and both
are wrong for the same underlying reason: each is somebody else's space.

- **Not the memory store.** That is what the separation above exists to prevent;
  routing the payloads back into it reintroduces the growth the split removes.
- **Not the chatlog.** The chatlog is a *user-space* construct, organised by
  `user_id` and `conversation_id` and surfaced through user-facing recall.
  Machine-to-machine coordination traffic, at roughly nine rows per memory and
  rising with agent count, would bury a user's own history in system noise.
  Search results are the shared resource being consumed, and that cost is paid
  by the person least able to see why.

  It is also not durable in the way a pointer target must be. `chatlog_decay`
  lowers importance and sets `valid_to` on content it judges ephemeral —
  explicitly transient ids, status snapshots and tool-result-only JSON, which is
  precisely the shape of machine deliberation — and the curation path can
  hard-delete rows. A queue pruned against a target that later decays leaves a
  pointer to nothing, and the failure is silent.

So: two tables, one store. `notifications` holds lightweight delivery state and
is pruned aggressively on ack; **`deliberation_log`** holds the payloads under
its own retention policy, sized for retrospective audit rather than forever.
Both live in the dispatch database, which keeps the pointer and its target in a
single backup, a single migration, and a single transaction boundary — the
cross-store dangling-reference problem disappears instead of being managed.

The name matters more than it looks. `chat_log` is already load-bearing in two
different ways — a table family (`chat_log_items`, …) and a row-type value that
`chatlog_decay` scopes every query to — so a dispatch table named `*_chatlog`
would sit one careless `LIKE` away from a curation pass that hard-deletes rows.
`deliberation_log` names the content and cannot be mistaken for user chat by a
human or a pattern match.

Two things this still requires:

- **Ordered retention.** The pointer may only be pruned once the archive row is
  written. A sweep that can outrun its target reintroduces the problem it was
  built to solve, so the archive write and the ack must not be independently
  reorderable.
- **A stated archive policy.** "TTL 30 days" is a starting point, not a
  decision: it should be written down, with what happens to a deliberation that
  outlives it. Anything that must survive indefinitely is a *memory*, and
  belongs in the memory store by the existing promotion path — which keeps the
  durable tier deliberate and small rather than a side effect of queue traffic.

The resulting tiers are: **queue** (ephemeral, pruned on ack) → **archive**
(bounded retention, same store, audit) → **memory** (durable, embedded,
retained, entered deliberately). The chatlog is not in this chain; it remains
the user's.

---

## Physical mapping across the backend seam

"A separate store" means different things per backend, and m3 has already
answered this once — for the chatlog — so the dispatch store copies that answer
rather than inventing a second one.

| backend | dispatch store is |
|---|---|
| SQLite | a separate file, `agent_dispatch.db` |
| PostgreSQL | prefixed tables in the one database |
| a future MariaDB | prefixed tables in the one database |

Two options were considered and rejected for PostgreSQL:

- **A separate `CREATE DATABASE`.** Two PostgreSQL databases cannot be read in
  one connection or written in one transaction, which forfeits the atomic
  "archive the payload, then ack" that putting both tables in one store exists
  to provide.
- **A separate `CREATE SCHEMA`.** This is the option that looks right and is
  not: it buys isolation that table naming already gives, at the cost of
  connection routing and `search_path` handling.

The chatlog fork states the reasoning for the surviving option directly —
distinct tables in the same schema give "isolation of indexes/lifecycle/policy
without the connection-routing cost of separate schemas."

**The resolver must gate on capability, not on negation.** The existing
`chatlog_table_for` keys off `backend == "sqlite"` rather than
`!= "postgres"`, precisely so a third SQL backend lands on the shared-database
name without anyone editing the map. The dispatch equivalent inherits that rule,
and one map stays the single source of truth for logical role → physical name.

⚠ **On PostgreSQL the separation is logical, not physical.** Dispatch tables
share the database, the WAL and the connection with core. The lock-contention
argument for splitting is therefore SQLite-specific — PostgreSQL's MVCC already
prevents queue writes from blocking memory reads. The case for the split does
not depend on it: unbounded growth, absent retention and whole-file backup bloat
are backend-independent, and those are the measurements that decided it.

## Retention

The queue has none today, and that is the defect the split does not by itself
fix: moving an unbounded table to a new file yields an unbounded table in a new
file. Retention comes first.

Three tiers, three policies:

- **`notifications`** — prunable as soon as a row reaches a terminal state and
  its payload is archived. This is the aggressive one; the table should stay
  small enough that its indexes fit in cache.
- **`deliberation_log`** — a bounded window, on the order of weeks. Long enough
  to answer "why did the agents decide that", short enough that it never becomes
  a second warehouse.
- **memory** — unbounded and deliberate. Anything that must outlive the archive
  window is a memory and enters through the existing promotion path, not by
  accumulating where it happened to land.

**Ordering is a correctness property, not an optimisation.** The pointer may
only be pruned once its payload is archived; if those two can be reordered or
can fail independently, the sweep silently destroys the history the archive
exists to keep. Same store makes this cheap — one transaction — but it does not
make it automatic.

**A dead-lettered row is not a spent row.** It is the record that a message
failed `max_attempts` times, which is a diagnosis, and it must survive the
prune that clears completed traffic. Retention for terminal states should
distinguish "done" from "gave up".

---

## Webhook delivery

SSE requires the agent to hold an open connection, which means it must be
running and able to sustain one. That excludes the cases a dispatcher is most
useful for: an agent woken by a schedule, one behind a proxy that kills
long-lived streams, one that simply was not running when the work arrived.

Webhooks are the complement, not the replacement. The agent registry carries an
optional callback URL; an agent with one can be delivered to cold, an agent
without one keeps exactly today's behaviour. Delivery mode becomes a property of
the agent rather than an assumption baked into the transport.

**Pairing, not stored credentials.** Authentication is established once, in a
handshake, and the link is then authorised by a secret derived from it — the
model a Bluetooth pairing follows. A new session re-pairs. This avoids standing
credentials in the registry, which is the part that would otherwise require key
management the "no security bloat" constraint rules out.

Each delivery is then **HMAC-signed over the body with a timestamp**, so the
receiver can verify authenticity and bound replay without the secret ever being
re-transmitted. `m3_http_auth` already mints tokens at a documented entropy
floor and already imports `hmac`; the pairing flow should call it rather than
grow a second token generator.

**Ephemerality has to be enforced to mean anything.** The lease is the model: it
is trustworthy because `claim_expires_at` is a real deadline something acts on.
A pairing secret needs the same — an explicit expiry and something that
invalidates it — or it is a standing credential with optimistic naming.

⚠ **Outbound delivery makes the daemon an HTTP client aimed at a URL the
registry supplies**, which is a different security posture from serving
requests. A callback URL is attacker-influenced input in the same way an agent
id is, and an unvalidated one turns the daemon into a confused deputy against
loopback, link-local or cloud-metadata addresses. **The egress policy — allowed
schemes, denied address ranges, redirect handling, response limits — belongs in
the design before the sender is written**, not after.

Retries reuse the lease. A failed POST fails the message, and `attempt_count`,
`claim_expires_at` and dead-lettering already express backoff and give-up. A
second retry mechanism beside them would be two owners for one rule.

---

## Open questions

- Request authentication: bearer vs JWT vs mTLS (see Security).
- Whether the daemon reuses the cognitive loop's supervision or runs standalone.
  Reusing it is preferred — a second always-on service doubles the install,
  restart, upgrade and uninstall surface, which is where this project's
  cross-platform defects have historically come from.
- Routing policy. "This agent is saturated" needs a definition before it can be
  implemented.
- The archive window. Weeks is the shape; the number is not chosen, and neither
  is what happens to a deliberation that reaches the end of it.
- What earns promotion to memory. Promoting every deliberation refills the
  memory store with queue traffic, which is what the split removes; promoting
  none loses the record the history constraint exists to keep. The predicate
  should be written before the sweeper, not after it has deleted something.
- Egress policy for callback URLs — allowed schemes, denied ranges, redirect
  and response handling. Required before any outbound sender is built.

## Prerequisites

None outstanding.

Test isolation was previously listed here as a blocker, on the understanding
that several tests wrote to the live store under fixed identifiers. They do
not: `tests/conftest.py` applies an autouse sandbox that pins `M3_ENGINE_ROOT`,
`M3_CONFIG_ROOT` and `M3_MEMORY_ROOT` to a per-test `tmp_path`, so each test
resolves to its own database. `tmp_path` is unique per test and per worker, so
this holds under `pytest-xdist -n auto` as well.

`pytest-xdist` is still not a declared dev dependency, which is the only reason
parallel runs are not used.
