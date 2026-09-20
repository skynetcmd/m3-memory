# Using m3 for coding work

**m3 is a memory layer, not an agent framework.** It owns durable, searchable,
multi-agent memory and stops there — so it composes with whatever you have
rather than asking you to adopt a stack.

Two seams, either or both:

- **MCP** — plumb it into another process. Any agent, any IDE, no code.
- **CLI** — plumb it into your own code. Every tool in the catalog reads JSON on
  stdin and writes JSON on stdout, so m3 is scriptable from any language, any
  runtime, and from hooks and CI. **MCP is optional**, not the price of entry.

Language-specific work — parsing your syntax tree, watching your git history —
is deliberately *outside* the layer. Not missing: outside. Those live in a few
lines of your own code on top of the CLI, and this page shows the code. The
payoff is a memory layer that never needs to know your language, your VCS, or
your framework, and so never becomes the thing you have to work around.

---

## Does m3 index my code symbols? Does it follow refactors?

m3 does not ship a parser for your language, and that is what keeps it useful
across all of them. **Symbol indexing is an integration, not a missing feature** —
you own the AST, m3 owns what you decided about it.

Use whatever parser you already trust — `ast` in Python, tree-sitter,
`ts-morph`, your language server — and write the result in:

```python
import ast, json, subprocess

tree = ast.parse(open("payments/ledger.py").read())
symbols = [n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.ClassDef))]

subprocess.run(
    ["m3", "memory", "memory_write", "--json-file", "-"],
    input=json.dumps({
        "type": "reference",
        "title": "payments/ledger.py symbols",
        "content": "\n".join(symbols),
    }),
    text=True,
)
```

That is the whole integration. Swap `ast` for tree-sitter and the same nine
lines index Go, Rust or TypeScript — because m3 never had an opinion about the
language in the first place.

**Why it is not built in.** Your coding agent already holds the file, the parse
and the repo state. A memory layer carrying a *second* parser would maintain a
stale copy of something the agent already knows, in every language you use, and
the two would disagree. The useful question is not "can m3 parse Python" but
"which component should own the answer" — and the one holding the buffer is the
better owner.

What m3 contributes is the part the agent loses at the end of the session:
durability across sessions, tools and model upgrades. When a refactor lands, the
agent records what it decided and why, and that record outlives the IDE it was
made in.

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

## <a id="the-cli-is-a-first-class-interface"></a>Can I use m3 without MCP?

Yes — the CLI is a full interface, not a fallback. It and the MCP server are
**two front doors to the same database** —
the whole tool catalog, grouped as `memory`, `files`, `chatlog`, `tasks`,
`agent`, `admin`, `conversations`, `diagnostics` and `entity`.

```bash
m3 memory memory_search --query "which signing algorithm did we pick?" --k 5
m3 memory memory_write --content "..." --type belief --title "..."
```

Results go to **stdout** and logs to **stderr**, and a listing returns records
rather than a rendered table, so output composes:

```bash
m3 memory memory_search --query postgres --k 20 2>/dev/null | jq '.items[].id'
m3 tasks task_list 2>/dev/null | jq '.items[] | select(.state=="pending") | .title'
```

The envelope is always `{count, items}` — `count` lets you check for truncation
without walking the list. Pass `--no-as_records` for the human-readable form.

> **`jq` is not an m3 dependency.** It is used in the examples below only
> because it reads clearly. m3 emits plain JSON on stdout, so anything that
> parses JSON works just as well — `python -c "import sys,json; ..."`,
> PowerShell's `ConvertFrom-Json`, Node, or your language's client. Nothing in
> m3 shells out to `jq`, and none of these pipelines require it to be installed.

Every tool also accepts a JSON object on **stdin**, which is what makes m3
scriptable from a hook or a CI step:

```bash
echo '{"query":"postgres","k":3}' | m3 memory memory_search --json-file -
```

Flags and piped JSON compose, and an explicit flag wins — so a script can pipe a
base object and override one field per invocation.

### Composing tools

Because output is records and input is JSON, one tool's result drives the next
without parsing prose. The bulk tools take arrays, so a query can drive an edit:

```bash
# Pin everything matching a query.
m3 memory memory_search --query "deployment runbook" --k 20 2>/dev/null \
  | jq '{updates: [.items[] | {memory_id: .id, pinned: 1}]}' \
  | m3 memory memory_update_bulk --json-file - --dry-run
```

Drop `--dry-run` to apply. The same shape works for `memory_delete_bulk` and
`memory_link_bulk`.

The same pipeline without `jq`, to make the point that it is only a convenience:

```bash
m3 memory memory_search --query "deployment runbook" --k 20 2>/dev/null \
  | python -c "import sys,json; d=json.load(sys.stdin); \
      print(json.dumps({'updates':[{'memory_id':i['id'],'pinned':1} for i in d['items']]}))" \
  | m3 memory memory_update_bulk --json-file - --dry-run
```

Grading a result set after you answer is the same pattern:

```bash
m3 memory memory_search --query "signing algorithm" --k 5 2>/dev/null \
  | jq '{grades: [.items[] | {memory_id: .id, verdict: "helpful"}]}' \
  | m3 memory memory_grade --json-file -
# -> {"ok": true, "applied": true, "graded": 5}
```

A search records that it retrieved those memories, which is what entitles you to
grade them — so this works from a shell, not only from an agent holding an MCP
session. Grades outside the feedback window come back `applied: false` with a
reason rather than failing.

**This is why a missing feature is rarely a blocker.** Anything m3 does not do
itself, you can drive from the side it does expose. A git `post-commit` hook that
records what changed is a CLI call:

```bash
# .git/hooks/post-commit
m3 memory memory_write --type note --title "$(git log -1 --format=%s)" \
  --content "$(git log -1 --format='%H%n%an%n%b')$(git diff-tree --no-commit-id --name-only -r HEAD)"
```

A CI step that writes a deployment note is a CLI call. Exit codes are real: a
rejected argument exits 2 with the accepted keys listed, so `set -e` works.

**It also means an MCP disconnect is not an outage.** A dropped stdio session
leaves the store completely intact and fully usable from the shell until the
client reconnects. Check with `m3 --version`: if the CLI answers, m3 is up.

---

## Where the boundary sits

m3 is a memory layer. These live on your side of it **by design**, and each is
a short integration rather than a gap to wait on:

| Outside the layer | Your integration | Shown in |
|---|---|---|
| Parsing your language | Any parser → `memory_write` | [above](#does-m3-index-my-code-symbols-does-it-follow-refactors) |
| Watching your VCS | A `post-commit` hook → CLI | [above](#the-cli-is-a-first-class-interface) |
| Your agent's prompt and control flow | Your framework; m3 is the store | — |

Keeping these out is what lets the same memory layer serve a Python monorepo, a
Rust service and a TypeScript frontend without forking.

### Current limits, stated plainly

These are real and on the roadmap — not design boundaries:

- **Pinning and expiry are not yet honoured at search time.** They govern the
  maintenance passes, so an expired-but-unpurged memory is still retrievable.
- **The "was this memory used?" signal is opt-in.** `memory_grade` is the
  deliberate version and depends on the caller choosing to send a verdict after
  answering. Low uptake means *less* reinforcement, never wrong reinforcement.

---

## Related

- [MCP Client Install](MCP_CLIENT_INSTALL.md) — wiring m3 into each agent
- [CLI Reference](CLI_REFERENCE.md) — the full command surface
- [Agent Instructions](AGENT_INSTRUCTIONS.md) — the behavioural rules agents follow
- [Multi-Agent Orchestration](MULTI_AGENT.md) — coordination patterns
