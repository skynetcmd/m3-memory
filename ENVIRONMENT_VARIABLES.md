# <img src="docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"> Environment Variables & Secure Credentials


This document specifies the environment variables required by M3 Memory.
 It is essential for security and portability that **no hardcoded values (IPs, API keys, etc.)** are present in any repository files.

## 🏛️ The "Zero-Leak" Architecture Principle

```mermaid
graph TD
    subgraph "1. Highest Priority"
        ENV[Environment Variables]
    end
    subgraph "2. OS Native"
        KEY[OS Keyring / Keychain]
    end
    subgraph "3. Synchronized Vault"
        VLT[Encrypted synchronized_secrets]
    end

    ENV -->|Fallback| KEY
    KEY -->|Fallback| VLT
```

All user-specific variables MUST be loaded into your shell's environment from a secure, local-only source.
 The recommended method is to use your operating system's native secret management service:

*   **macOS**: Keychain
*   **Linux**: Secret Service API (e.g., GNOME Keyring, KeePassXC)
*   **Windows**: Credential Manager

We provide example `zshenv.example` and `zshrc.example` files in the `config/` directory. These scripts automatically detect your OS and load secrets from the appropriate backend, making them available as environment variables.

## 🚀 Quick Setup

1.  **Copy the examples**:
    ```bash
    cp config/zshenv.example ~/.zshenv
    cp config/zshrc.example ~/.zshrc
    ```
2.  **Edit the new files (`~/.zshenv`, `~/.zshrc`)**:
    *   Set the `M3_MEMORY_ROOT` variable to the absolute path of your `m3-memory` directory.
    *   Follow the commented-out instructions to store your secrets (API keys, IPs, etc.) in your OS's keychain for the first time.
3.  **Restart your shell** (`zsh`). The scripts will now automatically and securely load your configuration on every new terminal session.

## 📋 Core Environment Variables

Your `.zshenv` should define and export the following variables by calling the `get_secret` function.

### Infrastructure & Connectivity

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `M3_MEMORY_ROOT` | **Required.** Absolute path to your workspace directory. | `export M3_MEMORY_ROOT="/path/to/your/m3-memory"` (Set directly) |
| `SYNC_TARGET_IP` | IP address of the central PostgreSQL/ChromaDB server. | `_keychain_set agentos_sync_target_ip "YOUR_SERVER_IP"` |
| `CHROMA_BASE_URL`| Full URL to the ChromaDB API. | `_keychain_set agentos_chroma_url "http://YOUR_SERVER_IP:8000"` |
| `PG_URL`| **Required.** Full PostgreSQL connection string with credentials. | `_keychain_set agentos_pg_url "postgresql://user:pass@host/db"` |

### API Keys & Authentication

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `AGENT_OS_MASTER_KEY`| **Required.** Master key for the encrypted vault. | `_keychain_set AGENT_OS_MASTER_KEY "your-secure-key"` |
| `LM_API_TOKEN` | **Required.** Token for your local LLM server (e.g., LM Studio, Ollama, vLLM). | `_keychain_set LM_API_TOKEN "your-token"` |
| `PERPLEXITY_API_KEY`| API key for Perplexity AI (web search). | `_keychain_set PERPLEXITY_API_KEY "your-ppl-key"` |
| `XAI_API_KEY`| API key for xAI/Grok (web search fallback). | `_keychain_set XAI_API_KEY "your-grok-key"` |
| `ANTHROPIC_API_KEY`| API key for Anthropic/Claude models. | `_keychain_set ANTHROPIC_API_KEY "your-claude-key"` |
| `GEMINI_API_KEY`| API key for Google/Gemini models. | `_keychain_set GEMINI_API_KEY "your-gemini-key"` |
| `OPENCLAW_GATEWAY_TOKEN`| Token for the OpenClaw sandbox gateway. | `_keychain_set OPENCLAW_GATEWAY_TOKEN "your-openclaw-token"` |
