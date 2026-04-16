# <a href="./README.md"><img src="docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Welcome to M3 Memory!

If you've ever felt like your AI agent is a "stranger" every time you start a new session—forgetting your architectural preferences, your naming conventions, or that specific bug you fixed yesterday—**you're in the right place.**

M3 Memory is designed to be your agent's "long-term brain." It’s local, it’s private, and it’s built to grow with you.

---

## 🕒 Your First 5 Minutes (The "Magic Moment")

The best way to understand M3 is to see it "wake up." Let’s skip the technical jargon and get straight to the payoff.

### 1. The Setup
If you have `pip` and an MCP-compatible agent (like Claude Code, Gemini CLI, or Aider), run:
```bash
pip install m3-memory
m3-memory setup  # This will walk you through a 1-minute local config
```

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
