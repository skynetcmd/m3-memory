# Using m3 for coding work

What m3 does for a coding agent, what it deliberately leaves to the agent, and
how to wire the parts it leaves out.

The short version: **m3 owns the memory store and exposes two seams.** MCP plumbs
it into another process — any agent, any IDE. The CLI plumbs it into your own
code — scripts, hooks, CI. Work that needs your repo's syntax tree or your git
history lives on the *other* side of that boundary, and this page explains why
that is a design choice rather than a gap.

---

## Does m3 index my code symbols? Does it follow refactors?

**No, and that is deliberate.** m3 stores memories; it does not parse your
language. There is no AST walker, no language server, and no symbol table for
your project inside m3.

The reason is that your coding agent already has all of it. Claude Code, Cursor,
Antigravity and the rest hold the file, the parse, and the repo state at the
moment you ask them to change something. A memory layer that built a *second*
parser would be maintaining a stale copy of a thing the agent already knows,
across every language you use — and the two would disagree. The interesting
question is not "can m3 parse Python" but "which component should own the
answer", and the component holding the buffer is the better owner.

What m3 contributes instead is the part the agent loses: durability across
sessions and across tools. When a refactor lands, the agent writes what it
decided and why. That record survives the session, the IDE switch, and the model
upgrade.

**Wiring it up:** have the agent write a memory when it makes a structural
decision, not when it touches a file. `m3 setup` registers the tools; the agent
calls `memory_write` on its own.

**What m3 genuinely does not do, stated plainly:** there is no git-history hook.
Nothing in m3 watches commits or reacts to a diff. If you want a memory written
on every commit, that is a `post-commit` hook calling the CLI — a few lines, and
[the CLI section below](#the-cli-is-a-first-class-interface) shows the shape.

---

## Does anything stop the memory filling with stale noise?

Yes — a background consolidation cycle, not a size cap.

`bin/m3_cognitive_loop.py` runs deferred work off the hot path: classification,
embedding, entity extraction, consolidation of near-duplicates, and promotion of
high-signal chat turns. Writes stay fast because none of that happens inline.

On top of that, memories have **dynamics** rather than a fixed importance:

| Force | What it does |
|---|---|
| **Decay** | Importance fades for memories nobody uses, toward a floor rather than to zero. |
| **Reinforcement** | Memories that are actually retrieved fade more slowly. |
| **Grading** | `memory_grade` lets an agent report, after answering, which memories it *relied on*. |
| **Floors** | Pinned and explicitly-graded memories stop decaying much sooner than ordinary ones. |

Two properties are worth calling out because they are easy to get wrong:

**Retrieval is treated as weak evidence.** Being returned in a result set means
the ranker matched a memory, not that the memory helped. If retrieval alone
strengthened a memory, a spurious one that kept matching queries would entrench
itself through its own noise. So the reinforcement from retrieval is
logarithmic, hard-capped, and bounded strictly below what a single explicit
`helpful` verdict earns. Being seen often can never become being useful.

**An autonomous pass never deletes.** Forgetting is deranking. A background sweep
can surface low-value memories as candidates, but removal is a decision a person
or an explicitly-invoked tool makes. `memory_restore` exists to bring back
anything an automated pass removed in the past, and it refuses to touch
deliberate deletions — overriding a human's delete with a machine's restore is
the same category error in reverse.

---

## Can several agents share one memory, and what happens under concurrency?

That is the shape m3 is built for rather than a mode it tolerates.

The store is a real database — SQLite in WAL mode with a 30-second busy timeout
(`bin/sqlite_pragmas.py`), or PostgreSQL, with the same code path on both. An
IDE extension, a terminal agent, and a CI job all read and write the same state
without a peer-to-peer mesh or a sync daemon.

There are dedicated coordination primitives, not just shared rows: **6 `agent_`
tools** (registry, heartbeat, trust), **8 `task_` tools** (create, assign,
tree), and **4 `notifications_` tools** for inter-agent messaging.

PostgreSQL is a first-class primary backend, not a sync tier — every backend-
varying construct goes through a dialect seam, so business logic never writes
engine-specific SQL.

---

## Do I need to build a dashboard to see what is in there?

No. `m3 dashboard` starts a local web UI that maps the knowledge graph, shows
conflicts and temporal history, and needs no external service.

For text, `m3 doctor` reports subsystem health and `m3 chatlog status` reports
capture state.

---

## <a id="the-cli-is-a-first-class-interface"></a>Is the CLI as capable as MCP?

Yes. The CLI and the MCP server are **two front doors to the same database** —
the whole tool catalog, grouped as `memory`, `files`, `chatlog`, `tasks`,
`agent`, `admin`, `conversations`, `diagnostics` and `entity`.

```bash
m3 memory memory_search --query "which signing algorithm did we pick?" --k 5
m3 memory memory_write --content "..." --type belief --title "..."
```

Results go to **stdout** and logs to **stderr**, so output pipes cleanly:

```bash
m3 memory memory_search --query postgres --k 20 2>/dev/null | jq '.'
```

Every tool also accepts a JSON object on **stdin**, which is what makes m3
scriptable from a hook or a CI step:

```bash
echo '{"query":"postgres","k":3}' | m3 memory memory_search --json-file -
```

Flags and piped JSON compose, and an explicit flag wins — so a script can pipe a
base object and override one field per invocation.

**This is why a missing feature is rarely a blocker.** Anything m3 does not do
itself, you can drive from the side it does expose. A git `post-commit` hook that
records what changed is a CLI call. A CI step that writes a deployment note is a
CLI call.

**It also means an MCP disconnect is not an outage.** A dropped stdio session
leaves the store completely intact and fully usable from the shell until the
client reconnects. Check with `m3 --version`: if the CLI answers, m3 is up.

---

## What m3 does not do

Kept honest deliberately — a list of strengths with no limits is not useful.

- **No code-symbol index.** No AST, no language server, no symbol table. See the
  first question for why this belongs to the agent.
- **No git-history hook.** Nothing watches commits. Wire one with the CLI.
- **No "was this memory used in the answer" signal by default.** `memory_grade`
  is the deliberate version of it, and it relies on the agent choosing to call
  it after answering. Uptake is voluntary; low uptake means less reinforcement,
  not wrong reinforcement.
- **Pinning and expiry are not yet honoured at search time.** They govern the
  maintenance passes. An expired-but-unpurged memory is still retrievable.

---

## Related

- [MCP Client Install](MCP_CLIENT_INSTALL.md) — wiring m3 into each agent
- [CLI Reference](CLI_REFERENCE.md) — the full command surface
- [Agent Instructions](AGENT_INSTRUCTIONS.md) — the behavioural rules agents follow
- [Multi-Agent Orchestration](MULTI_AGENT.md) — coordination patterns
