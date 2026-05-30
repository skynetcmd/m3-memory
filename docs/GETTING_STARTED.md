# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Welcome to M3 Memory!

If you've ever felt like your AI agent is a "stranger" every time you start a new session—forgetting your architectural preferences, your naming conventions, or that specific bug you fixed yesterday—**you're in the right place.**

M3 Memory is designed to be your agent's "long-term brain." It’s local, it’s private, and it’s built to grow with you.

---

## 🕒 Your First 5 Minutes (The "Magic Moment")

The best way to understand M3 is to see it "wake up." Let’s skip the technical jargon and get straight to the payoff.

### 1. The Setup
**One-line installer (Linux + macOS):**

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

The script installs m3, then runs the **one-command wizard** (`m3 setup`)
which sets up the embedder, wires every agent it detects on PATH, installs
chatlog hooks, and runs a final `m3 doctor`. Restart your agent — that's it.

**Already have Python/pipx?** Two lines:

```bash
pipx install m3-memory
m3 setup
```

**Claude Code users — install as a plugin instead:**

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

That gets you 15 `/m3:*` slash commands (`/m3:health`, `/m3:search`, `/m3:save`, …) plus auto-wired hooks. See [the plugin reference](./claude_code_plugin.md).

**Google Antigravity users — install as a plugin directly:**

```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```

That installs all 15 `/m3:*` slash commands as native agent Skills and auto-wires the chatlog hooks. See [the plugin reference](./antigravity_plugin.md).

**Windows or manual install:** see the [README](../README.md#-install), [INSTALL.md](../INSTALL.md), or the per-OS quickstarts ([Linux](./QUICKSTART_LINUX.md) / [macOS](./QUICKSTART_MACOS.md) / [Windows](./QUICKSTART_WINDOWS.md)).

> **Tool catalog stays small in your context.** m3 ships 87 MCP tools but
> groups them into 8 domains (memory, chatlog, files, entity, agent, tasks,
> conversations, admin). Only ~6 essentials load at MCP startup
> (~2,400 tokens vs ~16,100 if all 87 loaded eagerly). The agent pulls in a
> domain on demand — just say "load the files tools" and it does.

### 2. The "Cat Test" (Our Favorite Ritual)
Open your favorite agent and try this simple experiment:

1.  **Introduce yourself:** *"Hey, just so you know, my cat's name is 'Binary' and she only eats expensive tuna. Remember that for later."*
2.  **Verify the write:** The agent should call `memory_write` automatically.
3.  **The Fresh Start:** Close the agent completely. Kill the process. Open a brand new session.
4.  **The Payoff:** Ask: *"Remind me what I need to buy for my pet?"*

**The Moment:** Instead of saying "I don't have information about your pet," your agent will call `memory_search` and respond: *"You need to buy expensive tuna for your cat, Binary."*

**That’s the M3 experience: No more re-explaining. Just working.**

---

## 🛡️ Our Promise: Local-First, Privacy-Always

We believe your thoughts and project details are your own. 

- **Zero Cloud Egress:** Your memories live in a local SQLite database on your machine.
- **No API Keys for Memory:** You don't need a subscription to "remember" things.
- **Explainable:** Use `memory_suggest` anytime to ask the agent: *"Why did you remember this specific fact?"* It will show you the exact math behind its retrieval.

---

## 🗺️ Where to go from here?

Once you've had your first "Magic Moment," you might want to dive deeper:

- **[Core Features](./CORE_FEATURES.md)** — Learn about contradiction detection and the knowledge graph.
- **[Multi-Agent Teams](./MULTI_AGENT.md)** — How to let two different agents (like Claude and Gemini) share the same brain.
- **[Technical Details](./TECHNICAL_DETAILS.md)** — For the curious: schemas, search weights, and bitemporal logic.

**Welcome to the fleet. We're glad to have you building with us.** 🤝
