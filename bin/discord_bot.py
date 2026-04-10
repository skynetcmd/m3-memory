# -*- coding: utf-8 -*-
"""
M3_Bot — Discord bot for M3-Memory community support.
Monitors channels, answers questions using repo documentation.
Embedding failover: local → SkyPC (10.21.40.2:1234) → MacBook (10.21.32.226:1234)
"""

import os
import re
import json
import time
import logging
import asyncio
import hashlib
import httpx
import discord
from discord.ext import commands
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DOCS_DIR      = Path(os.environ.get("M3_DOCS_DIR", "/opt/m3-memory"))

# LLM failover chain (OpenAI-compatible)
LLM_ENDPOINTS = [
    os.environ.get("LLM_PRIMARY",   "http://localhost:1234"),
    os.environ.get("LLM_SECONDARY", "http://10.21.40.2:1234"),
    os.environ.get("LLM_TERTIARY",  "http://10.21.32.226:1234"),
]
LLM_MODEL = os.environ.get("LLM_MODEL", "")   # empty = auto-detect

# Embedding failover chain
EMBED_ENDPOINTS = [
    os.environ.get("EMBED_PRIMARY",   "http://localhost:1234"),
    os.environ.get("EMBED_SECONDARY", "http://10.21.40.2:1234"),
    os.environ.get("EMBED_TERTIARY",  "http://10.21.32.226:1234"),
]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")

# Channels M3_Bot actively monitors for questions
MONITORED_CHANNELS = {
    "ask-anything", "bug-reports", "agent-setup",
    "search-quality", "sync-federation", "security",
    "llm-integration", "schema-migrations", "performance",
    "memory-design", "benchmarks", "ideas-lab", "general-chat",
}

BOT_PREFIX     = "!"
COOLDOWN_SECS  = 10       # min seconds between bot replies per user
MAX_DOC_CHARS  = 12000    # context window budget for docs
MAX_REPLY_CHARS = 1800    # Discord message limit buffer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("m3bot")

# ── Doc loader ─────────────────────────────────────────────────────────────────
DOC_FILES = [
    "README.md", "ARCHITECTURE.md", "CORE_FEATURES.md",
    "TECHNICAL_DETAILS.md", "ENVIRONMENT_VARIABLES.md",
    "SETUP_INSTRUCTIONS.md", "CONTRIBUTING.md", "SECURITY.md",
    "TROUBLESHOOTING.md", "docs/API_REFERENCE.md",
    "install_linux.md", "install_macos.md", "install_windows-powershell.md",
]

def load_docs() -> dict[str, str]:
    docs = {}
    for fname in DOC_FILES:
        path = DOCS_DIR / fname
        if path.exists():
            docs[fname] = path.read_text(encoding="utf-8", errors="ignore")
            log.info(f"Loaded doc: {fname} ({len(docs[fname])} chars)")
        else:
            log.warning(f"Doc not found: {path}")
    return docs

def build_context(docs: dict[str, str], query: str, budget: int = MAX_DOC_CHARS) -> str:
    """Select most relevant doc sections for a query (simple keyword scoring)."""
    keywords = set(re.findall(r'\w+', query.lower())) - {
        "the","a","an","is","it","how","do","i","to","in","of","for","and","or","what","can","you"
    }
    scored = []
    for fname, content in docs.items():
        sections = re.split(r'\n#{1,3} ', content)
        for sec in sections:
            score = sum(sec.lower().count(kw) for kw in keywords)
            if score > 0:
                scored.append((score, fname, sec[:2000]))
    scored.sort(reverse=True)
    context, used = [], 0
    for score, fname, text in scored:
        if used + len(text) > budget:
            break
        context.append(f"### [{fname}]\n{text}")
        used += len(text)
    return "\n\n".join(context) if context else ""

# ── LLM caller with failover ───────────────────────────────────────────────────
async def get_best_model(endpoint: str, client: httpx.AsyncClient) -> str:
    try:
        r = await client.get(f"{endpoint}/v1/models", timeout=5)
        models = r.json().get("data", [])
        # Filter out embedding models, pick largest by id heuristic
        text_models = [m["id"] for m in models if "embed" not in m["id"].lower()]
        return text_models[0] if text_models else (models[0]["id"] if models else "")
    except Exception:
        return ""

