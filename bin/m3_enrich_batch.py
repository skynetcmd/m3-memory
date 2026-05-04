#!/usr/bin/env python3
"""m3-enrich-batch — async/batch variant of bin/m3_enrich.py.

Submits all eligible conversations as ONE batch via the provider's batch
API (currently Anthropic /v1/messages/batches), waits for completion,
then ingests results into memory_items + enrichment_groups using the
same state-machine discipline as the live worker.

Why: ~50% off list pricing in exchange for async wallclock (typically
5-60 minutes for the batch to complete on Anthropic).

Limitations vs the live m3_enrich.py:
  - Async: each slice submits, polls, ingests, then the next slice
    submits. Auto-splits via runner.max_batch_size when the request
    list exceeds the provider's per-batch ceiling.
  - Backends supported: anthropic (native /v1/messages/batches),
    openai-shim Gemini Developer API (/v1beta/models/<m>:batchGenerateContent).
    Other openai-shim providers (real OpenAI, xAI) raise
    NotImplementedError until their batch runner is added.
  - Crash recovery: batch_ids are persisted to enrichment_runs.notes
    under a structured "batches" array. A re-launch with
    --resume-run <enrichment_runs.id> picks up any batches that haven't
    been ingested yet, polls them, and ingests.

Usage:
  python bin/m3_enrich_batch.py \\
      --profile enrich_anthropic_haiku \\
      --core --core-db memory/your-corpus.db \\
      --source-variant your-source-variant \\
      --target-variant your-target-variant \\
      --source-conv-list .scratch/some_convolist.txt \\
      --track-state --resume \\
      --skip-preflight --yes

Or to resume polling/ingesting a previously-submitted run:
  python bin/m3_enrich_batch.py \\
      --profile enrich_anthropic_haiku \\
      --core-db memory/your-corpus.db \\
      --resume-run <enrichment_runs.id>

Status:  Phase E worker. Pairs with batch_runner.py (provider abstraction).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
_BIN = REPO_ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import enrichment_state as estate  # noqa: E402
import httpx  # noqa: E402
import run_observer as observer  # noqa: E402
from auth_utils import get_api_key  # noqa: E402
from batch_runner import BatchRequest, make_runner  # noqa: E402
from m3_enrich import _load_conv_list, _query_eligible_groups  # noqa: E402
from slm_intent import _parse_profile  # noqa: E402


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


# ── Default core type allowlist (same as m3_enrich.py default) ─────────────
DEFAULT_CORE_ALLOWLIST = ("decision", "plan", "knowledge", "fact", "preference",
                         "message", "conversation")


def _build_chunks_for_group(
    turns: list[tuple], profile, group_id: int,
) -> list[tuple[int, str]]:
    """Mirror run_observer.process_conversation's chunking. Returns a list
    of (chunk_idx, user_text_json) for each chunk. The same session_block
    JSON the live observer would send.
    """
    if not turns:
        return []
    # session_date resolution mirrors process_conversation
    session_date = "unknown"
    for t in turns:
        meta = t[5] if len(t) > 5 else None
        if meta:
            try:
                m = json.loads(meta)
                if m.get("session_date"):
                    session_date = str(m["session_date"]).split(" ")[0].replace("/", "-")
                    break
            except Exception:
                pass
    if session_date == "unknown" and turns:
        ca = turns[0][4] if len(turns[0]) > 4 and turns[0][4] else None
        if ca and len(str(ca)) >= 10:
            session_date = str(ca)[:10]
    input_max = getattr(profile, "input_max_chars", 20000) or 20000
    chunks = observer._chunk_turns(turns, input_max)
    out: list[tuple[int, str]] = []
    for ci, chunk in enumerate(chunks):
        block = observer._build_session_block(chunk, session_date)
        user_text = json.dumps(block, ensure_ascii=False)
        if input_max and len(user_text) > input_max:
            user_text = user_text[:input_max]
        out.append((ci, user_text))
    return out


# ── DB helpers ────────────────────────────────────────────────────────────

def _open_state_conn(db_path: Path):
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _reap_stale_runs(state_conn, *, stale_after_minutes: int = 360) -> int:
    """Cosmetic cleanup: mark `enrichment_runs.status='running'` rows as
    'aborted' if they've been "running" for more than stale_after_minutes
    without their finished_at being set. These are crashed-worker leftovers
    — they don't affect correctness (claims are tracked separately) but
    pollute audit queries. Default 6h is well above the longest legitimate
    run (Anthropic batch worst-case 24h, but those rare runs would update
    finished_at on success).

    Returns: count of rows reaped.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    cur = state_conn.execute(
        """UPDATE enrichment_runs
           SET finished_at = ?,
               status = 'aborted',
               abort_reason = COALESCE(abort_reason, 'stale_run_reaped')
           WHERE finished_at IS NULL
             AND status = 'running'
             AND started_at < ?""",
        (_utcnow_iso(), cutoff),
    )
    state_conn.commit()
    return cur.rowcount


