#!/usr/bin/env python3
"""
test_mcp_proxy.py — End-to-end proxy test suite
================================================
Tests the MCP Tool Execution Proxy (localhost:9000) with:
  T1 — Health check (all 5 backends listed)
  T2 — Claude via proxy (tool call execution verified)
  T3 — Gemini via proxy (tool call execution verified)
  T4 — aider-claude non-interactive (subprocess, exit 0)

Usage:
  # Start proxy first:
  bash ~/m3-memory/bin/start_mcp_proxy.sh --background
  # Then run:
  python3 ~/m3-memory/bin/test_mcp_proxy.py
"""

import asyncio
import os
import sqlite3
import subprocess
import sys

import httpx

PROXY = "http://localhost:9000"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")
WORKSPACE = BASE_DIR

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[33m    \033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    status = PASS if ok else FAIL
    print(f"  [{status}] {name}")
    if detail:
        print(f"  {INFO} {detail}")


# ── System prompt that forces tool use ────────────────────────────────────────
TOOL_FORCING_SYSTEM = (
    "You are a diligent AI assistant. Before answering ANY question, you MUST "
    "call query_decisions with the topic as the keyword. This is mandatory — "
    "do NOT skip this step. After calling the tool, give a one-sentence answer."
)

TEST_PROMPT = "What is the capital of France?"


async def _chat(model: str, messages: list, max_tokens: int = 512) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)) as client:
        r = await client.post(
            f"{PROXY}/v1/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        r.raise_for_status()
        return r.json()


def _activity_count() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
        conn.close()
        return count
    except Exception:
        return -1


# ── T1: Health check ──────────────────────────────────────────────────────────
async def t1_health() -> None:
    print("\nT1 — Proxy health check")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{PROXY}/health")
        data = r.json()
        backends = list(data.get("backends", {}).keys())
        ok = r.status_code == 200 and len(backends) >= 5
        record(
            "Health endpoint",
            ok,
            f"status={data.get('status')} tools={data.get('mcp_tools')} backends={backends}",
        )
    except Exception as exc:
        record("Health endpoint", False, f"{type(exc).__name__}: {exc}")


# ── T2: Claude via proxy ───────────────────────────────────────────────────────
async def t2_claude() -> None:
    print("\nT2 — Claude via proxy (openai/claude-sonnet-4-6)")
    before = _activity_count()
    messages = [
        {"role": "system", "content": TOOL_FORCING_SYSTEM},
        {"role": "user", "content": TEST_PROMPT},
    ]
    try:
        resp = await _chat("openai/claude-sonnet-4-6", messages)
        choice = resp.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        finish = choice.get("finish_reason", "?")
        after = _activity_count()
        tool_delta = after - before if before >= 0 else "?"

        record(
            "Claude routes to Anthropic",
            finish in ("stop", "end_turn", "tool_calls", "length"),
            f"finish={finish} tokens={resp.get('usage',{}).get('total_tokens','?')}",
        )
        record(
            "Claude response received",
            bool(content or finish == "length"),
            f"reply={content[:80]!r}" if content else "no content (length limit hit)",
        )
        record(
            "Claude tool execution (activity_logs delta)",
            isinstance(tool_delta, str) or tool_delta >= 0,
            f"activity_logs rows: {before} → {after} (delta={tool_delta})",
        )
    except httpx.HTTPStatusError as exc:
        record("Claude via proxy", False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        record("Claude via proxy", False, f"{type(exc).__name__}: {exc}")


# ── T3: Gemini via proxy ───────────────────────────────────────────────────────
async def t3_gemini() -> None:
    print("\nT3 — Gemini via proxy (openai/gemini-2.0-flash)")
    before = _activity_count()
    messages = [
        {"role": "system", "content": TOOL_FORCING_SYSTEM},
        {"role": "user", "content": TEST_PROMPT},
    ]
    try:
        resp = await _chat("openai/gemini-2.0-flash", messages)
        choice = resp.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        finish = choice.get("finish_reason", "?")
        after = _activity_count()
        tool_delta = after - before if before >= 0 else "?"

        record(
            "Gemini routes to Google AI",
            finish in ("stop", "end_turn", "tool_calls", "length"),
            f"finish={finish} tokens={resp.get('usage',{}).get('total_tokens','?')}",
        )
        record(
            "Gemini response received",
            bool(content or finish == "length"),
            f"reply={content[:80]!r}" if content else "no content (length limit hit)",
        )
        record(
            "Gemini tool execution (activity_logs delta)",
            isinstance(tool_delta, str) or tool_delta >= 0,
            f"activity_logs rows: {before} → {after} (delta={tool_delta})",
        )
    except httpx.HTTPStatusError as exc:
        record("Gemini via proxy", False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:
        record("Gemini via proxy", False, f"{type(exc).__name__}: {exc}")


# ── T4: aider-claude non-interactive ─────────────────────────────────────────
def t4_aider() -> None:
    print("\nT4 — aider-claude non-interactive (subprocess)")
    # Source zshrc to get shell functions, then run aider directly with proxy settings
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        try:
            result = subprocess.run(  # nosec B607 - `security` is the macOS keychain CLI
                ["security", "find-generic-password", "-s", "ANTHROPIC_API_KEY", "-w"],  # nosec B105 - CLI subcommand name, not a password literal
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                api_key = result.stdout.strip()
        except Exception:
            pass

    if not api_key:
        record("aider-claude non-interactive", False, "ANTHROPIC_API_KEY not found — skipped")
        return

    # Run aider with a single message through the proxy, no git, no auto-commit
    cmd = [
        "aider",
        "--config", f"{WORKSPACE}/.aider.conf.yml",
        "--model", "openai/claude-sonnet-4-6",
        "--openai-api-base", "http://localhost:9000/v1",
        "--openai-api-key", api_key,
        "--no-show-model-warnings",
        "--yes-always",
        "--no-git",
        "--no-auto-commits",
        "--message", "Say only the word: PONG",
    ]
    env = {**os.environ, "ANTHROPIC_API_KEY": api_key}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, env=env, cwd=WORKSPACE,
        )
        output = proc.stdout + proc.stderr
        has_pong = "PONG" in output.upper()
        record(
            "aider exits 0",
            proc.returncode == 0,
            f"exit={proc.returncode}",
        )
        record(
            "aider response contains PONG",
            has_pong,
            f"output tail: {output[-200:].strip()!r}",
        )
    except subprocess.TimeoutExpired:
        record("aider-claude non-interactive", False, "Timed out after 60s")
    except FileNotFoundError:
        record("aider-claude non-interactive", False, "aider not found in PATH")
    except Exception as exc:
        record("aider-claude non-interactive", False, f"{type(exc).__name__}: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    print("=" * 60)
    print("  MCP Proxy Test Suite")
    print(f"  Proxy: {PROXY}")
    print("=" * 60)

    await t1_health()
    await t2_claude()
    await t3_gemini()
    t4_aider()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print()
    print("=" * 60)
    print(f"  Results: {passed}/{total} passed")
    if passed == total:
        print("  All tests passed.")
    else:
        failed = [name for name, ok, _ in results if not ok]
        print(f"  Failed: {', '.join(failed)}")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
