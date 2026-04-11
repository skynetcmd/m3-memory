# M3 Memory: Architecture

> High-level overview. For schema details, search internals, sync protocol, and security, see [TECHNICAL_DETAILS.md](../TECHNICAL_DETAILS.md).

## Overview
M3 Memory is a local-first persistent memory system for MCP agents. It provides a semantic memory layer with hybrid search, encrypted secret management, and cross-device sync — all running on your hardware.

## Core Components

### 1. MCP Bridges (`bin/*_bridge.py`)
- **Memory Bridge**: Manages the semantic memory store (SQLite + ChromaDB). Modularized into `core`, `sync`, and `maintenance`.
- **Custom Tool Bridge**: Environmental sensing (thermals), system focus, and direct local inference routing.
- **Debug Agent Bridge**: Autonomous root-cause analysis and system debugging.

### 2. Memory System
- **Local Store**: SQLite (`agent_memory.db`) for low-latency retrieval and relationship mapping.
- **Federated Layer**: ChromaDB for distributed vector search across the LAN.
- **Warehouse**: PostgreSQL for long-term archival and multi-device synchronization.

### 3. Security & Auth (`bin/auth_utils.py`)
- **Encrypted Vault**: AES-128-CBC (Fernet) protected secrets.
- **Hardened Key Derivation**: PBKDF2HMAC with per-device persistent salts to prevent pre-computation attacks.
- **Zero-Knowledge Sync**: Secrets are synchronized across the warehouse in their encrypted state.

### 4. MCP Proxy (`bin/mcp_proxy.py`)
- Provides an OpenAI-compatible endpoint on `localhost:9000`.
- Injects 15+ Operational Protocol tools into every request.
- Enables MCP capabilities for non-native clients like Aider and OpenClaw.

## Data Flow
1. **Request**: Client sends a message to the MCP Proxy.
2. **Injection**: Proxy injects memory search and logging tools.
3. **Inference**: Request is routed to the best available model (auto-selected via `llm_failover.py`).
4. **Tool Loop**: If the model requests a tool, the Proxy executes it locally and feeds back results.
5. **Persistence**: Decisions and thoughts are automatically archived to the memory bridge.
