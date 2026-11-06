# <a href="README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Changelog

All notable changes to M3 Memory are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## Repo policy notes

**Commit message hygiene (forward-going from 2026-04-29):** commit messages
on `main` and any branch that pushes to `origin` describe features in
generic terms. Internal experiment names and specific corpus/variant tags
stay in private development. Published benchmark **results**
(README/CHANGELOG/registry descriptions) are intentional public positioning;
the policy is forward-going only.

---

## [Unreleased]

  - **Multi-agent and team setups: every read and write is checked against the
    calling agent.** In secure mode an agent sees and changes only its own private
    rows and the rows shared with it, and a row it may not reach answers exactly as
    a missing one. The checks add ~5-10%, with no more than 10s (p95) for most calls. 
    **Affected:** installs with `security.mode = "secure"` (schema size: all installs)
  · **Action:** none · **Data status:** unchanged.

  - **More of the hot path runs in the Rust core (pad on Project Oxidation).** These moves serve integrity and consistency across platforms, while minimizing real-world performance impacts. 
    **Affected:** installs with the Rust core · **Action:** none · **Data status:**
  unchanged.

## [2026.10.25.0] — 2026-10-25 — a cognitive loop that keeps running, backfill that keeps its checks

### Changed

- **CI test jobs stop a hung run within the hour.** The test matrix, the
  PostgreSQL lane and the pure-Python lane time out at about twice their
  slowest measured run, instead of GitHub's six hours.
  **Affected:** contributors · **Action:** none · **Data status:** none.

### Fixed

- **A contradiction check deferred while the embedder was down is no longer
  lost when a later backfill step fails.** The backfill now reads the owed
  checks from the store, runs them before its repairs, and runs every step even
  when one fails; a row whose check raises no longer stops the others.
  **Affected:** every OS and backend · **Action:** none ·
  **Data status:** memories whose check was skipped kept their flag and are
  checked by the first backfill after upgrading.

- **A backfill repair failure no longer reports the embedder as the cause.**
  The cognitive loop now says how many rows were embedded, which step failed
  with its traceback, and which steps completed. **Affected:** every OS and
  backend · **Action:** none · **Data status:** none.

- **The cognitive loop keeps running when one of its passes cannot.** A pass
  that stopped on a configuration problem (the entity pass, on a missing API
  key) ended the whole loop; the service manager restarted it every few seconds
  and no background work ran. That pass is now turned off with its reason in the
  loop's log, and `m3 doctor` names it and its fix. Doctor's remedy for a loop
  that is not running points at the loop's log and `m3 doctor --fix`.
  **Affected:** installs where a loop pass cannot start, most often entity
  extraction without its API key · **Action:** apply the fix `m3 doctor` names;
  the pass comes back when the loop restarts ·
  **Data status:** loop work missed while it restarted is picked up on later cycles.
- **Entity extraction runs on the LLM you set up, without a key it does not
  need.** Its shipped profile names a local Anthropic-format server; the run
  follows your configured LLM in OpenAI format, but the API key was checked
  against the profile as written, so a machine with a keyless local server
  stopped the pass for a missing key. The key is now checked against the
  endpoint the pass calls, and its run plan shows that endpoint.
  **Affected:** installs using a keyless local LLM (Ollama, LM Studio without
  auth) · **Action:** none · **Data status:** memories left without entities
  are extracted on the next passes.
- **Concurrent writers on Windows no longer fail taking a lock.** When one
  process released a lock as another took it, Windows refused to recreate the
  lock file while its deletion was still finishing, and the second writer
  failed with a permission error. It now waits out that moment; a denial that
  lasts is reported with the path.
  **Affected:** Windows: chat capture when two hooks fire together, the audit
  trail, config and secret writes · **Action:** none ·
  **Data status:** chat turns from a failed capture were retried by the next
  hook; an audit entry or setting write that failed this way was not made.
- **`m3_entities` run by hand finds your stores.** Without `--core-db` or
  `--chatlog-db` it looked under the checkout's old `memory/` folder and found
  no store; it now reads the configured ones.
  **Affected:** running entity extraction by hand · **Action:** none ·
  **Data status:** none.
- **`m3 doctor` no longer flags m3's own loop-watchdog timer.** It read the
  timer as "not a job this version installs", because a timer starts a service
  rather than running a program itself.
  **Affected:** Linux · **Action:** none · **Data status:** none.
- **`m3 upgrade` repairs what setup's checks find.** When setup finished the
  install but a closing check failed (a service down), the upgrade stopped and
  asked for setup to be re-run; it now runs its repair step and reports that
  step's result. Its steps no longer print "finish with `m3 setup`" while the
  upgrade is running setup.
  **Affected:** anyone running `m3 upgrade` · **Action:** none ·
  **Data status:** none.

## [2026.10.19.0] — 2026-10-19 — Codex, session recall, an LLM for enrichment, hooks that update with m3

### Added

