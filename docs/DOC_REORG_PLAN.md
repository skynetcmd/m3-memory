# Documentation Reorganization Plan

Status: Proposal (generated 2026-04-11)

---

## Current state

The repo has 10+ markdown files at root level plus 5 in `docs/`. Some overlap, some have stale branding, and the information architecture doesn't match what a new visitor needs.

## Target information architecture

| File | Role | Audience |
|------|------|----------|
| `README.md` | Landing page + install + fast proof + doc map | Everyone (first 20 seconds) |
| `QUICKSTART.md` | Pure onboarding: install, configure, verify, troubleshoot | New users (first 2 minutes) |
| `CORE_FEATURES.md` | Human-facing feature overview, structured and scannable | Users evaluating the project |
| `AGENT_INSTRUCTIONS.md` | Agent behavioral rules + all 25 MCP tool specs | AI agents (Claude, Gemini, etc.) |
| `TECHNICAL_DETAILS.md` | Deep reference: search internals, schema, sync, security | Developers auditing internals |
| `COMPARISON.md` | M3 vs alternatives, nuanced and time-bound | Users comparing options |
| `ENVIRONMENT_VARIABLES.md` | Config and credential reference | Users configuring |
| `CONTRIBUTING.md` | How to contribute | Contributors |
| `GOOD_FIRST_ISSUES.md` | Scoped starter issues | New contributors |
| `ROADMAP.md` | What's coming | Community |
| `CHANGELOG.md` | Release history | Everyone |

### docs/ subdirectory

| File | Role | Action |
|------|------|--------|
| `docs/ARCHITECTURE.md` | Human-readable architecture (storage, search, sync) | Keep. This is the actual architecture doc. |
| `docs/API_REFERENCE.md` | MCP tool API details | Keep. Scoped to m3-memory package. |
| `docs/TROUBLESHOOTING.md` | Common issues and fixes | Keep. |
| `docs/UNDERLYING_TOOLS.md` | Runtime dependencies and services | Review scope. Remove broader system content not relevant to m3-memory package. |
| `docs/CHANGELOG_2026.md` | Detailed 2026 changelog | Keep as supplementary to root CHANGELOG.md. |
| `docs/install_*.md` | Platform-specific install guides | Keep. |

---

## Changes already completed

### Files renamed
- [x] `ARCHITECTURE.md` (root) -> `AGENT_INSTRUCTIONS.md` — was agent instructions, not architecture
- [x] Merged `M3_Memory_Instructions.md` behavioral rules into `AGENT_INSTRUCTIONS.md`

### Branding cleanup
- [x] All "M3 Max Agentic OS" references updated to "M3 Memory" across:
  - 6 docs: `docs/ARCHITECTURE.md`, `docs/API_REFERENCE.md`, `docs/UNDERLYING_TOOLS.md`, `docs/TROUBLESHOOTING.md`, `ENVIRONMENT_VARIABLES.md`, `SETUP_INSTRUCTIONS.md`
  - 10 code/config files: `requirements.txt`, `.env.example`, `.aider.conf.yml`, `ci.yml`, `cleanup_logs.sh`, `crontab.template`, `install_schedules.py`, `weekly_auditor.py`, `zshrc.example`, `zshenv.example`, `install_os.py`, `config/CLAUDE.md`, `homelab-dashboard/backend/main.py`
  - Remaining "M3 Max" references are Apple hardware (correct, not old branding)

### README rewrite
- [x] Restructured around: hero -> install -> fast proof -> why -> what -> who -> trust -> tools -> agent install -> demos -> docs map -> comparison -> architecture -> roadmap -> community
- [x] Removed repeated emotional phrasing (local-first, no cloud, persistent memory said once each, not 5x)
- [x] Removed "Use Cases" section (redundant with "Who this is for" + feature descriptions)
- [x] Added "Why trust this" section with hard signals only
- [x] Comparison section rewritten with "best fit" framing instead of chest-thumping
- [x] Removed banner image from hero (text-first, cleaner)
- [x] Removed "Project Structure" section (belongs in CONTRIBUTING.md)
- [x] Removed duplicate "Next Steps" and "Contributing" sections (consolidated)
- [x] Shrunk roadmap to compact table with link to full ROADMAP.md
- [x] Documentation section reorganized as "Start here / Go deeper / Configure / Contribute"

---

## Remaining work (not yet done)

### Content deduplication
- [x] `QUICKSTART.md` rewritten as pure onboarding: install, configure, verify, troubleshoot. All philosophy/pitch/comparison removed.
- [ ] `CORE_FEATURES.md` repeats feature descriptions from README — make it the authoritative feature reference (README version is already ultra-short)
- [ ] `TECHNICAL_DETAILS.md` and `docs/ARCHITECTURE.md` may overlap on search pipeline and storage internals — audit and deduplicate

### Files to review for deprecation
- [x] `SETUP_INSTRUCTIONS.md` — kept as historical migration guide (IP hardcoding removal, env var renames). Not onboarding.
- [x] `docs/UNDERLYING_TOOLS.md` — scoped to m3-memory package only. Removed homelab-specific references (Proxmox, DeepSeek-R1 70B). Made LLM references generic (auto-selected via llm_failover.py).
- [x] `README2.md` — deleted (superseded).

### Symlink/reference consistency
- [x] Root `CLAUDE.md` and `GEMINI.md` both contain `AGENT_INSTRUCTIONS.md` (include directive for agents)
- [x] `~/.gemini/GEMINI.md` is a standalone file (not a symlink) — contains Gemini-specific memories, separate from repo
- [x] `config/CLAUDE.md` is a config template for OpenClaw sandbox use. Root `CLAUDE.md` is the agent include. Different purposes, both correct.

---

## Repo-wide cleanup checklist

### Naming consistency
- [x] All "M3 Max Agentic OS" -> "M3 Memory"
- [x] Root agent instructions file named `AGENT_INSTRUCTIONS.md`
- [x] `bin/embed_architecture.py` renamed to `bin/embed_agent_instructions.py`

### Broken expectations
- [x] `ARCHITECTURE.md` no longer misleadingly named
- [x] Demo GIF references replaced with actual SVG demos (no more "coming soon")

### Unfinished demo references
- [x] Removed "GIFs coming soon" language
- [x] SVG demos in place for contradiction, search, sync

### Conflicting doc descriptions
- [x] README docs table updated to match actual file purposes
- [x] All cross-references verified — no broken links, all referenced files exist

### Old branding in non-tracked files
- [ ] Check if deployed PyPI package description still says "M3 Max Agentic OS" (may need a version bump)

---

## Hero variants for maintainer choice

### Variant A: Minimal (current)
```
# M3 Memory

Persistent, local memory for MCP agents.

Your agent forgets everything between sessions. M3 Memory fixes that.
```

### Variant B: Technical
```
# M3 Memory

Drop-in persistent memory for MCP agents. SQLite-backed, locally embedded,
contradiction-aware. 25 tools. Works with Claude Code, Gemini CLI, and Aider.
```
