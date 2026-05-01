# Changelog

All notable changes to M3 Memory are documented here.

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

---

For the full technical history see [docs/CHANGELOG_2026.md](CHANGELOG_2026.md).
