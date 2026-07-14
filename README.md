<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory">
    <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/M3-banner.jpg" alt="M3 Memory Banner" width="100%">
  </a>
</p>

# 🧠 M3 Memory

M3 treats agent memory as a **distributed-systems infrastructure problem**, not a simple retrieval feature. 

Instead of every tool keeping its own throwaway context, M3 is a **shared, evolving, bitemporal knowledge base** that multiple heterogeneous agents and machines read and write. It is designed to solve a fundamental challenge: *How do agents maintain a consistent, evolving, and temporal knowledge base over months and years?*

> 🦜 **Core Release Feature: Drop-in LangChain & LangGraph Support**
> M3 now functions as a drop-in **Mem0 replacement** (one-line import swap) and is fully **LangMem-compatible** (`store=M3Store()`). Gain automatic contradiction supersession, bitemporal historical queries, local sovereign embedding, and the full 100+ MCP tool set inside your LangChain apps via `pip install m3-memory[langchain]`. (See [LangChain Integration Guide](docs/integrations/LANGCHAIN.md)).

---

## 🚀 Quick Links & Badges

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI" src="https://img.shields.io/pypi/v/m3-memory?style=flat-square&color=blue"></a>
  <a href="https://www.python.org"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square"></a>
  <a href="https://github.com/skynetcmd/m3-memory/blob/main/LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-100+_tools-orange?style=flat-square"></a>
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-0078D4?style=flat-square&logo=windows&logoColor=white">
  <img alt="Linux" src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black">
</p>