def _record_run_started(state_conn, *, profile, db_path: Path, source_variant: str,
                        target_variant: str, batch_id: str, n_groups: int,
                        slice_size: int, argv: list[str]) -> str:
    """Insert a new enrichment_runs row with structured notes for resume.

    notes schema (load-bearing — see _resume_batch_run):
      {
        "n_groups_submitted": int,
        "slice_size": int,
        "source_conv_list": str (path),  # set by caller via _update_run_notes
        "batches": [
            {"slice_idx": int, "batch_id": str, "ingested": bool}, ...
        ]
      }
    """
    run_id = str(uuid.uuid4())
    notes = json.dumps({
        "n_groups_submitted": n_groups,
        "slice_size": slice_size,
        "batches": [{"slice_idx": 0, "batch_id": batch_id, "ingested": False}],
    })
    state_conn.execute(
        """
        INSERT INTO enrichment_runs(id, started_at, profile, model, source_variant, target_variant,
            db_path, concurrency, launch_argv, host, git_sha, status, n_pending, n_success, n_failed,
            n_empty, n_dead_letter, total_cost_usd, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?)
        """,
        (run_id, _utcnow_iso(), getattr(profile, "name", "?"), profile.model,
         source_variant, target_variant, str(db_path), 0,
         json.dumps(argv), os.environ.get("COMPUTERNAME", "?"), "",
         "running", notes),
    )
    state_conn.commit()
    return run_id


