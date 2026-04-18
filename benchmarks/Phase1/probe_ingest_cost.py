"""Measure ingestion cost per variant for 1 and 10 LOCOMO turns.

Measures:
  - wall-clock seconds
  - Python-side CPU (user+sys), resource.getrusage
  - LM Studio-side CPU (Δuser+Δsys), psutil against LM Studio PID(s)
  - #LLM calls, prompt+completion tokens (from response.usage)
  - #embed calls, total chars embedded
  - rows written

Four variants:
  baseline         — no heuristic, no LLM enrichment
  heuristic_c1c4   — heuristic title/entities, no LLM
  llm_v1           — heuristic + force LLM title+entities
  llm_only         — LLM title+entities, no heuristic

Scratch DB via M3_DB_PATH; throwaway variant tags probe_<v>_n{N}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "bin"))
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:1234/v1")

SCRATCH_DB = Path(tempfile.gettempdir()) / f"m3_probe_{uuid.uuid4().hex[:8]}.db"
os.environ["M3_DB_PATH"] = str(SCRATCH_DB)

import psutil
import httpx

import memory_core
import temporal_utils
from memory_core import memory_write_impl, _db, _content_hash


VARIANTS = ["baseline", "heuristic_c1c4", "llm_v1", "llm_only"]


def heuristic_title(content: str, role: str) -> str:
    """Minimal heuristic title: role + first 5 content words."""
    first = " ".join((content or "").split()[:5])
    return f"{role}: {first}".strip(": ") or role


def heuristic_entities(content: str) -> list[str]:
    """Minimal heuristic entities: capitalized tokens of length >= 3."""
    out: list[str] = []
    for tok in (content or "").replace(",", " ").replace(".", " ").split():
        if len(tok) >= 3 and tok[0].isupper() and tok.isalpha():
            if tok not in out:
                out.append(tok)
        if len(out) >= 8:
            break
    return out


class CostCounter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.llm_calls = 0
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        self.embed_calls = 0
        self.embed_chars = 0


COUNTER = CostCounter()


def install_httpx_patch():
    """Patch httpx.AsyncClient.post to count LLM + embed calls and tokens."""
    orig_post = httpx.AsyncClient.post

    async def counting_post(self, url, *args, **kwargs):
        resp = await orig_post(self, url, *args, **kwargs)
        try:
            url_str = str(url)
            if url_str.endswith("/chat/completions"):
                COUNTER.llm_calls += 1
                try:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    COUNTER.llm_prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    COUNTER.llm_completion_tokens += int(usage.get("completion_tokens") or 0)
                except Exception:
                    pass
            elif url_str.endswith("/embeddings"):
                COUNTER.embed_calls += 1
                body = kwargs.get("json") or {}
                inp = body.get("input")
                if isinstance(inp, str):
                    COUNTER.embed_chars += len(inp)
                elif isinstance(inp, list):
                    COUNTER.embed_chars += sum(len(s) for s in inp if isinstance(s, str))
        except Exception:
            pass
        return resp

    httpx.AsyncClient.post = counting_post


def find_lm_studio_pids() -> list[int]:
    """Best-effort PID lookup for LM Studio server processes."""
    pids: list[int] = []
    for p in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            if "lm-studio" in name or "lmstudio" in name or "lm studio" in cmd \
               or "llama" in name or "llama-server" in cmd:
                pids.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def snapshot_lm_cpu(pids: list[int]) -> tuple[float, float]:
    """Return (user, sys) CPU time summed across LM Studio PIDs."""
    u = s = 0.0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            t = p.cpu_times()
            u += t.user
            s += t.system
            try:
                for ch in p.children(recursive=True):
                    try:
                        ct = ch.cpu_times()
                        u += ct.user
                        s += ct.system
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return u, s


def build_items_from_turns(sample: dict, n_turns: int, variant: str) -> list[dict]:
    """Build ingest items from the first n_turns of session_1 of sample.

    Variant controls which enrichment pipeline runs inside memory_write_bulk_impl.
    We apply the heuristic title/entities up-front for heuristic_c1c4 and llm_v1;
    baseline gets bare titles; llm_only gets bare titles + force_llm flag.
    """
    conv = sample["conversation"]
    sid = sample["sample_id"]
    speaker_a = conv.get("speaker_a", "Speaker A")
    speaker_b = conv.get("speaker_b", "Speaker B")

    sess_date_str = conv.get("session_1_date_time", "Unknown")
    anchor_dt = temporal_utils.parse_locomo_date(sess_date_str)
    turns = conv.get("session_1", [])[:n_turns]

    items: list[dict] = []
    for t_idx, turn in enumerate(turns):
        role = speaker_a if turn.get("speaker") == "speaker_a" else speaker_b
        content = turn.get("text", "") or ""
        anchors = temporal_utils.resolve_temporal_expressions(content, anchor_dt)

        if variant == "baseline":
            title = f"{role}:{sid}:S1:T{t_idx}"
            entities: list[str] = []
            embed_text = content
        elif variant == "heuristic_c1c4":
            title = heuristic_title(content, role)
            entities = heuristic_entities(content)
            embed_text = f"{title} [{', '.join(entities)}] {content}" if entities else f"{title} {content}"
        elif variant == "llm_v1":
            title = heuristic_title(content, role)
            entities = heuristic_entities(content)
            embed_text = f"{title} [{', '.join(entities)}] {content}" if entities else f"{title} {content}"
        elif variant == "llm_only":
            title = f"{role}:{sid}:S1:T{t_idx}"
            entities = []
            embed_text = content
        else:
            raise ValueError(f"unknown variant {variant}")

        items.append({
            "id": str(uuid.uuid4()),
            "type": "message",
            "title": title,
            "content": content,
            "embed_text": embed_text,
            "user_id": sid,
            "conversation_id": f"{sid}::S1",
            "variant": f"probe_{variant}_n{n_turns}",
            "source": "locomo_probe",
            "embed": True,
            "metadata": {
                "role": role,
                "session_id": "S1",
                "session_date": sess_date_str,
                "session_index": 1,
                "turn_index": t_idx,
                "entities": entities,
                "temporal_anchors": anchors,
                "variant_probe": variant,
            },
        })
    return items


async def run_one(variant: str, n_turns: int, sample: dict, lm_pids: list[int]) -> dict[str, Any]:
    # Clear LLM in-process caches so each variant pays its own cold cost
    memory_core._AUTO_TITLE_CACHE.clear()
    memory_core._AUTO_ENTITIES_CACHE.clear()

    # Set env gates based on variant
    if variant in ("llm_v1", "llm_only"):
        os.environ["M3_INGEST_AUTO_TITLE"] = "1"
        os.environ["M3_INGEST_AUTO_ENTITIES"] = "1"
    else:
        os.environ["M3_INGEST_AUTO_TITLE"] = "0"
        os.environ["M3_INGEST_AUTO_ENTITIES"] = "0"

    items = build_items_from_turns(sample, n_turns, variant)

    # For llm_only: strip the bare title so the "trivial title" heuristic fires
    # and _maybe_auto_title actually runs (gate is on, but it also requires a
    # trivial title unless force=True — which the current ingest path does not pass).
    if variant == "llm_only":
        for it in items:
            it["title"] = ""

    COUNTER.reset()

    py_proc = psutil.Process(os.getpid())

    # Snapshots — before
    t_wall0 = time.perf_counter()
    lm_u0, lm_s0 = snapshot_lm_cpu(lm_pids)
    py_t0 = py_proc.cpu_times()
    py_u0, py_s0 = py_t0.user, py_t0.system

    # Use per-item memory_write_impl so the _maybe_auto_title / _maybe_auto_entities
    # hooks actually fire for llm_v1 / llm_only (memory_write_bulk_impl skips them).
    ids: list[str] = []
    for it in items:
        rid = await memory_write_impl(
            type=it["type"],
            content=it["content"],
            title=it["title"],
            metadata=it["metadata"],
            user_id=it["user_id"],
            source=it["source"],
            embed=it["embed"],
            conversation_id=it["conversation_id"],
            variant=it["variant"],
            embed_text=it["embed_text"],
        )
        ids.append(rid)

    # Snapshots — after
    t_wall1 = time.perf_counter()
    lm_u1, lm_s1 = snapshot_lm_cpu(lm_pids)
    py_t1 = py_proc.cpu_times()
    py_u1, py_s1 = py_t1.user, py_t1.system

    return {
        "variant": variant,
        "n_turns": n_turns,
        "rows_written": len(ids),
        "wall_seconds": round(t_wall1 - t_wall0, 3),
        "py_cpu_user_s": round(py_u1 - py_u0, 3),
        "py_cpu_sys_s": round(py_s1 - py_s0, 3),
        "lm_cpu_user_s": round(lm_u1 - lm_u0, 3),
        "lm_cpu_sys_s": round(lm_s1 - lm_s0, 3),
        "llm_calls": COUNTER.llm_calls,
        "llm_prompt_tokens": COUNTER.llm_prompt_tokens,
        "llm_completion_tokens": COUNTER.llm_completion_tokens,
        "llm_total_tokens": COUNTER.llm_prompt_tokens + COUNTER.llm_completion_tokens,
        "embed_calls": COUNTER.embed_calls,
        "embed_chars": COUNTER.embed_chars,
    }


async def main(args):
    install_httpx_patch()

    with open(BASE / "data" / "locomo" / "locomo10.json", "r", encoding="utf-8") as f:
        dataset = json.load(f)
    sample = next(s for s in dataset if s["sample_id"] == args.sample)

    lm_pids = find_lm_studio_pids()
    print(f"LM Studio PIDs: {lm_pids or '(none found — LM CPU will be 0)'}")
    print(f"Scratch DB: {SCRATCH_DB}")
    print()

    results = []
    for n in (1, 10):
        for v in VARIANTS:
            print(f"  {v:20s} n={n:2d} ...", end=" ", flush=True)
            r = await run_one(v, n, sample, lm_pids)
            results.append(r)
            print(f"wall={r['wall_seconds']}s  llm={r['llm_calls']}x/{r['llm_total_tokens']}tok  embed={r['embed_calls']}x/{r['embed_chars']}ch")

    out = Path(__file__).parent / "probe_ingest_cost_results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    # Markdown table
    md = ["# Ingest cost probe — 4 variants", ""]
    md.append("| variant | N | rows | wall_s | py_user | py_sys | lm_user | lm_sys | llm_calls | llm_tokens | embed_calls | embed_chars |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        md.append(
            f"| {r['variant']} | {r['n_turns']} | {r['rows_written']} | "
            f"{r['wall_seconds']} | {r['py_cpu_user_s']} | {r['py_cpu_sys_s']} | "
            f"{r['lm_cpu_user_s']} | {r['lm_cpu_sys_s']} | "
            f"{r['llm_calls']} | {r['llm_total_tokens']} | "
            f"{r['embed_calls']} | {r['embed_chars']} |"
        )
    md_path = Path(__file__).parent / "INGEST_COST_PROBE.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {md_path}")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", default="conv-26")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
