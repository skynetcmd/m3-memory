# ⚡ M3 Memory Integration for Hermes Agent

Welcome! This guide walks you through integrating **M3 Memory** (State-of-the-Art, vector-accelerated rich memory) with **Hermes Agent**.

By combining Hermes' powerful autonomous execution with M3's advanced 103-tool multi-corpus memory system, you unlock SOTA long-term recall, semantic association, and tamper-proof cryptographic audit trails.

---

## 🌟 The Best of Both Worlds: Two Integration Modes

M3 is designed to be highly flexible. When hooking it up to Hermes Agent, you can choose between two main operating strategies:

### Option A: Optimal Replacement (Recommended for Unified SOTA Memory)
In this mode, you configure Hermes Agent to use **M3 as its primary memory provider**. 
* **Why do this?** Hermes' default file-based memory can grow slow and lacks semantic understanding. By replacing it with M3, Hermes gains instant access to **BGE-M3 vector embeddings**, **FTS5 hybrid search**, and automated **fact decay/curation**.
* **How it works:** Hermes loads the M3 memory provider plugin, redirecting all default read/write operations to the M3 SQLite memory catalog (`~/.m3-memory/memory/agent_memory.db`).

### Option B: Parallel Co-existence (SOTA Rich Memory Toolbelt)
In this mode, you keep Hermes' default memory provider active, but load M3's **100+ MCP tools** directly into Hermes' runtime toolbelt.
* **Why do this?** This allows Hermes to continue using its lightweight built-in memory for simple context holding while granting it access to M3's specialized tools (like `memory_link`, `task_create`, `files_ingest`, and `chatlog_search`) for complex research and file-indexing tasks.

---

## 🚀 Step-by-Step Setup Guide

### Step 1: Run the M3 Setup Wizard
The easiest way to integrate M3 is through our automated interactive wizard:
```bash
python -m m3_memory.setup_wizard
```
1. The wizard will automatically probe common system locations (such as `%LOCALAPPDATA%\hermes`, `~/.hermes`, `~/hermes-agent`) to **autodetect** your Hermes Agent installation.
2. When prompted:
   > `Install the m3 SOTA memory-provider plugin into Hermes Agent? [y/N]`
   Type **`y`** to automatically copy the M3 provider files into the Hermes plugins tree.

### Step 2: Configure Environment Variables
Hermes Agent loads plugins dynamically and needs to find M3's core package in its search path. You must add the path to the M3 repository's `bin/` directory to the `PYTHONPATH` environment variable in Hermes' launch environment.

#### On Windows (PowerShell):
```powershell
$env:PYTHONPATH = "C:\path\to\m3-memory\bin;" + $env:PYTHONPATH
```

#### On Linux / macOS (Bash/Zsh):
```bash
export PYTHONPATH="/path/to/m3-memory/bin:$PYTHONPATH"
```

> [!TIP]
> To make this change permanent, add the environment variable to your terminal profile (`.bashrc` / `.zshrc`) or user-level environment settings in Windows.

### Step 3: Select the M3 Plugin in Hermes
Now, tell Hermes Agent to activate the M3 provider. Memory providers in Hermes are **single-select**.

1. Open your Hermes terminal configuration or run the plugin selector:
   ```bash
   hermes plugins
   ```
2. Navigate to the **Memory Providers** section and select **`m3`**.
3. Save and close.

---

## 🔍 Verifying the Connection

To verify that the bridge is functional, you can run the provider logic verification script:
```bash
python m3_memory/integrations/hermes/test_provider_logic.py
```
If successful, the test suite will print a green confirmation indicating that Hermes is successfully reading, writing, and querying the M3 memory database.

---

## 🛠️ Diagnostics & Curation
Once hooked up, you can monitor the health and performance of the memory bridge using the live interactive diagnostics dashboard built into M3:
```bash
python bin/chatlog_status.py --live
```
From the TUI monitor, you can:
* Press **`[S]`** to run the embedding sweeper.
* Press **`[D]`** or **`[A]`** to review or apply deterministic decay sweeps.
* Press **`[F]`** to ingest or sync external project directories directly into M3.

For more details on M3's capabilities, check out our primary documentation:
* [Main Documentation Gateway](../README.md)
* [MCP Tools Catalog](MCP_TOOLS.md)
* [Curation Guide](curate-memory.md)