def _read_run_notes(state_conn, run_id: str) -> dict:
    """Load notes JSON from enrichment_runs row. Returns {} if missing/malformed."""
    row = state_conn.execute(
        "SELECT notes FROM enrichment_runs WHERE id=?", (run_id,),
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return {}


def _write_run_notes(state_conn, run_id: str, notes: dict) -> None:
    """Persist notes back to enrichment_runs."""
    state_conn.execute(
        "UPDATE enrichment_runs SET notes=? WHERE id=?",
        (json.dumps(notes), run_id),
    )
    state_conn.commit()


def _record_batch_submitted(state_conn, run_id: str, *, slice_idx: int,
                             batch_id: str) -> None:
    """Append a new batch_id to the run's notes.batches array. Idempotent
    on (slice_idx, batch_id) — won't duplicate if called twice."""
    notes = _read_run_notes(state_conn, run_id)
    batches = notes.setdefault("batches", [])
    for b in batches:
        if b.get("slice_idx") == slice_idx and b.get("batch_id") == batch_id:
            return  # already recorded
    batches.append({"slice_idx": slice_idx, "batch_id": batch_id, "ingested": False})
    _write_run_notes(state_conn, run_id, notes)


def _record_batch_ingested(state_conn, run_id: str, *, batch_id: str) -> None:
    """Mark a batch as ingested in the run's notes."""
    notes = _read_run_notes(state_conn, run_id)
    for b in notes.get("batches", []):
        if b.get("batch_id") == batch_id:
            b["ingested"] = True
            break
    _write_run_notes(state_conn, run_id, notes)


def _record_run_finished(state_conn, run_id: str, *, status: str,
                         n_success: int, n_failed: int, n_empty: int,
                         total_cost_usd: float, abort_reason: Optional[str],
                         batch_id: str = "") -> None:
    # Preserve existing notes.batches structure; just merge in the
    # comma-joined batch_id for legacy readers, and stamp status.
    notes = _read_run_notes(state_conn, run_id)
    if batch_id:
        notes["batch_id_legacy"] = batch_id
    notes["finished_status"] = status
    state_conn.execute(
        """
        UPDATE enrichment_runs
        SET finished_at=?, status=?, n_success=?, n_failed=?, n_empty=?,
            total_cost_usd=?, abort_reason=?, notes=?
        WHERE id=?
        """,
        (_utcnow_iso(), status, n_success, n_failed, n_empty,
         total_cost_usd, abort_reason, json.dumps(notes), run_id),
    )
    state_conn.commit()


# ── Result ingestion ───────────────────────────────────────────────────────

async def _ingest_one_group(
    *, group_id: int, conv_id: str, user_id: str, turns: list,
    chunk_results: list,  # list of BatchResult, ordered by chunk_idx
    target_variant: str, state_conn, db_path: Path,
    cost_per_in: float, cost_per_out: float,
) -> dict:
    """Parse all chunk results for one group, write observations,
    update state-machine row. Returns a per-group summary dict.
    """
    observations: list[dict] = []
    n_chunks = len(chunk_results)
    n_chunk_failed = 0
    last_err = ""
    tokens_in = tokens_out = 0
    cache_read = cache_write = 0

    for ci, br in sorted(chunk_results, key=lambda x: x[0]):
        # br is BatchResult
        tokens_in += br.usage.tokens_in
        tokens_out += br.usage.tokens_out
        cache_read += br.usage.cache_read_tokens
        cache_write += br.usage.cache_write_tokens
        if not br.succeeded:
            n_chunk_failed += 1
            last_err = br.error or "batch chunk failed"
            continue
        # Parse observations from succeeded text
        parsed = observer.parse_observations(br.text)
        observations.extend(parsed)

    # Cost from token counts. Service tier=batch on Anthropic returns
    # "batch" usage, which we charge at 50% of profile list. Cache reads
    # bill at 10%, writes at 125% per Anthropic pricing — we approximate
    # by treating cache_read as 0.1× and the rest at full rate.
    base_in_cost = ((tokens_in - cache_read) / 1_000_000.0) * cost_per_in
    cache_read_cost = (cache_read / 1_000_000.0) * cost_per_in * 0.10
    out_cost = (tokens_out / 1_000_000.0) * cost_per_out
    cost_usd = (base_in_cost + cache_read_cost + out_cost) * 0.5  # batch = 50%

    if not observations:
        # Pure failure or empty
        if n_chunk_failed > 0 and n_chunk_failed == n_chunks:
            estate.mark_failed(
                state_conn, group_id,
                error_class="batch_error",
                last_error=last_err[:200],
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
            )
            return {"status": "failed", "obs": 0, "cost": cost_usd}
        else:
            estate.mark_empty(
                state_conn, group_id,
                tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
            )
            return {"status": "empty", "obs": 0, "cost": cost_usd}

    # Write observations using the same writer as live mode.
    # process_conversation does this for us, but we already have the
    # parsed observations. Inline the write loop.
    source_turn_ids = [t[0] for t in turns]
    written = 0
    for obs in observations:
        try:
            obs_id = await observer.write_observation(
                obs, target_variant, user_id, conv_id, source_turn_ids,
                source_group_id=group_id,
            )
            if obs_id:
                written += 1
        except Exception as e:  # noqa: BLE001
            last_err = f"write_observation: {type(e).__name__}: {e!r}"
            n_chunk_failed += 1

    estate.mark_success(
        state_conn, group_id,
        obs_emitted=written,
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
        partial_failure_chunks=n_chunk_failed,
    )
    return {"status": "success", "obs": written, "cost": cost_usd}


# ── Main flow ──────────────────────────────────────────────────────────────

async def _poll_fetch_ingest_one_batch(
    runner, batch_id: str, *, client: httpx.AsyncClient,
    group_meta: dict, target_variant: str, state_conn, db_path: Path,
    cost_per_in: float, cost_per_out: float,
    poll_interval_s: float, max_wait_s: float,
    label: str = "batch",
) -> tuple[int, int, int, float]:
    """Wait for a single batch to end, fetch its results, ingest each
    group's chunks. Returns (n_success, n_empty, n_failed, cost_added).

    Used by both the live submit-loop (after each slice's submit) and the
    resume path (where the batch was submitted by a prior worker).
    """
    # Poll until ended
    last_state = None
    deadline = time.monotonic() + max_wait_s
    poll_n = 0
    while True:
        status = await runner.poll(batch_id, client=client)
        poll_n += 1
        if status.state != last_state or (poll_n % 10 == 0):
            done = status.n_succeeded + status.n_errored + status.n_canceled + status.n_expired
            total = done + status.n_processing
            print(f"[batch] {label} poll #{poll_n}: state={status.state} "
                  f"done={done}/{total} success={status.n_succeeded} "
                  f"errored={status.n_errored}", flush=True)
            last_state = status.state
        if status.state == "ended":
            break
        if status.state in ("canceled", "failed"):
            raise RuntimeError(f"{label} ended in state {status.state!r}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label} did not complete within {max_wait_s}s")
        await asyncio.sleep(poll_interval_s)

    # Fetch + ingest
    print(f"[batch] {label} fetching results...", flush=True)
    results_by_gid: dict[int, list[tuple[int, object]]] = {}
    n_results = 0
    async for r in runner.fetch_results(batch_id, client=client):
        n_results += 1
        try:
            if not r.custom_id.startswith("g"):
                raise ValueError("missing 'g' prefix")
            gid_str, ci_str = r.custom_id[1:].split("_c", 1)
            gid = int(gid_str)
            ci = int(ci_str)
        except Exception:
            print(f"[batch] WARN: bad custom_id {r.custom_id!r}", flush=True)
            continue
        results_by_gid.setdefault(gid, []).append((ci, r))
    print(f"[batch] {label} fetched {n_results} results across "
          f"{len(results_by_gid)} groups; ingesting...", flush=True)

    n_success = n_empty = n_failed = 0
    cost_added = 0.0
    ingested_in_slice = 0
    for gid, chunk_results in results_by_gid.items():
        meta = group_meta.get(str(gid))
        if meta is None:
            print(f"[batch] WARN: gid {gid} not in group_meta (orphan result?)",
                  flush=True)
            continue
        _, uid, conv_id, turns = meta
        res = await _ingest_one_group(
            group_id=gid, conv_id=conv_id, user_id=uid, turns=turns,
            chunk_results=chunk_results,
            target_variant=target_variant,
            state_conn=state_conn, db_path=db_path,
            cost_per_in=cost_per_in, cost_per_out=cost_per_out,
        )
        cost_added += res["cost"]
        if res["status"] == "success":
            n_success += 1
        elif res["status"] == "empty":
            n_empty += 1
        else:
            n_failed += 1
        ingested_in_slice += 1
        if ingested_in_slice % 100 == 0:
            print(f"[batch] {label} ingested "
                  f"{ingested_in_slice}/{len(results_by_gid)} "
                  f"(success={n_success} empty={n_empty} failed={n_failed} "
                  f"cost+=${cost_added:.2f})", flush=True)
    print(f"[batch] {label} done: groups_ingested={ingested_in_slice}",
          flush=True)
    return n_success, n_empty, n_failed, cost_added


async def _resume_run(args, *, profile, token: str, db_path: Path) -> int:
    """Resume an existing run row by id. Reads notes.batches; for each batch
    not yet ingested, polls + fetches + ingests. Re-derives group_meta from
    rows still in_progress under this run_id.

    Will leave the run unfinalized if any expected batch is still in_progress
    on the provider side and exceeds --max-wait-s.
    """
    from batch_runner import make_runner

    state_conn = _open_state_conn(db_path)
    n_reaped = _reap_stale_runs(state_conn, stale_after_minutes=360)
    if n_reaped:
        print(f"[batch] reaped {n_reaped} stale 'running' enrichment_runs rows",
              flush=True)

    run_id = args.resume_run
    row = state_conn.execute(
        """SELECT id, status, source_variant, target_variant, profile, model
           FROM enrichment_runs WHERE id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        sys.exit(f"ERROR: enrichment_runs row not found: id={run_id}")
    _, prior_status, src_v, tgt_v, prior_profile, prior_model = row
    print(f"[resume] run_id={run_id} prior_status={prior_status!r} "
          f"profile={prior_profile!r} target_variant={tgt_v!r}", flush=True)
    if prior_profile and prior_profile != getattr(profile, "name", "?"):
        print(f"[resume] WARN: --profile mismatch ({prior_profile!r} vs "
              f"{getattr(profile,'name','?')!r}); proceeding with --profile",
              flush=True)

    notes = _read_run_notes(state_conn, run_id)
    batches = notes.get("batches", [])
    if not batches:
        sys.exit("ERROR: no batches recorded in run notes; nothing to resume")

    not_ingested = [b for b in batches if not b.get("ingested")]
    print(f"[resume] {len(batches)} batches recorded, "
          f"{len(not_ingested)} not yet ingested", flush=True)
    if not not_ingested:
        # Just finalize the run row if it's still 'running'
        if prior_status == "running":
            _record_run_finished(state_conn, run_id,
                status="completed", n_success=0, n_failed=0, n_empty=0,
                total_cost_usd=0.0, abort_reason=None)
            print("[resume] all batches already ingested; finalized run row",
                  flush=True)
        else:
            print("[resume] all batches already ingested; nothing to do",
                  flush=True)
        state_conn.close()
        return 0

    # Re-derive group_meta from rows still in_progress under this run_id.
    # This skips groups that were ingested in a prior worker invocation.
    rows = state_conn.execute(
        """SELECT id, user_id, group_key
           FROM enrichment_groups
           WHERE enrich_run_id=? AND status='in_progress'""",
        (run_id,),
    ).fetchall()
    if not rows:
        sys.exit(f"ERROR: no in_progress claims under run_id={run_id}; "
                 f"can't resume (run was either fully ingested or claims "
                 f"were already released).")

    # Load full turn lists for these convos via _query_eligible_groups path
    conv_filter = {r[2] for r in rows}
    print(f"[resume] {len(conv_filter)} convs still in_progress; "
          f"re-loading turns...", flush=True)
    type_allowlist = DEFAULT_CORE_ALLOWLIST
    groups = _query_eligible_groups(
        db_path, type_allowlist, None, src_v or args.source_variant, conv_filter,
    )
    # Build group_meta same shape as live path
    gid_by_key = {(r[1], r[2]): r[0] for r in rows}
    group_meta: dict[str, tuple[int, str, str, list]] = {}
    for uid, conv_id, turns in groups:
        gid = gid_by_key.get((uid, conv_id))
        if gid is not None:
            group_meta[str(gid)] = (gid, uid, conv_id, turns)
    print(f"[resume] re-derived group_meta with {len(group_meta)} entries",
          flush=True)

    cost_per_in = float(getattr(profile, "input_cost_per_mtok", 0) or 0)
    cost_per_out = float(getattr(profile, "output_cost_per_mtok", 0) or 0)
    runner = make_runner(profile, token=token)

    n_success = n_empty = n_failed = 0
    total_cost = 0.0
    submitted_at = time.time()
    async with httpx.AsyncClient() as client:
        for batch_meta in not_ingested:
            batch_id = batch_meta["batch_id"]
            slice_idx = batch_meta.get("slice_idx", -1)
            label = f"resume-slice-{slice_idx}" if slice_idx >= 0 else "resume-batch"
            try:
                ns, ne, nf, cost = await _poll_fetch_ingest_one_batch(
                    runner, batch_id, client=client,
                    group_meta=group_meta, target_variant=tgt_v,
                    state_conn=state_conn, db_path=db_path,
                    cost_per_in=cost_per_in, cost_per_out=cost_per_out,
                    poll_interval_s=args.poll_interval_s,
                    max_wait_s=args.max_wait_s,
                    label=label,
                )
            except (RuntimeError, TimeoutError) as e:
                print(f"[resume] {label} FAILED: {type(e).__name__}: {e}",
                      flush=True)
                # Don't release claims here — user may want to re-run resume
                # again. Exit with non-zero so caller sees the failure.
                state_conn.close()
                return 4
            n_success += ns
            n_empty += ne
            n_failed += nf
            total_cost += cost
            _record_batch_ingested(state_conn, run_id, batch_id=batch_id)

        # Catch groups that had no result in any batch
        for gid_str, (gid, uid, conv_id, turns) in group_meta.items():
            r2 = state_conn.execute(
                "SELECT status FROM enrichment_groups WHERE id=?", (int(gid_str),),
            ).fetchone()
            if r2 and r2[0] == "in_progress":
                estate.mark_failed(
                    state_conn, int(gid_str),
                    error_class="batch_no_result",
                    last_error="no batch result returned for this group on resume",
                )
                n_failed += 1

    _record_run_finished(state_conn, run_id,
        status="completed", n_success=n_success, n_failed=n_failed,
        n_empty=n_empty, total_cost_usd=total_cost, abort_reason=None)

    elapsed = time.time() - submitted_at
    print()
    print("=" * 62)
    print("  m3-enrich-batch RESUME COMPLETE")
    print("=" * 62)
    print(f"  run_id:     {run_id}")
    print(f"  batches resumed: {len(not_ingested)}")
    print(f"  groups:     {len(group_meta)} (success={n_success} empty={n_empty} failed={n_failed})")
    print(f"  cost added: ${total_cost:.4f}")
    print(f"  wallclock:  {elapsed:.1f}s ({elapsed/60:.1f} min)")
    state_conn.close()
    return 0


async def _run_async(args) -> int:
    profile = _parse_profile(args.profile, Path(args.profile_path)) if args.profile_path \
        else _parse_profile(args.profile, REPO_ROOT / "config" / "slm" / f"{args.profile}.yaml")
    # Validate the profile's backend is supported by some BatchRunner.
    # make_runner() does the strict dispatch (anthropic | gemini-OAI-shim);
    # we fail fast here with a clearer error.
    try:
        # dry-run dispatch to surface unsupported backends before the slow
        # enumeration step
        from batch_runner import make_runner as _check_runner  # noqa: WPS433
        # Sentinel string — this dispatch never makes a network call, just
        # validates backend support. Real token resolved below from vault.
        _check_runner(profile, token="placeholder")  # nosec B106
    except NotImplementedError as e:
        sys.exit(f"ERROR: {e}")

    token = get_api_key(profile.api_key_service) or ""
    if not token:
        sys.exit(f"ERROR: {profile.api_key_service} not found in vault/env")

    db_path = Path(args.core_db).resolve()
    if not db_path.exists():
        sys.exit(f"ERROR: --core-db not found: {db_path}")

    # Set M3_DATABASE so memory_core.write picks the right DB
    os.environ["M3_DATABASE"] = str(db_path)

    # Resume path: if --resume-run is given, skip enumeration + claim + submit.
    # Goes straight to poll/ingest for any non-ingested batches in notes.
    if getattr(args, "resume_run", None):
        return await _resume_run(args, profile=profile, token=token, db_path=db_path)

    # New runs require --target-variant
    if not args.target_variant:
        sys.exit("ERROR: --target-variant is required for new runs (only optional for --resume-run)")

    # Cost rates (full list price; we halve for batch and discount cache reads)
    cost_per_in = float(getattr(profile, "input_cost_per_mtok", 0) or 0)
    cost_per_out = float(getattr(profile, "output_cost_per_mtok", 0) or 0)

    # Build conv-list filter
    conv_filter: Optional[set[str]] = None
    if args.source_conv_list:
        conv_filter = _load_conv_list(Path(args.source_conv_list).resolve())
        print(f"[batch] --source-conv-list: {len(conv_filter)} group_keys loaded",
              flush=True)

    # Enumerate groups (same as live mode)
    type_allowlist = DEFAULT_CORE_ALLOWLIST
    groups = _query_eligible_groups(
        db_path, type_allowlist, args.limit, args.source_variant, conv_filter,
    )
    if not groups:
        print("[batch] no eligible groups found", flush=True)
        return 0
    print(f"[batch] enumerated {len(groups)} candidate groups", flush=True)

    # Open state conn, claim groups, build batch requests
    state_conn = _open_state_conn(db_path)
    # One-time cleanup pass for crashed-worker ghost rows in enrichment_runs.
    # Cosmetic — keeps audit queries clean. Threshold is 6h (well above any
    # legitimate run wallclock).
    n_reaped = _reap_stale_runs(state_conn, stale_after_minutes=360)
    if n_reaped:
        print(f"[batch] reaped {n_reaped} stale 'running' enrichment_runs rows (>6h old)",
              flush=True)
    enrich_run_id_placeholder = "pending-batch-submit"
    batch_requests: list[BatchRequest] = []
    group_meta: dict[str, tuple[int, str, str, list]] = {}  # custom_id_prefix -> (gid, uid, cid, turns)

    # Resolve group_id by (uid, conv_id)
    rows = state_conn.execute(
        "SELECT id, user_id, group_key FROM enrichment_groups WHERE source_variant = ?",
        (args.source_variant,),
    ).fetchall()
    gid_by_key = {(r[1], r[2]): r[0] for r in rows}

    n_claimed = 0
    n_skipped = 0
    for uid, cid, turns in groups:
        gid = gid_by_key.get((uid, cid))
        if gid is None:
            n_skipped += 1
            continue
        # Claim the group with a placeholder run_id; we'll patch the run_id
        # column post-submit when we know the real one.
        ct = estate.claim_group(state_conn, gid, enrich_run_id=enrich_run_id_placeholder)
        if ct is None:
            n_skipped += 1
            continue
        chunks = _build_chunks_for_group(turns, profile, gid)
        if not chunks:
            estate.mark_empty(state_conn, gid, tokens_in=0, tokens_out=0, cost_usd=0.0)
            continue
        for ci, user_text in chunks:
            # Anthropic requires custom_id to match ^[a-zA-Z0-9_-]{1,64}$,
            # so use '_' separator (not '::').
            cid_str = f"g{gid}_c{ci}"
            batch_requests.append(BatchRequest(
                custom_id=cid_str,
                system=profile.system,
                user_text=user_text,
                cache_system=getattr(profile, "cache_system", True),
            ))
        group_meta[str(gid)] = (gid, uid, cid, turns)
        n_claimed += 1

    if not batch_requests:
        print("[batch] nothing to submit (all groups raced or already terminal)",
              flush=True)
        state_conn.close()
        return 0
    print(f"[batch] claimed {n_claimed} groups, "
          f"{n_skipped} skipped (raced/terminal/no-state-row), "
          f"built {len(batch_requests)} chunk requests", flush=True)

    if args.dry_run:
        print("[batch] --dry-run: would submit; not calling Anthropic. Releasing claims.",
              flush=True)
        # Roll back claims
        ph = ",".join("?" * len(group_meta))
        gids = list(group_meta.keys())
        if gids:
            state_conn.execute(
                f"UPDATE enrichment_groups SET status='pending', claim_token=NULL, "
                f"claimed_at=NULL WHERE id IN ({ph}) AND status='in_progress'",
                gids,
            )
            state_conn.commit()
        state_conn.close()
        return 0

    # Submit & process — auto-split into slices of runner.max_batch_size.
    # Each slice is one provider batch; we ingest results after each slice
    # ends so a crash mid-workload doesn't lose committed work.
    runner = make_runner(profile, token=token)
    if getattr(args, "slice_size", None):
        # Honor user override on both the slicer hint and the hard cap.
        try:
            runner.max_batch_size = args.slice_size
            if hasattr(runner, "INLINE_LIMIT"):
                runner.INLINE_LIMIT = args.slice_size
        except Exception:  # noqa: BLE001
            pass
    max_slice = getattr(runner, "max_batch_size", 100_000)
    n_slices = (len(batch_requests) + max_slice - 1) // max_slice
    print(f"[batch] runner={type(runner).__name__} max_batch_size={max_slice} "
          f"-> {n_slices} slice(s)", flush=True)

    # Record the run with the FIRST batch's id (we'll append more in notes).
    enrich_run_id: Optional[str] = None
    submitted_at = time.time()
    n_success = n_empty = n_failed = 0
    total_cost = 0.0
    batch_ids: list[str] = []

    async with httpx.AsyncClient() as client:
        for slice_idx in range(n_slices):
            slice_requests = batch_requests[slice_idx*max_slice : (slice_idx+1)*max_slice]
            print(f"[batch] slice {slice_idx+1}/{n_slices}: submitting "
                  f"{len(slice_requests)} requests...", flush=True)
            try:
                batch_id = await runner.submit(slice_requests, client=client)
            except Exception as e:  # noqa: BLE001
                print(f"[batch] FATAL: slice {slice_idx+1} submit failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                # Release in_progress claims belonging to THIS run. Two cases:
                #   - Slice 1 failure: claims still under enrich_run_id_placeholder
                #     (we never got to the placeholder->real swap).
                #   - Slice N>=2 failure: claims under enrich_run_id (the real one,
                #     swapped after slice 1's successful submit).
                # Earlier worker only released placeholder; that left N>=2-failure
                # claims orphaned and forced manual cleanup. Now we release both.
                ids_to_release = [enrich_run_id_placeholder]
                if enrich_run_id:
                    ids_to_release.append(enrich_run_id)
                ph = ",".join("?" * len(ids_to_release))
                state_conn.execute(
                    f"UPDATE enrichment_groups SET status='pending', claim_token=NULL, "
                    f"claimed_at=NULL, enrich_run_id=NULL "
                    f"WHERE enrich_run_id IN ({ph}) AND status='in_progress'",
                    ids_to_release,
                )
                state_conn.commit()
                # Also finalize the run row (if it exists) so audit doesn't show
                # a perpetually-running ghost.
                if enrich_run_id:
                    _record_run_finished(state_conn, enrich_run_id,
                        status="aborted",
                        n_success=n_success,
                        n_failed=n_claimed - n_success - n_empty,
                        n_empty=n_empty,
                        total_cost_usd=total_cost,
                        abort_reason=f"submit_failed_slice_{slice_idx+1}",
                        batch_id=",".join(batch_ids) if batch_ids else "")
                state_conn.close()
                raise

            batch_ids.append(batch_id)
            print(f"[batch] slice {slice_idx+1} submitted batch_id={batch_id}",
                  flush=True)

            # Record the run row on first successful submit; then patch
            # placeholder run_id rows to the real one.
            if enrich_run_id is None:
                enrich_run_id = _record_run_started(
                    state_conn, profile=profile, db_path=db_path,
                    source_variant=args.source_variant or "",
                    target_variant=args.target_variant,
                    batch_id=batch_id, n_groups=n_claimed,
                    slice_size=max_slice,
                    argv=sys.argv,
                )
                state_conn.execute(
                    "UPDATE enrichment_groups SET enrich_run_id=? "
                    "WHERE enrich_run_id=? AND status='in_progress'",
                    (enrich_run_id, enrich_run_id_placeholder),
                )
                state_conn.commit()
            else:
                # Slice 2+: append batch_id to run notes for resume
                _record_batch_submitted(state_conn, enrich_run_id,
                                        slice_idx=slice_idx, batch_id=batch_id)

            # Poll this slice
            last_state = None
            deadline = time.monotonic() + args.max_wait_s
            poll_n = 0
            while True:
                status = await runner.poll(batch_id, client=client)
                poll_n += 1
                if status.state != last_state or (poll_n % 10 == 0):
                    done = status.n_succeeded + status.n_errored + status.n_canceled + status.n_expired
                    total = done + status.n_processing
                    print(f"[batch] slice {slice_idx+1} poll #{poll_n}: "
                          f"state={status.state} done={done}/{total} "
                          f"success={status.n_succeeded} errored={status.n_errored}",
                          flush=True)
                    last_state = status.state
                if status.state == "ended":
                    break
                if status.state in ("canceled", "failed"):
                    print(f"[batch] FATAL: slice {slice_idx+1} ended in state "
                          f"{status.state!r}", flush=True)
                    _record_run_finished(state_conn, enrich_run_id,
                        status="aborted", n_success=n_success,
                        n_failed=n_claimed - n_success - n_empty, n_empty=n_empty,
                        total_cost_usd=total_cost,
                        abort_reason=f"batch_{status.state}",
                        batch_id=",".join(batch_ids))
                    state_conn.execute(
                        "UPDATE enrichment_groups SET status='pending', claim_token=NULL, "
                        "claimed_at=NULL WHERE enrich_run_id=? AND status='in_progress'",
                        (enrich_run_id,),
                    )
                    state_conn.commit()
                    state_conn.close()
                    return 1
                if time.monotonic() >= deadline:
                    print(f"[batch] slice {slice_idx+1} timeout after "
                          f"{args.max_wait_s}s; batch still in_progress. "
                          f"Re-run with the same conv-list later (claims preserved).",
                          flush=True)
                    state_conn.close()
                    return 2
                await asyncio.sleep(args.poll_interval_s)

            # Fetch + ingest this slice's results
            print(f"[batch] slice {slice_idx+1} fetching results...", flush=True)
            results_by_gid: dict[int, list[tuple[int, object]]] = {}
            n_results = 0
            async for r in runner.fetch_results(batch_id, client=client):
                n_results += 1
                try:
                    if not r.custom_id.startswith("g"):
                        raise ValueError("missing 'g' prefix")
                    gid_str, ci_str = r.custom_id[1:].split("_c", 1)
                    gid = int(gid_str)
                    ci = int(ci_str)
                except Exception:
                    print(f"[batch] WARN: bad custom_id {r.custom_id!r}", flush=True)
                    continue
                results_by_gid.setdefault(gid, []).append((ci, r))
            print(f"[batch] slice {slice_idx+1} fetched {n_results} results "
                  f"across {len(results_by_gid)} groups; ingesting...",
                  flush=True)

            # Per-group ingest for ONLY the groups touched by this slice.
            ingested_in_slice = 0
            for gid in results_by_gid:
                meta = group_meta.get(str(gid))
                if meta is None:
                    print(f"[batch] WARN: gid {gid} not in group_meta", flush=True)
                    continue
                _, uid, conv_id, turns = meta
                chunk_results = results_by_gid[gid]
                res = await _ingest_one_group(
                    group_id=gid, conv_id=conv_id, user_id=uid, turns=turns,
                    chunk_results=chunk_results,
                    target_variant=args.target_variant,
                    state_conn=state_conn, db_path=db_path,
                    cost_per_in=cost_per_in, cost_per_out=cost_per_out,
                )
                total_cost += res["cost"]
                if res["status"] == "success":
                    n_success += 1
                elif res["status"] == "empty":
                    n_empty += 1
                else:
                    n_failed += 1
                ingested_in_slice += 1
                if ingested_in_slice % 100 == 0:
                    print(f"[batch] slice {slice_idx+1} ingested "
                          f"{ingested_in_slice}/{len(results_by_gid)} "
                          f"(cumul: success={n_success} empty={n_empty} "
                          f"failed={n_failed} cost=${total_cost:.2f})",
                          flush=True)
            print(f"[batch] slice {slice_idx+1} done: groups_ingested="
                  f"{ingested_in_slice}", flush=True)
            _record_batch_ingested(state_conn, enrich_run_id, batch_id=batch_id)

            # Budget cap: check after each slice ingest. If we've blown the
            # cap, abort cleanly — release any remaining placeholder claims
            # and stop scheduling new slices.
            budget = getattr(args, "budget_usd", None)
            if budget is not None and total_cost >= budget:
                slices_remaining = n_slices - (slice_idx + 1)
                print(f"[batch] BUDGET CAP HIT: cost=${total_cost:.4f} "
                      f">= budget=${budget:.4f}; aborting with "
                      f"{slices_remaining} slice(s) unsubmitted.", flush=True)
                # Release claims for groups in unsubmitted slices.
                # Their custom_ids are in batch_requests but we never submitted
                # them, so they're still claimed under enrich_run_id.
                state_conn.execute(
                    "UPDATE enrichment_groups SET status='pending', "
                    "claim_token=NULL, claimed_at=NULL "
                    "WHERE enrich_run_id=? AND status='in_progress'",
                    (enrich_run_id,),
                )
                state_conn.commit()
                _record_run_finished(state_conn, enrich_run_id,
                    status="aborted", n_success=n_success, n_failed=n_failed,
                    n_empty=n_empty, total_cost_usd=total_cost,
                    abort_reason=f"budget_cap_${budget:.2f}",
                    batch_id=",".join(batch_ids))
                state_conn.close()
                return 3

        # All slices complete. Catch any groups that had no results in any
        # slice (shouldn't happen if claim & submit were consistent, but
        # defensive).
        for gid_str, (gid, uid, conv_id, turns) in group_meta.items():
            row = state_conn.execute(
                "SELECT status FROM enrichment_groups WHERE id=?", (int(gid_str),),
            ).fetchone()
            if row and row[0] == "in_progress":
                estate.mark_failed(
                    state_conn, int(gid_str),
                    error_class="batch_no_result",
                    last_error="no batch result returned for this group",
                )
                n_failed += 1

    # Finalize
    _record_run_finished(state_conn, enrich_run_id,
        status="completed", n_success=n_success, n_failed=n_failed, n_empty=n_empty,
        total_cost_usd=total_cost, abort_reason=None,
        batch_id=",".join(batch_ids))

    elapsed = time.time() - submitted_at
    print()
    print("=" * 62)
    print("  m3-enrich-batch COMPLETE")
    print("=" * 62)
    print(f"  slices:     {n_slices}")
    print(f"  batch_ids:  {batch_ids[0] if len(batch_ids)==1 else f'{len(batch_ids)} batches'}")
    print(f"  groups:     {len(group_meta)} (success={n_success} empty={n_empty} failed={n_failed})")
    print(f"  cost:       ${total_cost:.4f}")
    print(f"  wallclock:  {elapsed:.1f}s ({elapsed/60:.1f} min)")
    state_conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True,
                    help="profile name from config/slm/<name>.yaml. Supported backends: "
                         "anthropic, openai (Gemini OAI shim only).")
    ap.add_argument("--profile-path", default=None,
                    help="Override profile YAML path. Default: config/slm/<profile>.yaml")
    ap.add_argument("--core", action="store_true", help="Process the core memory DB.")
    ap.add_argument("--core-db", required=True, help="Path to the SQLite DB.")
    ap.add_argument("--source-variant", default=None)
    ap.add_argument("--target-variant", default=None,
                    help="Required for new runs; ignored for --resume-run "
                         "(target_variant is read from the existing run row).")
    ap.add_argument("--resume-run", default=None,
                    help="Resume an existing enrichment_runs row by id. Skips "
                         "enumeration + claim + submit; goes straight to poll "
                         "and ingest for any batches in notes.batches that are "
                         "not yet ingested. Re-derives group_meta from rows "
                         "still in_progress under this run_id. The remaining "
                         "args (--profile, --core-db, --source-variant) must "
                         "match the original run.")
    ap.add_argument("--source-conv-list", default=None,
                    help="File path: newline-list or JSON array of group_keys to filter.")
    ap.add_argument("--track-state", action="store_true", default=True,
                    help="Use enrichment_groups state machine. Always on for batch.")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="Resume mode (only claims pending/failed groups). Always on for batch.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of conversations submitted (smoke testing).")
    ap.add_argument("--poll-interval-s", type=float, default=30.0,
                    help="Seconds between batch poll requests. Default 30.")
    ap.add_argument("--max-wait-s", type=float, default=24*3600,
                    help="Max seconds to wait for batch completion. Default 86400 (24h).")
    ap.add_argument("--slice-size", type=int, default=None,
                    help="Override runner.max_batch_size. Use to fit Gemini's "
                         "Tier-1 enqueued-tokens cap (3M) — at ~5,600 tok/req "
                         "set this to ~500 for 16k-input convos.")
    ap.add_argument("--budget-usd", type=float, default=None,
                    help="Hard cap on total run cost in USD. The worker checks "
                         "after each slice ingest; if cumulative cost exceeds "
                         "the cap, the run aborts cleanly (claims released, "
                         "remaining slices NOT submitted). Use to prevent "
                         "runaway spend on a misconfigured conv-list.")
    ap.add_argument("--skip-preflight", action="store_true", default=True)
    ap.add_argument("--yes", "-y", action="store_true", default=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview chunks; release claims; do not submit to Anthropic.")
    args = ap.parse_args()
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
