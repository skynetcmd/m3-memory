#!/usr/bin/env python3
"""m3_enrich_batch_parallel — launch N pipelined batch workers against
disjoint shards of a conv-list, staggered so each worker's first-slice
submit is offset by --start-offset-s seconds from the previous worker's
first-slice submit.

Use this when one m3_enrich_batch.py worker can't keep Anthropic's batch
tier saturated (typical: at slice_size=500 with 5-21 min batch wallclocks,
a single worker spends most of its time waiting on Anthropic). Multiple
workers running in parallel against disjoint conv-list shards keep more
batches in flight on the provider side, with the only local contention
being SQLite's WAL writer lock (which serializes claims and ingests but
runs at ~ms scale, not minutes).

The --start-offset-s flag controls the gap between the START of one
worker (i.e. process spawn time) and the FIRST SUBMIT of the next
worker — accounting for enumeration time. With the patched fast
enumeration (~20s for 19K-key bucket), 120s gives each worker ~100s
of headroom to finish enumeration + first submit before the next one
starts hitting the bench DB.

Usage:
    python bin/m3_enrich_batch_parallel.py \\
        --workers 3 \\
        --start-offset-s 120 \\
        --profile enrich_google_gemini \\
        --core --core-db memory/your-corpus.db \\
        --source-variant your-variant \\
        --target-variant your-target \\
        --source-conv-list .scratch/full-list.txt \\
        --slice-size 500

The conv-list is sharded round-robin across workers. Each worker logs to
logs/<base>_worker<N>_<ts>.log. PIDs are reported up front for kill-by-PID.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _shard_conv_list(in_path: Path, n_shards: int, out_dir: Path,
                     base_name: str) -> list[Path]:
    """Round-robin split a conv-list into N shard files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with in_path.open(encoding="utf-8") as f:
        keys = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    shards: list[list[str]] = [[] for _ in range(n_shards)]
    for i, k in enumerate(keys):
        shards[i % n_shards].append(k)
    paths: list[Path] = []
    for i, shard in enumerate(shards):
        p = out_dir / f"{base_name}_shard{i+1}of{n_shards}.txt"
        p.write_text("\n".join(shard) + "\n", encoding="utf-8")
        paths.append(p)
    return paths


