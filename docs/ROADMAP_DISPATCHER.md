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
  remote agent ──SSE (work)──▶ ┌──────────┐ ──▶ queue tables
  remote agent ──POST (ack)──▶ │ dispatch │     (claim / lease / fence)
  local agent  ────────────────│  daemon  │ ──▶ agent registry
                               └──────────┘
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
neither. Recommended direction, not yet decided.

## Open questions

- Request authentication: bearer vs JWT vs mTLS (see Security).
- Whether the daemon reuses the cognitive loop's supervision or runs standalone.
  Reusing it is preferred — a second always-on service doubles the install,
  restart, upgrade and uninstall surface, which is where this project's
  cross-platform defects have historically come from.
- Routing policy. "This agent is saturated" needs a definition before it can be
  implemented.

## Prerequisites

The test suite is not currently order-independent: several tests write to the
live store under fixed identifiers. That must be fixed before a daemon with
shared state is added, or its tests will be untrustworthy in the same way.
