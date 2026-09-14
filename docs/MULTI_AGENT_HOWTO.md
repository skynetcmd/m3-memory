# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> How to run a project with two agents

> **This is the operational guide.** [MULTI_AGENT.md](./MULTI_AGENT.md) documents
> the primitives — registry, handoffs, tasks, notifications, delivery. This page
> is about making two agents actually *finish something together*: roles,
> message discipline, disagreement, and the failure modes that show up in
> practice.
>
> It is written from two completed runs — a design review driven to concurrence,
> and a multi-phase implementation taken from plan to published release.

---

## The shape that works

One **orchestrator** and one **executor**, on a shared m3 store.

| | Orchestrator | Executor |
|---|---|---|
| Owns | the plan, sequencing, phase gates, "done" | implementation, measurement, evidence |
| Decides | what happens next, and when a phase closes | *how*, and whether a claim is true |
| Never | writes the code | starts, skips, or closes a phase alone |

**Why one of each, not peers.** Two peers negotiate; one of them has to yield
and neither is meant to. Naming an orchestrator makes deadlock resolvable
without a human, and makes "who decides" a non-question at 3am.

**The executor is not a subordinate.** Its job includes contradicting the
orchestrator with measurement. Both runs produced their best outcomes from the
executor refusing a plan step and the orchestrator conceding — and from the
reverse. If your executor never pushes back, you have one agent with extra
latency.

---

## Setup

```bash
# Each agent registers ONCE, with a qualified id when several
# sessions of the same type may run. agent_register takes a structured
# argument, so it is passed as one JSON object rather than flags.
m3 agent agent_register --json '{"agent_id":"planner@<session>","role":"orchestrator","capabilities":[],"metadata":{}}'
m3 agent agent_register --json '{"agent_id":"coder@<session>","role":"executor","capabilities":[],"metadata":{}}'

m3 agent agent_list        # confirm both, and check last_seen
```

> On m3 **< 2026.9.14.1**, `capabilities` and `metadata` are required even
> though the schema marks them optional. Passing `[]` and `{}` as above works
> on every version.

Tell your peer the exact id you registered. An agent cannot address what it
cannot spell.

---

## The five rules that make it work

### 1. Every message gets acked

```bash
m3 admin notifications_poll --agent_id "coder@<session>" --unread_only
m3 admin notifications_ack --notification_id 42
```

A sender watching `read_at` cannot distinguish *read but not acked* from *dead*.
Skipping acks is the single most common way a working loop looks broken: the
orchestrator starts re-sending, or escalates to a human, while the executor is
happily working.

### 2. Poll on a signal, not a schedule

