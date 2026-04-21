#!/usr/bin/env python3
"""End-to-end test suite for debug_agent_bridge.py.

Tests all 6 MCP tools plus helper functions. LLM-dependent tests are
gracefully skipped when LM Studio is offline.
"""

import asyncio
import os
import sqlite3
import sys

try:
    import httpx
except ImportError:
    print("httpx required — pip install httpx")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS, FAIL, SKIP = "✅", "❌", "⏭ "
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    results.append((status, name, detail))
    suffix = f"  → {detail}" if detail else ""
    print(f"  {status}  {name}{suffix}")
    return condition


def skip(name: str, reason: str = "") -> None:
    results.append((SKIP, name, reason))
    print(f"  {SKIP}  {name}  (skipped: {reason})")


# Honors M3_DATABASE — run the suite against a scratch DB with:
#   M3_DATABASE=memory/_test.db python bin/test_debug_agent.py
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import resolve_db_path  # noqa: E402

DB_PATH = resolve_db_path(None)
AGENT = "test_debug_agent"


# ── LM Studio probe ──────────────────────────────────────────────────────────
async def probe_lm_studio() -> bool:
    """Returns True if LM Studio is online with at least one model."""
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
        model_ids = [m["id"] for m in resp.json().get("data", [])]
        return len(model_ids) > 0
    except Exception:
        return False


# ── DB helpers ────────────────────────────────────────────────────────────────
_VALID_TABLES = {
    "memory_items", "memory_embeddings", "memory_relationships",
    "chroma_sync_queue", "chroma_mirror", "chroma_mirror_embeddings",
    "sync_conflicts", "sync_state", "activity_logs", "project_decisions",
    "hardware_specs", "system_focus", "synchronized_secrets",
    "session_handoff", "conversation_log",
}

def db_count(table: str, where: str = "", params: tuple = ()) -> int:
    if table not in _VALID_TABLES:
        raise ValueError(f"Invalid table name: {table}")
    conn = sqlite3.connect(DB_PATH)
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def cleanup():
    conn = sqlite3.connect(DB_PATH)
    ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM memory_items WHERE agent_id = ?", (AGENT,)
        ).fetchall()
    ]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM chroma_sync_queue WHERE memory_id IN ({placeholders})", ids)
        conn.execute("DELETE FROM memory_items WHERE agent_id = ?", (AGENT,))
    # Also clean up test decisions
    conn.execute("DELETE FROM project_decisions WHERE project LIKE '%test_debug_agent%'")
    conn.commit()
    conn.close()