- **Codex CLI support.** `m3 setup` finds Codex, adds m3 to
  `~/.codex/config.toml` and wires two hooks into `~/.codex/hooks.json`: session
  start, and chat capture after every turn (`chatlog_ingest --format codex` reads
  Codex's session files). Upgrades and `m3 doctor --fix` repoint a stale entry.
  With the m3 Codex plugin installed, setup leaves these files alone.
  **Affected:** Codex users · **Action:** run `m3 setup`, then approve the hooks
  once in Codex with `/hooks` · **Data status:** none.
- **Google Antigravity: chat capture, memory tools and the session-start line,
  for the app, the IDE and the CLI.** `m3 setup` registers m3 in
  `~/.gemini/config/mcp_config.json` and adds m3's hooks to
  `~/.gemini/config/hooks.json`; each conversation's turns are saved when the
  agent finishes, with the model that wrote them. Until now Antigravity had
  neither m3's tools nor chat capture. On Windows, a hook path containing a space
  is refused with a message, since Antigravity cannot run it.
  **Affected:** Antigravity users · **Action:** run `m3 setup`, then restart
  Antigravity · **Data status:** earlier conversations were not captured; their
  transcripts stay in `~/.gemini/antigravity*/brain/`, and Antigravity rebuilds a
  conversation's transcript when it is resumed.
- **Chat capture for OpenCode and OpenClaw, wired by setup.** `m3 setup` installs
  a plugin in `~/.config/opencode/plugins/` that saves a session's turns when a
  turn finishes, and an OpenClaw hook in `~/.openclaw/hooks/m3-capture/` that
  saves them when a session is retired (`/new`, `/reset`, or a daily or idle
  reset), turned on with `openclaw hooks enable`. Until now both needed wiring
  by hand, and the documented OpenCode hook folder does not exist. OpenCode
  capture reads the session that finished, not the newest one. A one-shot
  `opencode run` is not captured: it exits before OpenCode delivers plugin
  events. `m3 chat init` now describes what setup wires for Codex, OpenCode and
  OpenClaw.
  **Affected:** OpenCode and OpenClaw users · **Action:** run `m3 setup`, then
  restart OpenCode · **Data status:** earlier sessions were not captured;
  OpenCode keeps them in its own store and OpenClaw in
  `~/.openclaw/agents/*/sessions/`.
- **One session-start hook for Claude Code, Codex and Gemini CLI.** It shows
  whether chat capture is working and which m3 is running against the newest
  release, and offers the session-recall menu on a fresh start. Gemini CLI gains
  the hook too.
  **Affected:** Claude Code, Codex and Gemini CLI users · **Action:** none; it is
  wired by `m3 setup` and on upgrade · **Data status:** none.
- **A daily version check.** m3 asks pypi.org for the newest release at most
  once a day, keeps the answer on this machine, and every agent's session-start
  line shows it. Setup asks once whether to turn it on (default yes;
  `--update-check` / `--no-update-check` for unattended installs); when it is
  off, the line says so.
  **Affected:** all installs · **Action:** answer the setup question, or
  `m3 config set updates.check true` · **Data status:** none.
- **A guide for remote MCP clients.** docs/REMOTE_MCP_CLIENTS.md shows verified
  requests for the xAI API (Grok) and the OpenAI Responses API against
  `m3 serve`, and which apps work today.
  **Affected:** users connecting cloud models to m3 · **Action:** none ·
  **Data status:** none.
- **Setup finds the LLM you already use for memory enrichment and asks.** It
  looks at Ollama's `OLLAMA_HOST`, `OPENAI_BASE_URL`/`OPENAI_API_BASE`, and
  Ollama and LM Studio on this machine, with their keys (`LM_API_TOKEN`,
  `OPENAI_API_KEY`; Ollama's cloud key only for ollama.com), probes each, and
  marks a cloud endpoint as one. Your choice is written to `llm.endpoints` and a
  key that worked to the secrets file. An upgrade or unattended install lists
  them with the commands instead of asking. `m3 doctor` and setup say whether
  enrichment has an LLM; with none, they name `m3 config set llm.endpoints
  <url>` and, for a server that needs a key, `m3 secret set LM_API_TOKEN`. The
  install guides describe it.
  **Affected:** all installs · **Action:** none; to choose an LLM later, run
  `m3 setup` · **Data status:** none.
- **A Codex plugin.** `codex plugin marketplace add skynetcmd/m3-memory`, then
  install m3 from `/plugins`: the memory server, the `m3-*` skills (the Claude
  Code commands and the m3 guide) and the chat-capture and session-start hooks.
  See docs/codex_plugin.md.
  **Affected:** Codex users · **Action:** optional; install the plugin and
  trust its hooks in `/hooks` · **Data status:** none.
- **A Gemini CLI extension.** `gemini extensions install
  https://github.com/skynetcmd/m3-memory` registers the memory server and loads
  a short guide to using m3 as context. See docs/gemini_extension.md.
  **Affected:** Gemini CLI users · **Action:** optional; install the extension
  · **Data status:** none.
- **`m3 hook <name>` runs one of m3's agent hooks.** The Claude Code plugin's
  hooks are now one line each (`m3 hook claude-session-start`,
  `m3 hook claude-capture`), and its session start reports capture status and
  the version.
  **Affected:** Claude Code plugin users · **Action:** update m3 with the
  plugin (`m3 upgrade`); on an older m3 the capture hooks fall back to its
  previous command, and only the session-start line waits for the upgrade ·
  **Data status:** none.
- **Session recall: a menu of your recent sessions at session start.**
  `session_brief` (MCP tool and `m3 memory session_brief`) lists numbered
  one-line summaries: this agent's recent sessions first, then each other
  agent's latest, sessions from the current git tree first, each with local time,
  tree and branch. The server instructions ask agents to show it once; a chosen
  line loads with `memory_get`. A session's summary is written when the session
  ends, in the background, by a SessionEnd hook (Claude Code, Codex, Gemini CLI,
  OpenCode, OpenClaw), so the menu does not wait on the cognitive loop; the loop's
  new `summarize_sessions` pass covers sessions that end without that event
  (Antigravity, a closed window) once quiet (`recall.quiet_minutes`). Each
  summary comes from the agent's own
  `session_note`, Claude Code's compaction summary, or the local
  `session_summary` model profile, which reads the session's latest compaction
  summary and its last turns and names the session by its main work; a summary
  is superseded when the session grows. When recent sessions have waited over an
  hour with no summary, the session-start line says so, and `m3 doctor` reports
  background session-end captures or summaries that failed in the last 24 hours.
  Captured turns now record `cwd`, `git_root`, `git_branch` and `tree`; earlier
  turns show as "(no project)". Settings live under `[recall]`;
  `recall.summary_profile` other than `local` sends the session's tail to that
  provider. `m3 doctor` reports how far behind the summary pass is.
  **Affected:** all installs · **Action:** none; turn off with
  `m3 config set recall.enabled false` · **Data status:** new
  `session_summary` memories (scope user); chat turns gain four metadata fields.

### Changed

- **Agent hook lines run m3's launchers in `~/.m3/hooks`.** Each agent's config
  holds one line pointing at a launcher there; m3 rewrites the launchers on every
  upgrade, so a hook fix no longer edits your agent configs, and a line survives
  the package moving to a new Python. Existing installs are repointed once, on
  upgrade.
  **Affected:** Claude Code, Codex, Gemini CLI, OpenCode and OpenClaw users ·
  **Action:** none · **Data status:** none.
- **Every stored timestamp is UTC and carries its zone.** A `valid_from` or
  `valid_to` given as a bare date is stored as that day at 00:00 UTC, a time
  without a zone is stored as UTC, and another offset is converted. An
  unparseable time is refused by `memory_write` and `memory_supersede`; in bulk
  writes and chat capture it is logged and replaced, so no turn is dropped.
  **Affected:** all installs · **Action:** none · **Data status:** on SQLite,
  migration 061 adds the UTC zone to existing bare-date and zoneless values;
  each keeps the instant it already meant. PostgreSQL stores were already zoned.
- **A session in a git worktree is named after its repository.** Session recall
  named a session by the worktree's directory and treated it as a separate
  project; it now uses the repository the worktree belongs to, so an agent's
  isolated copy lists with the main checkout. Detached checkouts are included.
  **Affected:** session recall users working in `git worktree` checkouts ·
  **Action:** none · **Data status:** none; turns captured earlier keep the name
  they were stored with.

- **Chat turns saved during a store outage reach the store once it is back.**
  A spill file that failed `max_drain_attempts` drains in a row was quarantined
  whatever the cause, so an outage longer than a few passes (a PostgreSQL store
  unreachable, a SQLite file held by another process) parked healthy turns until
  someone moved them back. A temporary failure is now retried every pass without
  counting toward the limit, and is still logged; quarantined files get one more
  attempt each day and leave quarantine when it succeeds.
  **Affected:** all installs · **Action:** none · **Data status:** turns already
  in quarantine are retried by the daily pass after the upgrade.

- **CI captures a recorded session for each agent host.** One job per host
  (Claude Code, Codex, Gemini CLI, Antigravity, OpenCode, OpenClaw, Aider)
  installs m3, runs the hook setup writes for that host on a recorded input, and
  checks every turn, the final reply included, reaches the chat store. A host's
  job runs when its files change, and all of them run on Linux, macOS and
  Windows for a release tag.
  **Affected:** contributors · **Action:** none · **Data status:** none.
- **Code comments describe the code, not its history.** A test counts comment
  blocks and docstrings that narrate change history (dates, PR or issue
  numbers, "used to"); the count may only fall. A comment whose history is
  the evidence carries `history-ok`.
  **Affected:** contributors · **Action:** none · **Data status:** none.

### Removed

- **`M3_HOME` is no longer read.** Hooks find m3 from their own location;
  `m3 doctor` names the variable if it is still set.
  **Affected:** installs that set `M3_HOME` · **Action:** none; unset it at will ·
  **Data status:** none.

### Security

- **`gdpr_forget` no longer reports a complete erasure while synced copies
  remain.** It erases this machine's stores; with a warehouse configured, the
  rows sync copied to it and to the machines syncing with it stay. The plan and
  the confirmation now list them under `copies_remaining`, and `complete` is
  false until no copy is known to remain.
  **Affected:** installs syncing to a PostgreSQL warehouse · **Action:** erase on
  each machine that syncs, and remove the rows from the warehouse ·
  **Data status:** erasures run before this release left those copies in place.

- **Hidden and control characters are removed from stored text.** Terminal
  escapes, other control characters, bidirectional overrides and invisible
  Unicode tag characters (which can spell an instruction a model reads and a
  person does not) were stored as written. `memory_write`, `memory_update` and
  bulk writes now store the text without them, record which code points were
  removed and where (never the original text), and say so in their reply; chat
  capture strips them and notes it on the turn; the session-start menu escapes
  any in older rows. Characters that shape real text are kept: tab, newline,
  zero-width joiners and the direction marks.
  **Affected:** all installs · **Action:** none · **Data status:** rows stored
  before this release are not rewritten.
- **A memory must have visible text.** An empty, whitespace-only or
  invisible-only `memory_write` or `memory_update` is refused, and bulk writes
  skip such items with a warning. Chat capture no longer stores a turn that is
  blank once harness framing and hidden characters are removed (a turn holding
  only a system reminder); it counts the turn as handled, not failed.
  Conversation containers are unaffected.
  **Affected:** all installs · **Action:** none · **Data status:** blank chat
  turns stored before this release remain.
- **Benchmark and end-to-end scripts no longer write test rows into your
  store.** `benchmark_memory.py`, `bench_memory.py`, `test_bulk_parity.py` and
  `test_memory_bridge.py` write to a new temporary database unless given
  `--database`, and on PostgreSQL run only with `--configured-store`. The
  retrieval benchmark wrote its test memories into the configured store and left
  them there soft-deleted.
  **Affected:** anyone who ran these scripts from `bin/` · **Action:** none ·
  **Data status:** earlier runs of `benchmark_memory.py` left soft-deleted
  test memories (agent ids starting `bench_`) in the configured store.
- **The dashboard escapes every stored value and error message it renders.** A
  memory history row's field and event names, and the text of four error
  replies, reached the page as HTML.
  **Affected:** dashboard users · **Action:** none · **Data status:** none.
- **A search query no longer takes quadratic time.** A query holding a date such
  as "1 jan" followed by a long run of spaces took seconds of CPU per search.
  **Affected:** all installs · **Action:** none · **Data status:** none.
- **A cloud LLM provider is recognised by its host, not by a substring of the
  URL.** A URL that merely contains a provider's name no longer gets that
  provider's API key header.
  **Affected:** dashboard LLM health checks · **Action:** none ·
  **Data status:** none.
- **Setup masks the PostgreSQL DSN in every reachability error.** One refusal
  path returned its error text unmasked.
  **Affected:** installs choosing PostgreSQL · **Action:** none ·
  **Data status:** none.

### Fixed

- **PostgreSQL timestamps are read in UTC.** m3's connections used the server's
  time zone, so on a server not set to UTC every timestamp m3 read back carried a
  local offset. Each connection now sets its session time zone to UTC.
  **Affected:** PostgreSQL stores on a server not set to UTC · **Action:** none ·
  **Data status:** none; stored values were always correct instants.

- **Editing a memory's text refreshes its vector.** `memory_update` re-embedded
  only with `reembed=true`, so semantic search kept matching an edited memory by
  its old wording. A text edit now computes a fresh vector (or, if the embedder
  is unavailable, removes the stale one so the background pass replaces it);
  `memory_update_bulk` text edits are re-embedded by the background pass.
  Title, tag and importance edits leave the vector alone.
  **Affected:** all installs · **Action:** none · **Data status:** memories
  edited before this release are re-embedded by the background embedding pass
  after the upgrade (`m3 doctor` counts any left).
- **`m3 doctor` no longer reports a mixed embedding space from deleted
  memories.** Its check counted vectors left on memories deleted outright, which
  no search reads, and could warn that rows "rank wrongly" when none could. It
  now counts the vectors searches reach (current and superseded memories), and
  it also reports memories edited after their newest vector.
  **Affected:** all installs · **Action:** none · **Data status:** none.
- **Hooks ignore a project's `PYTHONPATH`.** A project on `PYTHONPATH` with a
  folder named like one of m3's packages made the hooks import its code in place
  of m3's, so capture and the session-start line failed in that project. Hook
  lines and the processes they start run Python with `-E -s`.
  **Affected:** users whose agent runs with `PYTHONPATH` set · **Action:** run
  `m3 setup` (or upgrade) to rewrite the hook lines · **Data status:** turns from
  affected sessions were not captured; their transcripts remain with the agent.
- **Gemini CLI capture saves each turn again, not only at session end.** Setup
  wires m3's capture on AfterModel and PreCompress as well as SessionEnd, so a
  Gemini session that is killed keeps the turns it had. Hook lines from an
  earlier setup are replaced; your own hooks are kept.
  **Affected:** Gemini CLI users · **Action:** run `m3 setup` ·
  **Data status:** turns of sessions that ended without SessionEnd were not
  captured; their transcripts remain in Gemini's chat folder.
- **Semantic search works on installs without the native core.** With no Rust
  core loaded, m3 treated the embed server as unavailable: writes skipped their
  vectors and searches dropped to keyword-only, so a semantic search returned
  nothing, with a healthy embed server running. The warning also blamed failures
  that never happened; it now names the real cause.
  **Affected:** installs running m3 without the native core (pure Python), or
  with a second m3 without it first on PATH · **Action:** upgrade; vectors for
  earlier writes are filled in by the cognitive loop · **Data status:** none.
- **`m3 setup --agents` names an unknown agent.** It failed with a Python
  traceback; it now lists the valid names.
  **Affected:** unattended installs · **Action:** none · **Data status:** none.
- **The cognitive loop and the observer, reflector and entity jobs run against a
  server that needs no API key.** They refused to start without a stored key
  even when the LLM is a keyless Ollama or an LM Studio without authentication.
  Now they call without one; a server that requires a key answers 401 and the
  job names the `m3 secret set` that fixes it. Anthropic-format profiles still
  require their key.
  **Affected:** installs whose LLM needs no key (Ollama; LM Studio without
  authentication) · **Action:** none. With LM Studio authentication on, store
  its key: `m3 secret set LM_API_TOKEN` · **Data status:** none.
- **`chatlog_list_conversations` works on PostgreSQL.** It failed with a
  grouping error on every call there.
  **Affected:** PostgreSQL installs · **Action:** none · **Data status:** none.

- **Setup says when Hermes cannot load the m3 provider.** It reported the
  plugin "already current" while Hermes' own Python could not import m3 (for
  example a Python 3.11 Hermes venv), so Hermes listed it as not installed; it
  now names that Python and the `pip install` that fixes it.
  **Affected:** Hermes Agent installs · **Action:** none · **Data status:** none.
- **`m3 upgrade` no longer stalls on the Claude Code plugin update.** Where the
  `claude` CLI does not answer (e.g. over SSH with no login session), the plugin
  update stops after 90 s and says how to finish it by hand; it waited until the
  whole verification step timed out.
  **Affected:** upgrades run over SSH or without a Claude Code login, with the
  m3 plugin installed · **Action:** none · **Data status:** none.
- **`m3 doctor` no longer reports the loop watchdog's systemd timer as a dangling
  job.** A timer has no command of its own; the service it starts is checked.
  **Affected:** Linux installs with the cognitive-loop watchdog · **Action:**
  none · **Data status:** none.
- **A job whose profile names no API key says so.** It printed "no None in
  the secrets file".
  **Affected:** enrichment, entity, observer and reflector jobs with a profile
  lacking `api_key_service` · **Action:** none · **Data status:** none.

## [2026.10.17.3] — 2026-10-17 — tools that describe themselves, one environment variable

### Changed

- **Native core 3.10.172.** On Windows, importing the CUDA core and installing
  the embed service no longer flash a console window. The in-process embedder,
  the embed server and the FIPS settings take their values from
  `m3_config.toml` only: `M3_EMBED_*`, `M3_FIPS_MODE`, `M3_FIPS_STRICT` and
  `M3_HASH_PROVIDER` are not read, and `M3_MEMORY_ROOT/config` no longer
  locates the config.
  **Affected:** installs with the native core; an embed server configured by its
  service definition's environment · **Action:** none; `m3 upgrade` installs it
  and, when no `m3_config.toml` exists, copies those values into a new one.
  Otherwise `m3 config set embed.<key> <value>` · **Data status:** none.
- **Smaller default startup tool set.** Six tools register at startup
  (`memory_write`, `memory_search`, `memory_supersede`, `memory_get`,
  `chatlog_search`, `m3_call`; about 3,200 tokens, was 10 and about 4,000).
  `m3_call` reaches every other tool by name; `tools_load_domain` is reached
  through it. The server now honours `tools.startup` and `.tools_config.json`,
  which only `m3 doctor` read before.
  **Affected:** MCP clients in lazy mode (the default) · **Action:** none; to
  keep the old set, `m3 config set tools.startup "<names>"` (must include
  `m3_call`) · **Data status:** none.
- **One environment variable.** m3 reads only `M3_CONFIG_ROOT` (`--config` >
  `M3_CONFIG_ROOT` > `~/.m3/config`). Every setting comes from `m3_config.toml`,
  then its default; no other `M3_*` variable, `.env` line or `.llm_config.json`
  value is read. When no `m3_config.toml` exists, setup and upgrade create it and
  copy those values into it once, including from m3's systemd and launchd
  service definitions; an existing file is never re-imported or overwritten.
  Setup, upgrade and `m3 doctor` list each variable still set, where, and that it
  is not read; they also name the config file in use and any place that would
  read a different one. The Claude Code and Antigravity plugins drop their
  `endpoint`, `embed_fallback_url`, `embed_gguf` and `engine_root` settings.
  `sync_all.py` takes `--db PATH` instead of `M3_SYNC_DBS`.
  **Affected:** installs with `M3_*` variables in shell profiles, agent configs
  or service definitions; plugin users who set those fields · **Action:** set
  the values with `m3 config set KEY VALUE`, then delete the variables doctor
  lists · **Data status:** none.
- **MCP tools and m3 doctor: clearer and correct**
  - MCP tools describe themselves to agents: `tools/list` carries every
    parameter's description and each tool's `readOnlyHint` / `destructiveHint`
    (clients received bare types before), and tool descriptions say what each
    returns and when to use it instead of its neighbours.
    `bin/tool_quality_check.py` checks the catalog and the listed schemas.
  - SQLite: a bulk write (such as `memory_import`) right after a maintenance
    run could fail at once with "database is locked" while other agents were
    writing; write transactions now take the lock when they begin, so they
    wait for it instead. Agent writes through `memory_write` were not affected.
    `bin/bench_concurrent_writes.py` measures concurrent writers with ingestion
    running alongside.
  - `m3 doctor` checks other agents' capture when Claude Code is absent
    (without `~/.claude/settings.json` it stopped before them).
  - Three `m3 doctor` false alarms are gone: a folder in the spill quarantine
    counted as a quarantined file; a hook interpreter in another venv on the
    same m3 release reported as foreign; and a Claude MCP report that read only
    the user-level disable list. A folder with its own list (set with `/mcp`) is
    now listed with the server that runs there.
  - `m3 doctor` and `m3 chatlog status` label their figures: live chat rows
    vs stored (including soft-deleted), the newest row (which may have arrived
    by sync) vs the last capture on this machine.
  - `m3 chatlog status` reports `hooks[*].wired` as measured: from the hook
    check for Claude Code, `null` for agents it cannot see.
  - Over SSH, setup on a Mac prints how to turn on Touch ID for sudo
    (`m3 touchid enable`, at the Mac) instead of saying nothing.
  **Affected:** all installs and MCP clients · **Action:** none ·
  **Data status:** none.

### Fixed

- **Upgrades keep the embedder running.** On Linux, setup no longer removes a
  working embedder service whose command line is quoted (it read the program
  as missing), no longer gives it a second `--config` (the server then refused
  to start at the next restart), and no longer replaces a port it just imported
  with another. A service registered without `--config` is pointed at
  `m3_config.toml`, so its port follows `embed.server.port`.
  **Affected:** Linux installs with the embedder as a systemd user service;
  embedder ports other than 8082 · **Action:** none; `m3 upgrade` repairs it ·
  **Data status:** none.
- **The LLM choice setup records takes effect.** Setup and
  `m3 install --llm-endpoint` record it in `m3_config.toml` `[llm]`; they wrote
  `.llm_config.json`, which m3 no longer reads.
  **Affected:** installs that chose an LLM runtime or endpoint during setup ·
  **Action:** none; the setup `m3 upgrade` runs records it · **Data status:** none.
- **Audit trail: concurrent writers no longer lose entries or fork the chain.**
  Appends take a cross-process lock; a corrupt last line is reported instead of
  starting a new chain. On Windows, releasing m3's file locks no longer leaves
  a lock behind while another process is waiting on it.
  **Affected:** several agents or processes writing at once · **Action:** none ·
  **Data status:** entries written concurrently before this release may be
  missing from the trail.
- **`memory_supersede` refuses another owner's memory.** An `agent_id` or
  `user_id` that differs from the memory's own, or a `replacement_id` owned by
  someone else, is refused; an empty id still inherits the owner.
  **Affected:** multi-agent and multi-tenant stores · **Action:** none ·
  **Data status:** none.
- **A missing API key names `m3 secret set`.** The enrichment, entity,
  observer and reflector jobs suggested an environment variable, which m3
  does not read.
  **Affected:** jobs run without the key stored · **Action:** none ·
  **Data status:** none.
- **An engine root set in `m3_config.toml` is always used.** When a populated
  `~/.m3/engine/agent_memory.db` also existed, m3 used that store instead of
  the `[paths] engine_root` you set (only the deprecated `M3_ENGINE_ROOT`
  variable counted as a choice). Legacy copies of state and config files no
  longer replace files under a chosen engine or config root either, and a
  `[paths] memory_root` in the file now outranks `M3_MEMORY_ROOT`.
  **Affected:** installs that set `[paths] engine_root` to a non-default place
  while a store also exists at `~/.m3/engine` · **Action:** run `m3 doctor`
  and check the store path it reports · **Data status:** memories written
  before this release went to `~/.m3/engine/agent_memory.db`, not the chosen
  root; nothing was lost, and they can be moved with `memory_export` and
  `memory_import`.
- **FIPS mode: a PostgreSQL connection string without `verify-full` is named,
  with the fix.** Since 2026.10.17.1, FIPS mode refuses a networked PostgreSQL
  connection that does not verify the server, so warehouse sync stopped for
  FIPS users whose stored DSN had no `sslmode` (only the sync log showed it).
  `m3 doctor`, which `m3 upgrade` runs, now lists that DSN as refused with the
  fix, and the error points to the steps; the connection string is never
  printed.
  **Affected:** `crypto.fips_mode` on, with a warehouse or PostgreSQL primary
  DSN lacking `sslmode=verify-full` · **Action:** add `sslmode=verify-full` and
  the server's CA as `sslrootcert` to the DSN and store it again
  (`m3 secret set M3_CDW_PG_URL --keyring --stdin`); steps in
  [Troubleshooting](docs/TROUBLESHOOTING.md#fips-mode-refuses-the-postgresql-connection) ·
  **Data status:** none; sync resumes from where it stopped.

## [2026.10.17.2] — 2026-10-17 — doctor and upgrade correctness, announced admin prompts

### Changed

- **m3 install/upgrade UX & correctness**
  - `m3 doctor --fix` runs every check `m3 doctor` runs: it repairs, then
    reports, and exits nonzero when the report does (`--fix --verbose` adds
    detail only).
  - Every Windows administrator prompt is announced first ("You will be asked
    to authorize actions requiring elevated privileges:", the steps, the time),
    and its result is printed and kept in `elevation.log`: `permission
    approved`, `disapproved` or `timed out`, with the time it ended.
  - The native core installs on networks where GitHub's API refuses the
    lookup: the wheel comes from the release's checksum file and download links.
  - `m3 config import-env` skips a `paths.bin` inside a source checkout, a
    leftover from a development or test run (`--yes` imports it).
  - The embed server `m3 doctor --fix` starts as a stop-gap writes
    `embed_server_inproc.log`, as the boot task's server does.
  - The FIPS backend error says FIPS mode is on and its wolfSSL backend failed,
    not that a "compliance mode" is enforced.
  - `m3 upgrade` shows doctor's full output only when something needs
    attention; a healthy run stays one line.
  - Two database restores in quick succession each keep the store they moved
    aside; on Windows the second used to refuse.
  - The code base no longer carries workarounds for Python 3.11 and older:
    Python 3.12 is the supported floor (`requires-python >= 3.12`).
  **Affected:** all installs; the prompts on Windows only · **Action:** none ·
  **Data status:** none.

### Fixed

- **A Windows embed service registered by an older m3 now starts.** It started
  the new core without `--config`, could not find `m3_config.toml` and stopped,
  while `m3 doctor --fix` reported it restarted. Doctor now re-registers it
  under the same administrator prompt, and reports a restart only when the
  service answers on its port.
  **Affected:** Windows installs upgraded from before 2026.10.17.0 ·
  **Action:** run `m3 doctor --fix` at a console and approve the prompt ·
  **Data status:** items saved while the service was down may lack vectors;
  `m3 embedder backfill` adds them.

## [2026.10.17.1] — 2026-10-17 — dependency floor and clearer FIPS error

### Changed

- **`python-dotenv` 1.2.2 is accepted again** (the floor was 1.2.3), so m3
  installs beside tools that pin 1.2.2.
  **Affected:** installs that share an environment with such a tool ·
  **Action:** none · **Data status:** none.

### Fixed

- **m3 install/upgrade UX & correctness**
  - **The FIPS "no wolfSSL" error names the folders it searched.** It used to
    say `~/.m3/lib` even when the config root had moved m3's lib folder
    elsewhere; it now lists each path tried and says to set `M3_LIB_DIR`.
    *(Affected: installs in FIPS mode with relocated roots.)*
  - **The Hermes plugin reports m3's version.** Its manifest said 2026.10.16 in
    the 2026.10.17.0 release; it now follows each release.
    *(Affected: Hermes users (the version shown by `hermes plugins`).)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

## [2026.10.17.0] — 2026-10-17 — one config file for m3 and its Rust core

**Highlights.** One configuration file, `m3_config.toml`, now holds every setting, and every
m3 process reads it, background services and the Rust embed server included. Secrets live in a
user-only secrets file or the OS keyring, never in environment variables, and FIPS mode set in
the file takes effect everywhere. Also new: named stores, `m3 config get` / `set` / `unset` /
`import-env`, `m3 batch`, pgvector search on PostgreSQL, a read-access audit log, and sharing the
embed server with other machines over TLS (`m3 embedder secure`). m3's state moves from
`~/.m3-memory` to `~/.m3`. **To upgrade:** run `m3 upgrade`, which moves your existing settings
and secrets into the two files, then restart m3's services; on Windows, also run
`m3 embedder install` once from an Administrator terminal.

### Security

- **Secrets stay out of environment variables.** The OS installer hands the
  vault key to the step that stores it on stdin, and an unattended install
  that finds `AGENT_OS_MASTER_KEY` only in the environment names it and stores
  nothing. The shell example and the guides use `m3 secret set` and
  `m3 config set` instead of exporting values. **Affected:** unattended
  installs that set `AGENT_OS_MASTER_KEY`; anyone who copied the old
  `config/zshenv.example` · **Action:** run `m3 config import-env` (or
  `m3 secret set AGENT_OS_MASTER_KEY --keyring`), then delete secret `export`
  lines from shell profiles · **Data status:** none.
- **FIPS mode set in `m3_config.toml` takes effect everywhere**, including
  background services and the embed server, and error messages name the
  config key with its environment alias. A process configured by the file
  could run outside FIPS mode. **Affected:** installs that set FIPS or wolfSSL
  options in `m3_config.toml` · **Action:** upgrade, restart m3's services and
  run `m3 embedder install` (on Windows from an Administrator terminal); a
  missing wolfSSL now stops them, as FIPS mode requires · **Data status:** none.
- **The mac-agent example no longer resolves vulnerable `httpx2`/`httpcore2`.**
  Its requirements set security floors (`httpx2>=2.12.0`, `httpcore2>=2.10.0`),
  as the langchain-agent example does. **Affected:** users of
  `examples/mac-agent` · **Action:** reinstall its requirements ·
  **Data status:** none.
- **The dashboard and MCP proxy refuse unauthenticated network binds.** The
  dashboard refuses a non-loopback bind when its token cannot be resolved or
  decrypted or the auth module fails to load (it served without auth); on
  loopback it starts as before and says why auth is off. The proxy refuses a
  non-loopback bind without `MCP_PROXY_KEY` and compares keys in constant
  time. A vault probe failure no longer advises `--generate-token` (which can
  overwrite a working token). **Affected:** dashboard or proxy bound beyond
  loopback · **Action:** fix the token (`m3 doctor`) or bind 127.0.0.1 ·
  **Data status:** none.
- **Embed servers serve plaintext only on loopback.** The shared embed server
  and the Python embed servers refuse a non-loopback bind address (exit 78),
  since they receive raw memory text. **Affected:** setups binding an embed
  server to a LAN address or `0.0.0.0` · **Action:** bind `127.0.0.1`; to serve
  other machines, use `m3 embedder secure` · **Data status:** none.
- **FIPS mode no longer lets network connections fall back silently.** Under
  `M3_FIPS_MODE`, a PostgreSQL connection that crosses a network must use
  `sslmode=verify-full`; it could previously fall back to plaintext or skip the
  server's certificate. Under `M3_FIPS_STRICT`, network TLS is refused unless
  you attest a validated TLS library with `crypto.tls_validated = true`, and
  `psycopg2-binary` (which bundles its own OpenSSL) is refused. **Affected:**
  installs running `M3_FIPS_MODE` or `M3_FIPS_STRICT` with a PostgreSQL store or
  warehouse on another machine · **Action:** add `sslmode=verify-full` and the
  server's CA (`sslrootcert`) to that DSN; under strict, also see
  docs/FIPS_MODULE_BOUNDARY.md · **Data status:** none.
- **The `[langchain]` extra moves to LangChain 1.x.** It now requires
  `langchain-core>=1.4.0`, `langgraph>=1.2.4`, `langgraph-checkpoint>=4.1.1` and
  `langgraph-sdk>=0.4.4`, past the 2026 advisories in those packages (path traversal
  and SSRF in langchain-core, unsafe checkpoint deserialisation, an authorisation
  bypass in langgraph-sdk). The previous range stopped below 1.0, where none is fixed.
  **Affected:** installs with `m3-memory[langchain]` · **Action:** upgrade; code
  using `with_m3_history` keeps working with a deprecation warning ·
  **Data status:** none.
- **Search expansion no longer returns another agent's private memories.** With
  `requesting_agent` set (or `M3_ENFORCE_AGENT_ISOLATION`), a search kept other
  agents' private (`scope = agent`) memories out of its own results, but graph
  expansion (`graph_depth`), session and neighbouring-session expansion, two-stage
  observations and the bypass surface could add them back. Every expansion step
  now applies the same agent, user, scope and point-in-time filters as the search.
  **Affected:** stores shared by several agents that use agent isolation together
  with any expansion option · **Action:** upgrade · **Data status:** no data was
  changed; earlier search results may have included another agent's private
  memories.
- **PostgreSQL: a keyword-form DSN (`host=... dbname=...`) no longer puts its
  password into store identities and snapshot names.** The whole DSN was read
  as the database name, which also hid the warehouse from the same-database
  guard, missed forbidden hosts in a multi-host DSN, and skipped warehouse sync.
  **Affected:** PostgreSQL with a keyword-form DSN · **Action:** rename or
  delete store snapshots whose names contain the password ·
  **Data status:** snapshot names created by earlier versions may contain the
  DSN password.
- **The installer stores the primary PostgreSQL DSN in the OS keyring or the
  secrets file,** not in a plaintext environment variable, and `m3 doctor`
  names plaintext copies left behind.
  **Affected:** PostgreSQL installs · **Action:** remove the plaintext copies
  `m3 doctor` names · **Data status:** none.
- **Security and FIPS**
  - **A bad custom redaction regex is never quoted back.** With the native
    core, `m3 chatlog status` showed the text of a `custom_regex` that failed to
    compile; it now names the entry's position and length only, and reports an
    unknown pattern group.
    *(Affected: installs with the native core and a
  custom redaction regex that does not compile.)*
  - **The postgres_fdw sync link keeps the warehouse DSN's TLS terms.** It ignored
    `sslmode`, `sslrootcert` and `hostaddr`, so it ran at libpq's default (`prefer`),
    and it kept its first options after the DSN changed. Under FIPS tiers it is
    skipped and sync uses the bridge. A DSN naming `localhost` but dialing a
    remote `hostaddr` is no longer treated as local.
    *(Affected: PostgreSQL
  primaries syncing to a warehouse.)*
  - **Keys, IVs, salts and tokens come from the crypto provider's random source.**
    With the wolfCrypt backend they are drawn from wolfCrypt's DRBG instead of the
    operating system's. `m3 doctor` now shows each cryptographic path, what serves
    it and whether the active FIPS tier accepts it, and the FIPS documentation
    now lists every path, including network TLS, which the platform's TLS library
    serves.
    *(Affected: all.)*
  - **The dashboard's audit view escapes what it shows.** A memory's title,
    content, type and user id, and a memory id from the URL, are rendered as text,
    so a memory written with markup in it can no longer run script in the browser
    of whoever opens the audit view; the search query shown back is escaped too.
    *(Affected: installs that run `m3 dashboard`.)*
  - **m3's API keys and vault secrets no longer reach non-m3 programs.** `pg_dump`,
    `pg_restore` and the pip/uv upgrade step used to inherit every secret set in
    m3's environment; they now start without them. Child-process environments are
    built in one place.
    *(Affected: installs that put API keys or the vault key in the environment.)*
  - **Archive extraction applies Python's tarfile data filter** after m3's own
    path checks: setuid/setgid and group/other write bits are cleared and archive
    ownership is dropped.
    *(Affected: installs and upgrades that unpack an archive.)*
  - **FIPS mode: security hashes use the FIPS crypto provider** — the native-core
    wheel check, secret HMACs, the vault-salt fingerprint and the PostgreSQL
    store id.
    *(Affected: installs running in FIPS mode.)*
  - **Windows: the secrets file's access list is checked before writing and on
    read,** and a Credential Manager probe that reported any name as stored was
    removed.
    *(Affected: Windows.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **PostgreSQL: scored search applies the conversation, variant and
  agent-isolation filters.** That path ignored them and could return another
  conversation's or agent's rows.
  **Affected:** PostgreSQL; searches filtered by conversation, variant or
  requesting agent · **Action:** none · **Data status:** none.

### Added

- **One config file, complete and honoured by every m3 process.** When the
  config root has no `m3_config.toml`, setup and upgrade create it from the
  shipped default, checked against its SHA-256, listing every setting with its
  doc and default, commented out so later default changes still apply; an
  existing file is never replaced. Settings in it reach background services
  and the embed server (m3-core-rs 3.10.17), which no longer has its own `config.toml` (its values
  move into `m3_config.toml` and the secrets file; the old file is kept, unread,
  as `config.toml.migrated` for a rollback to an older core). `m3 embedder secure` records
  the listener there. `docs/ENVIRONMENT_VARIABLES.md` lists the variables m3
  still reads and where each legacy name moved. **Affected:** every install ·
  **Action:** run `m3 upgrade`, restart m3's services, and on Windows run
  `m3 embedder install` once from an Administrator terminal ·
  **Data status:** none.
- **Hermes Agent memory provider (Hermes 0.22+).** m3-memory registers a
  `hermes_agent.memory_providers` entry point, so installing it into Hermes'
  Python makes `m3` selectable in `hermes memory setup`. Cron and subagent
  contexts are recall-only. Turns are written with host agent `hermes`. A
  catalog-shape folder plugin (`m3_memory/integrations/hermes/folder_plugin`)
  delegates to the installed package. `hermes memory status` shows why m3 is
  unavailable, with the fix. **Affected:** Hermes users · **Action:** optional ·
  **Data status:** none.
- **`m3 embedder secure`: the shared embed server for other machines.** Adds a
  second listener to m3-embed-server with TLS 1.3 (rustls on aws-lc-rs; the
  `fips` build uses AWS-LC's FIPS module) and a bearer token, stored server-side
  as its SHA-256. The loopback listener is unchanged. Clients set
  `M3_EMBED_SERVER_TOKEN` and an `https://` `embed.fallback_url`, plus
  `embed.fallback_ca` for a private CA; the token is never sent in plaintext to
  another machine. **Affected:** multi-machine setups · **Action:** optional ·
  **Data status:** none.
- **[Coming from an older release](docs/HOW-TO-UPGRADE.md#coming-from-an-older-release)**
  in the Upgrade Guide: the one-time steps for a release without `m3 upgrade`.
  **Affected:** installs from before `m3 upgrade` · **Action:** follow it once ·
  **Data status:** none.
- **m3 says when an upgrade still needs `m3 setup`.** After a bare `pipx upgrade`,
  every `m3` command except the setup ones prints one line until setup has run on
  the new version, which it now records. **Affected:** pip and pipx installs ·
  **Action:** run `m3 setup` when it says so · **Data status:** none.
- **`m3 doctor` finds copies an older sync put in the wrong store** of a split
  install (chat turns in the memory store, memories in the chat store). `m3 doctor
  --fix` removes a copy only when its home store holds the same row, its links and
  an embedding. **Affected:** split installs that synced before this release ·
  **Action:** `m3 doctor --fix` · **Data status:** removes duplicate copies only.
- **`m3 doctor` names an older `m3-embed-server` copy on PATH** with the command
  that removes it. **Affected:** installs that followed older guides · **Action:**
  remove the copy if you like · **Data status:** none.
- **`m3 doctor` lists every m3 background job with a verdict**, on macOS, Linux and
  Windows: `current`, `outdated` (runs an older m3 install, with its version), `newer`,
  `retired`, `dangling` (its program is gone) or `unknown`. `m3 setup` (so `m3 upgrade`)
  and `m3 doctor --fix` remove outdated, retired and dangling jobs, saving each definition
  under `<config root>/removed-jobs/` first; `newer` and `unknown` are never removed.
  **Affected:** everyone · **Action:** none; check `m3 doctor` if you see m3 entries you do
  not recognise · **Data status:** none.
- **`m3 touchid status|enable|offer` (macOS).** Lets `sudo` accept Touch ID instead of
  your password, at any time. The install script now asks on the terminal (under
  `curl | bash` setup could not ask), and `m3 doctor` shows a tip on a Mac that could
  use it.
  **Affected:** macOS with Touch ID · **Action:** optional, `m3 touchid enable` at the
  Mac · **Data status:** none.
- **[Upgrading your warehouse](docs/WAREHOUSE_UPGRADE.md).** What m3 changes in the
  warehouse on upgrade, the ownership it needs, and how to move the warehouse server to a
  new PostgreSQL version.
  **Affected:** PostgreSQL warehouse users · **Action:** read it before upgrading ·
  **Data status:** none.
- **On PostgreSQL with pgvector, vector search runs in the database.** Results
  are unchanged (search is still exact); only the top results leave the server,
  so search is faster, most of all when the database is on another machine
  ([PERFORMANCE.md](docs/PERFORMANCE.md#postgresql-with-pgvector)). m3 creates the
  extension where the server offers it and the role may, and prepares existing
  rows in the background right after the upgrade; `m3 doctor` reports the state,
  and `m3 doctor --fix` or the cognitive loop finish any rows left. Without
  pgvector nothing changes.
  **Affected:** PostgreSQL stores on a server with pgvector · **Action:** none ·
  **Data status:** additive: a `vector` copy beside each embedding; the stored
  embedding is unchanged.
- **`m3 doctor` warns when the secrets file can leave the machine.** The secrets
  file holds secrets in plain text, protected by its permissions. Doctor names it
  when it sits inside a git working tree or a synced folder (OneDrive, Dropbox,
  iCloud Drive and other cloud folders), and says how to move it. On Windows and
  macOS it also says when the disk holding it is not encrypted (BitLocker or
  FileVault off); `m3 doctor --verbose` lists the `m3 secret set NAME --keyring`
  command for each secret.
  **Affected:** installs with a secrets file · **Action:** move the file if doctor
  names it · **Data status:** none.
- **`m3 config import-env`.** It moves settings into `m3_config.toml` from the
  places they used to live: the environment, shell startup files, `.env` files,
  MCP client `env` blocks (including `~/.claude.json`), the Windows registry
  environment and `.llm_config.json`.
  - **Conflicts:** a setting is imported only when every place agrees. When they
    differ, it asks which value to keep; when no one is at the console, it
    lists each place and value and imports nothing for that setting.
  - **What it leaves alone:** settings already in the file. Secrets are listed
    by name only.
  - **Safety:** `--dry-run` shows the plan. The file is backed up first, and a
    write that does not read back correctly is undone.
  - **`.llm_config.json`** is renamed `.llm_config.json.imported` once all its
    values are in the file. The old places are otherwise left as they are.

  **Affected:** none · **Action:** run `m3 config import-env --dry-run` to
  preview · **Data status:** none.
- **`m3 doctor` and setup report environment variables that disagree with
  `m3_config.toml`.** Each one is named with both values and which one m3 uses.
  Nothing is printed when they agree.
  **Affected:** installs with m3 settings in the environment · **Action:** remove
  the reported variable, or change the file · **Data status:** none.
- **Read-access audit log (GDPR Article 30).** Every memory read, by
  `memory_get` or as a search hit, is recorded with the reading agent, the data
  subject and the time. Rows are written in batches off the read path, kept 90
  days (`audit.read_log_retention_days`), included in `gdpr_export` as
  `access_log`, and erased by `gdpr_forget`. On by default; `audit.read_log`
  turns it off.
  **Affected:** all installs · **Action:** none (the table is created by
  migration) · **Data status:** new table `memory_access_log`; reads before
  this version were not recorded.
- **Memories written while no embedder is reachable are now checked for
  contradictions.** The check runs when the background embedding backfill gives
  them a vector, so an older memory they contradict is superseded then rather
  than staying active alongside them. Writes that turned the check off, and
  older rows, are not checked.
  **Affected:** installs whose embedder is sometimes unavailable · **Action:**
  none · **Data status:** memories written by earlier versions while the
  embedder was down are not re-checked.
- **Named stores.** `[stores.<name>]` in `m3_config.toml` defines a store (core
  plus files; on SQLite the files half is `<stem>.files.db` beside it). Select
  one per call with the `store` tool argument, or per process with `--store` on
  the CLI and on scripts that take a database. `store` together with `database`
  is refused, as is an unknown store.
  **Affected:** all installs · **Action:** none · **Data status:** a new
  PostgreSQL store's schema is created when it is first selected.
- **`agent_list` reports each agent's membership** — member (active within
  24 hours), idle (client still running) or left — derived from recorded
  activity.
  **Affected:** multi-agent setups · **Action:** none ·
  **Data status:** migrations `051` / `pg_059` add the activity columns.
- **Security and FIPS**
  - **`m3 secret set NAME [--keyring]`.** Stores one secret. m3 asks for the value
    twice and never shows it; a value is never taken from the command line. The
    value goes to the secrets file by default, or with `--keyring` to Windows
    Credential Manager or the macOS keychain (it is checked by reading it back,
    and its line in the secrets file is removed).
    *(Affected: none.)*
  - **`m3 secret refresh`, and API keys found where m3 stores them.** The LLM
    extraction keys (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) and the cloud embed
    token are now looked up through m3's secret store (environment, keyring,
    secrets file, vault) instead of the environment alone, so a key kept in the
    keyring also reaches background services. Each is read once per process and
    cached; storing, deleting, rotating or syncing a secret, or running
    `m3 secret refresh` after changing one outside m3, makes running processes
    re-read it within a few seconds.
    *(Affected: users of LLM extraction or the cloud embedder.)*
  - **A user-only secrets file** (`m3_secrets.toml` in the config root, or
    `[secrets].file`) is consulted after the OS keyring and before the vault. m3
    refuses a file that other users can read or that another user owns, and a
    name it does not know.
    *(Affected: all installs, mainly machines without a keyring.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **PostgreSQL support** for `M3Saver` (LangGraph checkpointer),
  `m3_entities_gliner`, `m3_enrich_stage`, `m3_enrich_assign` (migration
  `pg_060` adds routed enrichment claims), `m3_enrich_batch`,
  `m3_chatlog_enrich_backfill`, `migrate_entity_vocab`, `backfill_content_hash`,
  `build_kg_variant` and `ai_mechanic`. Their SQLite path arguments are refused
  on PostgreSQL.
  **Affected:** PostgreSQL installs · **Action:** none · **Data status:** none.

- **Search and memory**
  - **Semantic search on SQLite keeps vectors in memory.** With a Rust core that
    has the vector index, m3 holds each store's vectors in memory at a quarter of
    their size and reads only the nearest rows from the database, instead of every
    vector on every search. Results are scored exactly as before.
    *(Affected: SQLite stores without sqlite-vec.)*
  - **Long memories are embedded in fewer pieces.** When the embedder can count
    tokens exactly, m3 cuts a long memory at real token boundaries instead of a
    conservative estimate: on one store's 110 longest memories, 112 embeddings
    instead of 192.
    *(Affected: installs whose embed server or in-process
  embedder offers token windows.)*
  - **`explain` says whether to trust each result.** Every explained result now
    carries a `trust` block from its record: confidence, corroborations and
    contradictions, helpful/unhelpful grades, pinned, superseded, age, and who wrote
    it on which device. The reason line names cautions ("superseded",
    "contradicted 2x"), and a meaning-only match is no longer called a keyword
    match.
    *(Affected: all.)*
  - **`search.prefer_observations` and `search.two_stage_observations` settings.**
    The observation-ranking switches, previously env-only (`M3_PREFER_OBSERVATIONS`,
    `M3_TWO_STAGE_OBSERVATIONS`), can be set in `m3_config.toml` and appear in
    `m3 config show`. They still switch on automatically once 100 observations
    exist.
    *(Affected: none.)*
  - **`memory_supersede` can retire a memory into an existing one.** Pass
    `replacement_id` (a full id) instead of `content`: the old memory is closed and
    linked to the existing one, and nothing new is created. Use it to clean up
    duplicates; passing `content` for each duplicate adds another copy.
    *(Affected: all installs.)*
  - **`search_profile` tool:** per-stage timing of one real search.
    *(Affected: all installs.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **The [Upgrade Guide](docs/HOW-TO-UPGRADE.md) starts with how you installed m3** (pipx,
  pip, the install script, a GitHub checkout, a git URL, or a host plugin) and the upgrade
  for each.
  **Affected:** everyone upgrading · **Action:** none · **Data status:** none.

- **Configuration and CLI**
  - **`m3 batch` runs several commands in one process.** Each line of stdin is a
    command as it would follow `m3` (the same quoting on every OS), or a JSON
    array of its arguments. Results come back as one JSON object per command;
    `--text` prints readable sections instead.
    - **Failures:** a failing command does not stop the rest unless
      `--stop-on-error` is given.
    - **`--set` and `--config`** apply to the whole batch: `m3 "--set={...}" batch`.
    - **Not run inside a batch:** commands that prompt, that run until stopped,
      or that change the m3 installation. Each refusal says why.
    *(Affected: none.)*
  - **`--set` overrides settings for one run.** It works on any `m3` command and
    any m3 script. One table in double quotes goes right after `m3`, with the
    setting names of `m3_config.toml`; strings go in `^...^`:
    `m3 "--set={db.backend=^postgres^, search.row_cap=50}" search "query"`.
    The same form works in bash, zsh, PowerShell and cmd.
    - **Scope:** the whole run, including m3 processes it starts.
      `m3 config show --origin` labels these values `cli`.
    - **Refused:** secrets and paths (use the secrets file and `--config`). A
      refused `--set` names each problem and runs nothing. The format, with
      quoting for each shell, is in `m3 --help`.
    *(Affected: none.)*
  - **`m3 config get`, `m3 config set` and `m3 config unset`.** Read one setting,
    or write or remove it in `m3_config.toml`. A value is checked before it is
    written; secrets are refused. Changing the storage backend, a root directory
    or the secrets file asks for confirmation, or needs `--yes`. `set` names any
    environment variable that disagrees with the new value.
    *(Affected: none.)*
  - **`m3 --config FILE`.** Runs any `m3` command against that config file; its
    directory becomes the config root. It takes precedence over `M3_CONFIG_ROOT`.
    *(Affected: none.)*
  - **The engine, logs and models roots can be set in `m3_config.toml`.** Under
    `[paths]` (`engine_root`, `logs_root`, `models_root`); a value in the file wins
    over `M3_ENGINE_ROOT` / `M3_LOGS_ROOT` / `M3_MODELS_ROOT`, which still work. A
    malformed value stops with an error naming the key.
    *(Affected: installs that relocate a root.)*
  - **`m3_config.toml`: one file for every setting,** in the config root, with
    `m3 config show [--origin] [--json] [--config FILE]` to print each value and
    where it came from. Environment variables keep working and take precedence
    for the keys marked *env override*. Reference: `docs/CONFIG_REFERENCE.md`.
    *(Affected: all installs.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Agents and integrations**
  - **`m3 doctor` reports the dispatch doorbell.** It shows whether `m3-dispatchd`
    is serving this engine root, which reader sets wait on it and how much is
    pending, and says when a core that ships the daemon has nothing answering.
    Report-only.
    *(Affected: all installs.)*
  - **Agent mail can wake waiters at once instead of on a one-second poll.** When
    the Rust core's dispatch doorbell (`m3-dispatchd`) is running, sending a
    message rings it and each agent's waiter is woken immediately, once per burst
    (fifty messages, one wake). It runs on local IPC only and does no
    cryptography. Without it, waiters poll as before.
    *(Affected: installs
  running `m3-dispatchd`.)*
  - **`notifications_peek` reads an inbox without side effects.** It returns
    what `notifications_poll` returns but records no heartbeat and reclaims no
    leases, so checking another agent's inbox no longer marks that agent alive.
    *(Affected: multi-agent setups.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **The README states the project's status.** A "Project status" section near
  the top labels each feature Stable, Beta or Planned, lists the tested
  platforms and the known limits, and states the deprecation policy, each line
  linked to the doc with the detail. Tests keep it consistent with the ROADMAP
  and the package metadata.
  **Affected:** readers of the README · **Action:** none · **Data status:** none.

### Changed

- **`m3 embedder install --concurrency` has no effect and says so.** The
  server never read it; its worker count is `embed.server.streams`.
  **Affected:** scripts passing `--concurrency` · **Action:** use
  `m3 config set embed.server.streams <n>` · **Data status:** none.
- **The OS installer and `setup_memory.py` use the engine and logs roots.**
  `install_os.py` created `~/.m3-memory/memory` and `~/.m3-memory/logs`, and
  `setup_memory.py` defaulted to `<repo>/memory/agent_memory.db`; both now use
  `~/.m3/engine` and `~/.m3/logs` (or the configured roots). **Affected:**
  source installs using those scripts · **Action:** none ·
  **Data status:** none (existing stores are found by m3's resolver; nothing moves).
- **An unknown `dispatch.mode` is an error** naming the key, instead of
  meaning `separate`. **Affected:** configs with a misspelt dispatch mode ·
  **Action:** set `dispatch.mode` to a listed value · **Data status:** none.
- **Destructive operations need an explicit target.** `memory_consolidate`
  previews unless `dry_run=false` and requires `agent_filter`;
  `memory_consolidate` and `memory_dedup` are marked destructive;
  `migrate_memory.py down` requires `--target` (`main`, `chatlog` or
  `dispatch`); `enrich_pending` over MCP refuses to apply when no fact enricher
  is configured. **Affected:** scripts and agents calling these · **Action:**
  pass the target (and `dry_run=false` to apply) · **Data status:** none.
- **Windows: the cognitive-loop scheduled task changed.** **Affected:**
  Windows · **Action:** run `m3 schedules repair` from an elevated shell after
  upgrading · **Data status:** none.
- **m3's background jobs show under their own names on macOS and Linux.** Login items
  and process lists read `m3-cognitive-loop`, `m3-sync`, `m3-dashboard` and so on instead
  of seven entries named `python`. Each job runs through a launcher of that name, which
  pip and pipx recreate on every install and upgrade; the launchers are also on your PATH.
  Windows is unchanged: Task Scheduler already shows each task's name.
  **Affected:** macOS and Linux · **Action:** none; `m3 setup` (run by `m3 upgrade`)
  re-registers the jobs · **Data status:** none.
- **PostgreSQL 16 or newer is the supported range, as the store and as the sync
  warehouse.** It is tested on 16, 17 and 18, each on its own test machine.
  **Affected:** PostgreSQL users · **Action:** if your server is older than 16, plan
  an upgrade · **Data status:** none.
- **On a shared machine, another account's embedder is reported as such.** Doctor
  warns instead of saying OK when the embedder this account reaches belongs to
  another user, the stale-server check ignores other users' servers, and the
  "port already in use" note is dropped when the start check already names the
  owner. A dashboard or embedder that answers from another account is no longer
  reported as started or as this install's server, and setup does not suggest
  `m3 doctor --fix` for a port another account holds. Setup stops this account's
  embedder service when another account holds its port, instead of leaving it to
  fail and restart every few seconds, and `m3 doctor --fix` no longer starts it.
  When this account's embedder has already stopped because the port is taken,
  setup gives the same port advice instead of asking you to stop the other server.
  **Affected:** Linux and macOS machines with more than one m3 user ·
  **Action:** give each account its own port (`m3 config set embed.server.port`) ·
  **Data status:** none.
- **Hard delete is GDPR-only, and `gdpr_forget` takes two steps.** The first
  call (`user_id`, or the new `memory_ids` for specific memories) erases nothing:
  it returns what would be erased and a token valid for 15 minutes. The second
  call adds `confirm=<token>` and a compliance record with `legal_basis` and
  `verified_by`, and erases. A plain delete was already a soft delete (kept and
  recoverable); now the requests that used to remove rows are soft too:
  `memory_delete` / `memory_delete_bulk` / `task_delete` with `hard=true` and a
  curation plan's `delete_hard` soft-delete and return a notice naming
  `gdpr_forget`; expired memories are soft-deleted instead of removed;
  LangGraph `delete_thread` marks the thread deleted. The dashboard's Hard Delete
  and GDPR purge show the erasure plan and ask for the compliance record.
  Two-step GDPR forget is enforced on LangChain too: `Memory.forget()` returns
  the plan, and erases when called again with `confirm=` and `compliance=`.
  **Affected:** all installs; callers of `gdpr_forget`, of `hard=true`, of
  `delete_hard`, of `Memory.forget()`, and of the dashboard's Hard Delete or GDPR
  purge · **Action:** call `gdpr_forget` (or `forget()`) twice: plan, then confirm
  with the token and the compliance record; nothing else · **Data status:** from this release, expiry and
  `hard=true` keep the rows (soft-deleted) instead of removing them; existing
  data is unchanged.
- **`[autonomy]` in `m3_config.toml` sets the loop's autonomous-write toggles.**
  `consolidation_auto` and `chatlog_prune_auto` now resolve `m3_config.toml`
  first, then `.cognitive_loop_config.json`, then `M3_CONSOLIDATION_AUTO` /
  `M3_CHATLOG_PRUNE_AUTO` (read by the settings registry), then off.
  **Affected:** installs that enable an autonomous pass · **Action:** none;
  prefer `[autonomy]` in `m3_config.toml` · **Data status:** none.
- **Agents are asked to grade the memories they used, and grading is measured.**
  The server instructions ask an agent to call `memory_grade` (through `m3_call`)
  after answering. `memory_search` ends its results with `grade_with: [...]`, the
  ids it showed (`memory_search_slim` omits it). Graded verdicts are recorded per
  UTC day: `memory_feedback_stats` adds `today` and `last_7_days`, and
  `memory_lifecycle_summary` adds a `grading` block. Ranking is unchanged.
  **Affected:** all installs (migration 058 / pg_065 adds a small table) ·
  **Action:** none · **Data status:** new table `memory_grade_daily`; no
  existing data changed.
- **Three more settings move to `m3_config.toml`; `M3Context.get_setting` is
  deprecated.** `identity.default_user_id` (legacy `M3_DEFAULT_USER_ID`, the
  LangChain / mem0 tenancy default) and `db.postgres.pg_restore` (legacy
  `M3_PG_RESTORE`) are settings, as is `llm.endpoints` in setup's LLM probe.
  `M3Context.get_setting(name)` warns and reads the settings registry (a key name
  or one of its env names), no longer the raw environment. The installer's
  `config.json` no longer records `M3_CONFIG_ROOT` / `M3_ENGINE_ROOT` /
  `M3_FIPS_MODE` from the environment (nothing read them back).
  **Affected:** callers of `M3Context.get_setting`; installs that set these names ·
  **Action:** call `m3_core.config.settings.setting(name)` · **Data status:** none.
- **`m3_config.toml` reaches the ingest LLM gates and the startup tool set.**
  `[ingest] auto_title` / `auto_entities`, `[observer] no_embed`, `[tools] lazy`
  and the new `[tools] startup` are read through the settings registry, with
  `M3_INGEST_AUTO_*`, `M3_OBSERVER_NO_EMBED`, `M3_TOOLS_LAZY` and
  `M3_TOOLS_STARTUP` as legacy names; `[tools] startup` outranks
  `.tools_config.json`. `M3_TOOLS_LAZY=false` / `no` / `off` now turn lazy
  loading off in the MCP server too (it honoured only `0`); a value that is not a
  boolean is reported instead of read as on.
  **Affected:** installs that set these names · **Action:** none; prefer
  `m3_config.toml` · **Data status:** none.
- **`[governor]` in `m3_config.toml` sets the background-work governor.** The
  load thresholds resolve `m3_config.toml` first, then `.governor_config.json`,
  then the `M3_GOVERNOR_*` names (read by the settings registry), then the
  defaults; the free-RAM floors and the file's re-check interval read
  `m3_config.toml`, then their env names. An out-of-range `M3_GOVERNOR_*` value is
  now reported at startup instead of clamped.
  **Affected:** installs that tune the governor · **Action:** none; prefer
  `[governor]` in `m3_config.toml` · **Data status:** none.
- **The chatlog store's environment names are legacy; use `--chatlog-db`.** m3
  no longer sets them itself: `--chatlog-db` (cognitive loop, `migrate_memory`)
  and `chatlog_ingest --db` choose the store for the run in-process. A name still
  set logs one warning with the fix, and `m3 doctor` and `m3 config import-env`
  name where it is set. `m3 doctor --fix` no longer renames `CHATLOG_DB_PATH` /
  `CHATLOG_DB` to their `M3_` spellings.
  **Affected:** installs or scripts that set a chatlog store env name ·
  **Action:** pass `--chatlog-db PATH` or set `db_path` in `.chatlog_config.json`;
  then remove the name · **Data status:** none.
- **One setting decides the chatlog store, for every tool.** The five
  environment names that chose it (`M3_CHATLOG_DB_PATH`, `CHATLOG_DB_PATH`,
  `M3_CHATLOG_DB`, `CHATLOG_DB`, `M3_CHATLOG_DATABASE`) are read in one place, in
  that order, so the enrichment and backfill scripts and the chatlog tools agree.
  `M3_CHATLOG_DB` now selects the store outright, like the others; it used to
  change only the default, below a `db_path` set in `.chatlog_config.json`.
  **Affected:** installs that set one of these names · **Action:** none; prefer
  `--chatlog-db` or `db_path` in `.chatlog_config.json` · **Data status:** none.
- **The warehouse schema is versioned and applied by sync.** `pg_sync` brings
  the warehouse to its current schema before every run, from its own migration
  chain and under a lock so two machines cannot race; the manual
  `psql -f pg_warehouse_chatlog_v1.sql` step is gone. A warehouse that keeps its
  tables in `public` is migrated in place. Changes are additive, so a machine on
  an older m3 keeps syncing. `bin/pg_setup.py --check` reports the warehouse's
  version, and sync warns when rows sit in `public` copies it never reads. Each
  store's sync bookkeeping (watermarks, lock) now comes from its own migrations,
  chatlog included.
  **Affected:** installs that sync to a PostgreSQL warehouse · **Action:** the
  warehouse role needs `CREATE` on the warehouse schema for a migration to apply
  (it already needed it: sync created tables itself) · **Data status:** none;
  existing tables and rows are kept.
- **A chatlog store gets its enrichment queues from its own migration (007).**
  The enrichment drain no longer replays a main-store migration against it at
  runtime; schemas change only through migrations.
  **Affected:** installs that enrich a separate chatlog store · **Action:** none ·
  **Data status:** none; queues already present are kept.
- **The LangGraph checkpointer's tables are created by migrations.** Migration
  054 (SQLite) / pg_063 (PostgreSQL) creates them on a new store and brings an
  existing store's tables forward with their checkpoints.
  **Affected:** installs using the LangGraph checkpointer · **Action:** none ·
  **Data status:** none; existing checkpoints are kept and a `deleted_at` column
  is added.
- **`M3_DATABASE` is a legacy setting; use `--database` or `--store`.** m3 no
  longer sets it for itself. It is still read when nothing else names the
  database, with a warning (deprecated). A `--database`
  given to a script also chooses the chatlog when the chatlog is not pinned
  elsewhere, as `M3_DATABASE` did. `m3 config import-env` and `m3 doctor` name
  where `M3_DATABASE` is still set, with the replacement.
  **Affected:** installs or scripts that set `M3_DATABASE` · **Action:** pass
  `--database PATH`, or add `[stores.<name>]` to `m3_config.toml` and pass
  `--store NAME`; then remove `M3_DATABASE` · **Data status:** none.
- **The storage backend is chosen in `m3_config.toml`, not the environment.**
  `[db] backend = "postgres"` (or `m3 config set db.backend postgres`) selects
  it; `--set` changes it for one run. `M3_DB_BACKEND` no longer overrides the
  file: it is read only when the file does not set the backend, with a warning
  (deprecated). `m3 config import-env` (run by
  `m3 upgrade`) moves it into the file, and `m3 doctor` names leftover copies.
  **Affected:** installs that select PostgreSQL with `M3_DB_BACKEND` ·
  **Action:** run `m3 upgrade` (or `m3 config set db.backend postgres`), then
  remove `M3_DB_BACKEND` · **Data status:** none.
- **m3 no longer reads secrets from environment variables.** DSNs, API keys,
  tokens and the vault master key come from the user-only secrets file, then
  the OS keyring entry the file names (or `system/<NAME>` when the file does not
  list it), then the encrypted vault where allowed.
  - **The secrets file lists every secret:** `NAME = "value"`, or
    `NAME = { keyring = "service/key" }` for one kept in the keyring.
  - **New secrets go to the file on every OS**, including the PostgreSQL DSN
    setup stores. `m3 secret set NAME --keyring` stores one in the keyring
    instead and points the file at it.
  - **`m3 config import-env` moves them** from shell startup files, `.env`, the
    Windows persistent environment, m3's own MCP entries and
    `.llm_config.json`. A secret already in the keyring becomes a pointer.
    Copies that disagree are offered masked to a person at the console, or
    listed and skipped. A secret set only in the current shell is listed, not
    stored; another tool's MCP entry is left alone. `m3 upgrade` runs it.
  - **`m3 doctor` names secrets set only where m3 no longer looks**, and
    leftover plaintext copies (`--verbose` lists the places, never values).
  - **Install no longer refuses to run while the old `PG_URL` is set;** it
    names where, and `import-env` stores it as `M3_CDW_PG_URL`.
  - **A named store's `dsn_secret` can be stored in the secrets file.**

  **Affected:** installs that set a DSN, API key or token in the environment,
  a shell profile, `.env`, an MCP env block or the Windows registry ·
  **Action:** run `m3 upgrade` (or `m3 config import-env`), then `m3 doctor` ·
  **Data status:** none; legacy copies are left in place.
- **`m3 setup` moves settings into `m3_config.toml` and the secrets file.** Each
  run imports settings and keys from environment variables, shell startup files,
  `.env` and agent configs (`m3 config import-env --non-interactive`), so an
  upgrade from a release whose `m3 upgrade` lacks the step is covered too.
  Settings that every place agrees on are imported; disagreements are listed with
  the `m3 config set` that settles them, and nothing is written for them. A
  problem here never stops setup. **Affected:** all installs · **Action:** settle
  any disagreement setup lists · **Data status:** none.
- **`m3 doctor --fix` no longer rewrites a `.env` or `~/.claude.json`.** Doctor
  prints the exact rename to make by hand in a `.env`. For `~/.claude.json`,
  which the `claude` CLI manages, it prints the `claude mcp` command to use
  instead. Other MCP client configs are still fixed automatically.
  **Affected:** users with deprecated variable names in a `.env` or
  `~/.claude.json` · **Action:** make the rename doctor prints ·
  **Data status:** none.
- **`chatlog_set_redaction` is a CLI command only.** It writes your config, and
  MCP clients no longer get it: run `m3 chat chatlog_set_redaction --yes`. The
  MCP catalog lists one tool fewer.
  **Affected:** agents that called `chatlog_set_redaction` over MCP · **Action:**
  use the CLI command · **Data status:** none.
- **`M3_DEBUG` output goes to stderr.** The SQL and database-path lines it
  enables were printed to stdout, mixed into a tool's results.
  **Affected:** users who set `M3_DEBUG` · **Action:** read the lines from stderr
  · **Data status:** none.
- **m3's own state moves from `~/.m3-memory` to `~/.m3`.** `m3 setup`, and
  `m3 doctor --fix`, copy the audit trail, `config.json`, version state, `.env`
  and the vault salt into `~/.m3` (the salt into the config root), then
  switch to it. The old directory is kept, with a `MOVED.txt`; databases still in
  `~/.m3-memory/memory/` keep being found. Until the copy has run, m3 keeps using
  `~/.m3-memory`; `m3 doctor` says when it has not. A pinned `M3_MEMORY_ROOT` is
  never moved.
  **Affected:** installs that still have `~/.m3-memory` · **Action:** run
  `m3 setup` or `m3 doctor --fix`; delete `~/.m3-memory` once m3 runs from
  `~/.m3` · **Data status:** copied, never moved or deleted; a failed copy leaves
  m3 on the old directory.
- **The entity extractor's LM Studio address is `llm.lmstudio_url`.** It used
  `M3_EMBED_URL`, which names an embedding endpoint everywhere else.
  **Affected:** users who pointed extraction at LM Studio through `M3_EMBED_URL`
  · **Action:** set `M3_LMSTUDIO_URL` (or `llm.lmstudio_url`) instead ·
  **Data status:** none.
- **A bad `--embed-url` / `--embed-model` stops the run.** `m3_enrich`,
  `m3_enrich_batch` and `m3_entities` printed a warning and carried on with the
  default embedder.
  **Affected:** runs with an invalid embedder flag · **Action:** fix the flag ·
  **Data status:** none; before, such a run could embed with an unintended
  model.
- **About a hundred settings are now read through the configuration registry.**
  They can be set in `m3_config.toml` as well as through their environment
  variables, and `m3 config show` reports them. A malformed or out-of-range
  value (for example a non-number timeout, or a feedback window above its
  ceiling) now stops startup with a message naming the variable, where it was
  ignored or clamped before; a blank value means the default.
  **Affected:** installs that set m3 environment variables · **Action:** fix any
  value the startup error names · **Data status:** none.
- **The `database` argument is refused where it would be ignored:** on a
  non-SQLite backend and on every files tool, where the call used to run against
  the default store.
  **Affected:** callers passing `database` there · **Action:** use the `store`
  argument (see Added) or drop `database` · **Data status:** none.
- **Failures that were silent are reported.** A swallowed programming error or
  malformed query logs a WARNING; an unresolvable files backend and an
  unreadable migration ledger raise instead of answering SQLite and "nothing
  applied"; a missing or malformed named entity vocabulary warns; `m3 doctor`
  reports schema versions from the table the migrator writes (it always showed
  none) and says when it could not count a store.
  **Affected:** all installs · **Action:** read new warnings; each carries
  `observed:` and `inspect:` · **Data status:** none.
- **`memory_write_bulk`'s `*_concurrency` arguments warn** when set, since the
  process-wide settings `memory.fact_enrich_concurrency` and
  `entity.extract_concurrency` govern.
  **Affected:** callers passing those arguments · **Action:** set the settings
  instead · **Data status:** none.
- **Dependencies:** `tomlkit` is a new core dependency, and `pydantic` and
  `starlette`, which m3 imports directly, are declared. The `entity-ner` extra
  adds `packaging`, cannot be installed together with `embed-server`, and the
  GLiNER loader warns when `transformers` is older than m3 supports.
  **Affected:** all installs; `entity-ner` users · **Action:** none; install
  `entity-ner` and `embed-server` in separate environments ·
  **Data status:** none.
- **The `crewai` extra installs with uv and an `mcp` override;** plain pip
  cannot resolve it (crewai pins `mcp<1.29`, m3 requires `mcp>=1.30`). The
  README gives the command.
  **Affected:** CrewAI users · **Action:** install as the README shows ·
  **Data status:** none.
- **Security and FIPS**
  - **`m3 secret set NAME --stdin` stores a value piped in by a script.** The value
    could only be typed at a prompt, so scripted setups could not store the
    database address. A terminal on standard input is refused, so a typed value is
    never shown. With no keyring on the computer, the lookup note now says so
    instead of suggesting a package to install.
    *(Affected: scripted and automated installs.)*
  - **No secret is read from the environment, in two more places.** The dashboard's
    cloud health probe no longer falls back to an exported API key when the secret
    chain fails; it reports the cause. `m3 serve --generate-token` no longer warns that an
    exported `M3_SERVE_TOKEN` overrides the vault (it does not), nor suggests
    exporting one.
    *(Affected: dashboard health on cloud LLM profiles; `m3 serve` token setup.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **PostgreSQL and sync**
  - **Switching to PostgreSQL before storing its address is explained, not a
    crash.** `m3 doctor`, `m3 status` and other commands print what is missing and
    both ways out (store the address, or go back to SQLite) instead of a traceback.
    `m3 config set db.backend` says what the chosen store still needs and that
    running m3 programs keep the old store until `m3 stop` and `m3 setup`.
    *(Affected: installs switching to PostgreSQL.)*
  - **The macOS installer offers PostgreSQL 18.** When no server is found, `m3 setup`
    offers `brew install postgresql@18` (was `postgresql@16`), and the manual hint
    names the same formula.
    *(Affected: macOS users choosing PostgreSQL.)*
  - **Bulk writes on PostgreSQL no longer log a defect.** Every bulk write ran a
    SQLite-only WAL checkpoint and logged a syntax error on PostgreSQL; the write itself
    succeeded. Checkpointing now goes through the storage backend.
    *(Affected: PostgreSQL stores.)*
  - **SQLite-only tools say so on another backend:** `migrate_memory` and
    `split_chatlog_from_core` refuse PostgreSQL.
    *(Affected: PostgreSQL installs.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Search and memory**
  - **An empty, whitespace or `*` query lists memories by their filters**
    (`memory_search`, `memory_search_scored`), ordered by importance then
    recency, instead of returning nothing.
    *(Affected: callers passing an empty
  query.)*
  - **Several m3 processes can open a new SQLite store at the same time.** The
    second process to open a brand-new store could fail at start with "database is
    locked"; it now waits, within the 30-second busy timeout.
    *(Affected: SQLite stores opened by several processes at once before their first
  use.)*
  - **Entity-extraction status comes from migrations on every store.** Chatlog
    migrations 008/009 add the `status` column and its index; main migration 055
    restores that index on stores where it was missing, so processed turns are
    skipped by an index lookup there too.
    *(Affected: installs that run entity extraction.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **m3 install/upgrade UX & correctness**
  - **The notification waiter blocks on the dispatch doorbell when the core has it.**
    With a core that ships `m3-dispatchd` and `m3-wait`, the waiter starts the
    daemon if it is not running and waits in `m3-wait` (about 19 MB committed in
    all, against about 515 MB for the polling child), confirming each wake in the
    store. On that path, mail that arrived while no waiter ran is reported at its
    first wake. `m3 stop` also stops both programs. Otherwise the waiter polls as
    before.
    *(Affected: installs whose core ships the dispatch programs.)*
  - **The web dashboard, the cognitive loop and the Observer & Reflector stages are
    stable.** They leave hardening and follow the deprecation policy.
    *(Affected: everyone.)*
  - **Setup, `m3 doctor` and the install guides speak plainly.** Internal names
    (project code names, tier numbers, task names, step labels) are replaced by
    what each part does; setup no longer asks about Claude Code when it is not
    installed, says how to connect an agent when none is found, and `m3 setup
    --help` hides internal options. The guides give `m3 config set` commands
    instead of environment variables, and the README leads with `pipx` (with how
    to get it on each system). Doctor lines, the dashboard and background-processing
    questions, and the folder and speed-up module messages follow suit; "no agents
    wired" is printed once.
    *(Affected: everyone running setup or reading the guides.)*
  - **`m3 doctor --fix` no longer reports a Claude Code connection that does not
    exist.** With nothing to change it now gives the same verdict as `m3 doctor`,
    including "nothing to check" where Claude Code is not installed, and says when
    no agent is set up instead of "all healthy".
    *(Affected: machines without Claude Code or without wired agents.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Configuration and CLI**
  - **`m3` commands no longer print internal log lines.** `m3 memory ...` and other
    commands printed every embedding request (`httpx: [INFO] HTTP Request: ...`)
    and search details, because modules they load turned on informational logging
    for the whole process when imported. Logging is now set up only by the program
    being run; warnings and errors still show.
    *(Affected: everyone using the `m3` command line.)*
  - **`m3` commands start faster.** A command now loads only what it uses: the
    tool catalog loads for `m3 <domain> <tool>` commands, and the version only for
    `--version`. `m3 config get` and `m3 --help` went from about 350 ms to about
    150 ms on a Windows test machine.
    *(Affected: CLI users and agents that call `m3` directly.)*
  - **Each file m3 rewrites keeps `<name>.bak` and `<name>.backup_history`.**
    - **`<name>.bak`** is the file exactly as it was before the latest change;
      restoring is copying it back.
    - **`<name>.backup_history`** gets one UTC-stamped entry per change: the full
      previous content (every line prefixed `| `, so the history cannot be mistaken
      for the file) and the diff. `m3 restore <file>` lists the versions;
      `m3 restore <file> --at <time>` puts one back, and can itself be undone.
    - **An unfinished change** (an entry with no `END` line) is named by `m3 doctor`,
      with the restore.
    - **The history keeps every previous version,** including any API keys the
      file held. It has the same permissions as the file; delete it after
      rotating a leaked key.
    - **Which files:** MCP client settings, OpenCode, the Gemini hooks, the
      crontab and launchd agents.
    - **What it replaces:** the `.m3bak`, `.bak` and timestamped copies, which
      could overwrite one another. Older ones can be deleted.
    - **When a backup fails,** the file is not rewritten.
    *(Affected: all installs.)*
  - **The storage backend can be set in `m3_config.toml`.** `[db] backend =
    "postgres"` selects PostgreSQL; `M3_DB_BACKEND` still overrides it. Choosing
    PostgreSQL in `m3 install` now writes that line instead of setting
    `M3_DB_BACKEND` in the Windows registry or your shell profile; a value already
    there keeps working, and the installer reports one that disagrees.
    *(Affected: PostgreSQL installs.)*
  - **Setup records its choices in files, not environment variables.** The LLM
    runtime switches and an `--llm-endpoint` pin go to `.llm_config.json` in the
    config root, which every m3 process reads; setup no longer adds them to the
    Windows registry, your shell profile or MCP server `env` blocks. Values already
    there keep working.
    *(Affected: all installs.)*
  - **The LLM endpoint settings in `m3_config.toml` take effect.** `[llm]` `url`,
    `endpoints`, `lmstudio_failover` and `ollama_failover` now choose the failover
    list; they were ignored. Precedence: `m3_config.toml`, then the environment,
    then `.env`, then `.llm_config.json`. The switches accept `on`/`off` as well
    as `1`/`0`, and an unreadable value names the file it came from. `m3 doctor`
    and `m3 config show --origin` list each value still read from
    `.llm_config.json`.
    *(Affected: users who set LLM endpoints.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Agents and integrations**
  - **The agent dispatch store is migrated like the others.** `migrate_memory up
    --target dispatch` brings it to the current schema, `--target all` includes an
    existing dispatch store, and the first send migrates it through the same
    runner, on SQLite and in its PostgreSQL schema, so later dispatch migrations
    reach existing installs.
    *(Affected: installs that use agent dispatch.)*
  - **Tool schemas are smaller:** the universal `database`, `store` and `timeout`
    arguments carry short descriptions and no schema default, cutting the eager
    schema from about 40,800 to 29,100 tokens.
    *(Affected: MCP clients.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Other**
  - **Python 3.15 is tested on the 3.15.0 release and must pass like 3.12–3.14.**
    Release candidates are no longer used.
    *(Affected: Python 3.15 users.)*
  - **Lint and format checks cover the whole repository.** `ruff check .` is the
    gate; `scripts/`, `examples/` and the top-level scripts were outside it. Eight
    unused one-off scripts were removed from `scripts/`. The homelab dashboard
    example no longer blocks its event loop on file writes and the thermal probe.
    *(Affected: contributors; users of the homelab dashboard example.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

### Deprecated

- **`with_m3_history` (LangChain).** It wraps `RunnableWithMessageHistory`, which
  LangChain deprecated in langchain-core 1.3.3 and removes in 2.0. Use LangGraph
  persistence with `M3Saver` instead.
  **Affected:** code calling `with_m3_history` · **Action:** move to `M3Saver`
  before langchain-core 2.0 · **Data status:** none.
- **Legacy environment names for settings** still work and log a
  warning; `m3 config show --origin` shows which name supplied a value.
  `LLAMA_SERVER_EXE` and `GGUF_MODEL_PATH` are now `M3_LLAMA_SERVER_EXE` and
  `M3_GGUF_MODEL_PATH`.
  **Affected:** installs configured through environment variables ·
  **Action:** move settings to `m3_config.toml` or the current names ·
  **Data status:** none.
- **Renamed CLI options:** `embed_backfill --store core|chatlog` is
  `--target`, and `search_differential --store PATH` is `--engine-root`; the
  old spellings still work.
  **Affected:** scripts using those options · **Action:** switch to the new
  names · **Data status:** none.
- **The vault as a source for the primary PostgreSQL DSN** warns on every use.
  **Affected:** PostgreSQL installs whose DSN is in the vault ·
  **Action:** store it in the keyring or the secrets file · **Data status:** none.
- **The `m3-memory[dev]` extra** is now an alias of the `dev` dependency group.
  **Affected:** contributors · **Action:** `uv sync --group dev` (or
  `pip install --group dev` with pip 25.1+) · **Data status:** none.
### Removed

- **Settings that had no effect are retired;** `m3 config show` lists any that
  are still set: `CHATLOG_MODE`, `M3_CLOUD_MINIMIZATION_LEVEL`,
  `M3_CONFIDENCE_MODEL`, `M3_ENTITY_COALESCE_MAX_PAIRS`, `M3_FILES_CORPUS`,
  `M3_FILES_DB_PROMPT_ON_FIRST_USE`, `M3_FILES_DEDUP_LEAF_LIMIT`,
  `M3_FILES_DEDUP_MAX_PAIRS`, `M3_FILES_DEDUP_THRESHOLD`,
  `M3_FILES_DEFAULT_SCOPE`, `M3_FILES_EXTRACT_CONCURRENCY`,
  `M3_FILES_EXTRACT_MAX_ATTEMPTS`, `M3_FILES_PROMO_SUGGEST_THRESHOLD`,
  `M3_TRUST_AUTOTUNE`.
  **Affected:** installs that set them · **Action:** unset them ·
  **Data status:** none.
### Fixed

- **Chat turns are captured on a fresh split install.** The first turn written
  to a never-used chatlog store spilled instead of being stored; spill files
  now drain, chatlog hooks run reliably, and the watchdog honours a paused
  loop. **Affected:** split chatlog installs, integrations (Hermes, LangChain,
  CrewAI, PydanticAI) · **Action:** upgrade · **Data status:** spilled turns
  are stored on the next flush.
- **GDPR erasure removes everything derived from a subject's memories**,
  including in a separate chatlog store, and reports a step that failed.
  **Affected:** GDPR forget requests · **Action:** re-run erasure for subjects
  forgotten before upgrading · **Data status:** earlier erasures may have left
  derived rows.
- **Memory operations stay inside one user and agent.** Dedup, consolidation,
  contradiction detection and scoped search no longer cross users, agents or
  embedding models, skip pinned memories, and return in-scope matches a row
  limit used to drop. Updating a missing memory is an error. **Affected:**
  multi-user or multi-agent stores · **Action:** upgrade ·
  **Data status:** none.
- **Sync and queues hold up under interruption and concurrency.** An
  interrupted PostgreSQL sync no longer advances past rows it did not store,
  and concurrent writers no longer lose queue retry counts. A fact-enrichment
  run that finds no facts is not repeated every pass (SQLite migration 060).
  **Affected:** PostgreSQL sync; fact enrichment · **Action:** upgrade (the
  migration runs on first open) · **Data status:** none.
- **The CPU embedder (tier 2) comes back after an outage.** Once its breaker
  had opened, a background check could take the single retry slot meant for
  the next real embed, and tier 2 then stayed off until m3 restarted.
  **Affected:** installs using the shared CPU embedder · **Action:** none ·
  **Data status:** rows written meanwhile without a vector are filled by
  `m3 embedder backfill`.
- **Searches no longer stall on keyword-index rewrites.** Recording that a memory
  was read rewrote its keyword-index entry, and the index upkeep that followed
  could pause a search for a quarter of a second. The index is now rewritten only
  when a memory's text changes. **Affected:** SQLite stores · **Action:** none
  (applied by the schema upgrade) · **Data status:** none.
- **Upgrading from an older release leaves m3 fully working.** After `pipx upgrade`
  and `m3 setup`, answering *Keep the current settings* left Claude Code's hooks and
  the agents' entries on the old install, and verification failed. Setup now
  repairs wiring that points at an older install (backing up each file), registers
  `m3_memory` in Claude Code, accepts an empty leftover chat store instead of
  asking for it to be deleted, finds an embedding model already in the home folder
  instead of downloading another copy, and offers background processing to an
  install that never had it. **Affected:** installs upgraded from a release without
  `m3 upgrade` · **Action:** run `m3 setup` · **Data status:** none.
- **m3 in a `su` shell uses the user's systemd session.** The shell drops
  `XDG_RUNTIME_DIR`, so every service step failed; m3 now reruns the command with
  it set when the session runs, and says once, up front, how to enable one when
  there is none. **Affected:** Linux users reached through `su` · **Action:** with
  no session, `loginctl enable-linger <user>` as root · **Data status:** none.
- **The macOS notification waiter's job file is valid XML.** A comment in it contained
  `--`, which launchd tolerates but other tools reject, so they could not read the job.
  **Affected:** macOS · **Action:** none; `m3 setup` rewrites it · **Data status:** none.
- **LangChain chat history keeps turns in order when messages arrive quickly.** A second
  `add_messages` call could reuse the first call's turn numbers while those turns were still
  being written, and the conversation read back out of order.
  **Affected:** `[langchain]` users of `M3ChatMessageHistory` · **Action:** none ·
  **Data status:** conversations written out of order keep their stored order.
- **Sync no longer misses rows that reach the warehouse late.** Each sync read the rows
  created or updated after its previous sync, by the writing machine's clock, so a row
  written before another machine's sync and sent after it never reached that machine.
  The warehouse now stamps when each row arrives (warehouse migration 2), and syncs read
  by that. The first sync after upgrading reads the whole warehouse once, which also
  brings every row earlier versions missed (2,011 on one test machine).
  **Affected:** everyone syncing two or more machines · **Action:** none; the warehouse
  updates itself on the first sync and needs the ownership in
  [Upgrading your warehouse](docs/WAREHOUSE_UPGRADE.md) · **Data status:** missed rows are
  in the warehouse and arrive on the first sync.
- **With separate memory and chat stores, each store gets only its own rows.** Sync
  pulled the whole warehouse into both, so the memory store filled with chat turns and
  the chat store with memories. New syncs no longer add them; rows already copied stay.
  **Affected:** installs with separate memory and chat stores · **Action:** none ·
  **Data status:** existing copies are left in place.
- **Sync no longer fails on the notification store.** Since the notification store
  joined the migration set, every SQLite sync also tried to sync it and logged
  `no such table: memory_items`. Sync now covers only the memory and chat stores.
  **Affected:** SQLite installs that sync to a warehouse · **Action:** none ·
  **Data status:** none; memories and chat synced normally.
- **`m3 upgrade` no longer upgrades a source checkout or a git-URL install from PyPI.**
  An m3 installed with `pip install -e` or from a git URL was taken for a PyPI install, and
  the upgrade could target the system Python. It now names how m3 was installed and prints
  the steps for that method instead of running anything.
  **Affected:** source checkouts and git-URL installs · **Action:** follow the printed steps
  ([Upgrade Guide](docs/HOW-TO-UPGRADE.md)) · **Data status:** none.
- **Sync to a warehouse in the `m3_warehouse` schema works after the schema update.**
  The step that sets the schema was undone by a later check, so every warehouse table
  read as missing (`relation "memory_items" does not exist`) and nothing synced.
  **Affected:** PostgreSQL warehouse sync · **Action:** none · **Data status:** none; the
  failed runs changed nothing and the next run sends what they missed.
- **The warehouse schema update works when another account created the tables.** It
  stopped with `must be owner of table memory_items` even when nothing was missing. It now
  changes only what is missing, and a change that needs ownership names the table, its
  owner and the fix. `pg_setup --check` reports tables owned by another account (exit 3).
  **Affected:** warehouses whose tables an administrator created · **Action:** see
  [Upgrading your warehouse](docs/WAREHOUSE_UPGRADE.md) · **Data status:** none.
- **A point-in-time search finds a memory that was later superseded.** Searching
  with `as_of` returned nothing for a memory replaced after that time, so the
  history was stored but could not be searched. A memory deleted outright stays
  hidden.
  **Affected:** `memory_search` with `as_of`, on SQLite and PostgreSQL ·
  **Action:** none · **Data status:** none; superseded memories were always kept.
- **Moving settings out of shell startup files reads only real settings.** A value
  the shell works out when it starts, such as `$(security find-generic-password ...)`,
  was stored as the secret itself; lines inside functions and heredocs, and
  `NAME=value command` prefixes, were read as settings; and `$HOME/...` was called a
  disagreement with the same path written out. Such values are now reported and
  never stored, `$HOME` and `~` are expanded, and a setting found only in shell
  startup files says that m3's background services will now use it.
  **Affected:** macOS and Linux installs whose shell startup files set m3 variables ·
  **Action:** if an upgrade printed "stored" for a secret your shell reads from the
  keychain, run `m3 secret set NAME` with the real value ·
  **Data status:** a wrongly stored secret could make m3 read the wrong key when the
  keychain is locked; no memory data is affected.
- **A PostgreSQL store from an earlier release is brought up to date on first
  use.** Choosing PostgreSQL with `m3 config set db.backend postgres` against an
  existing store left it on its old schema while `m3 doctor` reported it healthy.
  The store now migrates the first time m3 uses it (one process at a time),
  `m3 doctor` shows its schema version, and `m3 doctor --fix` migrates it and says
  what it applied.
  **Affected:** PostgreSQL stores selected after install · **Action:** none ·
  **Data status:** missing tables and columns are added; existing data is kept.
- **An upgrade no longer drops the rest of a migration when a column already
  exists.** On SQLite, a migration that adds a column the store already had was
  rolled back whole and still recorded as applied, so its other statements never
  ran; migration 055 restores the one index this lost. Only the existing column is
  now skipped.
  **Affected:** SQLite stores upgraded across a migration whose column was already
  present · **Action:** none ·
  **Data status:** none; an index that speeds up entity extraction is re-created
  where it was missing.
- **The chatlog title and embedding backfill scripts find the databases under
  the engine root.** With no `--core-db`/`--chatlog-db` they looked in
  `<checkout>/memory`, so run from elsewhere they found nothing, and from a dev
  checkout they could pick up an empty stub.
  **Affected:** `m3_chatlog_backfill_title` and `m3_chatlog_backfill_embed` run
  without explicit database arguments · **Action:** none ·
  **Data status:** none; runs that found no database changed nothing.
- **Reads from a named store or `--database` record their access in that
  store.** A search stamped "last accessed" in the store the process first read
  from, so a memory read from another store never had it recorded: grading it
  with `memory_grade` was refused as stale, and decay did not see the use. A
  store chosen for a call also takes precedence over `M3_DATABASE` now.
  **Affected:** installs that use `--store`, `--database` or several stores ·
  **Action:** none · **Data status:** access counts for those stores were not
  recorded until now; nothing else changed.
- **The session-start capture check works on a PostgreSQL primary store.** It
  reported "capture status UNKNOWN" at every session start and could leave
  empty SQLite files behind; it now reads when the chatlog last wrote, from the
  same state `m3 chatlog status` reports, and opens no database.
  **Affected:** PostgreSQL-primary installs using Claude Code · **Action:**
  none · **Data status:** none; any empty `agent_memory.db` or
  `agent_chatlog.db` it created in the engine root holds no data.
- **The session-handoff server no longer opens the store when it starts.** It
  creates its table on the first save, in the store that run selected.
  **Affected:** users of the `session_handoff` MCP server · **Action:** none ·
  **Data status:** none; existing handoffs are kept.
- **`m3 doctor` checks Cursor and Cline for `M3_EMBED_GGUF`.** The check for an
  embedder path pinned in an MCP server's env block, and its `--fix` scrub,
  skipped those two clients.
  **Affected:** Cursor and Cline users · **Action:** run `m3 doctor` ·
  **Data status:** none.
- **Scheduled jobs and services use the config root chosen at setup.** With a
  non-default config root, the background jobs (cognitive loop, sync,
  maintenance, the dashboard and others) ran against the default roots while the
  MCP server and hooks used the chosen ones. Registered jobs now carry
  `--config`. Installs on the default roots are unchanged.
  **Affected:** installs with a non-default config root (`M3_CONFIG_ROOT` or a
  root chosen in `m3 setup`) · **Action:** run `m3 schedules repair` ·
  **Data status:** jobs may have written to the default store; check it for
  memories and chat logs that belong in your chosen store.
- **The notification waiter skips agents marked offline.** It kept watching
  retired agents, and when no agent had checked in recently it fell back to every
  agent ever registered without saying so.
  **Affected:** multi-agent setups · **Action:** re-register the waiter
  (`m3 schedules add AgentOS_NotificationWaiter`, admin shell on Windows) ·
  **Data status:** none.
- **Windows: the hourly sync no longer opens a window, and reports its real
  result.** When a `.venv` directory sat inside the installed package, `pg_sync`
  restarted itself under it: a console window flashed every hour, and the sync
  always read the restart as success, including runs that failed or were
  skipped. `pg_sync` and the knowledge CLIs now restart only from outside any
  virtual environment, and a restart keeps the exit code and arguments.
  **Affected:** Windows installs with a `.venv` inside the package directory ·
  **Action:** none (the stray `.venv` can be deleted) · **Data status:** a sync
  reported as successful may not have run; the next run catches up.
- **The embed server listens on the configured port.** m3 started
  `m3-embed-server` on 8082 whatever `embed.server.port` said in
  `m3_config.toml`, while checking the configured port.
  **Affected:** users who set `embed.server.port` in the config file ·
  **Action:** restart the embed server · **Data status:** none.
- **Text settings in `m3_config.toml` are type-checked.** Thirteen text settings
  (such as `dashboard.host`) accepted a number or a list without an error.
  **Affected:** config files with a wrong-typed text value · **Action:** fix the
  value the error names · **Data status:** none.
- **Every tool resolves the data roots the same way.** About twenty places
  carried their own copy of the root rules, and several had drifted: the
  notification waiter wrote its log under the engine root's parent and ignored
  `M3_LOGS_ROOT`; `.tools_config.json` did not expand `~` in `M3_CONFIG_ROOT`;
  the session-start capture check could look for the database under the code
  checkout and report capture as stopped when it was not. All of them now use
  one resolver.
  **Affected:** installs that set `M3_LOGS_ROOT`, `M3_MEMORY_ROOT` or a
  `~`-relative `M3_CONFIG_ROOT` · **Action:** none · **Data status:** none;
  the notification waiter's new log starts in the logs root.
- **A POSIX-style `M3_MODELS_ROOT` is kept as given on Windows.** Set from Git
  Bash or WSL (`/data/models`), it was turned into `C:\data\models`, as the
  other roots no longer do. The models root now follows the same rule as the
  config, engine and logs roots, and is returned with forward slashes like them.
  **Affected:** Windows installs that set `M3_MODELS_ROOT` or `M3_MEMORY_ROOT`
  to a POSIX-style path · **Action:** none · **Data status:** none; model files
  found in the old location are found in the new one unless the two differ.
- **Destructive-tool hints name `M3_MCP_PROXY_ALLOW_DESTRUCTIVE`.** The proxy's
  and `m3_call`'s messages still told users to set the deprecated
  `MCP_PROXY_ALLOW_DESTRUCTIVE`, and `measure_tool_tokens.py` set it itself,
  which printed a deprecation warning on every run.
  **Affected:** users enabling destructive tools through the proxy ·
  **Action:** none; the old name still works until it is removed ·
  **Data status:** none.
- **Re-embedding a long memory no longer flattens it.** `memory_update` with
  `reembed=True` and `re_embed_all.py` wrote one vector over all of a long
  memory's window rows, so the later parts stopped matching searches. Both now
  re-embed the way a new write does, with one vector per window.
  `m3 embedder backfill` also repairs memories written before 2026.9.8.0 that
  had window vectors but no default one, which search could not reach.
  **Affected:** all installs with memories long enough to be windowed ·
  **Action:** run `m3 embedder backfill` once to repair older memories ·
  **Data status:** memories re-embedded by earlier versions keep their flattened
  vectors until re-embedded again.
- **Warehouse sync no longer skips rows after a failed batch.** When a batch
  failed (for example "database is locked"), sync still moved on as if it had
  succeeded, so those rows were not sent again unless edited, and the run
  reported success. Now the failed direction is retried on the next run, and
  `pg_sync` exits 1 naming every direction that failed.
  **Affected:** installs that sync to a PostgreSQL warehouse · **Action:** none ·
  **Data status:** rows skipped this way by earlier versions are not re-sent
  until they change; a full re-sync (clear the `sync_watermarks` rows) sends them.
- **PostgreSQL: features that failed, several of them silently, now work.**
  Memory history diffs, the single-text embedding cache, conversation window
  and gist summaries, conversation search pairing, the bulk embedding cache
  (every bulk write re-embedded), `m3_enrich` group lookup, the LangChain store,
  client and chatlog delete, `chatlog_strip_framing_backfill`, `re_embed_all`,
  knowledge-base browse, the enrich report, files and chatlog full-text search,
  and secret rotation with its audit trail. `m3 doctor` lists the core, chat and
  files stores on PostgreSQL as it does on SQLite.
  **Affected:** PostgreSQL installs · **Action:** none ·
  **Data status:** window and gist summaries that earlier versions failed to
  create are not back-filled.
- **PostgreSQL: JSON metadata is read correctly.** `agent_get` failed,
  `memory_inbox` showed every sender as "?", and
  `chatlog_strip_framing_backfill --apply` and the GLiNER conversation
  allowlist crashed. `memory_get` returns `metadata_json` as JSON text, as on
  SQLite.
  **Affected:** PostgreSQL installs · **Action:** a client that read
  `memory_get`'s `metadata_json` as an object on PostgreSQL now receives a JSON
  string · **Data status:** none.
- **Memories written through the MCP server get contradiction detection,
  enrichment and entity extraction.** The MCP default `variant=""` made all
  three skip the memory.
  **Affected:** all installs, writes through MCP · **Action:** none ·
  **Data status:** memories written through MCP by earlier versions are not
  re-processed.
- **`memory_link` refuses an unknown relationship type,** as well as a link to
  a memory that does not exist, and reports a duplicate as "Link already
  exists". `explain=True` explains exact-match results and results found
  without a query vector, which printed nothing.
  **Affected:** all installs · **Action:** none · **Data status:** links with
  an unlisted relationship type created by earlier versions are kept.
- **`search_mode="keyword"` returns lexical results;** it returned nothing. An
  unknown mode is now an error, and `chatlog_search` refuses `vector` and
  `semantic`, which it accepted and ignored.
  **Affected:** all installs · **Action:** use `hybrid`, `semantic` or
  `keyword` (`chatlog_search`: `keyword`) · **Data status:** none.
- **`memory_search` leaves out scratchpad memories unless
  `include_scratchpad=true`,** as its schema always said; it returned them on
  every search.
  **Affected:** all installs · **Action:** pass `include_scratchpad=true` to
  keep them in results · **Data status:** none.
- **LangChain client: a search made just before the process exits records its
  access,** so `memory_grade` accepts feedback for it, and shutdown no longer
  leaves a pending task behind.
  **Affected:** the LangChain integration · **Action:** none ·
  **Data status:** access times that earlier versions dropped at exit are not
  recovered.
- **Tools given an explicit database act on that database.**
  `m3_enrich_assign`, `migrate_entity_vocab` and `m3_chatlog_enrich_backfill`
  with `--db` / `--main-db` acted on the `M3_DATABASE` store when that variable
  was set.
  **Affected:** those tools run with an explicit database while `M3_DATABASE`
  was set · **Action:** if you did, check the `M3_DATABASE` store for those
  changes · **Data status:** changes from such runs are in the `M3_DATABASE`
  store.
- **Calls that pass `database=` read that database.** Chatlog search, promote
  and the cost report read the default store, and files promotion started
  inside a running event loop wrote to the default store.
  **Affected:** those calls with `database=`; files promotion from async code ·
  **Action:** none · **Data status:** files promotions made from async code by
  earlier versions may be in the default store.
- **Legacy vault secrets are readable again** and are re-encrypted on first
  read; they resolved to nothing.
  **Affected:** installs with secrets stored in the older vault format ·
  **Action:** none · **Data status:** legacy secrets are re-encrypted in place
  on first read.
- **The device-salt consistency check runs** on every backend and warns on a
  mismatch; it never ran.
  **Affected:** all installs · **Action:** if m3 warns about a salt mismatch,
  follow the `inspect:` pointer in the message · **Data status:** none.
- **`enrichment_runs` start and finish times carry seconds.**
  `m3_enrich_batch` and `release_orphan_claims` wrote hours and minutes
  followed by microseconds.
  **Affected:** `m3_enrich_batch` users · **Action:** none ·
  **Data status:** rows written by earlier versions keep the malformed times.
- **Choosing FIPS mode in setup is saved.** It is written to `m3_config.toml`
  instead of asking you to add environment variables to your shell profile, and
  setup asks for an environment variable only when you put your settings folder
  somewhere other than the default.
  **Affected:** installs choosing FIPS mode or a custom settings folder ·
  **Action:** none · **Data status:** none.

- **PostgreSQL and sync**
  - **Warehouse sync probes the address it connects to.** With `hostaddr` in the
    DSN, the reachability check resolved the host name instead, which may name only
    the certificate, and skipped every sync as unreachable. A host list is
    reachable when any of its hosts answers.
    *(Affected: warehouse DSNs using
  `hostaddr` or several hosts.)*
  - **A warehouse address kept in an environment variable keeps working after an
    upgrade.** The new version reads no secret from the environment, and an older
    release's upgrade had no step to move it, so sync found no warehouse while the
    upgrade reported success. Setup moves it.
    *(Affected: installs whose warehouse
  DSN was an environment variable.)*
  - **Sync's reachability check probes the address it connects to.** A host setting
    naming another address (one kept after the warehouse moved) could skip a sync
    that would have worked.
    *(Affected: installs with `sync.target_ip` or
  `sync.warehouse_host`.)*
  - **A failed warehouse-sync pass in the cognitive loop logs a warning.** It was an
    INFO line, so a sync that failed every hour went unnoticed.
    *(Affected: installs
  that sync.)*
  - **A relationship or embedding whose memory has not arrived yet no longer fails the
    sync.** One such row failed its whole batch on every run; it now waits for its memory.
    *(Affected: syncing installs.)*
  - **Tools refuse a SQLite path on PostgreSQL cleanly.** With PostgreSQL selected
    and no connection string set, a tool given `--database` stopped with a
    traceback instead of its error message.
    *(Affected: PostgreSQL installs.)*
  - **PostgreSQL warehouse sync falls back correctly** when its fast path cannot
    be imported; it failed with `UnboundLocalError`.
    *(Affected: PostgreSQL warehouse sync.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Chatlog capture**
  - **Honest health and status.** The dashboard no longer reports "All
    subsystems nominal" when a probe failed, treats a cloud 5xx or 429 as
    healthy, or shows an unreadable store as absent; `m3 dashboard` start and
    `--stop` check the result; destructive chatlog maintenance is refused when
    the chatlog store cannot be resolved (it ran on the main store). The proxy's
    `/health` reports a failed tool catalog, and upstream key errors are 502s
    naming the backend's key.
    *(Affected: dashboard and proxy users.)*
  - **Library integrations get no chatlog hook.** For LangChain, `m3 chat init`
    stored and told you to source a hook script that does not exist.
    *(Affected: LangChain chatlog users.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Search and memory**
  - **Embedding settings changed at runtime take effect**, and the embed sweep
    says whether a batch failed for want of a vector, a dimension mismatch or a
    write error.
    *(Affected: all installs.)*
  - **With the local LLM's breaker open, the cloud embed tier still runs.** It
    was skipped. An unset `LM_API_TOKEN` no longer sends a placeholder bearer
    that an auth-enabled LM Studio rejects.
    *(Affected: setups using the cloud
  tier or LM Studio auth.)*
  - **Semantic search on SQLite considers every memory.** It ranked a fixed set of
    50 memories, the same for every query, so most of a store could not be found by
    meaning. Hybrid searches whose keywords matched nothing landed there too. It now
    ranks every memory in scope.
    *(Affected: SQLite stores.)*
  - **Deleting a memory no longer fails after nightly maintenance.** When the
    maintenance pass compacted a store, the next delete through an already-running
    m3 could fail with "no such table: memory_items". m3 now refreshes each
    connection's view of the database before using it.
    *(Affected: SQLite stores
  under 500 MB.)*
  - **`m3_entities_gliner` flags beat the environment.** `--model`, `--threshold`,
    `--batch-size` and `--device` were overridden by the matching `M3_GLINER_*`
    variables; a flag given on the command line now wins.
    *(Affected: users of `m3_entities_gliner` who set `M3_GLINER_*`.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **m3 install/upgrade UX & correctness**
  - **Setup never overwrites a file it cannot read.** Uninstall, the settings
    writers and the Hermes provider refuse a malformed `settings.json` or
    `m3.json` instead of replacing it; `m3 doctor` exit codes follow its
    verdict; Windows permission hints name the user.
    *(Affected: all installs.)*
  - **Embedder and HTTP failures say what happened.** Tier-2 probes (doctor,
    `embedder_status`, the shared-embedder check, the watchdog) tell refused,
    timed out, 401/403, untrusted certificate, bad URL and an unhealthy server
    apart, each with its fix, and all use the same `embed.fallback_url`. The
    watchdog no longer restarts a server that answers but refuses. LLM, cloud,
    proxy and dashboard errors name the endpoint (no query string), status and
    cause; response bodies in logs are redacted; one log line no longer quotes
    memory text.
    *(Affected: all installs.)*
  - **The embed watchdog guards only a server on this machine.** With
    `fallback_url` on another machine it restarted the local service whenever that
    server was unreachable.
    *(Affected: shared mode with a remote server.)*
  - **`m3 doctor` and `embedder_status` probe `https://` embed servers.** Both
    used a plaintext-only HTTP client and now share one probe.
    *(Affected: `https://` fallback URLs.)*
  - **A second m3 user on a shared computer gets an embedder and dashboard of its
    own.** Another account's m3 held the usual ports, so this account had neither,
    and the fix was to choose ports by hand. Setup now picks free ports (Linux,
    macOS) and records them; a registered embedder service moves to the new port
    (its own installer left the port out), clients follow it, and the doctor checks
    it. A port you chose is never changed.
    *(Affected: Linux and macOS computers
  with more than one m3 user.)*
  - **The embedder service starts when an unsupervised server of yours holds its
    port.** A repair could leave one running; the service then restarted in a loop,
    loading the model every few seconds. Setup stops your own unsupervised server
    first, never another account's.
    *(Affected: Linux and macOS.)*
  - **Setup no longer reports "verification passed — m3 is installed and working"
    before checking the background services**, which then failed.
    *(Affected: all
  installs.)*
  - **An embed server started by an older install's crontab line is replaced by m3's
    service.** It held the port, so the new service could not start; the job audit
    also misread `@reboot` lines. Where m3 can run the service, setup removes the line
    (the crontab is saved first) and stops what it started.
    *(Affected: Linux and
  macOS installs that followed older guides.)*
  - **Windows: an upgrade no longer asks for an administrator over the dashboard
    task.** `M3_CONFIG_ROOT` set to the default folder counted as a custom one, so
    every job gained a `--config` argument and setup tried to recreate a boot task.
    *(Affected: installs with `M3_CONFIG_ROOT` set to the default.)*
  - **`m3 doctor` reports what is true.** It no longer counts another user's or a
    container's processes as this user's (a container host reported a cognitive loop
    that did not exist); it checks the embedder with this install's binary, not an
    older copy on PATH; it no longer says "no agent is set up" when Claude Code is;
    it names `m3 setup --cognitive-loop` for a missing loop; and setup reports a
    dashboard boot task as starting only when it was enabled.
    *(Affected: all
  installs.)*
  - **An agent still naming the moved `~/.m3-memory` root is repointed.** The old
    folder survives its migration, so the entry passed the existence check while its
    state went to the old place.
    *(Affected: installs migrated from `~/.m3-memory`.)*
  - **Two m3 processes starting on a new store no longer fail its setup.** Each takes a
    backup before updating the store, and two in the same second chose the same file name;
    the second refused to overwrite the first and its update failed (`no such table`).
    *(Affected: fresh stores opened by two processes at once.)*
  - **`m3 schedules verify` names the agent the notification waiter watches.** With
    no agent registered it printed "watching every registered agent ... (0 of 0)"
    while it watched `claude-code`.
    *(Affected: installs with no registered agents.)*
  - **An installed m3 carries all of its schema migrations.** The package left out
    two migration sets, so after an upgrade `m3 doctor --fix` failed with "no
    migrations found" and the upgrade reported itself incomplete.
    *(Affected: installs from a built package (pip, pipx, `m3 upgrade`).)*
  - **On a computer shared by several people, setup no longer touches another
    user's m3.** It no longer offers to stop another account's background service
    (or asks for a password to do so). When another account's m3 already uses the
    embedder or dashboard port, setup and `m3 doctor` say so and name the setting
    to change, instead of reporting this account's server as running.
    *(Affected: Linux and macOS computers with more than one m3 user.)*
  - **A new install starts clean.** A fresh store no longer reports "DEGRADED ·
    chatlog: unreadable" before its first capture, and no longer gets an automatic
    weekly report as its first memory (the first one now comes a week after
    install). Commands no longer print a line per request to the embedder, and
    pressing Ctrl-D at `m3 secret set` stops cleanly instead of showing an error.
    *(Affected: new installs; every CLI user.)*
  - **Setup reports a failed write of Claude's settings.** When
    `~/.claude/settings.json` could not be written, setup still said the hooks were
    installed.
    *(Affected: all installs.)*
  - **`m3 doctor` and `m3 install` look in `~/.claude.json`.** The places checked
    for an environment variable (where a disagreeing or deprecated variable is
    set, and the refusal of a deprecated `PG_URL`) now include Claude Code's
    `~/.claude.json` server `env` blocks, `m3`'s own `.env` and
    `.llm_config.json`. A `PG_URL` there was missed.
    *(Affected: Claude Code users.)*
  - **Windows: `m3 schedules add` no longer stops the embed server.** Re-registering
    any task stopped the Rust `m3-embed-server` and tried to restart it through a
    task that does not exist on Windows, cutting off embeddings until its own
    service restarted it, and printing a misleading warning.
    *(Affected: Windows with the Rust embed server.)*
  - **Windows: `m3 schedules add/remove/repair` from an admin shell runs directly.**
    It asked "Run it elevated now?" even when already elevated; with no one at the
    console (a scheduled or hidden run) it waited for ever. It now runs when
    elevated, and with no one to answer it stops and says to use an admin shell.
    *(Affected: Windows.)*
  - **`m3 schedules verify` names the right fix.** It said "re-register with
    `--repair`", which `m3 schedules` does not accept; it now names
    `m3 schedules add <task>` or `m3 schedules repair`.
    *(Affected: all.)*
  - **Installed m3 finds its own scripts from every command.** Several commands
    looked for m3's `bin/` directory one level too high on a pip-installed copy,
    and two could put the current directory on Python's import path when it was
    missing. One resolver now locates it for all of them.
    *(Affected: pip/pipx installs.)*
  - **Setup and upgrade:** a home directory containing a quote or ending in a
    backslash no longer breaks the PostgreSQL schema step, and interpreter checks
    no longer import m3 from the current directory (a bare venv could be approved
    for scheduled tasks, and a removed m3 reported as installed).
    *(Affected: all platforms.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Configuration and CLI**
  - **Read-only opt-in tools run from the CLI without `--yes`.** `m3 memory
    memory_search_routed`, `memory_search_multi_db`, `memory_export` and `gdpr_export`
    asked for `--yes` and said they "mutate/delete data". They are still opt-in over MCP.
    *(Affected: CLI users of those four tools.)*
  - **Batch work no longer loses items.** A batch result shorter than its input
    is an error instead of dropping the remainder, `promote_pipeline` reports a
    short judge reply as that batch's error, a failed bulk embed or write cancels
    its sibling tasks, and the bulk embed path retries only network errors and
    HTTP 5xx/413/429. A failed entity-vector commit is no longer reported as rows
    written.
    *(Affected: all installs.)*
  - **Configuration and state files are written atomically with a unique
    temporary file,** so two writers cannot overwrite each other's file, and a
    failed write of `.migrate_config.json` no longer loses the remembered backup
    directory. JSON written this way has LF line endings on Windows too.
    `.log_config.json` is created where the governor config is.
    *(Affected: all installs.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

- **Agents and integrations**
  - **`conversation_summarize` works;** every call raised `NameError`.
    *(Affected: all installs.)*
  - **MCP tool calls honour `timeout` and the per-tool time budgets,** as the CLI
    and `m3_call` already did.
    *(Affected: MCP clients.)*
  - **SLM profiles without `api_key_service` work.** `m3_enrich`,
    `run_observer`, `run_reflector`, `m3_entities` and `m3_enrich_batch` crashed
    with `TypeError`.
    *(Affected: SLM profiles that omit `api_key_service`.)*
  - **The notification waiter wakes on every new notification.** On SQLite a
    write that left the file size unchanged within one clock tick could go
    unseen, and after a restart a leftover reader could end the new run.
    *(Affected: agents using `m3_notification_waiter`.)*
  - **Agent client detection skips venv launchers,** including uv-made venvs and
    the Windows `Scripts\python.exe` redirector, so the agent's own process is
    reported.
    *(Affected: venv and pipx installs.)*
  **Affected:** as each item says · **Action:** none · **Data status:** none.

## [2026.10.7.1] — 2026-10-07 — upgrades that say only what you need to do

### Fixed

- **`m3 stop`, setup and upgrades no longer stop programs that merely mention
  an m3 process.** A terminal running `grep m3-embed-server`, a `tail -f` of an
  m3 log or an editor with an m3 script open could be stopped as if it were
  that process; only processes that run it are stopped now.
  **Affected:** all platforms · **Action:** none · **Data status:** none.

- **An upgrade that updates the Claude Code plugin says so in one line and
  asks for one action: restart Claude Code.** It used to print the whole
  health report, then suggest `/mcp`, which does not load a new plugin.
  **Affected:** installs using the m3 Claude Code plugin · **Action:** none ·
  **Data status:** none.

- **`m3 upgrade` installs a release published minutes earlier** even when the
  package manager's cached index has not caught up; it used to report
  "no newer version".
  **Affected:** pipx installs, seen on Windows · **Action:** none ·
  **Data status:** none.

- **A new native core takes effect without a manual restart.** On Linux,
  re-registering the Rust embed server restarts it (m3-core-rs 3.10.7). On
  Windows, `m3 doctor --fix` at a console restarts an embed server an
  unattended upgrade could not, after one administrator prompt.
  **Affected:** installs with the Rust embed server (Linux, Windows) ·
  **Action:** on Windows, if `m3 doctor` reports the embed server still runs
  the previous native core, run `m3 doctor --fix` and approve the prompt ·
  **Data status:** none.

- **An unattended Windows upgrade no longer waits on an administrator prompt
  nobody can answer.** It raised one, waited out setup's 15-minute budget, and
  reported setup as failed; it now finishes and names `m3 doctor --fix` for the
  step that needs approval. An administrator step you approve is now reported
  as done (it was reported as declined), and a prompt left unanswered for five
  minutes no longer runs its steps if approved later.
  **Affected:** Windows setups and upgrades that need administrator rights ·
  **Action:** none · **Data status:** none.

### Changed

- **Upgrades no longer repeat "no local LLM runtime detected"** on a machine
  that never had one; they warn instead when a runtime that was enabled for
  enrichment is not reachable.
  **Affected:** all platforms · **Action:** none · **Data status:** none.

## [2026.10.7.0] — 2026-10-07 — upgrades that finish on their own, on Linux too

### Fixed

- **Linux: an upgrade that installs a new native core restarts the Rust embed
  server.** In 2026.10.6.0 the server kept the previous core and setup only
  printed the restart command.
  **Affected:** Linux installs with the Rust embed server ·
  **Action:** none; `m3 upgrade` restarts a server still on the previous core ·
  **Data status:** none.

- **`m3 upgrade` finds its own launcher when the shell's PATH lacks the pipx
  bin directory,** instead of reporting m3 as not installed.
  **Affected:** running `~/.local/bin/m3 upgrade` from a shell without
  `~/.local/bin` on PATH (macOS, Linux) · **Action:** none ·
  **Data status:** none.

## [2026.10.6.0] — 2026-10-06 — upgrades that finish on their own

### Fixed

- **Windows upgrades finish without manual steps.** When the upgrade command's
  own launcher is the only thing holding `m3.exe`, the upgrade continues in a
  new window and logs its output. Agent sessions running m3 are listed in the
  plan and, once you confirm, m3 is stopped in them (unattended runs: pass
  `--stop-agents`). Setup no longer waits on services it cannot stop. Scripts
  that cannot hand off get a non-zero exit and the exact command to run.
  **Affected:** Windows · **Action:** reconnect the listed agents afterwards
  (Claude Code: `/mcp`); on 2026.10.5.0 and earlier, run the
  `python.exe … m3_upgrade.py --yes` command it prints · **Data status:** none.

- **Upgrades do only the work that is needed and report it briefly.** Steps
  print in order and end in a summary (version change, agents to restart or
  reconnect, health, log file). Configuration, services and the search index
  are rewritten only when they differ from what is needed; each condition is
  reported once; healthy checks take one line (`m3 doctor --fix --verbose` for
  full detail). The loop watchdog no longer restarts a cognitive loop that has
  only just started, which could stall an upgrade for a minute.
  **Affected:** all platforms · **Action:** none · **Data status:** none.

- **`m3 upgrade` keeps the Claude Code plugin current,** and `m3 doctor` warns
  when the plugin is older than the installed package.
  **Affected:** installs using the m3 Claude Code plugin ·
  **Action:** run `m3 doctor --fix --fix-hooks`, then restart Claude Code ·
  **Data status:** none.

- **Agent configuration points at the installed m3, in the format each agent
  reads.** Setup run from inside a source checkout no longer wires agents to
  the checkout, and an unused `mcpServers` entry is removed from OpenCode's
  config.
  **Affected:** setups run from a source checkout; OpenCode users ·
  **Action:** if `m3 doctor` reports agent configs at a stale payload clone,
  run `m3 doctor --fix --fix-hooks` from another directory · **Data status:** none.

- **Setup messages name the actual cause and fix:** an embed server left
  running as administrator is described in plain words, the "OpenClaw CLI not
  on PATH" warning names the `~/.openclaw` folder it detects OpenClaw from, and
  `HERMES_HOME` — Hermes Agent's own variable — is no longer reported as a
  deprecated m3 setting.
  **Affected:** Windows installs with an administrator embed server; machines
  where OpenClaw was uninstalled; Hermes Agent users · **Action:** delete
  `~/.openclaw` if you no longer use OpenClaw; keep `HERMES_HOME` as it is ·
  **Data status:** none.

- **The Rust embed server reports what it did** (m3-core-rs 3.10.6).
  `m3-embed-server start`, `stop`, `install` and `uninstall` print the state the
  service reached rather than "signal sent"; errors name their cause (not
  installed, access denied, an unreadable config file, an invalid `M3_EMBED_*`
  value). On macOS, `stop` now stops the server (launchd restarted it before),
  and an installed but unloaded agent reports `stopped`, not `not installed`. A
  Windows server that is still starting is no longer mistaken for an
  unregistered one.
  **Affected:** installs using the Rust embed server · **Action:** none;
  `m3 upgrade` installs the new core · **Data status:** none.

### Security

- **The homelab-dashboard example pins `source-map-js` 1.2.2**
  (GHSA-68fv-2mgg-jv7q, a denial of service in a build-time dependency).
  **Affected:** anyone who ran `npm install` in
  `examples/homelab-dashboard/frontend` · **Action:** re-run `npm install`
  there · **Data status:** none.

### Changed

- **`m3 stop` on macOS and Linux leaves the Rust embed server running,** as
  Windows already did: it holds no data, and upgrades no longer interrupt
  embeddings for agents. Setup still restarts it when its binary is replaced.
  **Affected:** macOS and Linux with the Rust embed server ·
  **Action:** to stop it as well, run `m3 embedder stop` · **Data status:** none.

### Added

- **`m3 upgrade --from-pypi`** moves a pipx install that tracks a local wheel or
  path onto the PyPI release, within the same stop, setup and repair steps.
  **Affected:** pipx installs made from a wheel or path · **Action:** run
  `m3 upgrade --from-pypi` once if `m3 upgrade` says it upgrades from that source ·
  **Data status:** none.

## [2026.10.5.0] — 2026-10-05 — upgrades that cannot remove m3, and scheduled sync that runs

### Fixed

- **`m3 upgrade` on Windows no longer removes m3 when one of its programs is in
  use.** It now stops before changing anything, names what is holding the file,
  and prints the command that works; after any failed upgrade it reports what is
  actually installed.
  **Affected:** Windows pipx installs, in particular with Developer Mode on, or
  with an agent's m3 server running · **Action:** if the upgrade stops, run the
  command it prints · **Data status:** none.

- **The hourly warehouse sync runs on macOS and Linux.** macOS now gets launchd
  jobs for the hourly sync, the chat-embedding sweep and secret rotation, and
  the Linux cron jobs use the installed interpreter; on pipx installs the Linux
  hourly sync failed on every run. Scheduled jobs find the warehouse from its
  stored credentials, and `m3 schedules verify` checks each job's interval and
  command.
  **Affected:** macOS and Linux installs that sync to a warehouse ·
  **Action:** run `m3 setup` (or `m3 schedules repair`); on macOS a previous
  `com.m3memory.sync_all.plist` is saved as a `.bak-` copy and replaced ·
  **Data status:** local data is unaffected; rows written while the job was not
  running are sent by the next sync.

- **The scheduled sync includes the chat log.** A job that set `M3_DATABASE`
  synced only the main store.
  **Affected:** installs whose scheduled sync sets `M3_DATABASE`, such as a
  hand-made launchd plist · **Action:** run `m3 setup` once ·
  **Data status:** chat-log rows not yet in the warehouse are sent by the next sync.

- **`m3 stop` and setup no longer report a running embed server as stopped,**
  and `m3 doctor` names an embed server still running the previous native core,
  with the restart command.
  **Affected:** stop and setup reporting — Windows installs whose embed server
  runs as an elevated service; the doctor check — all platforms ·
  **Action:** none, or the restart doctor prints · **Data status:** none.

- **`memory_link` accepts the `precedes` and `follows` relationship types through
  MCP.** The tool's schema offered 9 of the 11 types the store supports.
  **Affected:** agents creating links through the MCP tools · **Action:** none ·
  **Data status:** none.

- **Setup no longer asks for an administrator shell to re-register a boot task
  that is already registered correctly.**
  **Affected:** Windows · **Action:** none · **Data status:** none.

- **`m3 doctor` reports an m3 server registered for a single directory** (Claude
  Code's local scope), which sessions started there load beside the main one;
  `m3 doctor --fix --fix-hooks` removes it.
  **Affected:** Claude Code users with an older `memory` registration ·
  **Action:** run `m3 doctor --fix --fix-hooks` if doctor reports it ·
  **Data status:** none.

- **`m3_call` honours `M3_MCP_PROXY_ALLOW_DESTRUCTIVE`,** as the MCP proxy
  already did; only the legacy name enabled destructive tools through it.
  **Affected:** operators who enable destructive tools for MCP proxies ·
  **Action:** none · **Data status:** none.

- **`M3_SYNC_DBS` accepts Windows drive-letter paths.** Separate entries with
  commas or the OS path separator (`;` on Windows, `:` elsewhere).
  **Affected:** Windows installs that set `M3_SYNC_DBS` ·
  **Action:** none · **Data status:** none.

- **Transcript backfill progress survives a reinstall.** The ingest cursor now
  lives under the engine root; existing progress carries over.
  **Affected:** installs that backfill transcripts with `chatlog_ingest` ·
  **Action:** none · **Data status:** none.

- **Setup and doctor messages name current settings.** The "payload not
  installed" error points to `m3 install-m3` and `M3_PATH_BIN`, not the removed
  `M3_BRIDGE_PATH`; `m3 doctor` says in-process embedding needs
  `M3_EMBED_INPROC=1`.
  **Affected:** all platforms · **Action:** none · **Data status:** none.

### Changed

- **Windows setup asks for administrator rights once,** for every step that
  needs them, and reports each step.
  **Affected:** Windows · **Action:** approve the single prompt ·
  **Data status:** none.

- **macOS setup offers Touch ID for `sudo`,** once, when run at the Mac.
  **Affected:** macOS 14 or later with Touch ID · **Action:** optional ·
  **Data status:** none.

### Documentation

- **Sync and scheduling guides match how jobs now run:** launchd on macOS, cron
  rendered by setup on Linux, the stored warehouse credentials, and
  `m3 schedules verify` / repair.
  **Affected:** readers of SYNC.md and the homelab guides · **Action:** none ·
  **Data status:** none.

- **Entity, relationship and memory-type counts agree across the docs,** and the
  docs state that entity types are configured without code changes.
  **Affected:** readers of the architecture and feature docs · **Action:** none ·
  **Data status:** none.

- **Windows upgrade, single administrator prompt and Touch ID are documented,**
  with troubleshooting for scheduled jobs.
  **Affected:** readers of the install, upgrade and troubleshooting guides ·
  **Action:** none · **Data status:** none.

- **The comparison guide marks agentmemory's LongMemEval figure as not
  comparable,** with the evidence linked.
  **Affected:** readers of COMPARISON.md · **Action:** none ·
  **Data status:** none.

- **Install, agent and operator guides corrected against the code:** Claude Code
  registers as `m3_memory`; use the plugin or `m3 setup`, not both; `m3 setup`
  keeps SQLite; in-process embedding is opt-in; agents use `memory_supersede`
  when a fact changes and `memory_delete` to forget one memory.
  **Affected:** readers of the install, agent-instruction and operator guides ·
  **Action:** none · **Data status:** none.

- **Retrieval is reported at k=8, m3's default search depth** (99.2%, the same as
  at k=10), and the QA figure states what the benchmark harness supplies.
  **Affected:** readers of the README and comparison guide · **Action:** none ·
  **Data status:** none.


## [2026.10.4.3] — 2026-10-04 — fresh pipx installs that work, and quieter upgrades

### Fixed

- **Fresh pipx installs get the native core and the shared embed server.**
  Installs made with recent pipx versions ran without either.
  **Affected:** installs created with pipx 1.17 or later, all platforms ·
  **Action:** if `m3 doctor` reports oxidation not installed, run
  `m3 embedder install-gpu` and then `m3 embedder install` ·
  **Data status:** none.

- **`m3 upgrade` stops before changing anything when the source pipx installed
  m3 from no longer exists,** and prints the commands to reinstall from PyPI.
  **Affected:** pipx installs made from a local wheel or path ·
  **Action:** follow the printed commands · **Data status:** none.

- **Setup on macOS and Linux no longer waits 30 seconds for m3's services to
  pause or asks whether to kill one.**
  **Affected:** macOS and Linux · **Action:** none · **Data status:** none.

- **Installing or re-registering one service no longer restarts the others**
  when they already run the installed version.
  **Affected:** all installs · **Action:** none · **Data status:** none.

- **`m3 schedules verify` no longer reports tasks that are absent by design as
  failures.**
  **Affected:** Windows · **Action:** none · **Data status:** none.

- **Background enrichment uses the chat model you configured.** Ollama, a
  custom server URL and an endpoint list were read only from your shell, which
  background services do not see, so entity extraction found no chat model.
  **Affected:** users of Ollama, `M3_LLM_URL` or `LLM_ENDPOINTS_CSV` whose
  services run under launchd, systemd or Task Scheduler · **Action:** run `m3 setup` once (keep the current settings) ·
  **Data status:** memories waiting for extraction were deferred, not dropped;
  they are processed once a chat model is reachable.

- **Setup, stop and doctor output is shorter and accurate.** `m3 stop` lists
  only the services it stopped, setup no longer prints internal trace lines or
  a bytecode warning, its summary names the shared embed server, and doctor
  says when zero extracted entities is because no chat model is reachable.
  Keeping the current settings no longer reports "no agents wired", pipx
  installs skip the bytecode question, doctor no longer prints library log
  lines, and it skips the Claude Code check when the `claude` CLI is not on PATH.
  Setup waits for the embed server to answer before verifying, and no longer
  calls an install "live" when verification fails.
  **Affected:** all installs · **Action:** none · **Data status:** none.

### Changed

- **Running `m3 setup` on an existing install asks one question:** keep the
  current settings, or answer them again.
  **Affected:** all installs · **Action:** none · **Data status:** none.

## [2026.10.4.2] — 2026-10-04 — upgrades that finish healthy, and PostgreSQL maintenance that runs

### Fixed

- **On PostgreSQL, memories and chat turns saved without an embedding now get
  one.** Such rows were missing from semantic search.
  **Affected:** PostgreSQL installs · **Action:** none — the loop embeds the
  backlog on its next passes · **Data status:** rows written while the embedder
  was unavailable had no vector until now; none were lost.

- **The dashboard's "backfill embeddings" action works on PostgreSQL.**
  `m3_chatlog_enrich_backfill`, which does not support PostgreSQL, now refuses
  to run there.
  **Affected:** PostgreSQL installs · **Action:** none · **Data status:** none.

- **Chatlog decay and prune run on PostgreSQL.**
  **Affected:** PostgreSQL installs · **Action:** none ·
  **Data status:** chat turns on PostgreSQL were never decayed or pruned; the
  first run acts on the whole backlog (prune writes only with
  `M3_CHATLOG_PRUNE_AUTO` set).

- **Upgrades on macOS and Linux restart the services they stop.** The embed
  server and notification waiter could stay down after `m3 setup`. On Linux,
  setup no longer replaces the Rust embed server's service with the Python one.
  **Affected:** macOS and Linux · **Action:** none · **Data status:** none.

- **`m3 setup` no longer reports a running cognitive loop as down** and exits
  with status 3.
  **Affected:** all installs running setup or `m3 upgrade` · **Action:** none ·
  **Data status:** none.

- **Upgrades no longer wait on a busy or sleeping cognitive loop.**
  **Affected:** all installs · **Action:** none · **Data status:** none.

- **`m3 schedules verify` and setup flag Windows tasks registered by an older
  install,** which keep the old script and log paths after a pip or pipx
  upgrade.
  **Affected:** Windows · **Action:** run `m3 schedules repair` from an
  elevated shell when setup reports it · **Data status:** none.

- **No empty SQLite database is created on PostgreSQL installs.**
  **Affected:** PostgreSQL installs · **Action:** an empty
  `<engine root>/agent_memory.db` left by earlier versions can be deleted ·
  **Data status:** none.

- **Cognitive loop logs survive upgrades.** They were written under the package
  directory, which each upgrade deleted.
  **Affected:** macOS and Linux pip/pipx installs · **Action:** run `m3 setup`
  once after upgrading · **Data status:** none (log files only).

- **m3's scheduled cron jobs run on pip and pipx installs.**
  **Affected:** macOS and Linux installs with m3's crontab block ·
  **Action:** run `m3 setup` · **Data status:** none.

- **`m3 stop` stops m3 on macOS and Linux.** Services previously came back
  within seconds; they now stay stopped until `m3 setup`.
  **Affected:** macOS and Linux · **Action:** none · **Data status:** none.

- **The NVIDIA GPU core is installed only where it can load.** Hosts with the
  CUDA toolkit but no NVIDIA driver get another backend instead of a core that
  fails to load.
  **Affected:** Linux and Windows hosts with the CUDA toolkit and no NVIDIA
  driver · **Action:** run `m3 embedder install-gpu --force` if `m3 doctor`
  reports the native core missing · **Data status:** none.

- **Setup and doctor report what is running.** Doctor's headline names shared
  mode, a stopped embed server is a warning, the roots' defaults are labelled
  correctly, and an outdated config.json version is not shown as installed.
  **Affected:** all installs · **Action:** none · **Data status:** none.

- **Running the test suite no longer changes the machine's installed m3
  services.** The embed server could be left failing to start, or the dashboard
  running from the test checkout.
  **Affected:** contributors on Linux or macOS who ran the suite on a machine
  with m3 installed; the package runtime is unaffected · **Action:** if the
  embed server or dashboard misbehaves after a test run, re-run `m3 setup` ·
  **Data status:** none.

- **The pre-push hook no longer blocks a push that only deletes branches.**
  **Affected:** contributors with the repo's `.githooks` installed ·
  **Action:** none · **Data status:** none.

### Changed

- **`m3 upgrade` shows where pipx will upgrade from,** and warns when it is not
  the PyPI release.
  **Affected:** pipx installs · **Action:** follow the printed command to track
  PyPI · **Data status:** none.

## [2026.10.4.0] — 2026-10-04 — verified backups, and tools that refuse the wrong store

### Fixed

- **The cognitive loop log no longer contains duplicated or split lines.**
  **Affected:** Windows and macOS installs running the loop as a scheduled
  task or launchd agent · **Action:** none · **Data status:** none (log file
  only).

- **`scan_repo_v7.py` keeps the caller's PATH.** Scanners and tools installed
  elsewhere (e.g. Homebrew) are no longer hidden.
  **Affected:** contributors running the security scan on macOS or Linux; the
  package runtime is unaffected · **Action:** none · **Data status:** none.

- **SQLite-only maintenance tools refuse to run on PostgreSQL.**
  `backfill_content_hash`, `migrate_entity_vocab`, `m3_enrich_assign`,
  `m3_enrich_batch`, `m3_entities_gliner`, `split_chatlog_from_core`,
  `migrate_memory` and `ai_mechanic` exit with an error under
  `M3_DB_BACKEND=postgres` instead of modifying a local SQLite file.
  `m3 doctor --fix` reports its SQLite repairs as skipped on PostgreSQL, and
  `chatlog_init` skips SQLite migrations there.
  **Affected:** PostgreSQL installs · **Action:** none ·
  **Data status:** earlier runs of `m3_enrich_batch` or `m3_entities_gliner`
  on PostgreSQL may have written duplicate observations or extra entity links;
  the other tools changed only a local SQLite file.

- **Backups taken by maintenance tools are complete and verified.**
  `m3_entities`, `m3_enrich`, `m3_chatlog_backfill_embed`,
  `m3_chatlog_backfill_title` and `reembed_space` could take a backup missing
  recent writes, and on PostgreSQL backed up a local file rather than the
  database. A run now stops before writing unless its backup verifies;
  `migrate_memory` does the same, and `homecoming` exits non-zero when a
  database fails to copy. Backups are written to `<engine root>/backups/<tool>/`.
  **Affected:** all installs · **Action:** on PostgreSQL, install
  `pg_dump`/`pg_restore` (or set `M3_PG_DUMP` / `M3_PG_RESTORE`) to run these
  tools · **Data status:** backups these tools made before this change may be
  missing recent writes.

- **`m3_chatlog_backfill_title` writes only the store it targets, and runs on
  PostgreSQL.** **Affected:** installs that ran the title backfill against a
  separate chatlog store · **Action:** none ·
  **Data status:** main-store rows with an empty or role-label title may have
  received a title from their own first line; no content was changed.

- **Entity extraction no longer blocks other writes to the main store.** While
  the cognitive loop worked through extraction backlog, memory saves from the
  MCP server or CLI could fail with `database is locked`.
  **Affected:** SQLite installs running entity extraction · **Action:** none ·
  **Data status:** none (writes that failed were not applied and can be
  retried).

- **Procedural distillation and belief consolidation complete, and a task is
  distilled once.** The same task was re-distilled every cycle, each pass
  adding a superseded copy of the procedure, and no provenance links were
  recorded. **Affected:** installs where distillation or consolidation reach a
  working model · **Action:** none ·
  **Data status:** repeated distillation leaves superseded duplicate procedure
  memories (source `distillation`) and procedures without `distills_from` links.

- **A local model server with no model loaded no longer makes entity
  extraction give up on memories.** They are deferred until a model is
  available instead of being excluded after three attempts. Distillation pauses
  for the cycle, the outage is reported once (then hourly), and model errors
  include the server's message (e.g. `No models loaded`). The entity run banner
  no longer says "DRY RUN" on real runs. **Affected:** installs using a local
  LM Studio / Ollama server · **Action:** keep a model loaded in the local
  server · **Data status:** memories excluded by an earlier outage stay
  excluded; they appear in `entity_extraction_queue` as `failed` with a "No
  models loaded" error and need re-queueing.

- **The selected backend is used consistently, and a change to
  `M3_DB_BACKEND` takes effect on the next call.** Code could reach SQLite
  while PostgreSQL was selected. **Affected:** PostgreSQL deployments ·
  **Action:** none · **Data status:** none known outside test runs; rows
  written to SQLite under a PostgreSQL selection are not migrated.

- **Scheduled-task and service logs are written to `~/.m3/logs`.** They were
  written inside the installed package, where a reinstall deletes them. New
  `M3_LOGS_ROOT` overrides the location (default `~/.m3/logs`, or
  `M3_MEMORY_ROOT/logs`). **Affected:** all installs ·
  **Action:** re-register scheduled tasks (`m3 setup`) so they pick up the new
  path; existing logs under the package's `logs/` can be moved by hand ·
  **Data status:** none (log files only).

- **The version-drift warning no longer repeats forever naming a dead process.**
  The boot record stopped being written on macOS and Linux, so every start
  warned that an older server might still be running and named a PID from the
  last successful record — advice that could not become unnecessary, because the
  record could not advance. The liveness probe no longer raises on a dead PID,
  so the boot is recorded and the warning clears.
  **Affected:** macOS and Linux installs; Windows was unaffected ·
  **Action:** none · **Data status:** `~/.m3-memory/version_state.json` resumes
  updating on the next start.

- **A shadowed `dialect` accessor no longer fails far from its cause.** When
  the `memory.backends` package had its `dialect` accessor replaced by the
  same-named submodule, every call site raised `'module' object is not
  callable` with nothing naming the package or the attribute. The package now
  refuses that rebind and logs where it came from, and the test suite fails the
  test that leaves the namespace in such a state instead of a later, unrelated
  one.
  **Affected:** nothing in a normal install — the condition is reached by code
  that replaces or reloads `memory.*` modules, which in practice means test
  runs · **Action:** none · **Data status:** none.

- **The pre-push leakage gate could report a clean scan it had not run.** A
  pattern the platform's `grep` could not compile disabled the whole policy, and
  the swallowed compile error read as "no leakage detected". The scan now
  compiles each pattern independently and **blocks the push** whenever the policy
  cannot be evaluated, and the patterns now mean the same thing on all three
  supported platforms.
  **Affected:** contributors pushing with the repo's `.githooks` installed; the
  published package and its runtime are unaffected · **Action:** reinstall hooks
  with `python bin/setup_hooks.py` if yours predate this change; a push now fails
  loudly on an unreadable or malformed pattern file instead of passing ·
  **Data status:** history was re-scanned with the repaired gate; no credentials
  or internal data were found.

- **A shipped backend could report itself unregistered after an unrelated
  module reimport.** Code holding a registry reference captured before the
  `memory.*` namespace was reimported saw an empty registry and raised `no
  dialect registered for backend 'sqlite'` (or `'postgres'`) for a backend that
  is shipped and importable. The lookup now adopts the live registration.
  **Affected:** PostgreSQL and SQLite lookups after a `memory.*` reimport;
  reached in test runs and by an embedder that reloads the namespace, not by a
  steady-state install · **Action:** none · **Data status:** none.

- **A partial native-core install no longer disables embed circuit breakers
  silently.** The breakers are built when `memory.embed` is imported; a core
  that is present but missing `CircuitBreaker` made that import fail, which
  surfaced as an unrelated `memory.config` import error. It now degrades to no
  breakers **and logs a warning** naming what it found and that retries are
  unbounded as a result. A core that is simply absent is unchanged and stays
  quiet. `m3 doctor`'s oxidation probe also now prints the frame that actually
  raised, instead of blaming `config.py`.
  **Affected:** installs with a partial or stubbed `m3_core_rs`; a normal
  install or a normal absence is unaffected · **Action:** none; if the new
  warning appears, reinstall the native core · **Data status:** none.

- **The test suite no longer crashes the interpreter at exit on Windows.**
  Collecting `tests/test_token_budget.py` imported `transformers` — and through
  it the `sentencepiece` native extension — only to probe whether a tokenizer
  was available. That extension's teardown faults during interpreter
  finalization, so the suite printed its summary and then the process died with
  an access violation. The probe now uses `importlib.util.find_spec`, which
  imports nothing.
  **Affected:** developers running the suite on Windows; no shipped code path ·
  **Action:** none · **Data status:** none.


- **A rejected `memory_write` now says what tripped the content guard, and
  where.** The message printed only the regex, so a writer facing a long note
  had no way to tell which span was at fault. It now names a human-readable
  reason and the offset and line. The guard itself is unchanged — memory text is
  rendered in UIs and read by downstream enrichment LLMs, so these payloads are
  dangerous stored, not merely executed — and the matched text is deliberately
  **not** echoed back, since for the prompt-injection pattern the match is the
  payload and error strings are read by agents.
  **Affected:** all installs; only writes that trip the guard · **Action:**
  none · **Data status:** none — no write that previously succeeded is now
  rejected, and none that was rejected is now stored.

- **The LangChain adapter's shared event loop is now shut down cleanly at exit.**
  `M3Client` runs one process-wide asyncio loop on a daemon thread (correct, and
  unchanged — m3's connection pool and embedder are affinity-bound to it), but
  nothing ever stopped it, so the thread was still inside `run_forever` when
  CPython finalized. The loop and its default executor are now stopped and
  closed via `atexit`, while the interpreter is still intact. This is resource
  cleanup; it is not known to change any user-visible behaviour.
  **Affected:** installs using the LangChain/LangGraph adapter · **Action:**
  none · **Data status:** none.



## [2026.10.1.1] — 2026-10-01 — the embed-server binary ships executable

### Fixed

- **The bundled `m3-embed-server` binary is executable again.** Every wheel in
  m3-core-rs `3.9.20` stored it non-executable (mode `0600` on Linux and macOS,
  `0644` on Windows), so on a fresh install the shared embed server could not
  start — surfacing as an `EACCES` that named no cause. The Rust-core pin moves
  to **`3.10.1` / `v2026.10.1`**, whose wheels store it correctly and install
  executable under every pip tested (25.1.1 through 26.2.1) and uv.
  `m3 doctor --fix` continues to repair the mode in place for anyone already on
  a `3.9.20` wheel.
  **Affected:** installs that fetched a `3.9.20` Rust-core wheel, all platforms ·
  **Action:** upgrade; `m3 doctor --fix` repairs an already-installed `3.9.20`
  wheel in place · **Data status:** no loss — writes were still accepted and
  searchable via FTS while the shared embedder was down, and their vectors
  backfill asynchronously once it starts.
- **PostgreSQL warehouse sync failed against a unix-socket DSN.**
  `postgresql:///mydb` — the usual shape for a local cluster using peer auth —
  carries no host and no user, and those were written into `postgres_fdw`'s
  `CREATE SERVER` options as SQL `NULL`, which PostgreSQL rejects outright
  (`syntax error at or near "NULL"`). Both options are now omitted when absent.
  A DSN with an explicit host was unaffected.
  **Affected:** PostgreSQL warehouse sync, and only with a host-less DSN
  (`postgresql:///db`, i.e. peer auth over the unix socket) · **Action:**
  upgrade · **Data status:** no loss — the sync failed closed while creating the
  foreign server, before any rows were read or written.

### Changed

- The Rust-core wheel matrix is 7 os/backend packages × CPython 3.12–3.15.
  CPython 3.11 is no longer built; the floor is `>=3.12`, matching m3-memory.
  **Affected:** CPython 3.11 users of the prebuilt Rust core — m3-memory itself
  already required `>=3.12`, so 3.11 could not run m3 regardless · **Action:**
  none on 3.12+; on 3.11, move to 3.12+ · **Data status:** none.


## [2026.10.1.0] — 2026-10-01 — four commands that reported success they had not checked

### Fixed

- **PostgreSQL could fail on m3's own text.** The connection pool adopted the
  server's `client_encoding` instead of pinning its own, so against a
  `SQL_ASCII` cluster every non-ASCII character m3 writes raised
  `UnicodeEncodeError` inside the driver — and m3's own schema and messages
  contain them, so the first schema load died pointing at the driver rather than
  at the encoding. The pool now requests UTF-8, which is correct against any
  server encoding; an explicit `client_encoding` in the DSN still wins.
  **Affected:** PostgreSQL backend against a cluster whose encoding is not UTF-8 (e.g. `SQL_ASCII`) · **Action:** upgrade · **Data status:** no loss — affected writes failed outright with a `UnicodeEncodeError` rather than being stored mangled.
- **`m3 doctor` told you the native extension was stale without telling you how
  to fix it.** The default (brief) output said only "reinstall"; the command
  lived in `--verbose`, and `--fix` cannot repair this probe. A stale extension
  is reached by an ordinary upgrade, because the native wheel is a separate
  distribution that `pip`/`pipx` does not touch. The line now names
  `m3 embedder install-gpu`.
  **Affected:** installs whose native extension is older than the pin · **Action:** none · **Data status:** none — diagnostic wording only.
- **`m3 doctor` could report a broken embedding cascade in reassuring words.**
  In shared-embedder mode it printed "shared tier-2 embedder online (tier-1
  appropriately offline)" regardless of health, so a failure arrived as a ❌
  beside the word "online", naming no cause and no remedy. That phrasing is now
  used only when the cascade is actually healthy; otherwise it reports the
  failing tier and names `m3 doctor --fix`.
  **Affected:** shared-embedder installs running `m3 doctor` · **Action:** none · **Data status:** none — diagnostic wording only.
- **`m3 setup` reported a background service as restarted when it had only
  requested a start.** On a host where the service was not installed, setup
  printed "[OK] cognitive-loop: restarted" immediately followed by
  "[!] cognitive-loop: NOT running". It now reports the attempt, and the verdict
  comes from re-reading the service registry.
  **Affected:** hosts where the cognitive-loop service was not installed · **Action:** re-run `m3 setup`; confirm with `m3 status` · **Data status:** no loss — background enrichment and embedding were deferred while the loop was not running, and backfill once it is.
- **`m3 embedder install` and `m3 embedder start` reported a stopped embed server
  as running, and exited 0.** A zero exit from the service manager means only that
  it accepted the start request — launchd, systemd and the Windows SCM all return
  success the moment they fork the child — so a daemon that cannot bind its port
  exits 0 as well. Separately, the already-registered path inferred "already
  serving" from a port probe, which says nothing about *whose* process is
  answering: a stray in-process embedder holding the port was enough to make a
  freshly registered service look healthy while it was dead. Since OS restart
  actions fire only on an abnormal exit, nothing would ever have restarted it, and
  `m3 doctor` would report "REGISTERED but STOPPED" about a state the installer
  had just called success. Both commands now confirm the outcome with the service
  manager before printing anything, and exit non-zero when it is not running. A
  held port gets a named diagnosis with a platform-correct command for finding the
  holder, rather than generic start advice; setup no longer folds this state into
  its benign "SKIPPED (not installed)" message; and the messages honour
  `M3_EMBED_SERVER_PORT` instead of hardcoding 8082.
  **Affected:** all platforms, any install using the shared embed server · **Action:** upgrade · **Data status:** no loss — writes stayed searchable via FTS and their vectors backfill once the embedder runs.
- **A stopped service could be read as running because the status check matched a
  bare substring.** `"running"` also matches "not running", so the one probe whose
  job is to refuse to claim health without evidence could do exactly the opposite.
  The same check read the SCM's transitional `START_PENDING` as "never
  registered", which would have re-installed over a service that was still coming
  up. Negative states are now matched before positive ones, and an unrecognised
  status is treated as not running.
  **Affected:** service status probes on all platforms · **Action:** upgrade · **Data status:** none — the probe reported wrongly; it changed no data.
- **`chatlog_rescrub` reported a partial sweep as a complete one.** It examines at
  most `limit` rows (default 10,000) and said nothing about the remainder, so on a
  larger store the result read as "nothing left to scrub" while most rows had
  never been examined — and because the query had no `ORDER BY`, the rows it did
  examine were an arbitrary slice that successive runs need not extend. The result
  now reports `scanned`, `limit` and `truncated`, and when truncated also the
  number of rows left and the limit that would cover them; rows are examined
  oldest-first so a larger re-run makes progress. Truncation is detected without
  an extra aggregate query, so a complete sweep costs nothing more than before.
  **Affected:** chat-log stores larger than the rescrub `limit` (default 10,000 rows) · **Action:** re-run `chatlog_rescrub` with a limit that covers the store — the result now names the limit that would · **Data status:** **redaction may be incomplete.** Rows beyond the limit were never examined, and with no `ORDER BY` successive runs did not necessarily extend coverage, so unscrubbed content may remain. Re-run to completion; rows are now examined oldest-first so a larger re-run makes progress.
- **Running the test suite overwrote the developer's own `.mcp.json`.** The
  config generator writes that file into the repository root unconditionally, and
  three tests reach the generator — directly or through the Claude settings
  installer — so a test run replaced it. The writer is now filtered by
  destination during those tests: writes inside the checkout are dropped, writes
  elsewhere still happen, so the tests keep the output they assert on. It had been
  invisible because the file is git-ignored and already exists on a developer
  machine; it surfaced only on a fresh clone.
  **Affected:** developers running the test suite; not end users · **Action:** none for users · **Data status:** no memory data affected — a developer's `.mcp.json` was overwritten; restore it or re-run `m3 setup`.
- **A test left a directory in the repository when run on Windows.** A mocked
  `subprocess.run` treated the last argument of every intercepted command as a
  path to create; on Windows the installer also queries a scheduled task, whose
  last argument is the task's name, so the mock created a directory named after
  it in the working tree. The mock now matches on the command rather than assuming
  it sees only one call, and the suite fails if a run leaves new entries in the
  repository root — previously nothing checked, so this was found by looking at
  `git status` rather than by any test.
  **Affected:** developers running the test suite on Windows · **Action:** none · **Data status:** none — a stray directory in the checkout.
- **Three tests reported a state the machine was not in, on any host without a
  full build toolchain.** `test_elbow_trim` errored at setup with a missing
  `torch` in a full run while passing alone: its fixture calls `hasattr` on every
  loaded module, and `hasattr` executes imports when a module defines a lazy
  `__getattr__` — as `transformers` does. `test_rust_core_install` asserted on the
  wrong subprocess call wherever `cargo` is absent, because the function under
  test probes for build tools first and returns early, and the test kept only the
  last call. `test_raw_connection_drift` failed outright when a virtualenv inside
  the tree was named anything other than `.venv`, counting third-party code
  against its budget; virtualenvs are now recognised by containing a
  `pyvenv.cfg` rather than by name. None of these were product defects, and all
  three were invisible on a developer machine that happened to have the missing
  pieces installed.
  **Affected:** developers running the suite without a full build toolchain · **Action:** none · **Data status:** none — test-reporting only.
- **Iterating a query result raised `RecursionError` on PostgreSQL.** The cursor
  wrapper's `__iter__` delegated to the driver's, which returns the cursor itself,
  so iteration re-entered the wrapper until the interpreter stopped it.
  `.fetchall()` was unaffected, so the two ways of reading a result had different
  correctness — and 31 places in the codebase read one by iterating, including
  backend-agnostic maintenance paths. Iteration now drives the driver's own
  batched fetch, keeps both index and key access on each row, and is covered by
  backend-conformance tests that assert iteration and `fetchall` agree.
  **Affected:** PostgreSQL backend, any path that iterates a query result · **Action:** upgrade · **Data status:** no loss — the read raised instead of returning a partial result set.
- **Promoting a chat log entry could silently drop a column, and re-read every
  row to do it.** The SQLite cross-file path selected all columns, rebuilt each
  row in Python, and inserted them one at a time — substituting NULL for any
  column it could not find, per row, without reporting it. It is now a single
  `INSERT ... SELECT` between the two attached databases, and a store missing a
  promoted column names the column and points at `m3 doctor` instead. The column
  list, previously written out three times, now has one owner, so the writer and
  both promote paths cannot drift apart.
  **Affected:** SQLite cross-file chat-log promotion · **Action:** upgrade; a store missing a promoted column is now named instead of silently filled · **Data status:** **verify.** The old path substituted `NULL` per row for any column it could not match, without reporting it, so rows promoted before this release may carry an unexpected `NULL`. The source chat-log rows are unaffected.
- **PostgreSQL parity tests could connect to the production warehouse.** They
  accepted the warehouse DSN as a fallback when no test DSN was configured, and
  treated the variable being *present* as the cluster being *reachable* — so an
  unreachable one cost a connect timeout per test rather than a skip. They now use
  the standard reachability gate and the shared DSN resolver, which never names a
  warehouse variable, and a new check fails the suite if any test reads one at
  import time, in a class body, or in class setup, where the environment sandbox
  cannot reach.
  **Affected:** developers running the suite with a warehouse DSN exported; not end users · **Action:** none for users · **Data status:** tests could open connections to the production warehouse. No write path was identified in the fix, and whether any run wrote to it was **not re-verified** — inspect that cluster if the suite was ever run with a warehouse variable set.
- **A live-PostgreSQL test reported a cleanup error instead of why it skipped,
  and never exercised the code it covers.** Its cleanup reassigned `autocommit`
  while an aborted transaction was still open, so a second error replaced the
  first and a deliberate skip surfaced as a failure. The skip was also permanent:
  `postgres_fdw` dials from inside the server, and the test handed it a
  client-side address, so on any host reached through a tunnel or proxy the
  foreign-server path was never run. It now gets an address the server can reach,
  and reads a real foreign table.
  **Affected:** developers running the live-PostgreSQL tests · **Action:** none · **Data status:** none — the test covered nothing and reported confusingly.
- **A schema guard failed on a schema that was correct.** The test asserting the
  notification sweeper uses its partial index built a fixture in which every row
  was a sweep candidate, so the index covered the whole table and a full scan was
  genuinely the cheaper plan. The fixture now models the queue's real shape —
  candidates are a small minority — and also checks that premise, so it cannot
  quietly go back to asserting nothing.
  **Affected:** developers running the suite · **Action:** none · **Data status:** none — a false test failure.
- **Two FIPS tests reported a missing crypto library on a machine where it was
  present.** A test that simulates another OS sets `sys.platform` process-wide,
  and the suite restored `os.name` before a test's teardown but never
  `sys.platform` — so the simulated value reached an unrelated fixture's teardown
  and sent the library resolver after the wrong per-OS filename. POSIX hosts
  only. Both guards now cover `sys.platform`, and both restore the identity
  captured at startup rather than whatever the previous test left behind.
  **Affected:** developers running the suite · **Action:** none · **Data status:** none — a false test failure.
- **Running the test suite could redirect the developer's own chatlog capture.**
  `chatlog_config.CONFIG_PATH` is resolved when the module is imported, so it is
  not covered by the environment-variable half of the test sandbox: a test that
  saved chatlog config wrote the real `~/.m3/config/.chatlog_config.json`,
  persisting a temporary `db_path` into it. Afterwards `m3 chat status` reported
  the live chatlog as empty and capture pointed at a pytest temporary directory
  that is later deleted — with nothing failing and the suite green. The sandbox
  now re-points that constant per test, as it already did for the migration
  config.
  **Affected:** developers running the test suite; not end users · **Action:** developers re-point `db_path` in `~/.m3/config/.chatlog_config.json`, or run `m3 doctor --fix --fix-hooks` · **Data status:** **developer capture loss.** A run rewrote the real chat-log config with a temporary `db_path`, so capture afterwards went to a pytest temp directory that is later deleted; `m3 chat status` showed the live chat log as empty. Stored memories were not affected.
- **The redaction warning's own fix command did not run.** It printed
  `chatlog_set_redaction --enabled true`, which argparse rejects with
  "unrecognized arguments: true" because `--enabled` is a boolean flag, and it
  recommended a bare `chatlog_rescrub` — the capped call described above. It now
  emits a command that works, with a limit sized from the store's row count.
  **Affected:** anyone who followed the printed remediation command · **Action:** re-run the corrected command the warning now prints · **Data status:** the recommended remediation never executed, so redaction may not have been enabled when it was expected to be — see the `chatlog_rescrub` entry above for coverage limits.
### Changed

- **Scripted installs now enable the cognitive loop by default.** `install.sh`
  passed no preference, so an unattended install silently got no background
  engine — entity extraction, consolidation and reflection never ran, entity
  search stayed empty, and the only hint was an `m3 doctor` warning. This now
  matches the interactive wizard, which has always recommended it. Pass
  `--no-cognitive-loop` to decline.
  **Affected:** unattended/scripted installs via `install.sh` · **Action:** none; pass `--no-cognitive-loop` to opt out · **Data status:** none — it enables background processing that was previously absent.
- **`m3 upgrade` now repairs as well as verifies, and cannot leave a daemon on
  the old code.** The final step was a report-only `m3 doctor`, which left
  self-repairable state broken behind a warning — and hook entries still
  pointing at the payload the upgrade had just replaced. It now runs
  `m3 doctor --fix --fix-hooks` (which backs up `~/.claude/settings.json` before
  writing). A second stop was added after the package is replaced: anything
  still running at that point is the old code, and the previous flow only
  restarted services it found stopped, so a survivor kept serving stale code.
  **Affected:** all upgrades · **Action:** none · **Data status:** none.
### Security

- **Pinned the Pillow floor.** Pillow reaches m3 only transitively, and the
  floor was pinned on an optional extra while core `fpdf2` permitted
  `Pillow>=8.3.2` — a resolver taking the lowest satisfying version lands on a
  release with 37 known CVEs. `pip install` takes the newest and was never
  affected; this closes the constrained-resolver and stale-lockfile cases.
  **Affected:** installs whose resolver picked the lowest satisfying Pillow · **Action:** upgrade · **Data status:** none.
- **Restored a scanner that had never run on this repository.** The security
  pipeline passes `--config <repo>/trivy.yaml`; both config files were absent,
  so trivy exited fatally and wrote no report while the run still reported
  success. The configs are now committed, with no suppressions.
  **Affected:** the project's own security pipeline; not user installs · **Action:** none · **Data status:** none.

---

<!-- impact-headers: required above this line -->

## Older releases

- [2026-Q3 (July to September)](docs/changelog/2026-Q3.md)
- [2026-Q2 (April to June)](docs/changelog/2026-Q2.md)

