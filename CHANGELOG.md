# Changelog

All notable changes to M3 Memory are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## Repo policy notes

**Commit message hygiene (forward-going from 2026-04-29):** commit messages
on `main` and any branch that pushes to `origin` describe features in
generic terms. Internal experiment names, private branch names, and
specific corpus/variant tags stay on the private development branches
(`private/lme`, `private/lme-runs`, `private/locomo` worktrees) and in
the run-catalog artifact on those branches. References that already
appeared in published commit history (README/CHANGELOG/registry
descriptions are openly discussing benchmark results — that is intentional
public positioning) are not retroactively rewritten; the policy is
forward-going only.

---

## [Unreleased]

### Web Diagnostics Portal (planned)

Work-in-progress; not yet released. Tracking under `bin/dashboard_server.py`.

#### Web Dashboard
- **Multi-DB overview** — native dynamic FastAPI control center for core memory nodes, chatlog sessions, and ingested files; HSL cyan/magenta/emerald themes, active context glows, transparency dimming, and alert banners.
- **Accurate Chatlog Sessions card** — distinct conversation sessions (coalescing legacy NULL values to `legacy` to represent untracked blocks) as the primary numeric, with total turns (`type='chat_log'`) as a Fira Code sub-label.
- **Accurate Files card** — total file chunks as the primary numeric, with deduplicated non-blank lines (via `text_sha256` GROUP BY) and files count as Fira Code sub-labels.
- **Process isolation** — `_DB_PATHS` cached at startup to isolate parallel tab queries from multi-DB focal views without process-wide env pollution.

#### System Diagnostics & Tasks launcher
- Integrated control panel inside the unified dashboard.
- Background execution of heavy-duty maintenance runs — ephemerality decay (`chatlog_decay.py`), lazy embedding backfill (`chatlog_embed_sweeper.py`) — without blocking the UI.
- Live-streamed stdout/logs from non-blocking subprocesses, with completion exit codes surfaced in the dashboard.

See [ROADMAP.md](docs/ROADMAP.md) for the broader observability plan.

---

## [2026.6.8.2] - 2026-06-08 — Documentation accuracy pass

Documentation-only release; no code or behavior changes.

### Documentation
- **Comparison accuracy & sourcing.** Competitor benchmark figures in the Sovereign Substrates table verified against primary sources and corrected/cited; metric mismatches (recall vs. QA accuracy) and a disputed/scam-flagged entry are now clearly caveated.
- **Retrieval vs. QA metric framing.** README, comparison table, core-features, and myths pages distinguish retrieval session-hit-rate (the metric most systems publish) from end-to-end QA accuracy (answer-model-dependent), with sourcing notes.
- **Default-state corrections.** Docs now reflect that entity-graph, fact-enrichment, the ingestion-enrichment heuristics, and intent routing are on by default, with cost/disable guidance.
- **Knowledge-graph configurability** documented (`M3_ENTITY_VOCAB_YAML`; stock 7-type / 34-predicate vocabulary); relationship-type count standardized to 9 across docs.
- **Tool-count drift policy** — public prose now says "100+ tools" (exact count pinned only in the generated manifest + drift test).
- Newly documented retrieval/ranking env knobs (elbow trim, expansion-displacement guard, contradiction gates, temporal-k-bump); ROADMAP refreshed.

---

## [2026.6.8.1] - 2026-06-08 — LongMemEval-S retrieval results

### Added

- **README incorporates the engine-upgrade SHR table** from the upstream LongMemEval discussion thread (`xiaowu0162/LongMemEval#43`): **98.2% / 99.2% / 100.0%** session-hit-rate at k=5/10/20 (versus the prior 96.2% / 96.8% report). Method note clarifies that SHR is `recall_any@k` — the same convention adjacent submissions report as "R@k". k=10 is M3's default search depth; every column uses the same engine settings the production `memory_search` ships with.
- **Bridging note** between SHR and E2E QA — the retrieval-vs-QA gap (99.2% SHR → 89.0% QA at k=10) is dominated by answer-model errors on already-retrieved gold evidence, not retrieval misses.

### Documentation

- README opening tagline synced to match the GitHub repo description and PyPI package summary (Memory Framework, broader agent compatibility, dual-mode positioning).
- `docs/ROADMAP.md` refreshed — Q2 themes restructured, `v2026.6.8.1` set as Current Version, planned sections re-prioritized against current reality.
- `docs/CHANGELOG.md` and `docs/CHANGELOG_2026.md` consolidated into this single canonical `/CHANGELOG.md` at the repo root.

### Notes

- No code changes from `v2026.6.8.0`.

---

## [2026.6.8.0] - 2026-06-08 — Cross-platform install hardening + m3-core-rs 3.6.6 wheels

### Fixed

- **`install.sh` re-runs no longer abort.** Running the canonical installer (or `m3 setup`) on an already-installed system used to fail with `repo already exists`. The setup wizard now passes `--force` to `install-m3`, which preserves user data (`.db`, `.json`, `.jsonl`) across the upgrade. Applies on macOS, Linux, and Windows identically.
- **Project Oxidation wheel now lands in the right venv with the right features.** The old Oxidation step pip-installed `m3-core-rs` into the payload's internal `.venv` with a stale hardcoded git tag and no Cargo features — so the wheel was invisible to the pipx-installed `mcp-memory` AND was missing GPU acceleration on every backend except CPU. The Oxidation step now delegates to `rust_core_install`, which auto-detects backend (Metal / CUDA / Vulkan / CPU), installs into the right interpreter, and selects the matching `embedded-*` Cargo features.
- **`M3_EMBED_GGUF` is now persisted on every platform.** When the wizard auto-discovers a BGE-M3 GGUF, it now writes the path to the appropriate shell rc (`~/.zshrc` / `~/.bashrc` on Unix, `setx` on Windows) AND patches the `memory` MCP server entry's `env` block in `~/.claude/settings.json` and `~/.gemini/settings.json` — MCP servers spawned by Claude Code don't inherit interactive shell env on macOS (launchd) or Windows (GUI process tree), so the env block is the only way to reach them. Linux LM Studio users were also missed by GGUF discovery; `~/.cache/lm-studio/models` is now probed.
- **PEP 427 wheel-filename preservation** in `install_from_github_release` — the function now writes the downloaded wheel under its original asset name (e.g. `m3_core_rs_macos_metal-3.6.6-cp314-cp314-macosx_11_0_arm64.whl`) so pip can parse the metadata, instead of handing pip a tempfile basename like `tmpXXXX.whl` that pip rejects with `Invalid wheel filename (wrong number of parts)`.

### Added

- **3-tier wheel install cascade** in `m3_memory/rust_core_install.py`:
  1. PyPI prebuilt — fastest path, no toolchain needed.
  2. GitHub Release prebuilt (NEW) — lists release assets via API, matches `m3_core_rs_<os>_<backend>-<version>-cp<py>-*.whl`, downloads (chunked with progress), pip-installs. **Required** for Linux CUDA (464 MiB wheel) and Windows CUDA (122 MiB wheel) — both exceed PyPI's 100 MiB cap. Defensive fallback for every other backend.
  3. Source build — only when caller opts in via `allow_source_fallback=True`. The CLI command `m3 embedder install-gpu` keeps this default. The `curl install.sh` Oxidation prompt switches to `allow_source_fallback=False` and prints a multi-line per-OS recommendation (cmake + Rust install commands + the `m3 embedder install-gpu` opt-in line) when both prebuilt tiers miss.
- **Build-tools preflight catches missing Rust.** `_check_build_tools` now includes the Rust toolchain. `_find_cargo` probes `~/.rustup/toolchains/*/bin/cargo` in addition to PATH, catching the rustup-installed-but-not-on-PATH case from prior install transcripts. The missing-tools error now lists per-OS prereq commands and the rustup curl-install line.
- **m3-core-rs 3.6.6 wheels** — 28 wheels (7 backends × cp311–cp314) published at <https://github.com/skynetcmd/m3-core-rs/releases/tag/v2026.06.07>. 20 on PyPI (`m3-core-rs-{macos-metal, linux-cpu, linux-vulkan, windows-cpu, windows-vulkan}`); 8 GitHub-only (linux-cuda + windows-cuda, by PyPI size policy).

### Hardened

- **curate-memory and curate-chatlog subagents — UUID-tail hallucination defense.** 2026-06-07 incident: `m3:curate-memory` reconstructed UUID tails from short prefixes seen in its own status output. The hallucinated full UUID collided with a real but unrelated memory's first 8 chars — so the supersede operation mutated the WRONG memory instead of erroring as not-found. The old prompt said "use full UUIDs" but didn't forbid reconstruction from prefixes. The new "UUID integrity" section in both agent prompts:
  - Hard rule: copy verbatim from tool output, never reconstruct from prefix
  - Verification step: scan each plan ID against prior tool results, drop ops whose IDs aren't found
  - Mandatory `phase=plan_integrity_drop n=<n>` heartbeat (even at n=0) so the user sees the check happened
  - APPLY-mode refusal for IDs not present in the embedded PLAN block

### Tests

- 13 new tests in `tests/test_setup_wizard_preflight.py` covering the force flag, persistence on Unix + Windows (`setx` path), idempotency, MCP-settings patching, setx failure non-fatality, shell-rc autodetect (zsh / bash / fallback), and the Linux LM Studio cache dir.
- 7 new tests in `tests/test_rust_core_install.py` covering the 3-tier ordering, GitHub Release happy path / 404 / no-asset / network-error, cargo detection via rustup toolchain dirs, and the build-tools cargo check.

### Notes

