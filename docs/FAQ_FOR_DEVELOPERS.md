# FAQ for Developers

Short answers to the "wait, how do I…?" questions that come up while working *on*
m3 (not using it as a product — that's the [user-facing FAQ](FAQ.md)). Each answer
points to the authoritative doc so you can go deep.

This is a **discovery layer**: things below *are* documented elsewhere, but a new
contributor doesn't know where to look. Start here, then follow the link.

New to the codebase? Read [CONTRIBUTING.md](CONTRIBUTING.md) →
[ARCHITECTURE.md](ARCHITECTURE.md) → [TESTING.md](TESTING.md) first; this FAQ is for
the specific snags.

---

## Tools & the MCP surface

### Q: A tool I need (e.g. `memory_delete`) isn't in my tool list / `ToolSearch` finds nothing. Is it missing?

**No — it's domain-gated, not missing.** Only the ~18 *essentials* (main searches +
writes) register at MCP session start; everything else loads on demand to keep the
startup context small. A tool not being in your surface is **not** "capability
absent," and you should **never** fall back to raw `sqlite3` or shelling out via
Bash to touch it.

Reach it through the dispatcher instead:

- **Known tool, one-off** → `m3_call(tool="memory_delete", args={...})` — invokes any
  catalog tool without loading its domain. Add `dry_run=true` to validate args and
  check the destructive gate first.
- **Unsure of the args** → `m3_index(domain)` first (lists name/args/destructive
  flag), then `m3_call`.
- **About to use many tools from one domain** → `tools_load_domain(domain)` (valid:
  `memory`, `chatlog`, `files`, `entity`, `agent`, `tasks`, `conversations`,
  `diagnostics`, `admin`).

Full detail: [AGENT_INSTRUCTIONS.md → "Reaching tools that aren't in your startup
surface"](AGENT_INSTRUCTIONS.md) and the [Domain Gating section in the
README](../README.md). Every catalog tool is listed in
[MCP_TOOLS.md](MCP_TOOLS.md).

### Q: `m3_call` refuses my delete/forget with `destructive_gated`. How do I proceed?

That gate is intentional. Destructive tools (`memory_delete`, `gdpr_forget`, …)
require `MCP_PROXY_ALLOW_DESTRUCTIVE=1` whether reached directly or via `m3_call`.
For low-stakes cleanup, prefer the **non-destructive** alternative rather than
enabling destructive ops — e.g. `memory_supersede` closes a memory's validity and
drops it from default search without hard-deleting it (it stays retrievable by id /
`as_of`). Only set the env flag when you genuinely mean to hard-delete.

### Q: I changed `bin/mcp_tool_catalog.py`. What else do I have to update?

Regenerate the derived docs and re-run the drift gate before pushing — the tool
catalog, `MCP_TOOLS.md`, and the "N tools" counts are generated, not hand-kept. See
the pre-push process in [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) /
[CONTRIBUTING.md](CONTRIBUTING.md); `python bin/check_tool_catalog_drift.py` is the
gate.

---

## Backends & the storage seam

### Q: How do I add a database backend (or a per-backend SQL variant)?

One self-contained `bin/memory/backends/<name>_backend.py`: a co-located `Dialect`
subclass overriding only the *divergent* SQL fragments, a backend class, and a
`@register_backend` line. Nothing in the shared modules changes.

Full recipe (including which dialect methods are divergent vs. deliberately
concrete): [EXTENDING.md → "Add a DB backend"](EXTENDING.md). The authoritative,
never-stale list of divergent methods is derived programmatically by
`tests/test_backend_conformance.py::_divergent_methods()`.

### Q: My SQL works on SQLite but breaks on PostgreSQL (`?` placeholders, `INSERT OR
REPLACE`, `datetime('now')`, `PRAGMA`, `GLOB`, …). What do I do?

Don't branch on backend name at the call site — route it through the dialect seam.
Ask `dialect()` for the fragment: `param()`/`placeholder(n)`, `insert_or_ignore()` +
`on_conflict_*`, `now()`, `columns_of()`, `glob_match()`, the error classifiers
(`is_integrity_error`, `is_undefined_object_error`), etc. If the primitive you need
doesn't exist, **add one to the seam** rather than a call-site workaround — that's
the standing rule. See [EXTENDING.md](EXTENDING.md) and the dialect docstrings in
`bin/memory/backends/dialect.py`.

### Q: Where do migrations go, and why are the SQLite and PostgreSQL numbers
different?

The two backends have **independently-numbered** migration sequences (PG runs ahead
because of a folded baseline + parity-catchup files). Compute the next number per
directory; don't try to sync them. The files store has its *own* migration set too.
See [memory/migrations/README.md](../memory/migrations/README.md) and
[bin/files_memory/migrations/README.md](../bin/files_memory/migrations/README.md).

---

## Testing

### Q: My `requires_pg` tests all **skip** even though PostgreSQL is running. Why?

The reachability probe couldn't connect from where pytest runs, so the whole suite
skipped (the result is cached per session). Most common cause: the DSN host isn't
actually reachable — e.g. a cluster bound loopback-only behind a flaky port forward.
Verify with:

```bash
python -c "import psycopg2,os; psycopg2.connect(os.environ['M3_PRIMARY_PG_URL']).close(); print('reachable')"
```

and point the tests at a host your machine can reach directly. Full detail (DSN env
vars, the auto-skip-is-expected semantics, the silent-skip footgun):
[TESTING.md → "PostgreSQL-backed tests"](TESTING.md). Maintainers keep the
machine-specific cluster stand-up in the internal runbooks (it carries local
credentials).

### Q: A test passes alone but fails in the full run.

Almost always **test pollution** — a fixture left global state set (a cached backend
selector, a monkeypatched module in `sys.modules`, an env var). Reset it in the
fixture (e.g. `selector._reset_for_tests()` when a test flips `M3_DB_BACKEND`). See
the seam-fixture notes in [TESTING.md](TESTING.md) and `tests/conftest.py`.

### Q: How do I run just the fast, no-external-deps tests?

`python -m pytest -q` on the pytest suite skips anything gated on an unavailable
resource (LLM, embedder, PG, native wheel) — those are `requires_*` markers that
auto-skip cleanly, not failures. The full ladder (unit → e2e → benchmark) is in
[TESTING.md](TESTING.md).

---

## Missing something?

If you hit a "how do I…?" that isn't here, it belongs here — add the question with a
one-paragraph answer and a link to the authoritative doc. The value of this page is
that it stays a *thin index* into the deep docs, not a second copy of them.