def _build_cmd(args, shard_path: Path, log_path: Path) -> list[str]:
    """Build a m3_enrich_batch.py invocation for one worker."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "bin" / "m3_enrich_batch.py"),
        "--profile", args.profile,
    ]
    if args.profile_path:
        cmd += ["--profile-path", args.profile_path]
    if args.core:
        cmd += ["--core"]
    if args.core_db:
        cmd += ["--core-db", args.core_db]
    if args.source_variant:
        cmd += ["--source-variant", args.source_variant]
    if args.target_variant:
        cmd += ["--target-variant", args.target_variant]
    cmd += [
        "--source-conv-list", str(shard_path),
        "--slice-size", str(args.slice_size),
        "--poll-interval-s", str(args.poll_interval_s),
        "--max-wait-s", str(args.max_wait_s),
    ]
    if args.budget_usd is not None:
        cmd += ["--budget-usd", str(args.budget_usd)]
    if args.embed_url:
        cmd += ["--embed-url", args.embed_url]
    if args.embed_model:
        cmd += ["--embed-model", args.embed_model]
    return cmd


async def _launch_worker_with_offset(
    worker_idx: int, n_workers: int,
    args, shard_path: Path, log_path: Path,
    start_at: float,
) -> subprocess.Popen:
    """Wait until start_at (monotonic seconds), then launch the worker.

    Returns the Popen handle so the caller can track PIDs and reap.
    """
    now = asyncio.get_event_loop().time()
    delay = max(0.0, start_at - now)
    if delay > 0:
        print(f"[parallel] worker {worker_idx+1}/{n_workers}: waiting {delay:.1f}s "
              f"before launch", flush=True)
        await asyncio.sleep(delay)
    cmd = _build_cmd(args, shard_path, log_path)
    log_f = log_path.open("w", encoding="utf-8")
    print(f"[parallel] worker {worker_idx+1}/{n_workers}: launching", flush=True)
    print(f"[parallel]   cmd: {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
    print(f"[parallel]   log: {log_path}", flush=True)
    p = subprocess.Popen(
        cmd, stdout=log_f, stderr=subprocess.STDOUT,
        cwd=str(REPO_ROOT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    print(f"[parallel] worker {worker_idx+1}/{n_workers}: pid={p.pid}", flush=True)
    return p


async def _run_async(args) -> int:
    in_path = Path(args.source_conv_list).resolve()
    if not in_path.exists():
        sys.exit(f"ERROR: --source-conv-list not found: {in_path}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = args.log_base or f"gemini_parallel_{ts}"
    shard_dir = Path(args.shard_dir or ".scratch").resolve()
    log_dir = Path("logs").resolve()
    log_dir.mkdir(exist_ok=True)

    print(f"[parallel] sharding conv-list into {args.workers} shards...", flush=True)
    shards = _shard_conv_list(in_path, args.workers, shard_dir, base)
    for i, shard_path in enumerate(shards):
        n_lines = sum(1 for _ in shard_path.open(encoding="utf-8"))
        print(f"[parallel]   shard {i+1}/{args.workers}: {shard_path} ({n_lines} convs)",
              flush=True)

    print(f"[parallel] launching {args.workers} worker(s) with "
          f"{args.start_offset_s}s offset between starts", flush=True)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    schedule_tasks = []
    for i in range(args.workers):
        log_path = log_dir / f"{base}_worker{i+1}.log"
        start_at = t0 + i * args.start_offset_s
        schedule_tasks.append(asyncio.create_task(
            _launch_worker_with_offset(i, args.workers, args, shards[i], log_path, start_at)
        ))
    procs = await asyncio.gather(*schedule_tasks)
    print()
    print("=" * 62)
    print("  m3-enrich-batch-parallel WORKERS LAUNCHED")
    print("=" * 62)
    for i, p in enumerate(procs):
        print(f"  worker {i+1}: pid={p.pid}  shard={shards[i].name}")
    print()
    print(f"  Log dir: {log_dir}")
    print("  To monitor:")
    for i in range(args.workers):
        print(f"    tail -F {log_dir / (base + f'_worker{i+1}.log')}")
    print()
    print("  To kill all workers:")
    if os.name == "nt":
        pids = ",".join(str(p.pid) for p in procs)
        print(f"    powershell -Command \"Stop-Process -Id {pids} -Force\"")
    else:
        pids = " ".join(str(p.pid) for p in procs)
        print(f"    kill {pids}")
    print()
    print("  Workers run detached; this orchestrator exits now.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=3,
                    help="Number of parallel workers. Default 3.")
    ap.add_argument("--start-offset-s", type=int, default=120,
                    help="Seconds between START of worker N and START of "
                         "worker N+1. Default 120.")
    # Pass-through to m3_enrich_batch.py
    ap.add_argument("--profile", required=True)
    ap.add_argument("--profile-path", default=None)
    ap.add_argument("--core", action="store_true")
    ap.add_argument("--core-db", required=True)
    ap.add_argument("--source-variant", default=None)
    ap.add_argument("--target-variant", default=None)
    ap.add_argument("--source-conv-list", required=True,
                    help="Will be sharded round-robin across workers.")
    ap.add_argument("--slice-size", type=int, default=500)
    ap.add_argument("--poll-interval-s", type=float, default=60.0)
    ap.add_argument("--max-wait-s", type=float, default=24*3600)
    ap.add_argument("--budget-usd", type=float, default=None,
                    help="Per-worker budget cap. Total spend can be up to "
                         "workers × budget_usd.")
    ap.add_argument("--embed-url",
                    default=os.environ.get("M3_EMBED_URL"),
                    help="Pin observation embeds to this URL on every "
                         "worker (passed through as --embed-url to each). "
                         "Default discovery prefers LMS :1234 (1-slot); "
                         "set this to the multi-slot llama.cpp endpoint "
                         "(e.g. http://127.0.0.1:8081/v1) to avoid "
                         "throttling 3-worker ingest through a single "
                         "slot. Env: M3_EMBED_URL.")
    ap.add_argument("--embed-model",
                    default=os.environ.get("M3_EMBED_MODEL"),
                    help="Model id for the override endpoint. See "
                         "m3_enrich_batch.py --embed-model. Env: M3_EMBED_MODEL.")
    ap.add_argument("--shard-dir", default=None,
                    help="Directory for shard files. Default: .scratch/")
    ap.add_argument("--log-base", default=None,
                    help="Log basename. Default: gemini_parallel_<ts>")
    args = ap.parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
