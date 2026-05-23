#!/usr/bin/env python3
"""
Retrieval Quality Benchmark for M3 Memory System.
Measures Hit@1, Hit@5, MRR, and Latency across diverse test cases.
"""

import asyncio
import os
import sys
import time
import uuid
import statistics
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from memory_core import memory_write_impl, memory_search_impl, memory_delete_impl, _db
import httpx

# ── Probe LM Studio (from test_memory_bridge.py) ─────────────────────────────
async def probe_lm_studio():
    try:
        from auth_utils import get_api_key
        token = get_api_key("LM_API_TOKEN") or get_api_key("LM_STUDIO_API_KEY")

        timeout = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            resp = await client.get(
                "http://127.0.0.1:1234/v1/models",
                headers=headers,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        print(f"Probe failed: {type(e).__name__}: {e}")
        return False

# ── Benchmark Config ──────────────────────────────────────────────────────────
BENCH_AGENT = f"bench_{uuid.uuid4().hex[:8]}"

TEST_DATA = [
    {"type": "fact", "title": "Capital of France", "content": "The capital of France is Paris, known for the Eiffel Tower."},
    {"type": "fact", "title": "Speed of Light", "content": "The speed of light in a vacuum is approximately 299,792,458 meters per second."},
    {"type": "decision", "title": "Project Alpha Stack", "content": "We decided to use React with TypeScript and FastAPI for Project Alpha."},
    {"type": "note", "title": "Shopping List", "content": "Buy milk, eggs, bread, and organic honey from the local market."},
    {"type": "preference", "title": "Editor Theme", "content": "The user prefers the 'Dracula' dark theme for VS Code and IntelliJ."},
    {"type": "code", "title": "Python Fibonacci", "content": "def fib(n): a, b = 0, 1; while a < n: print(a, end=' '); a, b = b, a+b"},
    {"type": "observation", "title": "System Latency", "content": "Observed 50ms spike in database response times during peak hours (2 PM)."},
    {"type": "plan", "title": "Q3 Roadmap", "content": "Q3 goals: Implement multi-tenancy, upgrade to Postgres 16, and add RAG support."},
    {"type": "snippet", "title": "Nginx Redirect", "content": "rewrite ^/old-path/(.*)$ /new-path/$1 permanent;"},
    {"type": "user_fact", "title": "User Allergies", "content": "The user is allergic to peanuts and shellfish."},
    {"type": "note", "title": "Travel Tips: Japan", "content": "Get a Suica card for trains and carry cash for small restaurants."},
    {"type": "fact", "title": "Mount Everest Height", "content": "Mount Everest's peak is 8,848.86 meters above sea level."},
    {"type": "decision", "title": "Remote Work Policy", "content": "Team members can work remotely up to 3 days per week starting September."},
    {"type": "config", "title": "Log Level", "content": "Production log level set to WARNING to reduce storage usage."},
    {"type": "observation", "title": "Battery Drain", "content": "MacBook Pro M3 battery drops 15% faster when running local LLMs."},
    {"type": "task", "title": "Fix Auth Bug", "content": "Fix the race condition in the session refresh token logic."},
    {"type": "code", "title": "Rust Hello World", "content": "fn main() { println!(\"Hello, world!\"); }"},
    {"type": "fact", "title": "First Moon Landing", "content": "Apollo 11 landed on the moon on July 20, 1969, with Neil Armstrong and Buzz Aldrin."},
    {"type": "decision", "title": "Database Choice", "content": "Selected SQLite for local storage and PostgreSQL for cloud synchronization."},
    {"type": "note", "title": "Meeting Notes: UX", "content": "Feedback: the dashboard icons are too small on mobile devices."},
]

TEST_CASES = [
    {"query": "What is the capital of France?", "expected_title": "Capital of France", "desc": "Simple fact retrieval"},
    {"query": "How fast is light?", "expected_title": "Speed of Light", "desc": "Scientific fact"},
    {"query": "What tech stack are we using for Alpha?", "expected_title": "Project Alpha Stack", "desc": "Project decision"},
    {"query": "What do I need to buy at the market?", "expected_title": "Shopping List", "desc": "Personal note"},
    {"query": "Does the user like light or dark themes?", "expected_title": "Editor Theme", "desc": "User preference"},
    {"query": "Show me a fibonacci implementation in python", "expected_title": "Python Fibonacci", "desc": "Code snippet"},
    {"query": "When does the database slow down?", "expected_title": "System Latency", "desc": "Observation"},
    {"query": "What are our goals for Q3?", "expected_title": "Q3 Roadmap", "desc": "Strategic plan"},
    {"query": "nginx path rewrite rule", "expected_title": "Nginx Redirect", "desc": "Technical snippet"},
    {"query": "What should the user avoid eating?", "expected_title": "User Allergies", "desc": "Safety/User fact"},
]

async def run_benchmark():
    if not await probe_lm_studio():
        print("⏭  LM Studio offline. Skipping benchmark.")
        sys.exit(0)

    print(f"--- Seeding {len(TEST_DATA)} memories for agent {BENCH_AGENT} ---")
    seeded_ids = []
    for item in TEST_DATA:
        res = await memory_write_impl(**item, agent_id=BENCH_AGENT, embed=True)
        mid = res.split("Created: ")[1].split()[0]
        seeded_ids.append(mid)

    print(f"\n--- Running {len(TEST_CASES)} Retrieval Test Cases ---")
    metrics = {"hits@1": 0, "hits@5": 0, "mrr": 0, "latencies": []}
    
    print(f"{'#':<3} | {'Query':<35} | {'Rank':<4} | {'Latency':<8} | {'Status'}")
    print("-" * 70)

    for i, case in enumerate(TEST_CASES, 1):
        start_t = time.perf_counter()
        # Search returns a formatted string; we need to parse it or modify search_impl to return data
        # For simplicity in this script, we'll check if the expected title is in the top results
        results_str = await memory_search_impl(case["query"], k=5, agent_filter=BENCH_AGENT)
        latency = (time.perf_counter() - start_t) * 1000
        metrics["latencies"].append(latency)

        lines = results_str.split("\n")
        # Find the line starting with "1. [" to establish base for ranking
        base_line_idx = -1
        for idx, line in enumerate(lines):
            if line.startswith("1. ["):
                base_line_idx = idx
                break
        
        rank = -1
        if base_line_idx != -1:
            for line_idx in range(base_line_idx, len(lines)):
                line = lines[line_idx]
                if case["expected_title"] in line and "title:" in line:
                    # Current rank is (line_idx - base_line_idx) / 3 + 1 because of separator and content lines
                    # A more robust way: each item starts with "N. ["
                    if ". [" in line:
                        rank = int(line.split(". [")[0].strip())
                    break
        
        status = "❌ FAIL"
        if rank == 1:
            metrics["hits@1"] += 1
            metrics["hits@5"] += 1
            metrics["mrr"] += 1.0
            status = "✅ HIT@1"
        elif 1 < rank <= 5:
            metrics["hits@5"] += 1
            metrics["mrr"] += (1.0 / rank)
            status = f"✅ HIT@{rank}"
        
        rank_label = str(rank) if rank > 0 else "N/A"
        print(f"{i:<3} | {case['query'][:35]:<35} | {rank_label:<4} | {latency:>6.1f}ms | {status}")

    # Aggregates
    n = len(TEST_CASES)
    avg_mrr = metrics["mrr"] / n
    hit1_rate = metrics["hits@1"] / n
    hit5_rate = metrics["hits@5"] / n
    avg_lat = sum(metrics["latencies"]) / n
    p50_lat = statistics.median(metrics["latencies"])
    p95_lat = sorted(metrics["latencies"])[int(n * 0.95)] if n >= 20 else max(metrics["latencies"])

    print("\n--- BENCHMARK SUMMARY ---")
    print(f"Mean MRR:       {avg_mrr:.4f}")
    print(f"Hit@1 Rate:     {hit1_rate * 100:.1f}%")
    print(f"Hit@5 Rate:     {hit5_rate * 100:.1f}%")
    print(f"Avg Latency:    {avg_lat:.1f} ms")
    print(f"P50 Latency:    {p50_lat:.1f} ms")
    print(f"P95 Latency:    {p95_lat:.1f} ms")

    print("\n--- Cleaning up test data ---")
    for mid in seeded_ids:
        memory_delete_impl(mid, hard=True)

    if avg_mrr >= 0.5:
        print("\n✅ Benchmark PASSED (MRR >= 0.5)")
        sys.exit(0)
    else:
        print("\n❌ Benchmark FAILED (MRR < 0.5)")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_benchmark())
