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

## Clustering quality (optional dependency)

Topic clustering works out of the box with a pure-Python algorithm. For tighter
communities on large memory sets, install the optional extra — it pulls in
`networkx` and M3 uses it automatically when present:

```bash
pip install "m3-memory[wiki]"
```

Force the pure-Python path (e.g. to compare) with `--no-networkx`.

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
  --importance-threshold F   Min importance to count as "core" (default 0.6)
  --no-files                 Memory-only vault (skip the files corpus)
  --synthesize               Add an LLM prose lede per topic (opt-in, cached)
  --obsidian                 Emit [[wikilinks]] so Obsidian's graph view + backlinks
                             work (opt-in; literal text elsewhere)
  --exclude REGEX            Drop memories whose title/content matches REGEX
  --html                     Also write a self-contained wiki.html viewer
  --no-networkx              Force the pure-Python clustering fallback
  --check                    Exit non-zero if the on-disk vault is stale

m3 wiki status [--out DIR]   Vault location, page count, last build time
```

Everything runs locally. No account, no API key, no network egress is required
for core generation (only `--synthesize` talks to a model, and that model is
yours).