async def llm_complete(prompt: str, system: str, client: httpx.AsyncClient) -> str:
    for endpoint in LLM_ENDPOINTS:
        try:
            model = LLM_MODEL or await get_best_model(endpoint, client)
            if not model:
                continue
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "max_tokens": 1024,
                "temperature": 0.3,
            }
            r = await client.post(
                f"{endpoint}/v1/chat/completions",
                json=payload, timeout=60
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.warning(f"LLM endpoint {endpoint} failed: {e}")
    return ""

# ── Question classifier ────────────────────────────────────────────────────────
QUESTION_PATTERNS = re.compile(
    r'\b(how|what|why|where|when|can|does|is|are|which|who|help|error|fail|install|setup|config)\b',
    re.IGNORECASE
)

def looks_like_question(text: str) -> bool:
    return bool(QUESTION_PATTERNS.search(text)) or text.strip().endswith("?")

def is_bot_mention(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    return bot_user in message.mentions or "m3_bot" in message.content.lower()

# ── System prompt ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are M3_Bot, the official community assistant for M3-Memory — a local-first, \
privacy-respecting agentic memory layer for AI agents (Claude Code, Gemini CLI, Aider, OpenClaw).

Your job is to answer user questions based ONLY on the provided documentation context. Be concise, \
accurate, and friendly. Use markdown formatting. If the answer is not in the docs, say so honestly \
and suggest they ask in #ask-anything or open a GitHub issue. Never make up API details or config values.

Keep responses under 1800 characters. For code, use code blocks. End with a helpful tip when relevant."""

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot   = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)
docs  = {}
cooldowns: dict[int, float] = {}   # user_id -> last_reply_time

@bot.event
async def on_ready():
    global docs
    log.info(f"M3_Bot online as {bot.user} (id: {bot.user.id})")
    docs = load_docs()
    log.info(f"Loaded {len(docs)} documentation files")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="M3-Memory docs 🧠"
    ))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    ch_name = getattr(message.channel, "name", "")
    mentioned = is_bot_mention(message, bot.user)
    in_monitored = ch_name in MONITORED_CHANNELS

    # Only respond if mentioned OR in a monitored channel with a question
    if not mentioned and not (in_monitored and looks_like_question(message.content)):
        return

    # Cooldown check
    now = time.time()
    last = cooldowns.get(message.author.id, 0)
    if now - last < COOLDOWN_SECS and not mentioned:
        return
    cooldowns[message.author.id] = now

    query = message.content.replace(f"<@{bot.user.id}>", "").strip()
    if len(query) < 5:
        return

    async with message.channel.typing():
        async with httpx.AsyncClient() as client:
            context = build_context(docs, query)
            if context:
                prompt = f"Documentation context:\n{context}\n\nUser question: {query}"
            else:
                prompt = f"User question (no matching docs found): {query}"

            answer = await llm_complete(prompt, SYSTEM_PROMPT, client)

    if not answer:
        answer = (
            "I couldn't reach any LLM endpoint right now. "
            "Please check **#ask-anything** or the repo docs directly."
        )

    # Trim if too long
    if len(answer) > MAX_REPLY_CHARS:
        answer = answer[:MAX_REPLY_CHARS - 20] + "\n*(truncated)*"

    await message.reply(answer, mention_author=False)

# ── Commands ───────────────────────────────────────────────────────────────────
@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! 🏓 Latency: {round(bot.latency * 1000)}ms")

@bot.command(name="docs")
async def list_docs(ctx):
    loaded = "\n".join(f"• `{k}`" for k in docs)
    await ctx.send(f"**Loaded documentation files:**\n{loaded}")

@bot.command(name="ask")
async def ask_command(ctx, *, question: str):
    """Force a doc lookup: !ask <question>"""
    async with ctx.typing():
        async with httpx.AsyncClient() as client:
            context = build_context(docs, question)
            prompt = f"Documentation context:\n{context}\n\nUser question: {question}" if context else question
            answer = await llm_complete(prompt, SYSTEM_PROMPT, client)
    await ctx.reply(answer or "No answer found.", mention_author=False)

@bot.command(name="reload")
@commands.has_permissions(administrator=True)
async def reload_docs(ctx):
    global docs
    docs = load_docs()
    await ctx.send(f"✅ Reloaded {len(docs)} documentation files.")

@bot.command(name="help")
async def help_command(ctx):
    await ctx.send(
        "**M3_Bot Commands:**\n"
        "`!ask <question>` — Ask anything about M3-Memory\n"
        "`!ping` — Check bot latency\n"
        "`!docs` — List loaded documentation files\n"
        "`!reload` — Reload docs (admin only)\n\n"
        "I also automatically answer questions in monitored channels. "
        "Just ask naturally or mention me `@M3_Bot`! 🧠"
    )

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