<p align="center">
  <a href="docs/integrations/LANGCHAIN.md"><img alt="LangChain" src="https://img.shields.io/badge/LangChain-1C3C3A?style=flat-square&logo=langchain&logoColor=white"></a>
  <a href="docs/claude_code_plugin.md"><img alt="Claude" src="https://img.shields.io/badge/Claude-D97753?style=flat-square&logo=claude&logoColor=white"></a>
  <a href="docs/antigravity_plugin.md"><img alt="Antigravity" src="https://img.shields.io/badge/Antigravity-4285F4?style=flat-square&logo=google&logoColor=white"></a>
  <a href="docs/HERMES.md"><img alt="Hermes" src="https://img.shields.io/badge/Hermes-DD8E35?style=flat-square&logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAABgAAAAcCAYAAAB75n%2FuAAAC30lEQVR4nK3WS6xdcxQG8N89V1u9LdWSFPHWegYdSLwf1wgTkk5ESIwwKQZGBiIkOjIhkRggBoYSRAhBRCVCE9FoxasSr3qLoN7Okv%2Ftt5vdo865eq1kZ%2B%2Bc%2Fd%2Fff63v%2B9b6n6mq0osBhrmfiZWYwif4CJdiJ17GCZhBA%2FgQXwSjrd8Nut%2FIjw38UByHH7ENv%2BNYXInncAt%2Bxev4DauxFodhSy%2FBdp97EPDFWIcjcAgOTLZ%2FYAlW4Wr8hBX55k98lkSX4pwkOOwSH%2BTlkQHfPxk1gGuyuMUl%2BBhvBLCtuSDvTsONOBw%2FBOv04NZUVa0P13%2BF0y%2FDeQO%2FCJvwDC7HVpwdet7F3anuLXyaxHaG3pObNm2DJRHsvJS5HIvweRa3j17qMgoNL%2BLCVP0CzohWjYGDg9OSenZqxEXtxUEReVXKfgxXRehHcG0y3JSKNuK1JNOc9kFY2CVCNhjESY2mfpyL2VByTGhp3G9ORRfjAWwf%2BW635TsXDXvgbaPpXO%2FgCdwQ8b7HNzg%2FQj8Z8EVZ3yVq1Kb9qGw2jEVPxU1pptnwfX80mu5V3n2zB%2BejGvyjRLssdz0exfs4OgI3wHv7TbW3GLdBV%2BpKHI8NMcGdoeqs0NeabWyW46LS2VvSG8%2FjnoyKX3BK1gz2ZYOugplw3ezXeuYVfI01%2BX1sTKpAsm3efxjfZhZdEW22TtJA02DMNcj9tqraWFUzVbWsqh6sqhNH1uz1mkTREEfhqejwam%2B4retyHIMxLw3WBGQH3sZ16Y8VC92gix24LKfYtsydtb3OH48xTw02VNUdVXPjfbaq7qqqxZP4n6RBPx7POH4ab2b4LR%2Frnv9AkVByXyx6Ukb6shGt9rmT5Zi8GbdnDh2Qzp5eqMiVDN%2FDd3goB8vm3nsL2WAqIEtzHG7P%2FGkUzSvmU8EAP%2BOrnGJtkrYRsccfrH9HmGCzEbveWlWr8zw36v8vm3aVNO6bXcc6px9%2FA6xcmIWZPQotAAAAAElFTkSuQmCC&logoColor=white"></a>
  <img alt="OpenClaw" src="https://img.shields.io/badge/OpenClaw-dc2626?style=flat-square&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiNmZmZmZmYiIGZpbGwtcnVsZT0iZXZlbm9kZCIgaGVpZ2h0PSIxZW0iIHN0eWxlPSJmbGV4Om5vbmU7bGluZS1oZWlnaHQ6MSIgdmlld0JveD0iMCAwIDI0IDI0IiB3aWR0aD0iMWVtIj48dGl0bGU%2BT3BlbkNsYXc8L3RpdGxlPjxnIGNsaXAtcGF0aD0idXJsKCNhKSI%2BPHBhdGggZD0iTTkuMDQ2IDcuMTA0YS41MjcuNTI3IDAgMTEwIDEuMDU1LjUyNy41MjcgMCAwMTAtMS4wNTV6IiAvPjxwYXRoIGQ9Ik0xNS4zNzYgNy4xMDRhLjUyOC41MjggMCAxMTAgMS4wNTYuNTI4LjUyOCAwIDAxMC0xLjA1NnoiIC8%2BPHBhdGggY2xpcC1ydWxlPSJldmVub2RkIiBkPSJNMTYuODc3IDEuOTEyYy41OC0uMjcgMS4xNC0uMzIzIDEuNjE2LS4wMzdhLjMxNy4zMTcgMCAwMS0uMzI2LjU0MmMtLjIyNy0uMTM2LS41NDctLjE1My0xLjAyMi4wNjgtLjM1Mi4xNjUtLjc2NS40NS0xLjIzNC44NjYgMi42ODMgMS4xNyA0LjQgMy41IDUuMTQ4IDUuOTIxYTYuNDIxIDYuNDIxIDAgMDAtLjcwNC4xODRjLS41NzguMDE2LTEuMTc0LjIwNC0xLjUwMi43MzUtLjMzOC41NS0uMjY4IDEuMjc2LjA3MiAyLjA2OWwuMDA1LjAxMi4wMDcuMDE0Yy41MjMgMS4wNDUgMS4zMTggMS45MSAyLjIgMi4yODQtLjkxMiAzLjI3NC0zLjQ0IDYuMTQ0LTUuOTcyIDYuOTg4djIuMTA5aC0yLjExdi0yLjExYy0xLjA0My40MTctMi4wODYuMDEtMi4xMSAwdjIuMTFoLTIuMTF2LTIuMTFjLTIuNTMxLS44NDMtNS4wNjEtMy43MTMtNS45NzMtNi45ODcuODgyLS4zNzMgMS42NzgtMS4yMzggMi4yLTIuMjg0bC4wMDctLjAxNC4wMDYtLjAxMmMuMzQtLjc5My40MS0xLjUxOC4wNzEtMi4wNjktLjMyNy0uNTMxLS45MjMtLjcxOS0xLjUwMy0uNzM1YTYuNDA5IDYuNDA5IDAgMDAtLjcwNC0uMTgzYy43NDktMi40MjEgMi40NjYtNC43NTEgNS4xNDktNS45MjItLjQ3LS40MTYtLjg4LS43MDEtMS4yMzQtLjg2Ni0uNDc0LS4yMjEtLjc5NC0uMjA0LTEuMDIxLS4wNjhhLjMxOC4zMTggMCAwMS0uNDM1LS4xMDkuMzE3LjMxNyAwIDAxLjEwOS0uNDMzYy40NzYtLjI4NiAxLjAzNi0uMjMzIDEuNjE1LjAzNy40OS4yMjkgMS4wMzEuNjI4IDEuNjIxIDEuMTgyQTkuOTI0IDkuOTI0IDAgMDExMiAyLjU2OGMxLjE5OSAwIDIuMjg0LjE5IDMuMjU2LjUyNi41OS0uNTU0IDEuMTMtLjk1MyAxLjYyLTEuMTgyek04LjgzNSA2LjU3N2ExLjI2NiAxLjI2NiAwIDEwMCAyLjUzMiAxLjI2NiAxLjI2NiAwIDAwMC0yLjUzMnptNi4zMyAwYTEuMjY3IDEuMjY3IDAgMTAwIDIuNTMzIDEuMjY3IDEuMjY3IDAgMDAwLTIuNTMzeiIgLz48cGF0aCBkPSJNLjM5NSAxMy4xMThjLS45NjYtMS45MzItLjE2My0zLjg2MyAyLjQxLTMuMzY1di0uMDAxbC4wNS4wMWMuMDg0LjAxOC4xNy4wMzguMjYuMDYuMDMzLjAwOS4wNjcuMDE3LjEuMDI3LjA4NC4wMjIuMTY4LjA0OC4yNTUuMDc2bC4wOS4wMjdjLjUyOCAwIC45NS4xNTggMS4xNi41MDEuMjEyLjM0My4yMTIuODctLjEwNSAxLjYxLS4wODUuMTctLjE3OC4zMzMtLjI3Ni40ODlsLS4wMS4wMTdhNC45NjcgNC45NjcgMCAwMS0uNjIuNzkxbC0uMDE5LjAyYy0xLjA5MiAxLjExNy0yLjQ5NiAxLjMzNi0zLjI5NS0uMjYyeiIgLz48cGF0aCBkPSJNMjEuMTkzIDkuNzUzYzIuNTc0LS41IDMuMzc4IDEuNDMzIDIuNDExIDMuMzY1LS41OCAxLjE1OS0xLjQ3NiAxLjM2MS0yLjM0Mi45NmwtLjAxMS0uMDA1YTIuNDE5IDIuNDE5IDAgMDEtLjExNC0uMDU2bC0uMDE5LS4wMWEyLjc1MSAyLjc1MSAwIDAxLS4xMTUtLjA2N2wtLjAyMy0uMDE0Yy0uMDM1LS4wMjItLjA3MS0uMDQ0LS4xMDYtLjA2OGwtLjA1LS4wMzVjLS41NS0uMzg4LTEuMDYyLTEuMDA3LTEuNDQtMS43Ni0uMjc2LS42NDctLjMxMS0xLjEzMi0uMTc0LTEuNDcyLjE3Ni0uNDM5LjYzNi0uNjM5IDEuMjMtLjYzOS4wMzItLjAxMS4wNjYtLjAyLjA5OS0uMDMuMDgtLjAyNi4xNi0uMDUuMjM4LS4wNzJsLjExNy0uMDNhNS41MDIgNS41MDIgMCAwMS4zLS4wNjd6IiAvPjwvZz48ZGVmcz48bGluZWFyR3JhZGllbnQgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiIGlkPSJiIiB4MT0iLS42NTkiIHgyPSIyNy4wMjMiIHkxPSIuNDU4IiB5Mj0iMjIuODU1Ij48c3RvcCBzdG9wLWNvbG9yPSIjRkY0RDREIiAvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzk5MUIxQiIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImMiIHgxPSItLjY1OSIgeDI9IjI3LjAyMyIgeTE9Ii40NTgiIHkyPSIyMi44NTUiPjxzdG9wIHN0b3AtY29sb3I9IiNGRjRENEQiIC8%2BPHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjOTkxQjFCIiAvPjwvbGluZWFyR3JhZGllbnQ%2BPGxpbmVhckdyYWRpZW50IGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIiBpZD0iZCIgeDE9Ii0uNjU5IiB4Mj0iMjcuMDIzIiB5MT0iLjQ1OCIgeTI9IjIyLjg1NSI%2BPHN0b3Agc3RvcC1jb2xvcj0iI0ZGNEQ0RCIgLz48c3RvcCBvZmZzZXQ9IjEiIHN0b3AtY29sb3I9IiM5OTFCMUIiIC8%2BPC9saW5lYXJHcmFkaWVudD48bGluZWFyR3JhZGllbnQgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiIGlkPSJlIiB4MT0iLS42NTkiIHgyPSIyNy4wMjMiIHkxPSIuNDU4IiB5Mj0iMjIuODU1Ij48c3RvcCBzdG9wLWNvbG9yPSIjRkY0RDREIiAvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzk5MUIxQiIgLz48L2xpbmVhckdyYWRpZW50PjxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImYiIHgxPSItLjY1OSIgeDI9IjI3LjAyMyIgeTE9Ii40NTgiIHkyPSIyMi44NTUiPjxzdG9wIHN0b3AtY29sb3I9IiNGRjRENEQiIC8%2BPHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjOTkxQjFCIiAvPjwvbGluZWFyR3JhZGllbnQ%2BPGNsaXBQYXRoIGlkPSJhIj48cGF0aCBkPSJNMCAwaDI0djI0SDB6IiAvPjwvY2xpcFBhdGg%2BPC9kZWZzPjwvc3ZnPg%3D%3D&logoColor=white">
  <img alt="OpenCode" src="https://img.shields.io/badge/OpenCode-10B981?style=flat-square&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIGZpbGw9IiNmZmZmZmYiIGZpbGwtcnVsZT0iZXZlbm9kZCIgaGVpZ2h0PSIxZW0iIHN0eWxlPSJmbGV4Om5vbmU7bGluZS1oZWlnaHQ6MSIgdmlld0JveD0iMCAwIDI0IDI0IiB3aWR0aD0iMWVtIj48dGl0bGU%2Bb3BlbmNvZGU8L3RpdGxlPjxwYXRoIGQ9Ik0xNiA2SDh2MTJoOFY2em00IDE2SDRWMmgxNnYyMHoiIC8%2BPC9zdmc%2B&logoColor=white">
