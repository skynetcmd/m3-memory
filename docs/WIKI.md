# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Wiki — an auto-generated knowledge base from your memories

`m3 wiki generate` compiles your **canonical memories** and your **indexed files**
into a browsable, interlinked Markdown vault. It is a *projection*, not a new
store: it reads `agent_memory.db` and `files_database.db` and renders pages — your
memory model is untouched, and re-running only refreshes the output.

The result is a folder of Markdown files that opens as an
[Obsidian](https://obsidian.md) vault, renders on GitHub, or browses offline via a
self-contained HTML viewer. By default it uses **standard Markdown links**
(`[text](page.md)`) so it's clickable in every renderer; add `--obsidian` to emit
`[[wikilinks]]` when you want Obsidian's **graph view and backlinks** to populate
(see [Using it in Obsidian](#using-it-in-obsidian)).

---

## Quick start

```bash
m3 wiki generate          # writes a vault to <engine_root>/wiki
m3 wiki status            # where it is, how many pages, last build
```

By default the vault lands in your engine root (e.g. `~/.m3/engine/wiki`) — it is
**private and per-machine**, and is never committed anywhere. Point it elsewhere
with `--out`:

```bash
m3 wiki generate --out ~/notes/m3-vault
```

Then open that folder in Obsidian (**Open folder as vault**).

---

## What gets included

A memory becomes a wiki entry when it is **canonical** — M3's three overlapping
signals for "this matters":

- **pinned** — explicitly marked as canon (never aged out)
- **high importance** — at or above the `--importance-threshold` (default `0.6`)
- **a consolidated type** — `belief`, `procedure`, or `reference` (these are
  already distillations, so they belong in the wiki regardless of importance)

Raise the bar for a tighter, higher-signal vault:

```bash
m3 wiki generate --importance-threshold 0.8
```

Your **files corpus** contributes a second layer: each indexed document becomes a
`sources/` page (from its summary), and — via M3's promotion bridge — a memory can
link *down* to the exact file a fact came from, while a source page links *up* to
every memory it fed. Skip the files layer with `--no-files`.

> **Backend note.** The wiki's *memory* layer works on both M3 backends (SQLite and
> PostgreSQL) — it reads through M3's core database seam. The files corpus
> is currently a local **SQLite** sidecar (`files_database.db`) on every backend, so
> the `sources/` pages are read from SQLite even on a PostgreSQL deployment.
> PostgreSQL support for the files corpus is planned (see the CHANGELOG). If you run
> PostgreSQL without a local files DB, use `--no-files` for a memory-only vault.

---

## How pages are organized

- **Topics** (`topics/*.md`) — related memories are clustered into one page per
  topic. Clustering uses M3's relationship graph *and* shared extracted entities,
  so memories that talk about the same thing land together even without a
  hand-authored link. Each page carries real frontmatter (`confidence`,
  `valid_from`, the source `memory_ids`), a member list, an **Evidence** section
  (links to source files), and **Backlinks**.
- **Sources** (`sources/*.md`) — one page per indexed file, with its summary and
  notable extracted facts.
- **index.md** — a reader-facing table of contents: a **⭐ Start here** shortlist
  of your most prominent topics, then sections grouped by kind (Knowledge,
  Runbooks, Decisions, References).
- **overview.md** — counts and your largest topics at a glance.
- **lint.md** — housekeeping: orphaned memories, and **contradictions** (memories
  that disagree are kept on one page and reported here, never silently dropped).

Superseded and contradicted memories are shown as history, not hidden — the wiki
reflects what M3 actually knows, including where it changed its mind.

---

## Prose summaries (optional)

By default a topic page lists its member memories. With `--synthesize`, M3 asks a
**local chat model** to write a short prose lede at the top of each topic:

```bash
m3 wiki generate --synthesize
```

This talks to an OpenAI-compatible `/v1/chat/completions` endpoint. Point it at
your model with environment variables (defaults shown):

| Variable | Default | Purpose |
|---|---|---|
| `M3_WIKI_SYNTH_URL` | `http://127.0.0.1:1234/v1/chat/completions` | Chat endpoint (LM Studio, llama-server, Ollama, vLLM, …) |
| `M3_WIKI_SYNTH_MODEL` | *(server's loaded model)* | Model id to request |
| `M3_WIKI_SYNTH_TIMEOUT` | `30` | Per-request timeout (seconds) |

Ledes are **cached on disk** by a content-hash of each topic, so an unchanged
topic is never re-summarized and repeat runs are cheap. If no model is reachable,
synthesis degrades gracefully — the page keeps its member list and generation
never fails.

---

## Compiled syntheses (`m3 wiki compile`)

`--synthesize` writes a prose lede into the *rendered vault*. `m3 wiki compile`
goes further: it writes each topic's synthesis back into your store as a durable
`synthesis` memory — a first-class, searchable, supersede-tracked row with
provenance edges to its sources. This is the "compile-at-ingest" model: knowledge
is distilled once and stored, not re-derived on every read.

```bash
m3 wiki compile               # compile the whole corpus
m3 wiki compile --dry-run     # show what would compile — no model call, no write
```

Compilation is **idempotent**: an unchanged topic is recognized by a content hash
and skipped (no model call), so re-running only recompiles topics that actually
changed. Each synthesis records its source `member_ids` and writes `consolidates`
provenance edges, so blast-radius, citation-drift, and the Knowledge Anchor Report
can all reason about how a page was derived.

### The admission gate — which topics earn a synthesis

Not every cluster deserves a compiled page. A cluster fused only by incidental
co-mention (two memories that happen to name the same file or host) is a grab-bag,
not a topic. The **admission gate** demotes such clusters back to the orphan list
*before* the model is called — so compilation spends effort only on genuinely
anchored topics, and the vault isn't padded with incoherent pages.

The gate scores each cluster on its **member-to-member link structure** and admits
it if any of these clears its floor:

| Signal | Meaning | Default floor |
|---|---|---|
| `backbone_ratio` | share of real (non-co-mention) edges — the primary discriminator | 0.6 |
| `provenance` | share of edges that are authored lineage (supersedes / extends / …) | 0.5 |
| `kas` | overall structural [Knowledge Anchor Score](#) | 0.5 |

Two properties are guaranteed by design:

- **Deterministic** — the same store produces a byte-identical result.
- **State-independent** — the decision reads only *authored* structure, never the
  `consolidates` edges that a prior compile wrote. A topic is admitted or demoted
  identically on the first compile and the thousandth; compiling never changes what
  the gate will do next time.

Tune the floors (or turn the gate off for an audit build) with an optional config
file at `$M3_CONFIG_ROOT/.wiki_admission.json`:

```json
{ "min_backbone_ratio": 0.6, "min_provenance": 0.5, "min_kas": 0.5, "enabled": true }
```

Any field may be omitted (it keeps the default). A `--dry-run` reports how many
clusters the gate would demote, so you can calibrate before a real run.

> Compiled syntheses derived from a memory that is later erased are handled under
> GDPR Art. 17 — see [The Wiki & the Right to Erasure](WIKI_GDPR.md).

---

## Keeping it fresh

The generator is **deterministic**: the same memories produce a byte-identical
vault, so a diff always reflects a real change in what M3 knows. Check whether the
on-disk vault is stale (useful in a scheduled job):

```bash
m3 wiki generate --check    # exit 0 if fresh, non-zero (and lists drift) if stale
```

`--check` runs on the deterministic vault only; it is not combined with
`--synthesize` (LLM prose isn't bit-reproducible).

---

## Clustering

The wiki is a **core feature — it ships in the base `pip install m3-memory`** and
needs no extra to run. `m3 wiki generate` works out of the box.

Topic clustering uses [`networkx`](https://networkx.org) greedy-modularity
community detection, which is a **base dependency** of m3-memory — it installs
with the core package, nothing extra to enable. Clustering is deterministic
run-to-run, so `m3 wiki generate --check` stays byte-reproducible.

> The old `[wiki]` optional extra (and the `--no-networkx` flag) are gone:
> networkx is now always present. `pip install "m3-memory[wiki]"` still works as a
> no-op back-compat alias so existing scripts don't break, but it installs nothing
> beyond the base package.

---

## Using it in Obsidian

The vault opens directly in Obsidian: **Open folder as vault**, point it at the
output dir. Every page is clickable straight away.

For Obsidian's **graph view** and **backlinks pane** to populate, generate with
`--obsidian`:

```bash
m3 wiki generate --obsidian
```

This emits `[[wikilinks]]` instead of standard Markdown links — Obsidian builds its
graph and backlinks from wikilinks, not from `[text](page.md)` links. The tradeoff:
wikilinks render as literal text outside Obsidian (GitHub, the HTML viewer), so
`--obsidian` is opt-in. Use the default (standard links) for a portable vault; use
`--obsidian` when Obsidian is your primary reader.

---

## Command reference

```
m3 wiki generate [options]
  --out DIR                  Output vault dir (default <engine_root>/wiki)
  --importance-threshold F   Min importance to count as "core" (default 0.55)
  --no-files                 Memory-only vault (skip the files corpus)
  --synthesize               Add an LLM prose lede per topic (opt-in, cached)
  --obsidian                 Emit [[wikilinks]] so Obsidian's graph view + backlinks
                             work (opt-in; literal text elsewhere)
  --exclude REGEX            Drop memories whose title/content matches REGEX
  --html                     Also write a self-contained wiki.html viewer
  --check                    Exit non-zero if the on-disk vault is stale

m3 wiki compile [options]    Compile topics into durable synthesis memories
  --importance-threshold F   Min importance to count as "core" (default 0.55)
  --dry-run                  Report what would compile — no model call, no write
                             (also shows how many clusters the admission gate demotes)

m3 wiki status [--out DIR]   Vault location, page count, last build time
```

Everything runs locally. No account, no API key, no network egress is required
for core generation (only `--synthesize` talks to a model, and that model is
yours).
