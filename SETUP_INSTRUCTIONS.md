# 🚀 Setup Instructions — <img src="docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"> Max Agentic OS


This document summarizes the required setup steps and configuration changes needed after recent repository updates.

## 📋 Pre-Flight Checklist

```text
  [ ] Python 3.14+ installed
  [ ] Local LLM Server (LM Studio/Ollama) online
  [ ] [Optional] PostgreSQL 15+ available
  [ ] [Optional] ChromaDB available
```

## 🛠️ What Changed

1. **Hardcoded IP placeholders removed** — All `10.x.x.x` placeholders replaced with actual IP `YOUR_SERVER_IP`
2. **Environment variables documented** — New `ENVIRONMENT_VARIABLES.md` lists all required and optional env vars
3. **Server reference renamed** — `HOMELAB_SERVER` → `POSTGRES_SERVER` for clarity
4. **Configuration templating** — New `.env.example` template for easy setup
5. **README updated** — References new documentation and corrects API key names

## Quick Setup

### 1. Copy Environment Template
```bash
cp .env.example .env
# Edit .env with your values (optional, env vars take precedence)
```

### 2. Set Required Environment Variables

#### macOS (Keychain recommended)
```bash
# Copy these commands and fill in YOUR actual values

security add-generic-password -s "GROK_API_KEY" -a "$USER" -w "YOUR-GROK-KEY"
security add-generic-password -s "PERPLEXITY_API_KEY" -a "$USER" -w "YOUR-PERPLEXITY-KEY"
security add-generic-password -s "LM_API_TOKEN" -a "$USER" -w "YOUR-LM-TOKEN"
security add-generic-password -s "AGENT_OS_MASTER_KEY" -a "$USER" -w "YOUR-MASTER-KEY"
security add-generic-password -s "PG_URL" -a "$USER" -w "postgresql://agent_os:agent_os_secure@YOUR_SERVER_IP:5432/agent_memory"

# Optional: export non-sensitive server addresses
export POSTGRES_SERVER="YOUR_SERVER_IP"
export CHROMA_BASE_URL="http://YOUR_SERVER_IP:8000"
```

#### Linux (Secret Service)
```bash
python3 -c "import keyring; keyring.set_password('system', 'GROK_API_KEY', 'YOUR-GROK-KEY')"
python3 -c "import keyring; keyring.set_password('system', 'PERPLEXITY_API_KEY', 'YOUR-PERPLEXITY-KEY')"
python3 -c "import keyring; keyring.set_password('system', 'LM_API_TOKEN', 'YOUR-LM-TOKEN')"
python3 -c "import keyring; keyring.set_password('system', 'AGENT_OS_MASTER_KEY', 'YOUR-MASTER-KEY')"
```

#### Windows (Credential Manager)
```powershell
python -c "import keyring; keyring.set_password('system', 'GROK_API_KEY', 'YOUR-GROK-KEY')"
python -c "import keyring; keyring.set_password('system', 'PERPLEXITY_API_KEY', 'YOUR-PERPLEXITY-KEY')"
python -c "import keyring; keyring.set_password('system', 'LM_API_TOKEN', 'YOUR-LM-TOKEN')"
python -c "import keyring; keyring.set_password('system', 'AGENT_OS_MASTER_KEY', 'YOUR-MASTER-KEY')"
```

### 3. Verify Setup

```bash
# Check that all critical endpoints are reachable
bin/mcp_check.sh

# Test memory system
python3 bin/test_memory_bridge.py

# Verify environment variables are accessible
python3 -c "import os; print('GROK_API_KEY:', 'SET' if os.getenv('GROK_API_KEY') else 'MISSING')"
```

## Configuration Reference

### Minimum Required Variables
- `GROK_API_KEY` — xAI Grok for real-time reasoning
- `PERPLEXITY_API_KEY` — Perplexity for web search
- `LM_API_TOKEN` — Local LLM server access (LM Studio, Ollama, vLLM, etc.)
- `AGENT_OS_MASTER_KEY` — Encryption vault master key
- `PG_URL` — PostgreSQL warehouse (optional, but recommended)

### Important Server Addresses
- `POSTGRES_SERVER` — Homelab server IP/hostname (default: `localhost`)
- `CHROMA_BASE_URL` — ChromaDB endpoint (default: `http://YOUR_SERVER_IP:8000`)
- `LM_STUDIO_EMBED_URL` — Embeddings endpoint (default: `http://127.0.0.1:1234/v1/embeddings`)

**All these are documented in [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).**

## Files Changed

### Repo Structure
- ✅ `ENVIRONMENT_VARIABLES.md` — NEW: Complete env var documentation
- ✅ `.env.example` — NEW: Configuration template
- ✅ `SETUP_INSTRUCTIONS.md` — NEW: This file
- ✅ `README.md` — UPDATED: References new docs, corrects API key names
- ✅ `.gitignore` — UPDATED: Enhanced secret exclusion patterns

### Code Fixes
- ✅ `bin/sync_all.py` — Fixed hardcoded IP placeholders
- ✅ `bin/memory_core.py` — Fixed CHROMA_BASE_URL default
- ✅ `bin/setup_memory.py` — Fixed CHROMA_BASE_URL in config
- ✅ `homelab-dashboard/backend/main.py` — Now uses `$POSTGRES_SERVER` env var
- ✅ `sandbox-openclaw/.env.example` — Updated with all env vars

### Documentation Removals
- `init_ai_os.py` — Script removed; use `.env.example` and environment variables instead

## Migration Checklist

If you previously had the repo set up:

- [ ] Copy `.env.example` to `.env` (optionally)
- [ ] Update shell environment or Keychain/Credential Manager with new variable names:
  - `XAI_API_KEY` → `GROK_API_KEY`
  - Add `POSTGRES_SERVER` (default: `YOUR_SERVER_IP`)
  - Add `CHROMA_BASE_URL` (default: `http://YOUR_SERVER_IP:8000`)
- [ ] Run `bin/mcp_check.sh` to verify all endpoints
- [ ] Check that `HOMELAB_SERVER` references have been updated to `POSTGRES_SERVER`
- [ ] Remove any hardcoded config files and rely on environment variables instead

## Troubleshooting

### "GROK_API_KEY not found"
- Verify it's set in `.env` or environment
- Check Keychain (macOS): `security find-generic-password -s "GROK_API_KEY"`
- Check Credential Manager (Windows): `cmdkey /list | findstr GROK_API_KEY`

### "ChromaDB unreachable at YOUR_SERVER_IP:8000"
- Verify `CHROMA_BASE_URL` is correct
- Check that ChromaDB container is running on the target host
- The system gracefully falls back to local `chroma_mirror` if unreachable

### "PostgreSQL connection failed"
- Verify `PG_URL` is set correctly: `postgresql://user:pass@host:port/database`
- Check network connectivity to `YOUR_SERVER_IP:5432`
- PostgreSQL sync is optional; the system works without it

### "MCP bridges not loading"
- Ensure MCP servers are registered in `~/.claude.json` or `~/.gemini/settings.json` (NOT `.claude/settings.json`)
- Verify paths in MCP config match your actual repo location
- Run `bin/mcp_check.sh` for diagnostic output

## See Also

- [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) — Complete environment variable reference
- [README.md](README.md) — System overview and architecture
- [ARCHITECTURE.md](ARCHITECTURE.md) — Detailed technical specification