</p>

> 💡 **Get Started Quickly:**
> * 🚀 **[5-Minute "Human-First" Guide](docs/GETTING_STARTED.md)**
> * 🖥️ **OS Installation:** [Windows Setup](docs/QUICKSTART_WINDOWS.md) · [macOS Setup](docs/QUICKSTART_MACOS.md) · [Linux Setup](docs/QUICKSTART_LINUX.md)

---

## 📑 Table of Contents

- [Overview & At a Glance](#-m3-at-a-glance)
- [Memory Model](#-memory-model-at-a-glance)
- [Installation & Onboarding](#-installation)
- [Domain Gating (Token Optimization)](#-domain-gating-keeps-the-catalog-small)
- [Sovereign & Air-Gapped Deployments](#-sovereign--air-gapped-deployments)
- [Interactive Features & Capabilities](#-what-m3-does)
- [Documentation Index](#-documentation-index)
- [Target Audience & Fit](#-who-this-is-for)
- [Quality Assurance & Compliance](#-why-trust-this)
- [Benchmarks & Performance](#-benchmarks)
- [Core Tools Reference](#-core-tools)
- [Agent Integration Prompts](#-for-ai-agents)
- [Interactive Demos](#-see-it-in-action)

---

## ⚡ M3 at a Glance

| Feature | Details |
| :--- | :--- |
| **Works With** | Claude Code · Gemini CLI · Aider · Google Antigravity · OpenCode · Hermes · LangChain/LangGraph · Any MCP Agent |
| **M3 Is** | A persistent memory layer · An MCP server · A hybrid retrieval engine · A bitemporal knowledge base |
| **M3 Is Not** | An LLM · A chatbot · A plain vector database · A RAG framework · An IDE |
| **Core Promise** | Private, offline-capable, locally owned memory shared securely across all your developer tools. |
| **Maturity** | Production-grade. Uses SQLite by default for lightweight operation; scales out to PostgreSQL for enterprise sync. (See [features.json](docs/features.json)) |

---

## 🧠 Memory Model at a Glance

M3 is a **typed, bitemporal, confidence-scored, self-maintaining knowledge base**. Every feature listed below is implemented natively (see [Memory Model Details](docs/MEMORY_MODEL.md)):

*   **Structured Metadata:** Every memory contains a `type`, `source`, `confidence`, `scope`, provenance (`change_agent`), and salience (`importance`, `decay_rate`).
*   **Bitemporal History:** Distinguishes valid-time from transaction-time. Superseded facts are closed rather than deleted, allowing you to query what the agent believed at any specific point in time.
*   **Contradiction Management:** Conflicting facts are resolved automatically on write. The stale fact is marked as superseded, and confidence values are updated dynamically via Bayesian confidence posteriors.
*   **Self-Maintaining Lifecycle:** Implements memory decay, deduplication, automatic consolidation into higher-order beliefs, TTL expiry, and GDPR erasure.
*   **Write-Gating & Content Safety:** Filters out low-signal noise via an enrichment queue and content safety guardrails before storage.
*   **Explainable Retrieval:** Hybrid engine combining vector similarity, BM25 (FTS5), MMR diversity, and reranking. `memory_suggest` returns the exact score breakdown per result. (See [Confidence and Trust Guide](docs/CONFIDENCE_AND_TRUST.md)).
*   **Proven Accuracy:** Evaluated via LongMemEval-S, yielding **92.0% end-to-end QA accuracy** and **99.2% recall @ k=10** (see [Benchmarking Report](benchmarks/longmemeval/LME-S_Benchmarking_Report.md)).

---

## 📦 Installation

### The One-Liner (macOS & Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```
*   *For Windows, please follow the [Windows Manual Installation Guide](docs/install_windows.md).*
*   *To install manually on any platform, refer to the [OS-Specific Install Instructions](INSTALL.md#tldr--manual-path-per-os) or examine the [installer script](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh).*

### Developer Setup Wizard
If you are developing inside python environments:
```bash
pip install m3-memory
m3 setup
```
The `m3 setup` wizard automatically scans your `PATH` for active agents (Claude Code, Gemini CLI, OpenCode, OpenClaw), installs settings files/hooks, provisions the sovereign CPU embedder, and performs a system diagnostic.

### Integrating with AI Coding Tools

#### 🤖 Claude Code
Install as a plugin to unlock `/m3:*` slash commands, curation subagents, and automatic hooks:
```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```
*See [Claude Code Plugin Reference](docs/claude_code_plugin.md) and [Claude.ai Connector Guide](docs/claude_ai_connector.md).*

#### 🪐 Google Antigravity
Install the plugin directly:
```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```
*See [Antigravity Plugin Reference](docs/antigravity_plugin.md).*

#### 🦊 Hermes Agent
Run the wizard to automatically wire up optimal memory providers:
```bash
m3 setup
```
*See [Hermes Plugin Integration Guide](docs/HERMES.md).*

#### 🐍 Python / LangChain & LangGraph
Use M3 as a drop-in Mem0 replacement or LangMem backend:
```bash
pip install m3-memory[langchain]
```
*See [LangChain Integration Guide](docs/integrations/LANGCHAIN.md).*

---

### Manual MCP Server Configuration
To expose M3 to any Model Context Protocol host, add it to your configuration file:

```json
{
  "mcpServers": {
    "memory": {
      "command": "m3"
    }
  }
}
```

---

## 🎚️ Domain Gating Keeps the Catalog Small

Exposing 100+ tools can overwhelm an LLM's context window. To avoid this, M3 groups its tools into **8 domains** (`memory`, `chatlog`, `files`, `entity`, `agent`, `tasks`, `conversations`, `admin`) and loads them lazily. 

Only 10 core tools register at startup. When your agent needs advanced functionalities, it calls `tools_load_domain(domain="...")` to fetch them dynamically.

| Gating Mode | Registered Tools | Tokens in Schema | % of 200K Window |
| :--- | :---: | :---: | :---: |
| **Lazy (Default)** | **10** | **~3,540** | **1.8%** |
| Typical Active Session | 64 | ~17,975 | 9.0% |
| Eager Mode (`M3_TOOLS_LAZY=0`) | 107 | ~24,918 | 12.5% |

> 🛠️ *Note: If your client does not support dynamic tool registration, set the environment variable `M3_TOOLS_LAZY=0` to register all tools eagerly.*

---

## 🛡️ Sovereign & Air-Gapped Deployments

M3 operates completely offline by default. 

### Sovereign Local Embedder
A high-performance BGE-M3 embedder runs locally on `127.0.0.1:8082` after installation.
*   **Default:** CPU execution using GGUF format (`_assets/models/bge-m3-Q4_K_M.gguf`).
*   **Hardware Acceleration (GPU):** Execute `m3 embedder install-gpu` to compile with CUDA, Vulkan, or Metal.
*   **External Provider Fallback:** Set `EMBED_BASE_URL` to route requests to Ollama, LM Studio, or vLLM.

### Rust-Oxidized Performance Core
M3 includes an optional Rust performance module (`m3_core_rs`) that speeds up MMR re-ranking, batch cosine distance calculations, and FTS compilations by **90× to 800×**. If absent, M3 falls back to pure Python execution automatically. Disable with `M3_CORE_RS_DISABLE=1`. (See [Oxidation Benchmarks](docs/OXIDATION_BENCHMARKS.md)).

### Enterprise Security & Compliance
*   **FIPS 140-3 Ready:** Standardized encryption pathways allow routing through validated cryptographic modules (e.g., wolfSSL via `M3_FIPS_MODE=1`).
*   **Air-Gapped Install:** Supports installation without internet access via pre-compiled python wheels. (See [Sovereign Deployment Guide](docs/SOVEREIGN_DEPLOYMENT.md) & [FIPS Boundary Reference](docs/FIPS_MODULE_BOUNDARY.md)).
*   **Storage Location:** All config and data files reside under `~/.m3-memory` (configurable via `M3_MEMORY_ROOT`).

---

## 🔮 What M3 Does

*   **Memory Persistence:** Saves system architecture, project decisions, and preferences across tool boundaries using a local SQLite database.
*   **Autonomous Cognitive Loop:** Background worker (`m3_cognitive_loop.py`) that periodically sweeps chat logs to extract facts, reconcile contradictions, and construct an entity relationship graph.
*   **Hybrid Vector & Keyword Search:** Seamlessly merges vector space, Full-Text Search (FTS5 BM25), and MMR diversity.
*   **Hierarchical File Ingestion:** A dedicated 26-tool files domain reads directories, chunks files, extracts facts, and reviews staleness.
*   **Cross-Device Sync:** Optional PostgreSQL or ChromaDB synchronization backend. Access the same memories on your laptop, desktop, or cloud environments.

---

## 📚 Documentation Index

| Quick & Core | Advanced & Architecture | Integrations & Compliance |
| :--- | :--- | :--- |
| 🚀 **[Getting Started Guide](docs/GETTING_STARTED.md)** | 🏗️ **[System Architecture](docs/ARCHITECTURE.md)** | 🧩 **[LangChain/LangGraph](docs/integrations/LANGCHAIN.md)** |
| ✨ **[Core Features](docs/CORE_FEATURES.md)** | 🔧 **[Technical Implementation](docs/TECHNICAL_DETAILS.md)** | 🧩 **[Hermes Agent](docs/HERMES.md)** |
| ⚙️ **[Environment Variables](docs/ENVIRONMENT_VARIABLES.md)** | 🧠 **[Memory Model Guide](docs/MEMORY_MODEL.md)** | 🛡️ **[Compliance Guide](docs/COMPLIANCE.md)** (GDPR, FISMA) |
| 🛠️ **[Operations Playbook](docs/OPERATIONS.md)** | ⚡ **[Rust Oxidation benchmarks](docs/OXIDATION_BENCHMARKS.md)** | 🛡️ **[FIPS Cryptographic Boundary](docs/FIPS_MODULE_BOUNDARY.md)** |
| 🤖 **[Agent Instructions & Rules](docs/AGENT_INSTRUCTIONS.md)** | 🔍 **[Myths & Facts Guide](docs/MYTHS_AND_FACTS.md)** | 🏠 **[Homelab Patterns](docs/HOMELAB_PATTERNS.md)** |
| 🧩 **[Tool Capability Matrix](docs/CAPABILITY_MATRIX.md)** | 🤖 **[AI Context Injection Profile](docs/llm-context.md)** | 🔢 **[Machine-Readable Features](docs/features.json)** |

---

## 🎯 Who This Is For

### M3 is a great fit if...
*   **You use multiple desktop coding agents:** Interoperate Claude Code, Gemini, and Aider on a shared local history.
*   **You build with LangChain/LangGraph:** An advanced replacement for standard memory models, adding bitemporal queries, contradiction management, and local embeddings.
*   **You need security and compliance:** Built-in `gdpr_forget` and `gdpr_export` tools, air-gapped support, and audit logs.
*   **You value privacy:** Zero external cloud requests or subscriptions required.

### M3 is NOT a fit if...
*   You use **CrewAI** and want standard, framework-native memory models (use [Mem0](https://mem0.ai)).
*   You need a hosted SaaS dashboard with managed infrastructure (use [Letta](https://letta.ai)).
*   You only want transient in-session chat context that resets when you exit the terminal (rely on your agent's defaults).

---

## 🛡️ Why Trust This

*   **Robust Coverage:** Verified with **563 end-to-end tests** spanning search, sync, GDPR lifecycle, and files ingestion.
*   **Audit Reports:** Regular vulnerability reports (Bandit, secrets scans, pip-audit) published directly under [`docs/audits/`](docs/audits/).
*   **Explainable Retrieval:** No black-box queries; retrieval math is open, readable, and scoring parameters are outputted directly.
*   **Open Source:** Apache 2.0 licensed, free, with no SaaS walls or usage limits.

---

## 📊 Benchmarks

### Retrieval Recall (Session Hit-Rate @ k)
Evaluated on the 500-question [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) dataset under default server configurations:

| Retrieve Depth (k) | Session Hit-Rate (SHR) | Success Count | vs. Prior Version |
| :---: | :---: | :---: | :---: |
| 5 | **98.2%** | 491 / 500 | +2.0pp |
| 10 (Default) | **99.2%** | 496 / 500 | +2.4pp |
| 20 | **100.0%** | 500 / 500 | First Report |

### End-to-End QA Accuracy
**92.0% accuracy** (460/500 correct responses) with zero oracle metadata routing:

| Question Domain | Count (n) | Accuracy |
| :--- | :---: | :---: |
| single-session-user | 70 | 94.3% |
| single-session-assistant | 56 | 96.4% |
| single-session-preference | 30 | 80.0% |
| multi-session | 133 | 87.2% |
| temporal-reasoning | 133 | 95.5% |
| knowledge-update | 78 | 93.6% |
| **Overall Summary** | **500** | **92.0%** |

*Methodology and reproducibility details are located in the [LongMemEval-S Benchmarking Report](benchmarks/longmemeval/LME-S_Benchmarking_Report.md).*

---

## 🧰 Core Tools

While M3 features 100+ tools, these five serve as your primary interface:

| Tool Name | Operation Description |
| :--- | :--- |
| `memory_write` | Save a specific fact, project preference, or technical configuration. |
| `memory_search` | Run hybrid keyword (BM25) and semantic vector search. |
| `memory_update` | Edit existing facts to keep memory accurate. |
| `memory_suggest` | Query memories alongside a mathematically explicit score breakdown. |
| `memory_get` | Fetch details of a single memory using its unique ID. |

*Refer to the [Agent Instructions Guide](docs/AGENT_INSTRUCTIONS.md) and [Full MCP Tool Catalog](docs/MCP_TOOLS.md) for complete parameter definitions.*

---

## 🤖 For AI Agents

You can drop the agent ruleset file [`examples/AGENT_RULES.md`](examples/AGENT_RULES.md) into your workspace to teach your agent best practices (e.g., query before writing, update existing records instead of duplicating).

### Command Installation Prompts
Copy and paste these prompts into your terminal client to let your agent set up M3 for you:

#### Claude Code Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"m3"}}} to my
~/.claude/settings.json under "mcpServers". For best retrieval, ensure 
Ollama is running with qwen3-embedding:0.6b (optional, falls back 
to keyword search without it). Then use /mcp to verify the memory server loaded.
```

#### Gemini CLI Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"m3"}}} to my
~/.gemini/settings.json under "mcpServers". For best retrieval, ensure 
Ollama is running with qwen3-embedding:0.6b (optional, falls back 
to keyword search without it).
```

#### Active Chatlog Capture Plugin
To configure instant conversation logging and backup, tell your active coding agent:
```text
Install the m3-memory chat log subsystem.
```
The agent executes `bin/chatlog_init.py` and configures execution triggers (see [Chat Log Architecture Guide](docs/CHATLOG.md)).

---

## 🎬 See it in action

### Contradiction Detection & Automatic Resolution
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_contradiction.svg" alt="Contradiction Demo" width="100%">
</p>

### Hybrid Search Scoring Details
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_search.svg" alt="Hybrid Search Demo" width="100%">
</p>

### Multi-Device Database Sync
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_sync.svg" alt="Sync Demo" width="100%">
</p>

---

## 💬 Community

[![Discord Badge](https://img.shields.io/badge/Discord-M3_Memory-5865F2?logo=discord&logoColor=white&style=flat-square)](https://discord.gg/ZcJ3EGC99B)
&nbsp;
[![GitHub Issues Badge](https://img.shields.io/badge/GitHub-Issues-181717?logo=github&style=flat-square)](https://github.com/skynetcmd/m3-memory/issues)

[How to Contribute](docs/CONTRIBUTING.md) · [Good First Issues](docs/GOOD_FIRST_ISSUES.md)

---

## 📜 License & Attributions

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

### Asset & Icon Credits
* **Hermes (Caduceus) Icon:** Derived from the Public Domain vector [Caduceus.svg](https://commons.wikimedia.org/wiki/File:Caduceus.svg) on Wikimedia Commons (originally by OpenClipart).
* **OpenClaw & OpenCode Icons:** Derived from the MIT licensed icon set by [LobeHub](https://github.com/lobehub/lobe-icons).

---

[![Star History Chart](https://api.star-history.com/svg?repos=skynetcmd/m3-memory&type=Date)](https://star-history.com/#skynetcmd/m3-memory&Date)

<!-- mcp-name: io.github.skynetcmd/m3-memory -->

