# mac-agent (Production Dual-Mode Agent for macOS)

This project turns your MacBook into a self-contained AI agent that:

- Uses LM Studio (MLX models) as the primary local LLM.
- Routes to Claude, Gemini, Grok, and Perplexity via a Python FastAPI router.
- Stores memory locally in SQLite with embeddings.
- Exposes HTTP tools for Gemini CLI (filesystem, LLM router, home discovery).
- Joins your home AI ecosystem (NeuralHome / Node-RED / home GPU servers) when available, but never depends on them.

It assumes:

- LM Studio is running on `http://localhost:1234` (OpenAI-compatible).
- FastAPI/Uvicorn will serve this app on `http://localhost:8000`.

## Quick start

```bash
cd ~/m3-memory/mac-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your API keys and home URLs

./scripts/start.sh