# ── Tests ─────────────────────────────────────────────────────────────────────
async def run(lm_online: bool) -> bool:
    from debug_agent_bridge import (
        _check_thermal,
        _get_largest_llm_model,
        _log_to_db,
        _safe_read_file,
        debug_analyze,
        debug_bisect,
        debug_correlate,
        debug_history,
        debug_report,
        debug_trace,
    )

    cleanup()  # fresh slate

    # ── 1: _check_thermal() ──────────────────────────────────────────────────
    print("\n── 1: _check_thermal() ────────────────────────────────────────")
    thermal = _check_thermal()
    check(
        "returns valid thermal state",
        thermal in ("Nominal", "Fair", "Serious", "Critical", "Unknown"),
        thermal,
    )

    # ── 2: _get_largest_llm_model() ──────────────────────────────────────────
    print("\n── 2: _get_largest_llm_model() ────────────────────────────────")
    if lm_online:
        model = await _get_largest_llm_model()
        check("returns non-empty model", bool(model) and not model.startswith("Error:"), model)
        # Verify it's not an embedding model
        is_embed = any(k in model.lower() for k in ("embed", "nomic", "jina", "bge", "e5", "gte", "minilm"))
        check("not an embedding model", not is_embed, model)
    else:
        skip("_get_largest_llm_model", "LM Studio offline")

    # ── 3: _log_to_db("decision", ...) ──────────────────────────────────────
    print("\n── 3: _log_to_db(decision) ────────────────────────────────────")
    _log_to_db("decision", "test_debug_agent", "test decision entry — test rationale")
    count = db_count("project_decisions", "project = ?", ("test_debug_agent",))
    check("decision row exists", count > 0, f"count={count}")

    # ── 4: _safe_read_file() existing file ───────────────────────────────────
    print("\n── 4: _safe_read_file() existing ──────────────────────────────")
    content = _safe_read_file(os.path.join(BASE_DIR, "bin", "debug_agent_bridge.py"))
    check("reads existing file", not content.startswith("Error:"), f"len={len(content)}")
    check("content contains expected text", "FastMCP" in content)

    # ── 5: _safe_read_file() missing file ────────────────────────────────────
    print("\n── 5: _safe_read_file() missing ───────────────────────────────")
    missing = _safe_read_file("/nonexistent/path/file.py")
    check("returns error for missing file", missing.startswith("Error:"), missing[:80])

    # ── 6: debug_report() stores to memory ───────────────────────────────────
    print("\n── 6: debug_report() stores to memory ─────────────────────────")
    report_result = await debug_report(
        issue_id="TEST-001",
        title="Test Debug Report",
        findings="This is a test finding from the debug agent test suite.",
    )
    check("report stored successfully", "Report stored successfully" in report_result, report_result[:80])

    # Verify in DB
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM memory_items WHERE title = 'Test Debug Report' AND agent_id = 'debug_agent'"
    ).fetchall()
    conn.close()
    check("row in memory_items", len(rows) > 0)
    if rows:
        # Get importance — need column index
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT importance, type FROM memory_items WHERE title = 'Test Debug Report'"
        ).fetchone()
        conn.close()
        check("importance is 0.85", abs(row["importance"] - 0.85) < 0.01, str(row["importance"]))
        check("type is document", row["type"] == "document")

    # ── 7: debug_report() rejects empty title ────────────────────────────────
    print("\n── 7: debug_report() rejects empty title ──────────────────────")
    err_result = await debug_report(title="", findings="some findings")
    check("returns error for empty title", "Error:" in err_result, err_result[:80])

    # ── 8: debug_history() with keyword ──────────────────────────────────────
    print("\n── 8: debug_history() with keyword ────────────────────────────")
    history = await debug_history(keyword="Test Debug Report", limit=5)
    check("returns results for known keyword", "Test Debug Report" in history, history[:100])

    # ── 9: debug_history() empty keyword ─────────────────────────────────────
    print("\n── 9: debug_history() empty keyword ───────────────────────────")
    history_empty = await debug_history(keyword="", limit=5)
    # Should return recent entries or "No debug history found"
    check(
        "returns formatted response",
        isinstance(history_empty, str) and len(history_empty) > 0,
        history_empty[:80],
    )

    # ── 10: debug_analyze() with mock error ──────────────────────────────────
    print("\n── 10: debug_analyze() with mock error ────────────────────────")
    if lm_online:
        analysis = await debug_analyze(
            error_message="TypeError: 'NoneType' object is not subscriptable",
            context="Occurs in data processing pipeline when API returns empty response",
        )
        check("returns non-empty analysis", bool(analysis) and len(analysis) > 50, f"len={len(analysis)}")

        # Verify it stored a finding
        count = db_count("memory_items", "agent_id = 'debug_agent' AND title LIKE '%TypeError%'")
        check("stored finding in memory", count > 0, f"count={count}")
    else:
        skip("debug_analyze (LLM)", "LM Studio offline")

    # ── 11: debug_analyze() graceful degradation ─────────────────────────────
    print("\n── 11: debug_analyze() graceful degradation ───────────────────")
    # This tests the memory-only path when LLM would fail
    # We just verify the function doesn't crash with any input
    degraded = await debug_analyze(
        error_message="test error for graceful degradation check",
    )
    check("returns string (graceful)", isinstance(degraded, str) and len(degraded) > 0)

    # ── 12: debug_trace() with real file ─────────────────────────────────────
    print("\n── 12: debug_trace() with real file ───────────────────────────")
    if lm_online:
        trace = await debug_trace(
            file_path=os.path.join(BASE_DIR, "bin", "debug_agent_bridge.py"),
            function_name="_safe_read_file",
        )
        check("returns execution flow", bool(trace) and len(trace) > 50, f"len={len(trace)}")
    else:
        skip("debug_trace (LLM)", "LM Studio offline")

    # ── 13: debug_trace() with missing file ──────────────────────────────────
    print("\n── 13: debug_trace() with missing file ────────────────────────")
    trace_missing = await debug_trace(file_path="/nonexistent/file.py")
    check("returns error for missing file", "Error:" in trace_missing, trace_missing[:80])

    # ── 14: debug_correlate() queries DB ─────────────────────────────────────
    print("\n── 14: debug_correlate() queries DB ───────────────────────────")
    if lm_online:
        corr = await debug_correlate(time_range="24h")
        check("returns correlation data", isinstance(corr, str) and len(corr) > 0, f"len={len(corr)}")
    else:
        # Even without LLM, it should try and return something
        corr = await debug_correlate(time_range="24h")
        check("returns data (partial)", isinstance(corr, str) and len(corr) > 0)

    # ── 15: debug_bisect() stale bisect detection ────────────────────────────
    print("\n── 15: debug_bisect() stale detection ─────────────────────────")
    # Just verify the function handles git validation
    bisect_result = await debug_bisect(
        test_command="echo test",
        good_commit="HEAD~1",
        bad_commit="HEAD",
    )
    # It should complete without crashing (may fail with "Could not identify" which is fine)
    check(
        "bisect completes without crash",
        isinstance(bisect_result, str) and len(bisect_result) > 0,
        bisect_result[:80],
    )

    # ── 16: cleanup ──────────────────────────────────────────────────────────
    print("\n── 16: cleanup ────────────────────────────────────────────────")
    cleanup()
    remaining = db_count("memory_items", "agent_id = ?", (AGENT,))
    test_decisions = db_count("project_decisions", "project LIKE '%test_debug_agent%'")
    check("test memory items cleaned", remaining == 0, f"remaining={remaining}")
    check("test decisions cleaned", test_decisions == 0, f"remaining={test_decisions}")

    return True


# ── Runner ────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("  Debug Agent Bridge — E2E Test Suite")
    print("=" * 60)

    lm_online = await probe_lm_studio()
    print(f"\n  LM Studio: {'✅ online' if lm_online else '⏭  offline (LLM tests will be skipped)'}")

    try:
        await run(lm_online)
    except Exception as exc:
        print(f"\n  ❌ FATAL: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    skipped = sum(1 for s, _, _ in results if s == SKIP)
    total = len(results)
    print(f"  {passed}/{total} passed | {failed} failed | {skipped} skipped")

    if failed:
        print("\n  Failed tests:")
        for s, name, detail in results:
            if s == FAIL:
                print(f"    ❌ {name}: {detail}")

    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