A timer that only fires when the agent is idle will not fire during the long
operations where messages actually arrive. Watch continuously and let arrival
interrupt you — see *Delivery* in [MULTI_AGENT.md](./MULTI_AGENT.md#-delivery-pull-by-design-push-by-adapter).

A poll loop that checks between tasks is a poll loop that checks when nothing
is happening.

### 3. Put the decision in the payload

Every message should be answerable without asking a follow-up. In practice that
means: the ask, the evidence, and what you will do if you hear nothing.

```json
{
  "summary":  "P2 built; one planned tool measured LARGER than its original.",
  "evidence": "724B vs 670B — its only params are the injected ones.",
  "asking":   "Drop it and ship four instead of five?",
  "default":  "Holding. Not committing a failing test either way."
}
```

A message that says only *"P2 done, advise"* costs a full round trip.

### 4. State your default when you ask

The orchestrator may be mid-task for minutes. `"I will do X unless you say
otherwise"` keeps the loop moving; `"what should I do?"` stops it.

### 5. Fix the vocabulary before you start

Agree the `kind` strings up front and write them down. In a real run the same
intent arrived under **two different spellings** — `orchestrator_directive` (32
messages) and `orchestration_directive` (5) — because nobody fixed the
vocabulary and each agent guessed. Nothing broke, but any filter on `kind`
would have silently missed a fifth of the traffic.

Suggested minimum: `directive`, `question`, `answer`, `status`, `blocked`,
`done`.

---

## Disagreement: the part that produces the value

**Three rounds, then the orchestrator decides.** Each round must add evidence —
a measurement, a counter-example, a test result. Restating a position is not a
round. After three, the orchestrator rules, the executor complies, and the
disagreement is recorded for the human rather than escalated live.

**Rules that make disagreement productive:**

- **A reviewer's fix is a hypothesis too.** Apply it and run the tests before
  accepting it. In one run an executor applied a proposed one-line security fix
  and found it *introduced* a vulnerability — caught only because it was tested
  rather than trusted.
- **Bring the measurement, not the opinion.** "That will be slow" starts an
  argument. "That is 2,768 of 3,213 tokens, here is the script" ends one.
- **Concede explicitly and in writing.** "I was wrong about X, you were right"
  costs one line and stops the point being relitigated three messages later.

**What this actually catches.** Across two runs, mutual review caught: a
security fix that opened a hole, a plan step that would have deleted the
messaging surface the agents were using, a tool that cost more after being
"optimized", and a scanner that would have published a false all-clear. None
was found by the agent that wrote the thing.

---

## Failure modes seen in practice

| Symptom | Actual cause | Fix |
|---|---|---|
| Peer looks dead | messages read but not acked | ack every message |
| Inbox empty, mail piling up | polling only your qualified id | poll the bare type too |
| Two sessions fight over work | both polling the bare type | poll your qualified id; agree who takes the bare queue |
| Messages missed during long work | idle-only timer | continuous watch |
| `kind` filter misses traffic | vocabulary never agreed | fix the strings up front |
| Both agents "done", nothing shipped | no one owned the phase gate | orchestrator closes phases, explicitly |

**Addressing is asymmetric** — the most common structural surprise. A bare type
(`coder`) is a **fan-out** read that sees mail for the type's instances; a
qualified id (`coder@abc`) is a **direct** read that sees only its own. Running
several sessions of one type? Register qualified, tell peers that id, and poll
both queues.

---

## Working agreements to set before starting

Decide these once, in writing, so they are not litigated at 3am:

1. **Who decides** — name the orchestrator.
2. **Deadlock rule** — rounds before the orchestrator breaks it.
3. **Irreversible actions** — list what neither agent does without a human:
   pushing to a public remote, publishing a package, deleting data, anything
   that leaves the machine. *Say this before it comes up.* A rule stated in
   advance and honored under pressure is worth more than one invented at the
   moment it becomes inconvenient.
4. **Commit and report boundaries** — what goes in a public artifact versus
   private notes. Public commit messages and changelogs state *what* changed;
   reasoning, rejected options and post-mortems belong in private notes. Never
   publish live agent/session identifiers or unfixed defects described in
   enough detail to act on.
5. **What "done" means per phase** — the orchestrator's call, stated before
   work starts, not after.

---

## The gate an orchestrator should hold

Before closing a phase, require:

- **The evidence, not the claim.** "Tests pass" is a claim. "4,751 passed, 2
  failed, both pre-existing — here is the same run on clean `main` showing the
  identical pair" is evidence.
- **Verification against the artifact, not the source.** A change can be
  present in a file and absent from the built product. Check what ships.
- **A counter-test for every new guard.** Re-introduce the defect and confirm
  the test fails. A guard that cannot fail is not coverage — and one run
  produced eight green tests that all stayed green while the defect was put
  back, because they tested the function and not its call site.

---

## Worth knowing

- **Capture survives a dropped connection.** Chatlog writes go to the database
  directly. Losing the MCP connection means you cannot search or write memory;
  it does not mean turns are being lost. Do not report it as data loss.
- **The CLI is a full fallback.** Every orchestration tool has a `m3 <domain>
  <tool>` form. Coordination continues through a connection drop.
- **Write the decision, not the chatter.** Durable outcomes — rulings,
  concessions, measurements that changed a design — belong in `memory_write`.
  The message log is transport; memory is the record.

---

## See also

- [MULTI_AGENT.md](./MULTI_AGENT.md) — primitives, workflow patterns, delivery
- [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) — per-agent protocol and
  addressing asymmetry
