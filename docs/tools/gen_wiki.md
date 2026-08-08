---
tool: bin/gen_wiki.py
sha1: fef2e9d1bd65
mtime_utc: 2026-08-07T23:53:52.046526+00:00
generated_utc: 2026-08-08T14:40:49.858688+00:00
private: false
---

# bin/gen_wiki.py

## Purpose

gen_wiki.py — compile a browsable wiki from core memories + the files corpus.

Thin CLI shell around the pure builder in `bin/wiki/`. Reads agent_memory.db and
(optionally) files_database.db, renders deterministic Markdown, and writes an
Obsidian-ready vault. Default output is <engine_root>/wiki.

    python bin/gen_wiki.py generate [--out DIR] [--check] [--no-files]
                                    [--importance-threshold F]
    python bin/gen_wiki.py status  [--out DIR]

Invoked by `m3 wiki generate` / `m3 wiki status` (see m3_memory/cli.py). The wiki
is a core feature and runs on the base install — clustering uses networkx, which
is a base dependency of m3-memory (no optional extra required).

---

## Entry points

- `def main()` (line 487)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--out` | Output vault dir (default <engine_root>/wiki). | None |  | str |  |
| `--check` | Exit non-zero if the on-disk vault differs from a fresh build. | `False` |  | store_true |  |
| `--no-files` | Skip the files corpus (memory-only vault). | `False` |  | store_true |  |
| `--synthesize` | Write an LLM prose lede per topic via a local chat endpoint (opt-in; cached; degrades to member-lists if no model). Mutually exclusive with --check. | `False` |  | store_true |  |
| `--check-drift` | Add a citation-drift lint section: a local chat model audits each synthesis against the sources it was compiled from and flags claims that no longer match (report-only; opt-in; cached; fail-open if no model). Mutually exclusive with --check. | `False` |  | store_true |  |
| `--review-derivability` | Refine the GDPR derivability review queue with a local chat model: for each synthesis restricted by an erasure, judge whether it is still derivable from its surviving sources (report-only; the deterministic queue renders regardless; fail-safe if no model). Mutually exclusive with --check. | `False` |  | store_true |  |
| `--importance-threshold` | Min importance for a memory to count as 'core' (default 0.55). | None |  | float |  |
| `--exclude` | Drop any memory whose title/content matches this regex (case-insensitive). REPLACES the default private/bench filter — pass --no-default-exclude to drop it instead. | None |  | str |  |
| `--no-default-exclude` | Do NOT apply the built-in private/bench exclusion. The vault will then contain benchmark and private-project material verbatim — only for a local, unshared build. | `False` |  | store_true |  |
| `--html` | Also write a single self-contained wiki.html viewer — open it in any browser to click through the vault offline (no server, no dependencies). | `False` |  | store_true |  |
| `--obsidian` | Emit [[wikilinks]] instead of standard Markdown links so Obsidian's graph view and backlinks work. (Wikilinks show as literal text outside Obsidian, so this is opt-in.) | `False` |  | store_true |  |
| `--importance-threshold` | Core-memory importance floor (default 0.55). | None |  | float |  |
| `--scope` | Tenancy scope for written syntheses (default agent). | `agent` |  | str |  |
| `--user-id` | Owner user_id for written syntheses. | `` |  | str |  |
| `--dry-run` | Report what would be compiled; no model call, no write. | `False` |  | store_true |  |
| `--out` |  | None |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `uri`` (line 64)


---

## Notable external imports

- `files_memory.config (FILES_DB_PATH)`
- `files_memory.config (memory_db_path)`
- `files_memory.db (_db)`
- `files_memory.db (_is_postgres)`
- `m3_core.paths (get_m3_engine_root)`
- `memory.backends.selector (active_backend)`
- `memory.db (_db)`
- `wiki (admission)`
- `wiki (cluster)`
- `wiki (select)`
- `wiki.build (WikiOptions, build_wiki)`
- `wiki.citation_drift (DriftConfig)`
- `wiki.citation_drift (DriftConfig, ModelDriftJudge)`
- `wiki.compile (compile_clusters, load_heads)`
- `wiki.derivability_review (ModelDerivabilityJudge)`
- `wiki.html_view (build_html)`
- `wiki.prose_compiler (LLMProseCompiler)`
- `wiki.select (DEFAULT_IMPORTANCE_THRESHOLD)`
- `wiki.synth (SynthConfig, Synthesizer)`

---

## File dependencies (repo paths referenced)

- `.m3-wiki-manifest.json`
- `.md`
- `agent_memory.db`
- `files_database.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
