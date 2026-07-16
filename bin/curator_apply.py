"""Deterministic apply of a curator plan — one entry point, no LLM in the loop.

The curate-{memory,chatlog} subagents emit a structured plan in their PLAN
spawn. Historically the APPLY spawn was another LLM agent that interpreted
that plan and called MCP tools one operation at a time. That path has
failed twice (2026-05-17) with two distinct failure modes:

  1. Agent looped single-id `memory_delete` for ~486 IDs (~16 min budget).
  2. After the prompt was rewritten to mandate `memory_delete_bulk`, the
     replacement agent instead invented a Bash-file-writes-the-ids strategy
     and ran past its budget reasoning about Windows path mapping.

This module is the structural fix per memory `4090f663` (the diagnose-the-
tool-shape rule, generalized): make the wrong path *impossible* by replacing
the agent-driven apply procedure with one deterministic function. The
agent's job becomes "emit a plan, call apply, read the report" — one MCP
round-trip instead of N.

Plan schema (both stores use the same shape; sections without a key are
treated as a no-op):

    {
        # memory.db plan
        "delete":   ["<uuid>", ...]                          # soft delete
        "delete_hard": ["<uuid>", ...]                       # cascade delete
        "link":     [{"from_id": ..., "to_id": ...,
                      "relationship_type": "related"}, ...]
        "update":   [{"id": ..., "importance": 0.9, ...}, ...]

        # chatlog.db plan
        "decay":    True | {"batch_size": 1000} | False      # run chatlog_decay
        "dedup":    [{"keep_id": ..., "drop_ids": [...]}, ...]
        "promote":  [{"ids": [...], "target_type": "conversation"}, ...]
        "prune":    [{"conversation_id": ..., "reason": "..."}, ...]
    }

Returns a structured dict per section + a summary. No exceptions cross
the boundary; per-section errors surface in the result.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── memory.db plan apply ─────────────────────────────────────────────────────

def apply_memory_plan(plan: dict) -> dict:
    """Deterministically apply a memory.db curation plan.

    Sections honored: delete (soft), delete_hard (cascade), link, update.
    Any section may be omitted or empty; result reports per-section counts.
    """
    # Backend-agnostic: this delegates entirely to memory_core's bulk impls
    # (memory_delete_bulk_impl / memory_link_bulk_impl / memory_update_bulk_impl),
    # which route through the backend-aware _db() and dialect their SQL — so it
    # works on both SQLite and PostgreSQL. (Previously gated SQLite-only; the bulk
    # impls were dialected and verified on PG, so the gate was removed.)
    import memory_core

    out: dict[str, Any] = {
        "store": "memory",
        "started_at": time.time(),
        "delete": None,
        "delete_hard": None,
        "link": None,
        "update": None,
        "errors": [],
    }

    # Soft-delete
    soft_ids = _coerce_id_list(plan.get("delete"))
    if soft_ids:
        try:
            out["delete"] = memory_core.memory_delete_bulk_impl(soft_ids, hard=False)
        except Exception as e:  # noqa: BLE001 — surface, don't propagate
            out["errors"].append({"section": "delete", "error": f"{type(e).__name__}: {e}"})

    # Hard-delete
    hard_ids = _coerce_id_list(plan.get("delete_hard"))
    if hard_ids:
        try:
            out["delete_hard"] = memory_core.memory_delete_bulk_impl(hard_ids, hard=True)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "delete_hard", "error": f"{type(e).__name__}: {e}"})

    # Link
    link_specs = plan.get("link")
    if link_specs:
        try:
            out["link"] = memory_core.memory_link_bulk_impl(link_specs)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "link", "error": f"{type(e).__name__}: {e}"})

    # Update
    update_specs = plan.get("update")
    if update_specs:
        try:
            out["update"] = memory_core.memory_update_bulk_impl(update_specs)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "update", "error": f"{type(e).__name__}: {e}"})

    out["completed_at"] = time.time()
    out["wall_seconds"] = round(out["completed_at"] - out["started_at"], 3)
    out["summary"] = _summarize_memory(out)
    return out


def _summarize_memory(out: dict) -> dict:
    s = {"deleted_soft": 0, "deleted_hard": 0, "linked": 0, "updated": 0}
    if out["delete"]:
        s["deleted_soft"] = len(out["delete"].get("succeeded", []))
    if out["delete_hard"]:
        s["deleted_hard"] = len(out["delete_hard"].get("succeeded", []))
    if out["link"]:
        s["linked"] = len(out["link"].get("created", []))
    if out["update"]:
        s["updated"] = len(out["update"].get("succeeded", []))
    return s


# ── chatlog.db plan apply ────────────────────────────────────────────────────

def apply_chatlog_plan(plan: dict, db_path: Optional[str] = None) -> dict:
    """Deterministically apply a chatlog.db curation plan.

    Sections honored: decay, dedup, promote, prune.
    `db_path` defaults to chatlog_config.chatlog_db_path() (the same resolver
    the chatlog subsystem uses everywhere).
    """
    import chatlog_config
    import chatlog_decay
    import memory_core

    resolved_db = db_path or chatlog_config.chatlog_db_path()

    out: dict[str, Any] = {
        "store": "chatlog",
        "db_path": resolved_db,
        "started_at": time.time(),
        "decay": None,
        "dedup": None,
        "promote": None,
        "prune": None,
        "errors": [],
    }

    # DECAY — delegate to chatlog_decay.run_sweep
    decay_spec = plan.get("decay")
    if decay_spec:
        batch_size = 1000
        if isinstance(decay_spec, dict):
            batch_size = int(decay_spec.get("batch_size", 1000))
        try:
            out["decay"] = chatlog_decay.run_sweep(resolved_db, apply=True, batch_size=batch_size)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "decay", "error": f"{type(e).__name__}: {e}"})

    # DEDUP — for each group, drop_ids → memory_delete_bulk routed to the
    # chatlog DB (not the main memory.db, which is what `memory_core._db()`
    # resolves to by default). `active_database()` is a context manager from
    # m3_sdk that overrides the resolver for the scope of the `with`.
    dedup_groups = plan.get("dedup") or []
    if dedup_groups:
        from m3_sdk import active_database
        dedup_results = []
        for group in dedup_groups:
            keep = group.get("keep_id")
            drops = _coerce_id_list(group.get("drop_ids"))
            if not drops:
                dedup_results.append({"keep_id": keep, "succeeded": [], "not_found": [], "skipped": "no_drops"})
                continue
            try:
                with active_database(resolved_db):
                    r = memory_core.memory_delete_bulk_impl(drops, hard=False)
                dedup_results.append({"keep_id": keep, **r})
            except Exception as e:  # noqa: BLE001
                dedup_results.append({"keep_id": keep, "error": f"{type(e).__name__}: {e}"})
        out["dedup"] = {
            "groups": dedup_results,
            "total_succeeded": sum(len(g.get("succeeded", [])) for g in dedup_results),
            "total_not_found": sum(len(g.get("not_found", [])) for g in dedup_results),
        }

    # PROMOTE — chatlog_promote_impl per spec (it's async; run one event loop)
    promote_specs = plan.get("promote") or []
    if promote_specs:
        try:
            out["promote"] = _run_promotes(promote_specs)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "promote", "error": f"{type(e).__name__}: {e}"})

    # PRUNE — abandoned conversations. Sets is_deleted=1 on every row in each
    # conversation_id, scoped to type='chat_log'. Direct SQL — no per-row LLM.
    prune_specs = plan.get("prune") or []
    if prune_specs:
        try:
            out["prune"] = _apply_prunes(resolved_db, prune_specs)
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"section": "prune", "error": f"{type(e).__name__}: {e}"})

    out["completed_at"] = time.time()
    out["wall_seconds"] = round(out["completed_at"] - out["started_at"], 3)
    out["summary"] = _summarize_chatlog(out)
    return out


def _run_promotes(promote_specs: list) -> list:
    """Run chatlog_promote_impl (async) for each spec, sharing one loop."""
    import chatlog_core

    async def _do_all():
        results = []
        for spec in promote_specs:
            ids = _coerce_id_list(spec.get("ids"))
            target_type = spec.get("target_type", "conversation")
            try:
                raw = await chatlog_core.chatlog_promote_impl(
                    ids=ids, target_type=target_type, copy=True
                )
                results.append({"spec": spec, "result": _maybe_parse_json(raw)})
            except Exception as e:  # noqa: BLE001
                results.append({"spec": spec, "error": f"{type(e).__name__}: {e}"})
        return results

    return asyncio.run(_do_all())


def _apply_prunes(db_path: str, prune_specs: list) -> dict:
    """For each prune spec, soft-delete every chat_log row in that conversation.
    Direct SQL inside one connection. Always filters type='chat_log'."""
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        for spec in prune_specs:
            conv_id = spec.get("conversation_id")
            if not conv_id:
                results.append({"spec": spec, "skipped": "no_conversation_id"})
                continue
            cur = conn.execute(
                "UPDATE memory_items SET is_deleted=1, updated_at=? "
                "WHERE type='chat_log' AND conversation_id=? AND is_deleted=0",
                (now_iso, conv_id),
            )
            results.append({
                "conversation_id": conv_id,
                "reason": spec.get("reason", ""),
                "rows_pruned": cur.rowcount,
            })
        conn.commit()
    finally:
        conn.close()
    return {
        "conversations": results,
        "total_rows_pruned": sum(r.get("rows_pruned", 0) for r in results),
    }


def _summarize_chatlog(out: dict) -> dict:
    s = {
        "decay_applied_writes": 0,
        "dedup_deleted": 0,
        "promoted": 0,
        "pruned": 0,
    }
    if out["decay"] and isinstance(out["decay"], dict):
        s["decay_applied_writes"] = out["decay"].get("applied_writes", 0)
    if out["dedup"]:
        s["dedup_deleted"] = out["dedup"].get("total_succeeded", 0)
    if out["promote"]:
        # promote results are per-spec; tally how many ids got promoted
        for entry in out["promote"]:
            if "result" in entry and isinstance(entry["result"], dict):
                s["promoted"] += entry["result"].get("promoted", 0)
    if out["prune"]:
        s["pruned"] = out["prune"].get("total_rows_pruned", 0)
    return s


# ── shared helpers ───────────────────────────────────────────────────────────

def _coerce_id_list(val: Any) -> list[str]:
    """Accept None, list, or comma-separated string. Return a deduped list."""
    if not val:
        return []
    if isinstance(val, str):
        items = [v.strip() for v in val.split(",") if v.strip()]
    elif isinstance(val, (list, tuple)):
        items = [str(v).strip() for v in val if v]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _maybe_parse_json(raw: Any) -> Any:
    """chatlog_promote_impl returns a JSON string; parse if so, else passthrough."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return {"raw": raw}
    return raw


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="curator_apply",
        description=(
            "Deterministically apply a curator plan (memory or chatlog) "
            "from a JSON file. No LLM in the loop."
        ),
    )
    parser.add_argument(
        "store",
        choices=("memory", "chatlog"),
        help="Which store the plan targets.",
    )
    parser.add_argument(
        "--plan", required=True,
        help="Path to a JSON file containing the plan, or '-' to read stdin.",
    )
    parser.add_argument(
        "--db", default=None,
        help="Override DB path (chatlog only; memory uses M3_DATABASE).",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print the result JSON (default: compact one-line).",
    )
    args = parser.parse_args()

    if args.plan == "-":
        plan = json.load(sys.stdin)
    else:
        with open(args.plan) as f:
            plan = json.load(f)

    if args.store == "memory":
        result = apply_memory_plan(plan)
    else:
        result = apply_chatlog_plan(plan, db_path=args.db)

    indent = 2 if args.pretty else None
    print(json.dumps(result, indent=indent, default=str))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
