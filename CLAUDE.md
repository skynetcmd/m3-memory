docs/AGENT_INSTRUCTIONS.md

## Git Standards
- **Commit Messages:** Do NOT include "Co-Authored-By" lines in commit messages. Focus on clear, concise descriptions of "why" and "what".
- **Pre-push process (MANDATORY, all agents):** After cloning, run `python bin/setup_hooks.py` once to enable the shared pre-push gate. Before any push, the tool-catalog drift check (`python bin/check_tool_catalog_drift.py`) and bench-data leakage scan must pass. If you change `bin/mcp_tool_catalog.py`, regenerate the catalog/inventory and update the "N tools" counts in the same change. Full rationale: the "Pre-push process — ALL agents" section in `docs/AGENT_INSTRUCTIONS.md`. This is enforced by the local hook AND CI, not just convention.
- **Cutting a release (version bump):** The version has ONE source of truth — `pyproject.toml` `[project].version`. Do NOT hand-edit version strings in the derived manifests. Instead: (1) bump `pyproject.toml`, (2) run `python bin/sync_manifest_versions.py` — it writes the new version into every derived manifest (`server.json`, `mcp-server.json`, `.claude-plugin/plugin.json`, `.antigravity-plugin/plugin.json`; the plugin manifests are served straight from `main` to every user's `/plugin install`), (3) commit all together. `tests/test_tool_count_drift.py::test_all_manifests_synced_to_pyproject_version` (runs `--check`) fails the build if you skip step 2, so a stale manifest can't ship. Plugin manifest descriptions say "100+ MCP tools", never an exact count (also test-guarded).

## Homecoming Architecture (decoupled roots)

Persistent state is split across **three roots** so the engine (databases) and
the configuration can live and be secured independently:

- **`M3_CONFIG_ROOT`** — configuration. Default `~/.m3/config`. Resolution:
  `M3_CONFIG_ROOT` env > `M3_MEMORY_ROOT/config` > `~/.m3/config`. Holds
  `.chatlog_config.json`, `.migrate_config.json`, `.agent_os_salt`.
- **`M3_ENGINE_ROOT`** — databases + runtime state. Default `~/.m3/engine`.
  Resolution: `M3_ENGINE_ROOT` env > `M3_MEMORY_ROOT/engine` > `~/.m3/engine`.
  Holds `agent_memory.db`, `agent_chatlog.db`, chatlog state/cursor, spill dir.
- **`M3_MEMORY_ROOT`** — the repo/payload (code), and a *master override*: if set,
  config and engine derive from it (`/config`, `/engine`) unless their own env
  vars are set.

### Migration
`bin/homecoming.py` relocates legacy state (repo-relative `memory/*` or the old
unified `~/.m3-memory/`) into the decoupled roots. It rewrites the chatlog
config's pinned `db_path` to the new engine root (a verbatim copy would leave the
chatlog pointing at the old path), and prints a post-migration checklist.

### Split-brain hazard (read before changing roots)
Two env paths must BOTH be pinned or the system splits: the **MCP server** reads
its root from the server's `env` block in client `settings.json` (it does NOT
source `~/.zshenv`), while the **chatlog Stop/PreCompact hook** inherits Claude
Code's *process* env (NOT the server `env` block). Pinning only one makes the
server read the new root while the hook keeps writing turns to the old one. Fix:
add `M3_ENGINE_ROOT` + `M3_CONFIG_ROOT` to every m3 MCP server `env` block AND
inline-prefix both hook `command`s with the same two vars. Claude Code reloads
hooks **live** but re-resolves server env only on restart — a mid-session pin
diverges the two chatlog DBs; reconcile with a UNION merge (`INSERT OR IGNORE`),
never an overwrite.