- Out of scope (deferred): `homecoming.py` legacy-DB path bug, `migrate_memory.py` db-lock retry, `install-m3 --non-interactive` flag, pip cache noise, m3-core-rs Cargo.toml fix where `embedded-metal/cuda/vulkan` should imply `embedded` (PR'd separately against `skynetcmd/m3-core-rs`).

---

## [2026.6.6.0] - 2026-06-07 — FTS search crash fix

### Fixed

- **Search no longer crashes on operator characters in queries.** The FTS5 query sanitizer used a blocklist that left `-` `:` `^` `/` `.` and most punctuation to reach the FTS5 parser, so any chatlog/memory search whose terms contained a model name (`gpt-4o`), a hyphenated identifier (`claude-code`), a range (`100-200MB`), or a `field:value` token raised `OperationalError: no such column …` / `syntax error near …`. Plain-word searches worked, so the bug presented as intermittent. The sanitizer is now an allowlist (every non-word / non-space char → space), aligned with the tokenizer so `gpt-4o` → `gpt 4o` still matches. Interior quotes in exact-phrase queries are now escaped too.
- **Doctor tier-1 classification.** `m3 doctor` reported tier-1 as `offline` instead of `not-configured` when the optional native extension is absent and no GGUF is set; it now classifies configured-ness from the GGUF first.

### Changed

- **Package description** updated: "Agentic Memory for AI Agents · Works with Claude, Gemini, OpenCode, OpenClaw, Hermes".

### Notes

- Pure-Python release — installs and runs fully without any native wheel. The optional `m3-core-rs` oxidation speedup is unchanged and still optional.
- The remainder of this release is test-isolation hardening that brought CI fully green across Linux / macOS / Windows × Python 3.11–3.12.

---

## [2026.6.1.0] - 2026-06-01 — Polars bitemporal + doctor --fix + decoupled roots

### Added

- **Polars-accelerated bitemporal history** (`bin/memory/history.py`) — high-performance columnar grouping and delta analysis for bitemporal memory timelines. Pure-Python fallback included; Polars is an optional performance dependency.
- **Doctor quick-repair mode** — `m3 doctor --fix` with full CLI dispatch for auto-healing SQLite migrations, FTS5 index rebuilds, and bitemporal cohesion checks. `--dry-run` flag previews repairs without applying them.
- **SDK oxidation — native FFI shims** (`bin/m3_sdk.py`) — Rust-backed implementations of system telemetry (`sysinfo`), advisory file locking (`fs2`), and atomic circuit breakers via PyO3. All shims are lazy-import-guarded behind `M3_CORE_RS_DISABLE` for environments without the native extension.
- **Decoupled config/engine roots** — `~/.m3/config` and `~/.m3/engine` are now independently relocatable via environment variables.

### Changed

- `bin/m3_sdk.py` — `get_system_telemetry` routes through the native sysinfo shim when available, falling back to `psutil` gracefully.
- `bin/memory_core.py` — lazy shims added for history analytics and oxidation paths.

### Tests

- 31+ new tests across `test_doctor.py`, `test_sdk_oxidation.py`, `test_sqlite_vec_integration.py`, and `test_memory_history.py`.

---

## [2026.5.30.2] - 2026-05-30 — Decoupled roots wizard + sqlite-vec + FFI parity

### Added

- Setup wizard decoupled-roots and dynamic plugin-architecture lazy loading.
- `sqlite-vec` integration and full FFI parity re-exports in `memory_core`.

### Fixed

- Restored missing public FFI re-exports (`os`, `_infer_change_agent_util`) in `memory_core`.

---

## [2026.5.30.1] — May 30, 2026 — Google Antigravity plugin and native integration

### Added

- **First-class Google Antigravity plugin** under the `.antigravity-plugin/` directory, packaging 15 modular skills (slash commands), active lifecycle hooks, and curators (`curate-memory.md` / `curate-chatlog.md`).
- **Antigravity CLI setup support** in the `m3 setup` wizard and installer pipeline. It automatically registers the m3 MCP server in `~/.gemini/antigravity-cli/settings.json`.
- **Integrated Antigravity CLI chatlog auto-capture** under `bin/chatlog_config.py`, `bin/chatlog_core.py`, `bin/chatlog_init.py`, and `bin/chatlog_ingest.py`.
- Dedicated Antigravity plugin documentation at `docs/antigravity_plugin.md` and linked across references.

---

## [2026.5.30.0] — May 30, 2026 — Entity coalescing v2 (reversible apply/unapply) + CLI exit-code fix

### Added

- **Entity-coalescing v2 — reversible overlay apply/unapply** (`files_entity_coalesce_apply`
  + `files_entity_coalesce_unapply` MCP tools; `entity-coalesce-apply` /
  `entity-coalesce-unapply` CLI). Materializes reviewed/auto-merge candidates as
  a reversible `same_as` + shared-`cluster_id` overlay — members are never
  deleted, the canonical view is a read-time projection, and a deterministic
  representative is chosen per cluster. `unapply` fully reverses one cluster
  (drops edges, clears flags, strips aliases) and **tombstones** the candidate
  (`review_action='unapplied'`) so the auto-merge path will not silently
  re-merge it (the "unmerge is a recorded decision" pattern); deliberate
  re-apply remains available via explicit candidate UUIDs.

### Changed

- **Auto-merge band scoped to one detection run.** `apply --auto-merge` now
  applies only the latest run's `merge` band (or an explicit `--run`), so a
  superseded pre-guard run can't be silently materialized.
- **Two false-merge guards on the detect pass.** Names differing only by a
  leading underscore (private-vs-public) or a trailing numeric/version token
  (distinct configs/versions) are demoted from `merge` to `needs_llm` — they
  score high on similarity but are usually different entities.

### Fixed

- **CLI error exit codes no longer masked to 0 on Windows.** The UTF-8 re-exec
  (`_ensure_utf8`) used `os.execv`, which on Windows spawns a child and returns
  to the parent — so every non-zero exit (argparse errors, destructive-gate
  refusals, bad `--json`, impl failures) was silently rewritten to 0. The
  re-exec now propagates the child's exit code on Windows; the POSIX path is
  unchanged.
- **Embed-tier reporting keyed off the recorded model, not `M3_EMBED_GGUF`.**
  A fast in-process GGUF run with the env var unset was wrongly reported as the
  HTTP fallback. `_memory_db` mutations are also isolation-hardened (optional
  explicit target + a confirm/db_path guard on real applies).

## [2026.5.29.7] — May 30, 2026 — Entity coalescing v1 + search crash fix

### Added

- **Entity-coalescing pass v1** (`files_entity_coalesce` + `_list` + `_review`
  MCP tools; `files_memory.tools entity-coalesce[-list|-review]` CLI). Post-ingest
  cleanup of provisional entities created by files fact-extraction: quarantines
  non-entity noise (prices/code-tokens/fragments — reversible flag, never delete)
  and flags near-duplicate entities into a review queue (block → rapidfuzz →
  embed-survivors → two-band). **Detection + review only — never merges or
  auto-applies**; "coalescing" is modeled as a reversible same_as/cluster overlay
  decided by human review. Persists an `entity_embeddings` cache (re-runs skip
  embedded names) and reports the embed tier with a hint to set `M3_EMBED_GGUF`
  for the in-process path. Adds `rapidfuzz` (difflib fallback if absent).

### Fixed

- **`memory_search` NameError when observation gates were enabled.** Commit
  d78fc1d extracted two call sites (`_apply_observation_preference`,
  `_apply_two_stage_expansion`) but never created the functions, so enabling
  `M3_PREFER_OBSERVATIONS` or `M3_TWO_STAGE_OBSERVATIONS` crashed search at
  runtime. Restored both verbatim from the pre-refactor inline logic + added
  regression tests and an end-to-end gated-search check.

## [2026.5.29.6] — May 29, 2026 — Windows UTF-8 mode (cp1252 crash class)

### Fixed

- **Eliminated the Windows cp1252 crash class across all clients.** On Windows
  both stdio AND `open()` default to cp1252, so any non-cp1252 character
  (em-dashes, arrows, box-drawing, emoji) crashed with `UnicodeEncodeError` on
  print or `UnicodeDecodeError` on a no-encoding `open()`. The prior per-file
  stdout reconfigure only patched stdio for one process. Now the entrypoints
  force true Python UTF-8 mode (PEP 540): `m3_memory.cli` (covers Claude Code /
  Gemini CLI / OpenCode, which launch the `m3` console script) and
  `bin/mcp_proxy.py` (covers OpenClaw, which launches the proxy directly) set
  `PYTHONUTF8=1` and re-exec once with `-X utf8`, so stdio and `open()` are both
  UTF-8 for the whole process tree. Re-exec is bounded to once (sentinel; cannot
  loop) and is a no-op when already in UTF-8 mode. Shared resolver added as
  `m3_sdk.ensure_utf8`.

## [2026.5.29.5] — May 29, 2026 — Files entity-linking fix

### Fixed

- **File fact-extraction now links entities into the core memory DB.** The
  entity linker read its DB path from `M3_DATABASE`, which during ingest points
  at the *files* DB (`files_database.db`) — so it looked for the `entities`
  table there ("no such table: entities") and never populated
  `fact_entity_refs`. Entities live in the core store (`agent_memory.db`) by
  design (facts in files.db, entities in memory.db, linked via
  `fact_entity_refs`). Added `config.memory_db_path()` resolving the core DB
  independently of `M3_DATABASE` (`M3_MEMORY_DB` override, else the m3_sdk core
  default). Verified live: refs populate, existing entities matched, unknowns
  created provisional per the resolution policy.

## [2026.5.29.4] — May 29, 2026 — Files fact-extraction fix + docs

### Fixed

- **File fact-extraction now works against an auth-enabled LM Studio.** The
  files-memory LLM extractor sent no `Authorization` header, so an auth-enabled
  endpoint (LM Studio's default) silently produced zero facts; and it sent
  `response_format={"type":"json_object"}`, which some builds reject with HTTP
  400. Added a shared `config.llm_auth_headers()` (reads `LM_API_TOKEN`, empty
  when unset so tokenless endpoints keep working) wired into extract / summarize
  / carry-forward, and dropped the unsupported `response_format` hint. Verified
  end-to-end producing well-formed atomic facts.

### Documentation

- **Documented how to enable fact extraction** (`FILES_MEMORY.md` → "Enabling
  fact extraction"): endpoint env vars (`M3_FILES_EXTRACT_URL` /
  `M3_FILES_EXTRACT_MODEL` / summary + `M3_LMSTUDIO_URL` fallbacks), the
  `LM_API_TOKEN` auth requirement, `extract_mode` none/inline/queue, and a
  verified queue→drain example. Added the `M3_FILES_*` vars to
  `ENVIRONMENT_VARIABLES.md`.
- **Fixed the Files Memory Quick Start** CLI invocation (`PYTHONPATH=bin`).

## [2026.5.29.3] — May 29, 2026 — Fix Windows installer crash

### Fixed

- **`install_os.py` crashed on Windows** with `UnicodeEncodeError` when the
  console code page is cp1252 (the default): the banner prints a rocket emoji
  that cp1252 can't encode, aborting the post-install OS-setup step. Force the
  stdio streams onto UTF-8 at module load (same `reconfigure(..., errors=
  "backslashreplace")` guard the `m3` CLI already uses), so the installer runs
  cleanly regardless of console code page. Caught by a full clean-room
  `pip install` → `install-m3` test of v2026.5.29.2.

## [2026.5.29.2] — May 29, 2026 — Tool dispatcher + human CLI + CVE bumps

Two new always-on tools let agents reach the whole catalog without paying for
every schema at startup, a generated `m3 <domain> <tool>` human CLI surface,
a generated manifest + drift test that keep the documented tool count honest,
and dependency CVE remediation.

### Security — dependency CVE remediation

- **`urllib3` → `>=2.7.0`** (clears PYSEC-2026-141/142) and **`transformers`
  pinned `>=4.53.0,<5`** (clears 18 of 20 known CVEs). Ceiling is `<5` because
  transformers 5.x removed `is_torch_fx_available`, which FlagEmbedding imports
  — verified to break the embedder on 5.9.0. Two CVEs remain accepted +
  documented (CVE-2026-1839 needs 5.x; PYSEC-2025-217 has no fixed version) —
  both require loading a malicious checkpoint, and m3 only runs embedding
  inference on its own trusted local model, so neither is reachable.

### Added — `m3_call` / `m3_index` dispatcher

- **`m3_call`** invokes any catalog tool by name without loading its domain —
  the low-token path to the full surface. Supports `batch` (a list of
  `{tool, args}`, each isolated, capped at 100) and `dry_run` (validate args +
  check the destructive gate without executing). Destructive tools still
  require `MCP_PROXY_ALLOW_DESTRUCTIVE=1`.
- **`m3_index`** lists the catalog (optionally one domain) as structured rows —
  name, domain, one-line summary, destructive flag, and arg specs — so an agent
  can discover a tool's signature before calling it. Read-only metadata.
- Both join the always-registered **essentials** set, so the dispatcher is
  reachable in every session alongside `tools_list_domains` /
  `tools_load_domain`. Agents no longer need to fall back to raw `sqlite3` or
  Bash to touch a non-essential tool.

### Added — generated tool-catalog manifest

- **`bin/gen_tool_manifest.py`** emits `docs/tools/MCP_CATALOG.json` from
  `mcp_tool_catalog.TOOLS` — per-tool name, domain, summary, destructive flag,
  and arg specs (the universal `database` arg excluded), plus a top-level
  `count` of non-meta tools. Output is deterministic (sorted, `indent=2`) so
  re-running produces no spurious diff.
- **`tests/test_tool_count_drift.py`** asserts the manifest `count`, the live
  catalog count, and every hardcoded "N tools" claim in the public docs all
  agree — so a catalog change that forgets to update the docs fails the build.

### Changed

- Catalog total is now **96 tools**; README / `COMPARISON.md` /
  `MYTHS_AND_FACTS.md` / `docs/tools/files_memory.md` updated to match, and
  `docs/MCP_TOOLS.md` + `docs/API_REFERENCE.md` document the dispatcher.

### Added — generated `m3 <domain> <tool>` human CLI surface

- The `m3` CLI now generates a subcommand for every catalog tool, grouped by
  domain: `m3 files <tool>`, `m3 memory <tool>`, `m3 entity/agent/tasks/admin/
  conversations/diagnostics <tool>`. The dispatch runs through the same
  `execute_tool_structured` path as `m3_call`, so the human CLI and the agent
  surface cannot drift. Flat-arg tools get one `--<prop>` flag each (booleans
  via `--flag/--no-flag`); the few structured-arg tools take a single
  `--json OBJ` blob. Every tool subcommand also accepts `--database`,
  `--dry-run` (validate + gate-check without executing), and `--yes` (required
  to run a destructive tool).
- The chatlog domain is reached as **`m3 chat <tool>`** (e.g.
  `m3 chat chatlog_search`), because top-level `m3 chatlog` is the pre-existing
  operational command wired into `hooks.json`. `m3 chat` also carries the
  operational `init` / `status` / `doctor` / `hook-path` subcommands, so it is
  the single chatlog namespace.
- **`m3 chatlog <init|status|doctor|hook-path>`** remains a back-compat alias —
  existing hooks and install guides are unaffected.

---

## [2026.5.18.1] — May 18, 2026 — Security: harden content-safety regex (CodeQL #29)

**Security fix.** The content-safety filter on `memory_write` had two issues
introduced by the Phase 7/8 modularization (924d6d3): a regex that missed
`<script\n>` / `<script\t>` / `<script foo='bar'>` style bypasses (CodeQL
`py/bad-tag-filter` alert #29), and a duplicate definition that let the two
copies drift in pattern coverage.

- `bin/memory/util.py` — `<script.*?>` → `<script\b`; ported the full pattern
  set (SQL DDL, `eval`/`exec`/`__import__`, prompt-injection phrases) that
  previously lived only in `memory_core.py`.
- `bin/memory_core.py` — duplicate `_POISON_PATTERNS` + `_check_content_safety`
  collapsed into a re-export from `memory.util`. One source of truth.
- `tests/test_content_safety.py` — import switched to `memory.util` (the
  actual runtime path used by `memory.write`); added the 5 CodeQL bypass
  cases plus a `test_single_source_of_truth` guard that fails fast if the
  two import paths ever diverge again.

No API or behavior change for benign content. Malicious content that
previously slipped past via the newline/tab/attribute bypass is now rejected.

---

## [2026.5.18.0] — May 18, 2026 — Files-memory, Project Oxidation, modular core, one-command setup

The largest release since launch. Two new memory surfaces (Files-Memory and the deterministic Curator), a Rust compute core that lands measurable speedups on the hot path, a fully modularized `memory_core`, an 85% reduction in startup tool-catalog size via domain-gated lazy loading, and a one-command `m3 setup` wizard that wires m3 into Claude Code, Gemini CLI, OpenCode, and the OpenClaw proxy in a single step.

### Added — Files-Memory: ingest, watch, and ascend whole corpora

A new first-class subsystem (`bin/files_memory/`, 21 MCP tools) for memory that originates from files rather than chat turns. Lives in its own `files.db` alongside `memory.db` with explicit promotion paths between them.

- **Phase 1 — walker + hybrid search.** Corpus walker with per-format chunkers (markdown, PDF, text), schema, FTS5 + vector hybrid search, and a 22-question fixed-corpus eval harness as the regression gate.
- **Phase 2 — extraction + ascension.** Per-chunk observation extraction; "ascension" promotes high-signal chunks into the main memory store. Staleness review keeps the file index honest as files change on disk.
- **Phase 3 — provenance, carry-forward, dedup, rename detection, promotability scoring.** Files moved or renamed retain their ingest history; near-duplicate content is collapsed; only chunks that score above the promotability threshold are eligible for ascension.
- **Phase 4 — watch daemon + multi-corpus.** Persistent watcher reconciles edits incrementally; multiple corpora can coexist with independent configs. A five-smoke acceptance harness exercises the full pipeline (walk → extract → ascend → reconcile → search).
- **21 MCP tools** added to the catalog under the `files` domain, covering ingest, search, corpus management, watch control, and the ascension lifecycle.

### Added — Project Oxidation: opt-in Rust compute core

Hot-path numerical operations now have a Rust implementation in [`m3-core-rs`](https://github.com/skynetcmd/m3-core-rs). The core is installed manually until wheels reach PyPI: `pip install "m3-core-rs @ git+https://github.com/skynetcmd/m3-core-rs.git@v0.9.0#subdirectory=crates/m3-core-py"` (needs Rust ≥1.94 + maturin). Python remains the default path; `M3_CORE_RS_DISABLE` forces it back even when installed.

- **In-process llama.cpp embeddings** routed through `m3_core_rs` for the embed path; tuned httpx client halves CPU-fallback p95 latency on the HTTP path.
- **`memory_dedup` Rust hot path** — 40× speedup on a 1,000-row scan.
- **MMR rerank** uses the Rust packed-bytes path with zero-unpack; cosine and batch cosine routed through `m3_core_rs`.
- **Scrub** (chatlog redaction) and the auto-route shadow comparator are both Rust-backed behind kill-switches.
- **Per-backend circuit breakers** in the embed cascade; typed embed exceptions; chunked cache lookup; `@lru_cache` on `_content_hash` and `_query_title_token_set`.
- Pinned to `m3-core-rs v0.9.0`. Parity harnesses (cosine, MMR, policy-aware MMR) keep Python and Rust outputs bit-for-bit equivalent inside the swap gate.

### Added — `m3 setup`: one-command wizard

`m3 setup` (in `m3_memory/setup_wizard.py`) probes for installed agents and wires m3 into each in a single guided run.

- **Auto-detection** of Claude Code, Gemini CLI, OpenCode, and OpenClaw on PATH (plus the npm-global fallback for Gemini and OpenClaw). Each detected agent defaults to ON.
- **OpenClaw** has no native MCP, so detection drives the proxy default: present `openclaw` CLI, `~/.npm-global/bin/openclaw`, `~/.openclaw/` workspace, or an `OPENCLAW_GATEWAY_TOKEN` env var all flip the proxy prompt (`localhost:9000`) to default-ON.
- **Sovereign baseline embedder** (BGE-M3 CPU on port 8082) installs unconditionally — works with no LM Studio, no Ollama, no GPU, no internet.
- **Optional GPU embedder** auto-detects CUDA / Vulkan / Metal and builds the in-process accelerator from `m3-core-rs`.
- Chatlog capture mode (`both` / `stop` / `precompact` / `none`) is selected once and threaded through to every agent's hook config.
- Every interactive prompt has a `--flag` equivalent, so `install.sh` / `install.ps1` drive the same logic with `--non-interactive`.

### Added — Domain-gated lazy tool loading

The MCP startup tool catalog now ships with a minimal core set; specialist domains load on first reference.

- **Startup catalog: 16K → 2.4K tokens** (~85% reduction). Models see the small, always-loaded surface; the long tail of tools is described by domain name only until needed.
- **`tools_list_domains` / `tools_load_domain`** discover and pull a domain in one call.
- Domain boundaries documented in `bin/tool_domains.py`. Files-memory's 21 tools, the entity-graph tools, and the chatlog tools all live behind their domain gates.

### Added — Deterministic Curator (one-call apply)

Curation is no longer a multi-step "survey then apply" dance.

- **`curator_apply` module** plus two MCP tools (`memory_dedup` apply variant and the chatlog dedup apply) execute a full curation pass in a single call with deterministic ordering.
- **`bin/chatlog_decay.py`** — deterministic ephemeral-content decay independent of LLM-driven curation.
- **`m3:memory-curator` and `m3:chatlog-curator` subagents** (renamed from `curate-{memory,chatlog}` to verb-subject form). Both emit per-call APPLY heartbeats so progress is visible during long passes, with bounded tool calls per pass to keep them tractable.
- **Bulk MCP variants** for `memory_link` and `memory_update` cut curator wallclock on large passes.
- **`memory_delete_bulk`** for curation passes that need to drop many rows in one transaction.

### Changed — `memory_core` fully modularized

The monolithic `bin/memory_core.py` is now a thin facade over a package of focused modules. Behavior is preserved; every phase shipped with its own regression baseline.

- **Phase 1–2** — config, util, FTS5 helpers, and DB primitives extracted into `bin/memory/{config,util,fts,db}.py`.
- **Phase 3** — embed pipeline → `bin/memory/embed.py`.
- **Phase 4.A** — Chroma federation → `bin/memory/chroma.py`.
- **Phase 4.B** — scoring helpers, query routing, ranker post-processing, reranker, route helpers, and four retrieval implementations split out across `search.py` and friends.
- **Phase 6** — entity graph → `bin/memory/entity.py`, with a full per-tool doc set.
- **Phase 7–8** — emitters, graph, and write isolation → `emitters.py`, `graph.py`, `write.py`.
- **Submodules now honor `memory_core` monkeypatches**, so tests that swap embedders or fakes at the top-level still work end-to-end.

### Added — Retrieval quality and observability

- **Sliding-window chunking** for long passages, with dense-content recovery on the write path.
- **Scale-aware elbow trim** with retrieval telemetry plumbing.
- **Entity-graph seed/frontier stoplist** for persona/role tokens — prevents the graph from over-expanding on generic identity terms.
- **Expansion-displacement guard** with per-tool toggles, so expansion rows can't displace high-confidence top results.
- **Auto-related-link candidates** are now scoped to the same variant, eliminating cross-corpus link drift.
- **MCP tool inventory** regenerated to reflect the modular structure; CodeQL-clean across the new module boundary.

### Added — Onboarding and install polish

- **Sovereign embedder by default** in `m3 setup` — no LM Studio dependency for first-run users.
- **Smoother fresh-install path** in `bin/install*` covering embedder and main DB setup.
- **Domain-gated tool callout** in install docs, quickstarts, plugin docs, connector docs, and `SOVEREIGN_DEPLOYMENT.md` — operators see the lazy-loading model up front.
- **`m3 setup` migration** across 13 entry-point docs (QUICKSTART, GETTING_STARTED, plugin, connector, install_* family) so every install path leads to the wizard.

### Added — Documentation

- **`EMBED_INPUT_RECIPE.md`** — operator recipe for the input-side embed pipeline.
- **`EMBED_DEPLOYMENT.md`** — deployment guide including Windows build-tools bootstrap.
- **`FILE_INGESTION_PLAN.md`** — files.db architecture and the ascension design.
- **`ARCHITECTURE.md`** refreshed for the modularized `memory_core`.
- **Project Oxidation env vars** documented end-to-end; env-var reconcile report covers the M3_* surface plus non-prefixed and auth groups.

### Fixed

- **Scheduler console-window flashes eliminated** on Windows; cross-OS single-instance guard added for the cognitive loop.
- **Credential-store subprocess** no longer pops a console window.
- **FTS5-only retrieval** uses an OR-style query with a soft-fail fallback so empty FTS results don't kill a hybrid search.
- **MMR rerank** no longer collapses to a no-op when candidate vectors are missing from the lookup.
- **`mention_offset` threading** in the entity-link write path.
- **`_ensure_sync_tables` fast-path** handles TEXT-affinity `schema_versions` correctly.
- **`migrate_memory.py`** coerces `schema_versions.version` to int and skips non-numeric markers — recovers older deployments that wrote string markers.
- **`_OXIDATION_DISABLED`** is now safe to import from any path, preventing a circular-import edge case on cold start.
- **Chatlog curator dedup** routes at the chatlog DB rather than the main DB (regression caught by the 2026-05-17 curator pass).
- **Two memory bugs** surfaced by the 2026-05-17 curator pass: routed-expansion defaults retuned; temporal patterns broadened so day-of-week and relative-date queries hit consistently.

### Performance

- **Retrieval hot path vectorized**; AUTO overshoot pool is reused across phases.
- **MMR Python fallback** capped on iteration count to prevent hangs on very large pools.
- **Phase 11 supersede demotion** moved post-reranker so demoted rows can't pollute the rerank input set.
- **Reranking knobs exposed** for gating expansion-row displacement at top ranks.

### Schema

- **`files.db`** — new database for Files-Memory (walker state, chunks, provenance, promotability scores, ascension links).
- **Entity-graph stoplist** persisted; expansion-displacement margin default raised to 2.0×.

### Notes

- All CI checks (Lint, Mypy, Bandit + pip-audit, ubuntu/macos/windows tests) pass on the bump.
- The `oxidation` extra requires a Rust toolchain (≥1.94) and `maturin`; without it, m3 runs entirely on the Python path with no functional gaps.
- `m3-core-rs` source lives at `github.com/skynetcmd/m3-core-rs`, pinned to `v0.9.0`.

---

## [2026.5.6.3] — May 7, 2026 — GH Actions Node-24 upgrade + banner refresh

Pure infrastructure + asset bump; no library behavior changes.

### Changed

- **GitHub Actions pinned to Node-24-compatible SHAs.** GitHub announced Node.js 20 deprecation: default flips to Node 24 on 2026-06-02; Node 20 removed from runners 2026-09-16. Every workflow action bumped to its latest release-tag SHA so CI keeps working past the removal:

  | action | from | to |
  |---|---|---|
  | `actions/checkout` | v4 | v6.0.2 |
  | `actions/setup-python` | v5 | v6.2.0 |
  | `actions/upload-artifact` | v4 | v7.0.1 |
  | `actions/download-artifact` | v4 | v8.0.1 |

  Major-version bumps in the artifact actions verified safe for our usage: `upload-artifact` v7's optional unzipped uploads aren't enabled (we don't set `archive: false`); `download-artifact` v5's path-behavior fix doesn't affect us (we download by name into a fixed path); `download-artifact` v8's hash-mismatch-errors-by-default is desirable and our pipeline doesn't rely on partial downloads.

- **`docs/M3-banner.jpg`** — replaced with new banner art. README hero image only.

All CI checks (Lint, Mypy, Bandit + pip-audit, ubuntu/macos/windows tests) pass on the bump.

---

## [2026.5.6.2] — May 6, 2026 — Harden Gemini endpoint check (CodeQL #27, #28)

### Fixed

- **`bin/unified_ai.py:37` and `bin/batch_runner.py:540`** — `py/incomplete-url-substring-sanitization` (CodeQL alerts #27 and #28). Replaces `"generativelanguage.googleapis.com" in url` substring tests with hostname parsing via `urlparse`, centralized on `unified_ai._is_gemini_endpoint`. Both call sites are config-driven dispatch decisions (pick the hardened httpx client for Gemini's OAI-compat hang workaround; pick the Gemini batch runner) sourced from our own YAML config — neither was a real SSRF or open-redirect risk. The parsed form is more correct anyway (catches edge cases where the hostname appears in a query string or as a look-alike suffix) and silences the CodeQL warnings.

  Smoke-tested with 8 cases including the CodeQL bypass shape (`http://evil.com/?x=generativelanguage.googleapis.com` → `False`) and the look-alike suffix attack (`evil-generativelanguage.googleapis.com.attacker.io` → `False`). Both alerts auto-closed by GitHub's post-merge CodeQL re-scan.

- **CI debt cleared.** The same PR that fixed the CodeQL alerts also cleaned up errors that had accumulated since the FIPS-readiness merge:
  - **ruff:** 82 → 0 (auto-fix + manual fixes for missing imports — `http.client` in `setup_embedder.py`, `subprocess` in `m3_memory/cli.py` — and a truncated `ASSETS` dict in `fetch_sovereign_assets.py` that had been replaced with a `# ...` placeholder).
  - **mypy:** 4 → 0 (Path/str rebind in test fixture; Windows-only `subprocess.CREATE_NEW_PROCESS_GROUP` fetched via `getattr` for cross-platform safety).
  - **bandit:** 3 → 0 (B113 timeout added on `requests.get`; B103/B105 annotated `# nosec` with justification on cases that aren't credentials).
  - **tests:** `tests/test_m3_enrich.py` assertion aligned with the `qwen/qwen3-8b → qwen/qwen3.5-9b` config bump.

### Added

- **`m3_memory/installer.py` `_prompt_cognitive_loop`** — passthrough stub for the `--cognitive-loop` install flag. Reserves the prompt site for when the cognitive-loop install path lands; today returns the flag verbatim.

---

## [2026.5.6.1] — May 6, 2026 — Bound WAL growth, centralize SQLite pragma stack

### Fixed

- **Unbounded WAL growth on long-running write workloads.** `m3_sdk.py`'s connection pool never set `wal_autocheckpoint` or `journal_size_limit`. SQLite's default `wal_autocheckpoint` (1000 pages, ~4 MiB) fires only in PASSIVE mode and busy-fails against active readers, after which SQLite silently lets the WAL grow. Without `journal_size_limit`, the WAL is never truncated even after a successful checkpoint. Result: a long-running enrichment workload could accumulate a ~15 GB `*-wal` file alongside a much smaller main DB.

### Added

- **`bin/sqlite_pragmas.py`** — single source of truth for pragma stacks. Three profiles (`production`, `chatlog`, `bench`) with a `profile_for_db` selector. Universal pragmas across all profiles: `wal_autocheckpoint`, `journal_size_limit`, `temp_store`, `foreign_keys`, `busy_timeout`. Two helper functions for runtime checkpoint discipline: `checkpoint_passive` and `checkpoint_truncate`.
- **`bin/test_sqlite_pragmas.py`** — regression test asserting the WAL stays under `journal_size_limit` under sustained writes across all three profiles.

### Changed

- `m3_sdk.py`'s connection pool now sources its pragmas from `sqlite_pragmas.py`. `agent_memory.db` gains `wal_autocheckpoint=2000` + `journal_size_limit=64MiB` it didn't have; cache and mmap settings are unchanged.
- `chatlog_config.py` sources its pragma values from `sqlite_pragmas.py` (chatlog profile). Pragma values are bit-for-bit identical to before; the change is structural, not behavioral.
- 7 ad-hoc inline pragma blocks across `bin/` replaced with calls to the shared helper.

### Documentation

- WAL discipline notes added to `docs/AGENT_INSTRUCTIONS.md` and `docs/CONTRIBUTING.md`: never `rm` a `-wal`/`-shm` file; if WAL is huge, run `PRAGMA wal_checkpoint(TRUNCATE)`.

---

## [2026.5.4.6] — May 4, 2026 — Hardened inventory scanner + boot-start improvements

### Changed

- **`bin/gen_tool_inventory.py`** hardened: now captures both reads and writes to `os.environ`; source scan expanded to include the `m3_memory` package; missing load-bearing modules added (`enrichment_state`, `thermal_utils`, etc.).
- **Cognitive-loop boot persistence.** `AgentOS_CognitiveLoop` now uses `ONSTART` on Windows for continuous background execution.
- Final documentation audit: 90+ internal modules and CLI tools have up-to-date documentation in `docs/tools/`.

---

## [2026.5.4.5] — May 4, 2026 — Autonomous Cognitive Loop + multi-DB hardening

### Added

- **`bin/m3_cognitive_loop.py`** — unified background heartbeat that automates entity extraction, fact enrichment (Observer), and consistency management (Reflector).
  - Resource-optimized: SQL-based "has work" detection skips redundant AI calls when idle.
  - Robust: PID-based single-instance locking and signal-aware graceful shutdown.
  - Fire-and-forget: `--background` flag for self-daemonization.
- **`mcp-memory install-m3 --cognitive-loop`** — interactive onboarding for the cognitive loop during installation.

### Changed

- **`bin/migrate_memory.py`** uses absolute paths and case-insensitive matching (Windows) for reliable split-DB detection across worktrees.

### Schema

- **Migration 004** — adds Entity Graph tables to chatlog databases for consistent tracking across the chatlog and main memory DBs.

---

## [2026.5.4.7] — May 3, 2026 — Embedder URL override across enrichment workers

Closes a routing gap that caused observation embeds during ingest to fall
through to the default discovery (which prefers a 1-slot LM Studio
endpoint at :1234) even when the operator wanted to pin all worker
embeds to a multi-slot llama.cpp server. Under multi-worker parallel
ingest the 1-slot path becomes the bottleneck — symptoms were visible
as periodic high-volume traffic to LMS that nothing in the foreground
flow seemed to be issuing.

### Added

- **`bin/m3_enrich.py --embed-url / --embed-model`** — flag parity with
  `bin/m3_enrich_batch.py` and `bin/m3_entities.py`. When set, the live
  worker exports the env var (so any subprocess that re-imports
  `memory_core` picks it up) and calls `memory_core.set_embed_override`
  (so the already-imported module's resolved `_EMBED_URL_OVERRIDE` is
  updated in-process). Default is `os.environ.get("M3_EMBED_URL")`.

### Fixed

- **`bin/memory_core._embed_many`** — now honors `_EMBED_URL_OVERRIDE`
  the same way `_embed()` (singular) already did. Prior to this fix the
  bulk embed path silently fell through to `get_best_embed` discovery
  even when an override was set, which is the path most ingest writes
  flow through. Effect: with `--embed-url` set, all embeds (singular and
  bulk) now route to the chosen endpoint instead of being split between
  override and discovery.

### Notes

- `bin/m3_enrich_batch_parallel.py` accepts and forwards `--embed-url` /
  `--embed-model` to every worker it spawns (already wired in 2026.5.4.6
  but only effective end-to-end after the `_embed_many` fix above).
- Operator guidance unchanged: set `--embed-url http://127.0.0.1:8081/v1`
  for the multi-slot llama.cpp path during multi-worker bench ingest;
  leave unset to use the default discovery.

---

## [2026.5.4.1] — May 4, 2026 — Provider-neutral batch enrichment + reliability fixes

Adds a batch-API enrichment path (50% off list pricing in exchange for
async wallclock) with provider-neutral abstraction, plus several
hardness/correctness fixes to the live enrichment worker.

### Added

- **`bin/batch_runner.py`** — provider-neutral batch interface
  (`BatchRunner` Protocol + `BatchRequest` / `BatchResult` / `BatchUsage`
  dataclasses). Two implementations: `AnthropicBatchRunner` (native
  `/v1/messages/batches`, ephemeral cache_control supported) and a
  Gemini Developer API implementation that uses the inline-batch path
  on `models/<model>:batchGenerateContent` (no Cloud Storage upload
  required). Factory `make_runner(profile, token=...)` dispatches on
  `profile.backend` + URL host. Shared `run_to_completion_chunked`
  helper auto-splits requests by `runner.max_batch_size`.
- **`bin/m3_enrich_batch.py`** — async batch worker. Wraps a runner with
  the same `enrichment_groups` claim/finalize state machine as the live
  worker, ingests results into `memory_items` via `run_observer.write_observation`.
  CLI mirrors `m3_enrich.py`: `--profile`, `--core-db`, `--source-variant`,
  `--target-variant`, `--source-conv-list`, plus batch-specific flags
  (`--slice-size`, `--budget-usd`, `--poll-interval-s`, `--max-wait-s`,
  `--resume-run`).
- **`--resume-run <enrichment_runs.id>`** — pick up where a crashed
  worker left off. Reads `notes.batches` (a structured array of
  `{slice_idx, batch_id, ingested}` entries persisted as the worker
  submits each slice), polls any non-ingested batches, fetches results,
  and ingests against the in-progress claims still under that run_id.
  Doesn't re-submit to the provider.
- **`--budget-usd <N>`** — hard cap on cumulative cost. Worker checks
  after each slice's ingest; if cumulative cost ≥ cap, releases the
  remaining unsubmitted claims, finalizes the run as `aborted` with
  `abort_reason=budget_cap_$<N>`, returns exit code 3.
- **`bin/release_orphan_claims.py`** — safe cleanup utility for
  `enrichment_groups` rows stuck in `in_progress` after a crash. Three
  filter modes (`--run-id`, `--older-than <min>`, `--all`), plus
  `--dry-run` preview and `--skip-qps-done` defensive flag (the latter
  excludes rows whose `question_pipeline_state` is already terminal —
  prevents a reverse-drift class of bugs where releasing a claim
  re-flags an already-complete question as incomplete).

### Changed

- **`_query_eligible_groups` enumeration phase: 40-43× faster.**
  When `conv_filter` is provided (the common batch-worker case), the
  filter is now pushed to SQL via a chunked `IN`-list (800 group_keys
  per chunk to stay under SQLite's default `SQLITE_MAX_VARIABLE_NUMBER`).
  A 3-conv-filtered enumeration on a 50GB DB went from 128s to 3s; a
  6,490-conv-filtered enumeration went to 27s. The unfiltered path is
  unchanged. Each group's turns are sorted post-load (by `turn_index,
  created_at`) since the chunked path can return rows interleaved
  across chunks.
- **Hardened submit-failure cleanup in `m3_enrich_batch.py`.** When a
  later-slice submit raises, the worker now releases claims under
  *both* the placeholder enrich_run_id and the real enrich_run_id, plus
  finalizes the run row with the right partial counts. Prior code only
  released placeholder claims, leaving real-run-id claims orphaned and
  forcing manual SQL cleanup.
- **`_reap_stale_runs` startup pass.** On every batch-worker startup,
  rows in `enrichment_runs` with `status='running'` and no `finished_at`
  older than 6 hours get marked as `aborted` with
  `abort_reason='stale_run_reaped'`. Cosmetic but keeps audit clean
  after crashed-worker incidents.
- **`enrichment_runs.notes` schema upgraded to structured form.** Was
  a free-form JSON blob with one `batch_id` field. Now:
  `{n_groups_submitted, slice_size, batches: [{slice_idx, batch_id,
  ingested}, ...]}`. Read/write via `_read_run_notes` / `_write_run_notes`
  helpers; per-slice transitions tracked via `_record_batch_submitted`
  / `_record_batch_ingested`. Required for `--resume-run` to know which
  batches still need ingestion.
- **`call_observer` test signature updated.** The function returns
  `(observations, usage_meta)` since the cost-tracking work landed in
  `2026.5.3.3`; two unit tests in `tests/test_observer.py` were still
  asserting the old single-value return shape.
- **Entity-vocab unit test caught up to v2 vocabulary.** Migration
  `concept→legacy_concept` and `object→legacy_object` happened
  pre-2026.5.3.3 but the assert-list in
  `tests/test_entity_graph.py::test_type_enum_validates_known` still
  used the old names. Updated to assert against the v2-active names
  plus `legacy_*` aliases.

### Fixed

- **Reverse-drift bug in `question_pipeline_state` ↔ `enrichment_groups`
  sync.** A class of bug where releasing an `in_progress` claim
  unconditionally flips the row to `pending`, even when the row's
  question_pipeline_state was already terminal — the next forward sync
  then drags qps back to `pending`, causing previously-complete
  questions to drop from "100% done" rolls. Fixed two ways: the
  worker's submit-failure path now scopes its release to the run_id;
  the `release_orphan_claims.py` utility's `--skip-qps-done` flag
  excludes already-terminal rows via `NOT EXISTS` guard. Documented
  reverse-sync SQL pattern for any future raw cleanup.

### Notes

- Batch-mode cost validated end-to-end on an Anthropic Haiku run:
  $0.000237/kB observed on a 6,490-conv batch — ~50% of live-mode
  list pricing, exactly the documented batch-tier discount.
- Gemini batch path validated end-to-end: 598 success across two
  1000-request slices before hitting Tier-1's 3M-enqueued-tokens cap.
  Tier-1's per-batch real-shape ceiling is ~1,000 reqs at 5,600 input
  tokens each. Tier-2 ($100 cumulative spend + 3 days) lifts the cap
  to 400M tokens.
- Ruff, Bandit, Mypy clean on touched files; full test suite (464
  passed, 2 skipped) green.

---

## [2026.5.3.3] — May 3, 2026 — Cost tracking + embed sweepers + tool inventory drift

Patch release on top of 2026.5.3.2. Largest m3-memory patch this week —
real per-row cost tracking, two new general-purpose CLI tools, a
shared embed-loop helper, and a tool-inventory drift fix.

### Added

- **`bin/embed_backfill.py`** — sweeper for memory_items rows missing
  embeddings. Companion to the `M3_OBSERVER_NO_EMBED=1` ingest pattern
  (decouple write throughput from embedder throughput, fill embeddings
  asynchronously). Works on any m3-memory DB. Hardened: per-batch
  timeout, runtime cap, consecutive-fail abort, dim validation,
  oversize/empty skip, optional lockfile, `--id-prefix` sharding,
  `after_id` cursor advance so skipped rows don't reselect forever.
  21 tests.
- **`bin/backfill_content_hash.py`** — populates
  `memory_embeddings.content_hash` on legacy NULL rows so they become
  visible to the embed-cache lookup in `_embed` / `_embed_many`.
  Real-data run today on this machine: 65,900 rows fixed across the
  chatlog + main DBs with 0 errors. `--all-types` opt-in for cross-
  type backfill; `--augment-anchors` matches inline `_embed` behavior
  for non-chatlog types. 20 tests.
- **`bin/embed_sweep_lib.py`** — internal shared embed-loop helper.
  Both `embed_backfill.py` and `chatlog_embed_sweeper.py` now drive
  the same loop via callbacks (fetch / write / transform). Future
  hardening lands in one place; chatlog gained `after_id` cursor
  protection it previously lacked. 7 tests; the chatlog sweeper's
  `embed_batch` function is preserved as a thin compat wrapper for
  any external caller.
- **`bin/m3_enrich_assign.py`** — bulk-tags `enrichment_groups.send_to`
  for parallel multi-provider runs. Pair with `m3_enrich.py --send-to`
  (also new) to route disjoint subsets across providers by explicit
  assignment rather than bucket-bound disjointness.
- **Migration 031 — `enrichment_groups.send_to`** — adds the column
  + index that the `--send-to` routing relies on. Idempotent under
  `bin/migrate_memory.py`.
- **`--budget-usd` actually works on enrichment runs.** The schema
  and DB API supported it but the caller never populated
  `tokens_in / tokens_out / cost_usd`. Wired up: every successful and
  failed enrichment row now carries its own cost. Reads native
  `usage` from both openai-compat and anthropic backends; falls back
  to `profile.input_cost_per_mtok / output_cost_per_mtok` when the
  upstream API doesn't return cost natively.
- **Pricing fields on all 6 paid-cloud profiles** —
  `connect_xai_grok_4_fast`, `enrich_google_gemini`,
  `enrich_google_gemini_3_flash`,
  `enrich_google_gemini_3_flash_lite`, `enrich_anthropic_haiku`,
  `enrich_openai_gpt`. Per published list pricing (2026-04). Drives
  the budget watchdog and per-row cost provenance.

### Changed

- **`memory_search_multi_db` reclassified.** Was landing in
  "Uncategorized" in the auto-generated MCP tool inventory because
  `bin/gen_mcp_inventory.py`'s category map missed it on its
  introduction in `2026.5.3.1`. Now lives under "Memory Operations"
  alongside `memory_search` and `memory_search_routed`.
- **Tool count: 72 → 73 across all user-facing docs.**
  `memory_search_multi_db` (shipped in `2026.5.3.1`) was a real
  capability addition that the count-claim docs had missed. Updated:
  `pyproject.toml` PyPI description, `README.md` badge + 4 prose
  references, `examples/AGENT_RULES.md`, `docs/MYTHS_AND_FACTS.md`,
  `docs/COMPARISON.md` (3 refs), `docs/CORE_FEATURES.md` (2 refs),
  `docs/API_REFERENCE.md`, `docs/QUICKSTART.md` (2 refs),
  `docs/claude_ai_connector.md`. The auto-generated `docs/MCP_TOOLS.md`
  was regenerated; `bin/gen_mcp_inventory.py:EXPECTED_TOOL_COUNT`
  bumped to match.
- **`enrich_openai_gpt.yaml`** — bumped `input_max_chars` 6000 → 32000
  and `max_tokens` 1024 → 4096 (gpt-4o-mini supports 128K input /
  16K output, so well within limits). Tuned the system prompt to be
  less restrictive on hypothetical / project-discussion content;
  added explicit user-context inference guidance + worked examples
  so the model emits inferred user-facts for project conversations
  rather than returning empty.
- **`bin/m3_enrich.py` `--limit` semantics documented.** The flag
  fires at outer-cycle boundaries (not per-batch), so smoke tests
  using `--limit N` may overshoot by up to one cycle's fetch
  (`batch_size * concurrency * 4 = 4096` rows at defaults). Help
  text and helper docstring now state the boundary; for strict caps,
  pair with smaller `--batch-size` and `--concurrency`. No behavior
  change.
- **`bin/chatlog_embed_sweeper.py`** internally migrates to the
  shared `embed_sweep_lib`. Public surface (CLI flags, scheduled-job
  contract) unchanged. Behavior change: candidate selection now
  ORDERs BY id ASC instead of created_at ASC (UUIDs aren't
  time-ordered, so within-batch ordering shifts negligibly), gaining
  infinite-loop protection on skipped rows. Sweeper now also writes
  `content_hash` on new embedding rows.

### Fixed

- **Windows Unicode crash in `--resume` size-label** (already on
  2026.5.3.2; carried forward, no regression).
- **Tool inventory drift.** `memory_search_multi_db` was missing from
  the category map (landing in "Uncategorized") and `EXPECTED_TOOL_COUNT`
  was stuck at 72 even after the tool was added. The next inventory
  regeneration will be clean by construction.

### Security

- **Pre-release scan (DefectDojo engagement 30, commit `7072f7f`)**:
  0 active findings across gitleaks / trufflehog / trivy / bandit
  (33,225 LOC) / semgrep (147 rules) / pip-audit / checkov /
  osv-scanner. Beats the prior 0C/0H/1M/5L baseline (engagement 9).

---

## [2026.5.3.2] — May 3, 2026 — send_to routing + Windows Unicode fix

Patch release on top of 2026.5.3.1. Two changes:

### Added

- **`--send-to` routing for parallel multi-provider runs.** Migration
  031 adds `enrichment_groups.send_to TEXT` so the same source variant
  can be split across providers by explicit assignment instead of
  relying on bucket-bounds disjointness. New
  [`bin/m3_enrich_assign.py`](../bin/m3_enrich_assign.py) bulk-tags
  rows; `bin/m3_enrich.py --send-to <name>` claims only matching rows.
  Rows with `send_to IS NULL` are excluded in routed mode (NULL means
  unassigned, and a routed worker should not steal unassigned rows).
  Backwards compatible: when `--send-to` is omitted, the column is
  ignored entirely.

### Fixed

- **Windows Unicode crash on `--resume` size-label.** The infinity
  symbol (`U+221E`) in the resume-banner format string crashed Python
  on Windows when stdout was redirected to a file (cp1252 default
  encoding). Replaced with `"inf"`. Reproduced today launching an
  enricher with `--min-size-k` set and `--max-size-k` unset — the
  print path tripped `UnicodeEncodeError` before the work loop.

---

## [2026.5.3.1] — May 3, 2026 — Multi-variant search, entity-graph v2, xAI/Grok provider

This release lands real new capabilities on top of the May 1 enrichment
baseline: multi-variant retrieval (and a new `memory_search_multi_db`
MCP tool), the four-layer entity-graph vocabulary v2, an xAI/Grok
enrichment profile, the cross-platform schedule installer, and an
enricher rate-limit cascade guard that stops a run cleanly when an
upstream API is throttling instead of burning the retry budget.

### Added

- **`memory_search_multi_db` MCP tool** — searches across multiple
  M3 databases in a single call, scoring and merging hits by the same
  hybrid pipeline used in `memory_search`. The existing `memory_search`
  also gains a multi-variant filter (pass a list/tuple/set as
  `variant=` and it expands to `IN (?,?,...)` instead of `=`).
- **Entity-graph vocabulary v2** — four-layer model (provenance /
  stable / event / change) with 42 entity types and 34 predicates as
  the superset default for `memory_core`. Migration aligns existing
  m3 vocab with the v2 superset; narrower per-domain vocabs load via
  `M3_ENTITY_VOCAB_YAML`. New `config/lists/entity_graph_v2.yaml`
  (human-life narrow vocab, 11 types / 16 predicates) ships alongside.
- **xAI / Grok enrichment profile** —
  `config/slm/connect_xai_grok_4_fast.yaml` runs Grok 4.1 Fast
  (Non-Reasoning) against `api.x.ai`'s OpenAI-compatible endpoint.
  Drop-in alongside the existing local + Gemini + Anthropic profiles.
- **Schedule installer** — cross-platform installer for the
  background tasks (auto-enrich queue drain, chatlog backfill).
  Single `bin/install_schedules.py` covers Windows Task Scheduler
  and `cron`.
- **Enricher rate-limit cascade guard** — `m3_enrich.py` now detects
  when an upstream provider is returning HTTP 429 across most inflight
  requests and aborts the run cleanly with a `RATE LIMIT CASCADE`
  log line, preserving `pending` rows for `--resume` after the quota
  resets. Avoids burning each row's retry budget against a wall.
- **Per-50 progress + ISO-UTC timestamps** on `m3_enrich.py` and
  `run_observer.py` emitters so a tail-following monitor sees a
  steady cadence with comparable absolute timestamps across logs.
- **`fact_enriched` auto-classify** — adds the type to the LLM
  classifier's allowlist so enriched-fact writes route correctly
  instead of falling back to `note` (drift fix).
- **`benchmarks/locomo/README.md`** — public placeholder noting the
  LoCoMo audit is pending; results published when complete.

### Changed

- **HTML-rendered docs** (`COMPARISON_TABLE.html`,
  `COMPLIANCE_TABLE.html`) converted to native Markdown so GitHub
  renders them inline; htmlpreview-proxy links used for the
  remaining HTML artifacts. Removes the click-through and the
  dependency on a third-party rendering proxy.
- **README "Who this is for"** split into two distinct tables
  (current users vs adjacent personas) for clearer fit.
- **Comparison-table** — sticky section labels visible during
  horizontal scroll; always-show scrollbar so users on hidden-by-
  default OS scrollbar settings notice horizontal overflow.
- **Heading-spacing standard (option-B)** applied repo-wide to
  Markdown docs and the audit reports for consistent rendering.
- **`docs/AGENT_INSTRUCTIONS.md`** — new Rule 7 (entity lookups)
  cross-linked from `AGENT_RULES.md`. Tool count reference bumped
  from 66 → 72 to match v2026.5.1.1's MCP inventory.
- **bench-territory removed from main** — the LongMemEval / LoCoMo
  harnesses, plans, and run artifacts now live exclusively on the
  `private/lme` and `private/locomo` worktrees. Public main carries
  only the LME-S report and the LoCoMo placeholder.

### Fixed

- **Enricher 429 retry-budget bug** — HTTP 429 responses no longer
  consume a row's `attempts` counter, so transient throttling
  doesn't silently push rows into `dead_letter`. Counts toward the
  cascade detector instead.
- **Comparison table scrolling** — section labels stayed pinned but
  became hidden on horizontal scroll on narrow viewports; fixed.
- **README benchmarks link** — pointed at
  `benchmarks/longmemeval/README.md` (which lives only on
  `private/lme`); now points at the real
  `benchmarks/longmemeval/LME-S_Benchmarking_Report.md` that ships
  on main.
- **Windows install link** — broken anchor in the Windows install
  doc fixed.
- **CI greens:** `pip-audit` scoped to locally-installed deps
  (skips pip's own meta-CVE); mypy backlog cleared with
  `types-PyYAML` + B311 suppressions; ruff backlog cleared in
  `bin/` (two latent bugs fixed in passing); chatlog ingest fixture
  + fact-enriched schema + drain-queue env unblocked; migration
  002 + chatlog migration 003 + two flaky tests stabilized.
- **CLAUDE.md / GEMINI.md symlinks** converted to regular files so
  pytest collects them on Windows (symlinks across the worktree
  boundary tripped the collector).

---

## [2026.5.1.1] — May 1, 2026 — Enrichment pipeline matures + doc/security hardening

This release rolls up roughly five weeks of ingest-pipeline work plus a
documentation, hygiene, and security pass. The two prior `[Unreleased]`
sections (Phase D Observer/Reflector pipeline and cross-encoder rerank)
are part of this release; their detail is preserved below.

### Added

- **Tool count: 66 → 72.** Six new MCP tools landed during this wave,
  spanning entity-graph, fact enrichment, routed retrieval, and bulk
  ingest. Inventory regenerated; `docs/MCP_TOOLS.md` is the canonical
  list.
  - `entity_search`, `entity_get`, `extract_pending` — entity-graph
    extraction + retrieval
  - `enrich_pending` — drains the fact-enrichment queue
  - `memory_search_routed` — temporal-aware multi-signal routing
    with optional graph expansion and cross-encoder rerank (default
    off; matches `memory_search` byte-for-byte when both are off)
  - `memory_write_from_file` — bulk-import path that bypasses
    autoregressive decode latency for large memory bodies
- **`bin/m3_enrich.py` CLI** — first-class user-facing enrichment
  tool over the Observer/Reflector pipeline. Durable per-group state
  machine (`enrichment_groups`, `enrichment_runs` tables, migrations
  028–030); `--resume` / `--budget-usd` / `--sample` knobs;
  size-bounded resume via `--min-size-k` / `--max-size-k`;
  `--source-conv-list` for opt-in conversation slicing.
  See [`docs/M3_ENRICH_GUIDE.md`](M3_ENRICH_GUIDE.md).
- **Cloud SLM profiles** — `enrich_anthropic_haiku.yaml`,
  `enrich_google_gemini.yaml` (Gemini 2.5 Flash with
  `reasoning_effort=none` for ~3× latency reduction) plus preview
  profiles for Gemini 3 Flash / 3.1 Flash-Lite. Local profiles
  (`enrich_local_qwen.yaml`, `enrich_local_gemma.yaml`) remain the
  default; no spend unless you pick a cloud profile.
- **`bin/unified_ai.py`** — multi-provider chat client + hardened
  httpx helper. Auto-selected for Gemini's OAI-compat endpoint
  (HTTP/1.1 + keepalive disabled, the configuration Google support
  recommended to avoid a keep-alive hang we observed at high
  concurrency). LM Studio and Anthropic paths continue to use stock
  httpx so connection reuse benefits them.
- **One-command install.** `mcp-memory install-m3` wires
  `settings.json`, MCP config, hooks, and the chatlog subsystem in
  one shot. Auto-install on first `mcp-memory` run is also available
  for the truly hands-off path. Existing `pip install m3-memory` +
  manual MCP-config edit continues to work.
- **Multi-DB sync.** `bin/sync_all.py` is now manifest-driven, with
  `bin/pg_sync.py` refactored for multi-DB. Postgres warehouse
  migrations included for fleet deployments.
- **Documentation pass:**
  - [`docs/COMPARISON.md`](COMPARISON.md) — new "Where the cognition
    lives" framing section; honest table additions for
    multi-agent concurrent writes and cognition placement.
  - [`docs/M3_Comparison_Table.md`](M3_Comparison_Table.md) —
    rebranded sovereign-substrates comparison table; honest
    reordering, accurate MCP expansion, candor block on the LME-S
    accuracy gap.
  - [`docs/COMPLIANCE.md`](COMPLIANCE.md) +
    [`docs/M3_Compliance_FISMA.md`](M3_Compliance_FISMA.md) +
    [`docs/M3_Compliance_CMMC.md`](M3_Compliance_CMMC.md) —
    framework alignment notes (FISMA / NIST 800-53, CMMC 2.0 /
    NIST 800-171).
  - [`docs/HOMELAB_PATTERNS.md`](HOMELAB_PATTERNS.md) — three
    deployment patterns + hardware sizing + multi-agent guidance.
  - [`docs/MYTHS_AND_FACTS.md`](MYTHS_AND_FACTS.md) — Anti-FAQ that
    answers AI-hallucinated claims about M3 with the truth, anchored
    to source code.
  - [`docs/audits/`](audits/) — dated security-scan reports;
    [`security-scan-2026-05-01.md`](audits/security-scan-2026-05-01.md)
    is the first.

### Changed

- **CI: new `security` job.** Runs Bandit on every push + `pip-audit
  --strict` against M3 installed from `pyproject.toml` into a clean
  venv (core deps only — no `[dev]`, no opt-in rerank path). Gates
  merges on shipped-library CVEs without false-alarming on bench/dev
  transitives.
- **Process-global LLM endpoint caching** in `bin/llm_failover.py`.
  Both `get_best_llm` and `get_smallest_llm` cache their first
  successful discovery; `clear_failover_caches()` invalidates on
  persistent failure. Saves the GET /v1/models roundtrip on every
  call (~9s with discovery vs ~50ms direct on warm LM Studio).
- **Pre-compiled regex** for two per-LLM-response hot paths: the
  markdown code-fence stripper (de-duplicated across `run_observer`,
  `run_reflector`, and `m3_entities` — single source of truth in
  `agent_protocol.strip_code_fences`) and the UUID extractor in
  `run_observer.write_observation`. Follows the 2026-04-17 regex
  precompile decision criteria (per-item-loop = compile, cold path =
  leave inline).
- **`memory_get` accepts an 8-char ID prefix** in addition to the
  full UUID; ambiguous prefixes return an error listing matches.
- **Memory-type vocabulary widened** with 10 inventory + scoping
  types (`home_automation`, `infrastructure`, `linux_only`,
  `local_device`, `macos_only`, `migration-log`, `network_config`,
  `security`, `to_do`, `windows_only`). Existing types unchanged.

### Fixed

- **Windows Unicode in `print()` was crashing multi-chunk
  enrichment runs silently.** A `→` arrow in
  `bin/run_observer.py`'s chunking message raised `UnicodeEncodeError`
  on cp1252-default Windows stdout; the exception got swallowed by
  `asyncio.gather(return_exceptions=True)`, and runs reported
  `0 processed / 0 written / 0 failed` with no diagnostic. Replaced
  with ASCII; bug only surfaced on conversations that chunked into
  more than one piece (intermittent and easily missed).
- Internal references (memory IDs, sweep dates, experiment names)
  scrubbed from public MCP tool descriptions in
  `bin/mcp_tool_catalog.py`. Substantive guidance preserved.
- Several `m3_enrich` bugs found by the first large-corpus run:
  group-scoped counters, partial-failure tracking via
  `enrichment_groups.partial_failure_chunks`, real exception
  messages preserved on chunk failures (instead of generic placeholder).

### Security

- 2026-05-01 scan results: 0 HIGH, 0 MEDIUM Bandit findings; no
  secrets in tree; 14 CVEs flagged by pip-audit, all in
  bench/dev-only transitive deps (none in shipped library
  dependencies). Full report:
  [`docs/audits/security-scan-2026-05-01.md`](audits/security-scan-2026-05-01.md).

### Honest acknowledgment

This release wave shipped fast (≈58 commits since v2026.4.24.12) and
the enrichment pipeline matured rapidly under live-fire conditions.
The core (storage, retrieval, GDPR, MCP tools, sync) is stable and
covered by the test suite. The newer enrichment + reflector pipeline
is production-ready for personal, homelab, and multi-agent developer
workflows; for regulated workloads, do your own evaluation against
your specific use case. See
[`docs/MYTHS_AND_FACTS.md`](MYTHS_AND_FACTS.md) for what we *don't*
claim.

---

## Phase D Mastra Observer + Reflector ingest pipeline (2026-04-28, included in 2026.5.1.1)

Two-stage LLM ingest pipeline on top of m3's existing primitives.
Stage 1 (Observer) extracts atomic three-dated observations from
multi-turn conversation blocks. Stage 2 (Reflector) merges,
deduplicates, and supersede-flags observations across sessions.
Both stages run on user-provided LLMs via YAML profiles in
`config/slm/`; local-SLM profiles are included for zero-spend setups.

### Added

- **`type='observation'` memory rows** with three-date semantics:
  - `observation_date` (when assistant logged it) → `metadata_json`
  - `referenced_date` (when fact is about) → `valid_from`
  - `relative_date` (verbatim user phrasing) → `metadata_json`
  - `confidence` + `supersedes_hint` → `metadata_json`

- **Migration 025** (`memory/migrations/025_observation_queue.up.sql`):
  - `observation_queue` table — drained by `bin/run_observer.py`
  - `reflector_queue` table — drained by `bin/run_reflector.py` when
    per-(user, conversation) observation count exceeds
    `M3_REFLECTOR_THRESHOLD` (default 50)
  - `idx_mi_type_user_obs` partial index for fast obs-vs-raw partition

- **Observer + Reflector SLM profiles** (`config/slm/observer_local.yaml`,
  `config/slm/reflector_local.yaml`). Observer prompt enforces atomic-fact
  decomposition with three-date metadata. Reflector prompt performs
  merge / supersede / preserve over `{existing, new}` observation lists.

- **`bin/run_observer.py`** — drainer with two modes:
  - Variant mode (`--source-variant ... --target-variant ...`): bulk
    enrichment over a corpus snapshot.
  - Queue mode (default): pop rows from `observation_queue` with
    backoff/retry. Production drainer.

- **`bin/run_reflector.py`** — drainer with two modes:
  - Queue mode (default): pop rows from `reflector_queue`.
  - Force mode (`--force-conversation CID --force-user UID`): bypass
    queue for tests and one-off triggers.

- **`memory_core.observation_enqueue_impl()`** and
  **`memory_core.reflector_enqueue_impl()`** — explicit conversation-close
  triggers. UNIQUE-on-key dedup means re-enqueue is a no-op.

- **Retrieval preference for observations** in
  `memory_search_scored_impl`: env-gated by `M3_PREFER_OBSERVATIONS=1`.
  Partition top-k results into obs-hits and raw-hits; if obs-hits supply
  enough context (sum of token estimates above
  `M3_OBSERVATION_BUDGET_TOKENS`, default 4000), return obs-only. Else
  interleave obs first, raw to fill remaining slots.

- **YAML `max_tokens` / `input_max_chars` knobs** on SLM profiles
  (`bin/slm_intent.py`). Replaces hardcoded 512 / 4000 with per-profile
  tunables. Default `max_tokens=512` preserves existing classifier
  callers; Observer + Reflector profiles override as needed. Default
  `input_max_chars=None` (no truncation) for classifier callers; longer
  contexts (Observer / Reflector) opt in to explicit caps.

- **Tests** (`tests/test_observer.py`, `tests/test_reflector.py`):
  21 unit tests covering JSON parsing edge cases, code-fence stripping,
  date normalization, string-"null" coercion, low-confidence filtering,
  three-date metadata population, supersedes no-op filtering, Anthropic +
  OpenAI dispatch, and prefix-match fallback for find-by-text.

### Changed

- **`bin/slm_intent.py`**: `Profile` dataclass adds `max_tokens` (int=512)
  and `input_max_chars` (Optional[int]=None) fields. Both
  `_call_model` Anthropic + OpenAI paths use `prof.max_tokens`.

---

## Cross-encoder rerank in core retrieval (2026-04-28, included in 2026.5.1.1)

Adds an opt-in cross-encoder rerank stage to `memory_search_routed`.

### Added

- **Cross-encoder rerank** in `bin/memory_core.py`, exposed through
  the MCP `memory_search_routed` tool. Default off — `rerank=False`
  is byte-identical to pre-feature output. When on, re-scores
  the top `rerank_pool_k or 3*k` hits with sentence-transformers
  `CrossEncoder` (default `cross-encoder/ms-marco-MiniLM-L-6-v2`),
  blends with hybrid score per `rerank_blend`, re-sorts. Lazy-loaded
  — no `sentence_transformers` import at module-import time.
  Capture metadata records `rerank_applied`, `rerank_model`,
  `rerank_pre_count`, `rerank_post_count` for AUTO-routing diagnostics.

### Changed

- `memory_search_routed` MCP schema gained four properties
  (`rerank`, `rerank_model`, `rerank_pool_k`, `rerank_blend`).

---

## [2026.4.24.12] — April 25, 2026 — Plugin commands self-resolve on Windows

### Fixed

- `/m3:*` plugin commands now show an explicit resolver chain (`mcp-memory`
  → `python -m m3_memory.cli` → `.venv/Scripts/...`) so they work even
  when `pip install --user` puts the script shim somewhere not on PATH —
  the common Windows pain point.
- `docs/install_windows.md` gained a complete PATH-fix section with the
  correct `[Environment]::SetEnvironmentVariable` invocation that actually
  expands `$env:APPDATA` before writing to user PATH.
- `/m3:doctor` summary trimmed from a paragraph to one line.

### Renamed (post-release)

- `/m3:doctor` → `/m3:health` to avoid collision with Claude Code's
  built-in `/doctor` command (autocomplete preferred the namespaced one).

---

## [2026.4.24.11] — April 25, 2026 — Claude Code plugin + claude.ai HTTP transport

### Added

- **First-class Claude Code plugin.** Install with
  `/plugin marketplace add skynetcmd/m3-memory && /plugin install m3@skynetcmd`.
  Auto-registers the memory MCP, wires up Stop + PreCompact chatlog hooks,
  adds 15 `/m3:*` slash commands plus a `memory-curator` subagent.
  Plugin manifest at `.claude-plugin/plugin.json`, marketplace manifest at
  `.claude-plugin/marketplace.json`, commands flat under `commands/`,
  hooks at `hooks/hooks.json`. Plugin name is `m3` so commands appear as
  `/m3:health`, `/m3:search`, etc.
- **`mcp-memory serve` subcommand.** Runs the same 66-tool bridge over
  Streamable HTTP at `http://127.0.0.1:8080/mcp` for claude.ai web/desktop
  and Anthropic API MCP Connector integration. Driven by env vars
  (`M3_TRANSPORT=http`, `M3_HTTP_HOST`, `M3_HTTP_PORT`, `M3_HTTP_PATH`)
  for systemd / docker. Default bind is `127.0.0.1` — public exposure
  must go through a tunnel (cloudflared / tailscale / ngrok) or reverse
  proxy with auth, since the endpoint includes destructive tools.
- **`mcp-memory chatlog hook-path` subcommand.** Prints absolute path to
  the chatlog hook script for the OS, used by plugin hooks.
- **`docs/claude_code_plugin.md`** and **`docs/claude_ai_connector.md`** —
  plugin reference + full self-host walkthrough for Cloudflare Tunnel /
  Tailscale Funnel / ngrok / reverse proxy with systemd + launchd unit
  files.

### The 15 `/m3:*` slash commands

`/m3:health` `/m3:status` `/m3:search` `/m3:save` `/m3:write` `/m3:get`
`/m3:graph` `/m3:forget` `/m3:export` `/m3:tasks` `/m3:agents`
`/m3:notify` `/m3:find-in-chat` `/m3:install` `/m3:help`

The other 51 MCP tools remain callable directly via tool calls.

---

## [2026.4.24.10] — April 25, 2026 — One-line installer

### Added

- **`install.sh` at repo root** (~180 lines) — single curl|bash install
  for Linux + macOS. Detects distro from `/etc/os-release` / `uname`,
  installs prerequisites via `apt` / `dnf` / `pacman` / `zypper` / `apk` /
  `brew`. Refuses to run as root; sudo only for OS package install.
  Idempotent. Distro coverage: Debian/Ubuntu/Mint/Pop, Fedora/RHEL/Rocky/Alma,
  Arch/Manjaro, openSUSE, Alpine, macOS via brew.
- **`docs/install_linux.md`**, **`docs/install_macos.md`**,
  **`docs/install_windows.md`** — new focused per-OS guides. Pre-existing
  dense homelab walkthroughs moved to `*_homelab.md` siblings.
- **README install section** is now one curl|bash command + three OS
  links. Replaces the previous multi-step block per distro family.

---

## [2026.4.24.9] — April 25, 2026 — apply-claude reports PATH fix accurately

### Fixed

- When Claude Code is installed via `npm install -g` AFTER `mcp-memory
  install-m3` and the user runs `mcp-memory chatlog init --apply-claude`:
  the `~/.npm-global/bin` PATH gets added to `~/.profile` correctly, but
  the status message read `[=] no change — chatlog entries already present`
  even though the PATH was just fixed. Now reports both actions.

---

## [2026.4.24.8] — April 25, 2026 — Install docs + dependency bumps

### Added

- README + INSTALL.md lead with the apt one-liner for PEP 668 distros.
  Earlier releases assumed pipx/git/sqlite3 were already on the system;
  on a fresh Debian 13 minimal LXC, they aren't.
- INSTALL.md gained a "Prerequisites — what needs admin (sudo) once" table.

### Updated

- `psycopg2-binary` 2.9.11 → 2.9.12, `chromadb-client` 1.5.7 → 1.5.8,
  `sqlglot` 30.4.3 → 30.6.0, `fastapi` 0.135.3 → 0.136.1, `uvicorn`
  0.44.0 → 0.46.0, `cryptography` upper bound widened to `<48`,
  dev: `ruff` 0.15.10 → 0.15.12, `mypy` 1.20.1 → 1.20.2, `build` 1.4.2 → 1.4.4.
- All changes minor / patch — no breaking changes. `pip-audit` clean.

---

## [2026.4.24.7] — April 25, 2026 — pipx XDG path + Claude PATH ordering hotfix

### Fixed

- **Hooks couldn't find pipx-installed Python on pipx >= 1.4.** pipx 1.4
  moved venvs to `~/.local/share/pipx/venvs/` (XDG spec); chatlog hook
  scripts only checked the legacy `~/.local/pipx/venvs/`. On Debian 13 /
  Ubuntu 24.04+ / Fedora 40+ this caused every captured turn to spill
  with `ModuleNotFoundError: httpx` while hooks reported "executed
  successfully." Both paths are now probed (XDG-first), plus
  `$PIPX_HOME/venvs/m3-memory`.
- **`chatlog init --apply-claude` did not fix npm-global PATH.** When
  Claude Code was installed via `npm install -g` AFTER `install-m3`,
  `~/.npm-global/bin` wasn't on `~/.profile` so non-login shells (cron,
  hooks, scripts) couldn't find `claude`.

---

## [2026.4.24.6] — April 24, 2026 — Install UX sprint

### Added

- **`mcp-memory doctor` reports chatlog health.** New section: DB path,
  captured row count, last-capture timestamp, per-agent hook state for
  Claude (Stop/PreCompact) and Gemini (SessionEnd). Uses stdlib sqlite3
  in read-only URI mode.
- **`LLM_ENDPOINTS_CSV` defaults probe Ollama + LM Studio.** Was
  LM-Studio-only; Ollama users had to set the env var manually.
- **`mcp-memory chatlog init|status|doctor` subcommand.** Wraps the
  previously-undiscoverable bin scripts. `chatlog doctor` exits nonzero
  on warnings.
- **`install-m3` post-install phase.** Auto-registers memory MCP with
  Gemini CLI when detected, prints sqlite3 install hints per OS, fixes
  `~/.npm-global/bin` PATH for non-interactive shells, prompts for
  endpoint + capture-mode (with `--non-interactive`, `--endpoint`,
  `--capture-mode` flags for CI).
- **`chatlog init --apply-claude` / `--apply-gemini`.** Idempotently merge
  hook entries into `~/.claude/settings.json` and `~/.gemini/settings.json`
  instead of printing snippets for paste. Timestamped backup before any
  write; preserves user-authored hooks; refuses to clobber unparseable
  JSON. `apply-gemini` also writes the `mcpServers.memory` and
  `security.auth.selectedType` entries needed for headless `gemini --prompt`.
- **`install-m3 --force` preserves user data.** Stashes
  `*.db`/`*.json`/`*.jsonl` from `repo/memory/` before the rmtree,
  restores onto the fresh tree.
- **Top-level `INSTALL.md`** with OS matrix and audit-before-running
  instructions for the bash installer.
- **`.gitattributes`** — forces LF on `*.sh` and `*.py`, CRLF on `*.ps1`,
  marks binary types. Prevents Windows checkouts from breaking Linux
  hooks with CRLF.

### Fixed

- **Empty `post-install:` section** when all helpers returned None.
- **Hook scripts now find pipx venv Python** — fallback chain prevents
  the `ModuleNotFoundError: httpx` failure.
- **`chatlog init --non-interactive` runs migrations.** Previously
  exited leaving an empty schema-less DB; first hook fire then died
  with `no such table: memory_items`.
- **`chatlog_ingest` parses Gemini CLI 0.39+ JSONL transcripts.** Parser
  assumed single-JSON `{sessionId, messages:[]}`; Gemini writes one
  record per line. Both formats handled.
- **Doctor reports Gemini SessionEnd hook state** (was a static note
  that hid real wired-vs-unwired state).

---

## [2026.4.24.5] — April 24, 2026 — Auto-install on first `mcp-memory` run

### Added

- **One-command install.** `mcp-memory` now auto-fetches the system payload
  when invoked against a missing `~/.m3-memory/repo/`. No more required
  follow-up `mcp-memory install-m3` step for the common case — `pip install
  m3-memory` is enough. Behavior depends on whether we're talking to a human:
  - **Interactive TTY** (user at a shell): prompts `Fetch from GitHub? [Y/n]`
    before cloning, since auto-downloading a GitHub repo on first run is
    surprising enough to deserve a confirmation.
  - **Non-interactive** (launched as an MCP subprocess by an agent; no TTY):
    auto-fetches silently with a `[m3-memory] auto-fetching ...` line to
    stderr. Prompting would deadlock the parent waiting for input.
  - **`M3_AUTO_INSTALL=0` env**: hard opt-out. `mcp-memory` falls through
    to the actionable error message pointing at explicit `install-m3`.

  The explicit `mcp-memory install-m3` / `update` / `uninstall` / `doctor`
  subcommands from 2026.4.24.3 remain available for users who prefer the
  explicit flow. Tests: 5 new cases in `tests/test_installer.py` covering
  each of the three paths + env opt-out + failure propagation.

---

## [2026.4.24.4] — April 24, 2026 — Fix Windows Unicode crash in `install-m3`

### Fixed

- **`mcp-memory install-m3` crashed on Windows consoles with a `UnicodeEncodeError`.**
  The default Windows console code page is `cp1252`, which can't encode
  the arrow (`→`) and em-dash (`—`) characters that had snuck into
  user-facing print strings in `installer.py` and `cli.py`. Verified
  end-to-end on a fresh venv: `pip install m3-memory==2026.4.24.3 &&
  mcp-memory install-m3` crashed on the first print; `2026.4.24.4` runs
  to completion and populates `~/.m3-memory/` correctly.

  Two-layer fix:
  1. Replace the non-ASCII glyphs in user-facing strings with ASCII
     equivalents (`->`, `-`) so the output looks fine on every terminal.
  2. Reconfigure `sys.stdout` / `sys.stderr` to UTF-8 with
     `errors="backslashreplace"` at CLI entry, so future non-ASCII in
     output strings degrades gracefully instead of crashing.

  Linux/macOS users were not affected because those terminals default
  to UTF-8.

---

## [2026.4.24.3] — April 24, 2026 — Fix max-kind retrieval pool-halving

### Fixed

- **`memory_search_scored_impl(vector_kind_strategy="max")` was returning a
  truncated candidate pool.** The SQL join against `memory_embeddings`
  returns one row per `(memory_id, vector_kind)` pair, but the SQL-level
  `LIMIT 1000` and the in-Python `SEARCH_ROW_CAP` (default 500) were both
  applied to the raw row count, so the effective unique-item pool was
  `limit / kinds_in_use`. For a dual-embed corpus that halved the pool.
  Symptom: large session-hit-rate regressions vs `vector_kind_strategy="default"`
  on the same ingest (validated on 500-question LongMemEval-S:
  0.706 → 0.976 SHR at k=20 post-fix, matching `strategy="default"` within
  0.2pp).

  Fix: double the SQL `LIMIT` under `strategy="max"` and defer the
  `SEARCH_ROW_CAP` trim until after the dedup pass so the cap counts
  unique items. No behavior change for `strategy="default"` (the base
  cap already counts unique items since the SQL pins to one kind).

  Callers on `2026.4.24.1` or `2026.4.24.2` who enabled the opt-in
  dual-embed path should upgrade. Callers on default paths are
  unaffected.

---

## [2026.4.24.2] — April 24, 2026 — One-command install

### Added

- **`mcp-memory install-m3` / `update` / `uninstall` / `doctor` subcommands.**
  The pip wheel still ships thin (tiny CLI only); `install-m3` fetches the
  full system payload from GitHub into `~/.m3-memory/repo/` pinned to the
  wheel version and writes a persistent config file pointing the bridge
  there. Resolution order for finding the bridge, in precedence:
    1. `$M3_BRIDGE_PATH` (unchanged; power-user override)
    2. `~/.m3-memory/config.json` (written by `install-m3`)
    3. Walk up from the package file looking for a sibling
       `bin/memory_bridge.py` (preserves the `pip install -e .` dev flow)
  `install-m3` prefers `git clone --depth 1 --branch v<version>` and falls
  back to downloading the GitHub release tarball if git isn't available.
  Tests: `tests/test_installer.py` (13 cases covering resolution order,
  config persistence, git + tarball paths, uninstall, doctor output).

### Fixed

- **`mcp-memory --version`** now reads `m3_memory.__version__` instead of a
  hardcoded string that had drifted to `2026.4.8`.

### Docs

- README.md + QUICKSTART.md updated: the canonical install flow is now
  `pip install m3-memory && mcp-memory install-m3`. Clone-based dev setups
  remain supported and auto-detected.

---

## [2026.4.24.1] — April 24, 2026 — Dual-Embedding Retrieval + SLM-Enriched Embeds

### Upgrade notes

- New migrations **v021** (composite index on `memory_embeddings(content_hash, embed_model)`) and **v022** (`vector_kind` column on `memory_embeddings`) apply automatically on next `migrate_memory up`. Both are reversible. v021 is an index-only add; v022 is `ALTER TABLE ADD COLUMN` with `NOT NULL DEFAULT 'default'`, which is metadata-only on current SQLite versions (no row rewrite).
- All new kwargs default to pre-release behavior. `memory_write`, `memory_search`, and every MCP tool schema are byte-identical to `2026.4.22.x`. The dual-embed and Anthropic-backend paths are opt-in; callers who don't touch them see no change.

### Added
- **Dual-embedding ingest + max-kind retrieval fusion.** See the [DUAL_EMBED.md](DUAL_EMBED.md) walkthrough for a worked example. Migration **v022** adds a `vector_kind` column to `memory_embeddings` so a single `memory_id` can carry multiple embedding vectors distinguished by kind (`NOT NULL DEFAULT 'default'` — existing rows migrate in place).
  - `memory_write_bulk_impl` gains `dual_embed: bool = False`. When `True` **and** an `embed_key_enricher` transforms `embed_text`, Phase 2 emits two rows per item: `vector_kind='default'` from the raw pre-enrichment text and `vector_kind='enriched'` from the SLM output. Pass-through enrichment and `dual_embed=False` emit a single `'default'` row — existing callers unaffected.
  - `memory_search_scored_impl` gains `vector_kind_strategy: "default" | "max"`. `"default"` (the new default) pins the SQL join to `vector_kind='default'`, a strict superset of pre-v022 behavior. `"max"` lets all kinds through and dedupes by `memory_id` keeping the row with the highest query-vector cosine. `bm25` is per-item, so the drop only discards vector-similarity signal — no FTS information is lost.
  - Tests: `tests/test_embed_key_enricher.py` (dual-embed cases), `tests/test_vector_kind_strategy.py`.

- **SLM profile `backend: anthropic`** — `slm_intent` can now target Anthropic's `/v1/messages` endpoint in addition to OpenAI-compatible `/v1/chat/completions`. Anthropic path uses `x-api-key` header, sends `system` as a top-level field, and optionally wraps it in a `cache_control` ephemeral block (`cache_system: true`, default) so repeated calls pay the system prompt once. **Opt-in only** — no shipped default-named profile declares `anthropic`; pick a profile that names a cloud URL and pass it explicitly. Example profile at `config/slm/contextual_keys_haiku.yaml` (not loaded by any default code path).

- **`embed_key_enricher` hook on `memory_write_bulk_impl`** — bulk-ingest callers can now supply an `async` callback that rewrites the `embed_text` of each prepared item before embedding. Content stays verbatim; only the vector-path key changes ("keys only, values verbatim" per the LoCoMo `llm_v1` / LongMemEval contextual-keys paper finding). New kwargs:
  - `embed_key_enricher: Callable[[str, dict], Awaitable[str]] | None = None` — `None` is a no-op (unchanged baseline behavior).
  - `embed_key_enricher_concurrency: int = 4` — semaphore cap on concurrent enricher calls.

  Errors fall open: if the enricher raises, the item's `embed_text` reverts to its anchor-augmented baseline and the ingest continues. The kwarg is bulk-only (not exposed via MCP) — intended for benchmark and import drivers. Tests: `tests/test_embed_key_enricher.py`.

- **`slm_intent.extract_text()`** — sibling of `extract_entities` that returns the raw model output unchanged (no comma-splitting, no length filter). Needed for callers that want the SLM's reply as a single string — the first consumer is the LongMemEval benchmark's `--contextual-keys` ingest flag, which prepends SLM-extracted atomic facts to each turn's `embed_text`. Signature: `async def extract_text(text, profile, client=None) -> Optional[str]`. `profile` is required (no sensible default for free-text extraction). Documented in `docs/SLM_INTENT.md` §5 alongside the new "Choosing the right extractor function" comparison table.

- **SLM profile `post:` block for output post-processing** — profiles that drive `extract_text` / `extract_entities` now support a three-part optional cleanup pipeline applied to every reply before it's returned:
  - `post.skip_if_matches` — regex list; if any matches the raw reply (case-insensitive search), the function returns `""` so callers fall back. Catches refusals like `"no extractable facts"` and dash-only outputs.
  - `post.strip_prefixes` — regex list; stripped from the start of the reply, iterated until none match. Handles "Sure. Here are the facts: …" preambles.
  - `post.format` — wrapper string containing the literal `{text}` placeholder (validated at load time).

  Invalid regexes or malformed `format` strings raise `ValueError` during `load_profile()` so deploy errors surface loudly. `classify_intent` intentionally does NOT apply `post:` — its label-matcher handles prose cleanup inline. Tests: `tests/test_slm_intent.py` (8 new cases).

- **New profile `config/slm/contextual_keys.yaml`** — atomic-fact extractor for ingest-time embed-key enrichment. Consumed by `slm_intent.extract_text()` from the LongMemEval bench when `--contextual-keys` is passed. Ships with a `post:` block that strips "Sure." / "Here are the facts:" preambles and skips dash-only / "no facts" refusals.

- **Tunable elbow-trim on `memory_search_scored_impl`** — three new kwargs let callers tune adaptive-K behavior without patching the underlying utility:
  - `elbow_sensitivity: float = 1.5` — previously hardcoded inside `_trim_by_elbow`. Lower values trim more aggressively (cut off sooner); higher values keep more results. The default reproduces prior shipped behavior exactly.
  - `adaptive_k_min: int = 0` — floor on trimmed K. When set, undoes the trim if it leaves fewer than `adaptive_k_min` results. `0` (default) disables the floor.
  - `adaptive_k_max: int = 0` — cap on trimmed K. When set, caps the trimmed list at `adaptive_k_max` results. `0` (default) disables the cap.

  All three kwargs are back-compat defaults. `memory_search_impl` and the MCP `memory_search` tool are unchanged — they invoke with default values and see prior behavior. Tests: `tests/test_elbow_trim.py` (4 cases covering default, tunable sensitivity, edge conditions).

  Motivation: the prior hardcoded `sensitivity=1.5` can over-trim temporal and multi-session retrieval pools in practice, making adaptive-K counterproductive for some workloads. Exposing the knob lets callers tune trim aggressiveness per use case without altering default-path behavior.

- **Always-on: temporal-anchor prefix in `embed_text`.** When `metadata["temporal_anchors"]` contains resolved `YYYY-MM-DD` dates, they are prepended to the embed text as `[YYYY-MM-DD, ...] …` before embedding. No flag; free when anchors are absent. Lets vector / FTS queries hit absolute dates even when the source says "yesterday".
- **New memory type `event_extraction`** added to `VALID_MEMORY_TYPES` (now 21 types) and the `type="auto"` classifier's local set.
- **Opt-in ingestion enrichment** (off by default; fire only for `type="message"` rows with a `conversation_id`):
  - `M3_INGEST_WINDOW_CHUNKS=1` — emit a `type="summary"` row every `M3_INGEST_WINDOW_SIZE` (default 3) turns concatenating prior bodies. Captures Q&A pairs single-turn embeds miss.
  - `M3_INGEST_GIST_ROWS=1` — emit a heuristic `type="summary"` gist row once a conversation passes `M3_INGEST_GIST_MIN_TURNS` (default 8), then every `M3_INGEST_GIST_STRIDE` (default 8) turns. Deterministic; no LLM.
  - `M3_INGEST_EVENT_ROWS=1` — regex-extract `<ProperNoun> <verb> ... <date hint>` sentences and emit one `type="event_extraction"` row per match, linked back via `references`. Deterministic; no LLM.
  - `M3_QUERY_TYPE_ROUTING=1` — retrieval-side: when a query matches "When / what date / which day" + a proper noun, shift `vector_weight` to `0.3` (BM25-heavy) so the named-entity signal isn't diluted by embedding similarity.

### Docs
- **ENVIRONMENT_VARIABLES.md** — new "Ingestion Enrichment (opt-in)" section with the five new env vars and the always-on temporal-anchor behavior.
- **TECHNICAL_DETAILS.md** — env-var rows added; valid-type count corrected 20 → 21 (includes `knowledge` and new `event_extraction`).

### Notes
- Emitters run from the per-item `memory_write` path only; `memory_write_bulk` intentionally bypasses enrichment for fast loader throughput.

---

## [2026.4.12b] — April 12, 2026 — Conversation Grouping, Refresh Lifecycle, Reversible Migrations

### Added
- **Reversible migration system** — `bin/migrate_memory.py` rewritten as a subcommand CLI: `status`, `up`, `down --to N`, `backup`, `restore`. Paired `NNN_name.up.sql` / `NNN_name.down.sql` files. File-level DB backups (including `-wal` / `-shm`) written automatically before every `up`/`down` to a user-chosen directory (default `~/.m3-memory/backups/`, persisted in `memory/.migrate_config.json`). Interactive confirmation with `-y` escape hatch for CI. Legacy v001–v012 treated as up-only — `down` refuses to cross them with a clear error naming the lowest reversible target.
- **`memory_items.conversation_id`** (migration v013) — groups memories by conversation / team session. Same ID space as `conversation_start` / `conversation_append`. Accepted as a parameter on `memory_write`, `memory_update`, and `memory_search`.
- **`memory_items.refresh_on` + `refresh_reason`** (migration v014) — planned-obsolescence timestamps. Partial index on `refresh_on WHERE refresh_on IS NOT NULL` keeps lookups O(flagged-rows).
- **`memory_refresh_queue` MCP tool** (45 total) — read-only query for memories due for review. Params: `agent_id`, `limit`, `include_future`.
- **Refresh backlog surfaces via three off-path channels:**
  - Pull: `memory_refresh_queue` tool
  - Lifecycle hint: `agent_register` and `agent_offline` response strings append `| N memories of yours due for refresh` when backlog is non-empty
  - Push: `memory_maintenance` emits one `refresh_due` notification per distinct owning agent, deduped against existing unacked notifications
- **Composite partial index** `idx_mi_conversation_id ON memory_items(conversation_id, created_at) WHERE is_deleted = 0` (migration v015) — replaces the plain v013 index so `conversation_id` scoped retrieval gets an index scan with ordered results. Verified with `EXPLAIN QUERY PLAN` on a synthetic 1000-row fixture.

### Changed
- **`memory_write`** — accepts `conversation_id`, `refresh_on`, `refresh_reason` parameters. All nullable; existing callers unaffected.
- **`memory_search`** — accepts `conversation_id` filter. Propagated through all recursive fallback paths (FTS → semantic, no-match → semantic, operational-error → semantic).
- **`memory_update`** — accepts `refresh_on`, `refresh_reason`, `conversation_id`. Sentinel `"clear"` sets a field to NULL; empty string means no change. Field-level audit rows written to `memory_history`.
- **`memory_maintenance`** — appends `Refresh queue: N memories due for review` to its report when the backlog is non-empty, then fans out notifications by owning agent.

### Docs
- **AGENT_INSTRUCTIONS.md** — new behavioral rule §6 "Review the Refresh Queue Periodically" with startup / long-session / breakpoint guidance; new parameters documented in `memory_write` / `memory_search` / `memory_update` tables; `memory_refresh_queue` added to retrieval table.
- **CORE_FEATURES.md** — new "Refresh Lifecycle" and "Conversation Grouping" feature sections; 25→45 MCP tool summary table (now grouped by category including Orchestration).
- **TECHNICAL_DETAILS.md** — new "Indexes on `memory_items`" table, expanded "Migrations" section covering subcommands / file naming / version tracking / backups / reversibility rules, new top-level "Refresh Lifecycle" section with data flow diagram and design rationale for reusing `memory_history` instead of a parallel soft-delete lifecycle.
- **README.md** — minimal updates (44→45 tool count in badge and summary text).

### Test Coverage
- 193/193 end-to-end tests passing (unchanged from previous entry — all new paths are additive)
- 12/12 mcp_proxy unit tests passing — `test_full_catalog_count` bumped 44→45; `test_legacy_dispatch_table_complete` confirms `memory_refresh_queue` is reachable through the proxy's legacy dispatch path; `test_inject_agent_id_on_memory_write` confirms agent_id enforcement still holds with the new `conversation_id` / `refresh_on` / `refresh_reason` parameters
- New end-to-end verification covers: conversation_id write/read roundtrip, refresh_on past/future/clear lifecycle, maintenance notification fan-out and dedup, post-ack re-notification, planner confirmation for v015 composite index

---

## [2026.4.12] — April 12, 2026 — Multi-Agent Orchestration + MCP Proxy v2

### Added
- **Orchestration primitives** — agent registry (`agent_register`, `agent_heartbeat`, `agent_list`), handoffs (`memory_handoff`), notifications (`notify`, `notifications_poll`, `notifications_ack`), and tasks (`task_create`, `task_assign`, `task_update`, `task_set_result`, `task_tree`) for multi-agent coordination
- **`m3-team` CLI** — `m3-team init|check|run` for spinning up multi-agent teams from a single YAML file
- **`examples/multi-agent-team/`** — provider-agnostic orchestrator with bounded dispatch loop (`DispatchLimits`: max_turns=8, max_tool_calls=24, max_seconds=120, provider_retries=3) and terminal `DispatchResult` taxonomy
- **`team.minimal.yaml`** — single LM Studio agent example, zero API keys required
- **`bin/mcp_tool_catalog.py`** — single source of truth for all MCP tool definitions via `ToolSpec` dataclass; 55 tools (66 with destructive enabled)
- **MCP proxy v2** (`bin/mcp_proxy.py`) — catalog-driven dispatch replacing the prior 15-tool hardcoded list; reads `X-Agent-Id` header and enforces `inject_agent_id` so client-claimed identity cannot be bypassed
- **`MCP_PROXY_ALLOW_DESTRUCTIVE`** env flag — gates 9 destructive tools (`memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`) behind opt-in
- **`bin/test_mcp_proxy_unit.py`** — 12 in-process unit tests covering imports, tool counts, destructive filtering, dispatch, and agent_id injection

### Changed
- **License** → Apache 2.0 (from MIT) for clearer patent grant in multi-agent contexts
- **`VALID_MEMORY_TYPES`** expanded to 20 types; `bin/memory_core.py` auto-classifier kept in sync
- **MCP proxy** now sources its tool list from `mcp_tool_catalog` instead of an inline hardcoded list — adds 29 previously missing tools to proxy clients (Aider, OpenClaw)

### Fixed
- **mcp_proxy ImportError** — `LM_STUDIO_BASE` and `LM_READ_TIMEOUT` were imported from `m3_sdk` but no longer exist there; inlined as proxy-local env reads
- **Tool count gap** — proxy clients had access to only 15 of 55 catalog tools; now have full parity
- **Agent identity bypass** — proxy did not enforce `inject_agent_id`, letting clients spoof `agent_id` on `memory_write`; now overridden from `X-Agent-Id` header

### Test Coverage
- 193/193 end-to-end tests passing
- 12/12 mcp_proxy unit tests passing
- Default tool count: 5 protocol + 6 debug + 35 catalog = 46
- With `MCP_PROXY_ALLOW_DESTRUCTIVE=1`: 5 + 6 + 44 = 55

---

## [2026.4.8] — April 10, 2026 — PyPI Launch

### Added
- `m3_memory` Python package with `mcp-memory` CLI entry point — `pip install m3-memory` now works end-to-end
- `mcp-memory` command auto-starts the MCP server; no path configuration required for pip installs
- `ROADMAP.md` — v0.2 through v1.0 milestones with community voting link
- `publish.yml` GitHub Actions workflow — automated PyPI publish on GitHub Release via OIDC trusted publishing

### Changed
- `pyproject.toml` — proper package discovery, pinned `dependencies`, `[project.optional-dependencies]`, fixed license metadata

---

## [2026.04.06] — April 6, 2026 — Production Release

### Added
- **Conversation summarization** — `conversation_summarize` compresses long threads into 3-5 key points via local LLM
- **LLM auto-classification** — `type="auto"` lets the local LLM categorize memories into one of 18 types
- **Explainable search** — `memory_suggest` returns full score breakdowns (vector + BM25 + MMR penalty) per result
- **Multi-layered consolidation** — `memory_consolidate` merges old memory groups into LLM-generated summaries
- **Portable export/import** — JSON round-trip backup including embeddings and relationships
- **Retrieval benchmarks** — `bin/bench_memory.py` measures MRR, Hit@k, and latency
- **Configurable thresholds** — `DEDUP_LIMIT`, `DEDUP_THRESHOLD`, `CONTRADICTION_THRESHOLD`, `SEARCH_ROW_CAP` via env vars
- **MCP tool set** — memory ops, knowledge graph, conversations, lifecycle, data governance, and operations (55 catalog tools as of 2026.4.12)

### Fixed
- Search recursion bug in `memory_search_impl` — FTS-to-semantic fallback was incorrectly passing state into bitemporal filter parameters
- `memory_export` excluded non-existent `metadata_json` column from `memory_relationships`
- LM Studio connectivity checks standardized to `localhost` with proper API tokens

### Changed
- `VALID_MEMORY_TYPES` expanded to include `auto`
- `VALID_RELATIONSHIP_TYPES` expanded to include `consolidates`
- AES-256 vault upgraded to PBKDF2 600K iterations (auto-migrates legacy 100K secrets on first decryption)

### Test Coverage
- 41 end-to-end tests passing across all features
- Retrieval MRR 1.0 achieved in standardized benchmarks
- CI: lint (Ruff) + typecheck (Mypy) + pytest on Ubuntu/macOS/Windows × Python 3.11/3.12

