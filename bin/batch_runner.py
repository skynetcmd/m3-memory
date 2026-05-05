"""Provider-neutral batch-API runner protocol with Anthropic implementation.

Use when you have a pile of independent LLM calls and want a 50% cost
discount in exchange for async wallclock (typically minutes-to-hours
for the batch to complete).

Currently implements:
  - AnthropicBatchRunner: /v1/messages/batches, 50% off list pricing.

Stub points for future:
  - OpenAIBatchRunner: /v1/batches with JSONL Files API (50% off).
  - VertexBatchRunner: GCS-backed batch (50% off, but needs Cloud Storage
    bucket provisioning).

Calling code uses BatchRequest/BatchResult dataclasses; runners translate
to/from native API formats. See bin/m3_enrich_batch.py for usage.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional, Protocol

import httpx

# ── Provider-neutral request/result shapes ─────────────────────────────────

@dataclass
class BatchRequest:
    """One LLM call inside a batch.

    custom_id: caller-chosen id, echoed back in BatchResult so callers can
        match results to whatever schema they care about (e.g.
        "<group_id>::<chunk_idx>").
    system: system prompt string.
    user_text: the user message body.
    cache_system: when True, mark the system prompt for caching (Anthropic
        ephemeral cache; ignored on providers without prompt caching).
    """
    custom_id: str
    system: str
    user_text: str
    cache_system: bool = True


@dataclass
class BatchUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    service_tier: str = ""


@dataclass
class BatchResult:
    custom_id: str
    succeeded: bool
    text: str = ""           # the assistant reply when succeeded=True
    error: str = ""          # error description when succeeded=False
    usage: BatchUsage = field(default_factory=BatchUsage)


@dataclass
class BatchStatus:
    batch_id: str
    state: str               # "in_progress", "ended", "failed", "canceled"
    n_processing: int = 0
    n_succeeded: int = 0
    n_errored: int = 0
    n_canceled: int = 0
    n_expired: int = 0


# ── Protocol the rest of the codebase calls ───────────────────────────────

class BatchRunner(Protocol):
    """Provider-neutral batch interface. Implementations encapsulate the
    native API shape, auth, and result-fetch dance.
    """

    profile: object
    """The slm_intent.Profile that selects model/key/url; runners read
    profile.model, profile.api_key_service, profile.max_tokens, etc."""

    max_batch_size: int
    """Per-batch hard limit (varies by provider). The auto-split helper
    reads this to decide how many slices to create. Anthropic: 100K,
    Gemini Tier-1 with real-shape requests: ~1K."""

    async def submit(
        self, requests: list[BatchRequest], *, client: httpx.AsyncClient
    ) -> str:
        """Submit a list of requests as ONE batch. Returns a provider-native
        batch_id used by poll/fetch_results.
        """
        ...

    async def poll(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> BatchStatus:
        """Return current status of the batch."""
        ...

    def fetch_results(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> AsyncIterator[BatchResult]:
        """Yield each completed result via async-for iteration. Caller is
        responsible for not iterating until poll() reports state='ended'.

        Note: protocol-level signature is sync-returning the async iterator
        so callers can `async for r in runner.fetch_results(...)` directly.
        Implementations are async generators (`async def ... yield`).
        """
        ...


# ── Anthropic implementation ───────────────────────────────────────────────

_ANTHROPIC_BATCH_BETA = "message-batches-2024-09-24"


class AnthropicBatchRunner:
    """Anthropic /v1/messages/batches — 50% off list pricing.

    Per docs: max 100K requests per batch, max 256MB payload. Typical
    completion 5-60 minutes; hard ceiling 24h.

    Instantiate with the same Profile object you'd pass to live calls;
    we read profile.model, profile.api_key_service, profile.system,
    profile.max_tokens, profile.temperature, profile.cache_system,
    profile.anthropic_version, profile.url is unused (we always hit the
    batches endpoint).
    """

    # Per-batch hard limit. Used by run_to_completion_chunked to slice.
    max_batch_size = 100_000

    def __init__(self, profile, *, token: str):
        self.profile = profile
        self._token = token
        self._beta = _ANTHROPIC_BATCH_BETA
        self._base = "https://api.anthropic.com/v1/messages/batches"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._token,
            "anthropic-version": getattr(self.profile, "anthropic_version", "2023-06-01"),
            "anthropic-beta": self._beta,
        }

    def _build_one(self, req: BatchRequest) -> dict:
        # Per-request `system` overrides the batch-wide system. We send it
        # per request because callers may want different system prompts for
        # different requests (and prompt caching keys on the *content* of
        # the system block anyway, so identical prompts still cache).
        # Anthropic accepts either a plain string OR a list-of-blocks for
        # the `system` field; the latter form lets us mark the prompt for
        # ephemeral caching.
        system_field: Any
        if req.cache_system and getattr(self.profile, "cache_system", True):
            system_field = [{
                "type": "text",
                "text": req.system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_field = req.system
        return {
            "custom_id": req.custom_id,
            "params": {
                "model": self.profile.model,
                "max_tokens": getattr(self.profile, "max_tokens", 1024),
                "system": system_field,
                "messages": [{"role": "user", "content": req.user_text}],
                "temperature": getattr(self.profile, "temperature", 0),
            },
        }

    async def submit(
        self, requests: list[BatchRequest], *, client: httpx.AsyncClient
    ) -> str:
        if not requests:
            raise ValueError("submit() requires at least one request")
        if len(requests) > 100_000:
            raise ValueError(
                f"Anthropic batch limit is 100,000 requests; got {len(requests)}. "
                f"Split into multiple batches."
            )
        payload = {"requests": [self._build_one(r) for r in requests]}
        r = await client.post(
            self._base, json=payload, headers=self._headers(), timeout=120.0
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"anthropic batch submit http {r.status_code}: {r.text[:500]}"
            )
        data = r.json()
        bid = data.get("id")
        if not bid:
            raise RuntimeError(f"anthropic batch submit returned no id: {data}")
        return bid

    async def poll(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> BatchStatus:
        r = await client.get(
            f"{self._base}/{batch_id}", headers=self._headers(), timeout=30.0
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"anthropic batch poll http {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        # Anthropic states: in_progress, canceling, ended (per docs)
        ps = data.get("processing_status", "in_progress")
        rc = data.get("request_counts", {}) or {}
        state = "ended" if ps == "ended" else ps
        return BatchStatus(
            batch_id=batch_id,
            state=state,
            n_processing=int(rc.get("processing", 0)),
            n_succeeded=int(rc.get("succeeded", 0)),
            n_errored=int(rc.get("errored", 0)),
            n_canceled=int(rc.get("canceled", 0)),
            n_expired=int(rc.get("expired", 0)),
        )

    async def fetch_results(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> AsyncIterator[BatchResult]:
        # First, get results_url from the batch metadata.
        r = await client.get(
            f"{self._base}/{batch_id}", headers=self._headers(), timeout=30.0
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"anthropic batch metadata http {r.status_code}: {r.text[:300]}"
            )
        results_url = r.json().get("results_url")
        if not results_url:
            raise RuntimeError(
                f"anthropic batch {batch_id} has no results_url; status was "
                f"{r.json().get('processing_status')!r}"
            )
        # Stream JSONL results
        async with client.stream(
            "GET", results_url, headers=self._headers(), timeout=300.0
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(
                    f"anthropic batch results http {resp.status_code}: {body[:300]!r}"
                )
            buf = b""
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    yield self._parse_one(json.loads(line))
            if buf.strip():
                yield self._parse_one(json.loads(buf))

    def _parse_one(self, rec: dict) -> BatchResult:
        cid = rec.get("custom_id", "")
        result = rec.get("result", {}) or {}
        kind = result.get("type", "")
        if kind == "succeeded":
            msg = result.get("message", {}) or {}
            text = "".join(
                b.get("text", "") for b in (msg.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            )
            u = msg.get("usage", {}) or {}
            return BatchResult(
                custom_id=cid,
                succeeded=True,
                text=text,
                usage=BatchUsage(
                    tokens_in=int(u.get("input_tokens") or 0),
                    tokens_out=int(u.get("output_tokens") or 0),
                    cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                    cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                    service_tier=str(u.get("service_tier") or ""),
                ),
            )
        # Errored / canceled / expired
        err_type = result.get("error", {}).get("type", kind or "unknown_error")
        err_msg = result.get("error", {}).get("message", "")
        return BatchResult(
            custom_id=cid,
            succeeded=False,
            error=f"{err_type}: {err_msg}"[:500] if err_msg else err_type,
        )


# ── Gemini implementation ──────────────────────────────────────────────────

class GeminiBatchRunner:
    """Google Gemini Developer API batch — 50% off list pricing.

    Uses the inline-requests path (no Cloud Storage / file upload required):
      POST /v1beta/models/<model>:batchGenerateContent  (x-goog-api-key header)
      GET  /v1beta/<batch_name>                         (poll + fetch — output is inline)

    Per Google's batch docs, BATCH_STATE_SUCCEEDED carries
    metadata.output.inlinedResponses with per-request response or error
    inline. No separate results URL, no JSONL streaming.

    Selected when profile.backend == 'openai' AND profile.url contains
    'generativelanguage.googleapis.com' — i.e. our Gemini-via-OAI-shim
    profiles. Other 'openai' backends (real OpenAI, xAI, etc.) currently
    fall through to NotImplementedError.

    Limits per Google docs:
      - Inline mode: up to ~100 requests per submission. Larger batches
        require File API upload, which this runner does NOT implement.
      - Submitter must split if more than INLINE_LIMIT requests.

    To run a 100K-request workload, the calling code (m3_enrich_batch.py)
    needs to chunk into multiple batch submissions.
    """

    # Gemini Developer API inline-batch ceiling is shape-dependent. Trivial
    # request bodies submit at 50K+, but realistic 6KB-user-content requests
    # cap out around 1500 per slice (verified by probe 2026-05-04 — 1000
    # passes, 2000 hits RESOURCE_EXHAUSTED). We default to 1000 so:
    #   - real-shape enrichment workloads submit cleanly
    #   - poll observability stays usable (one slice ~ minutes)
    #   - per-slice failure blast radius is bounded
    # Override on the instance if you have evidence a different limit fits
    # the workload better.
    INLINE_LIMIT = 1_000
    max_batch_size = INLINE_LIMIT

    def __init__(self, profile, *, token: str):
        self.profile = profile
        self._token = token
        # Gemini requires "models/<id>" prefix; profiles store bare id.
        m = profile.model
        self._model = m if m.startswith("models/") else f"models/{m}"
        self._base = "https://generativelanguage.googleapis.com/v1beta"

    def _build_one(self, req: BatchRequest) -> dict:
        # Gemini batch nests system_instruction at the request level.
        # Per Google docs, system_instruction is a top-level field on the
        # request object, not inside the contents list.
        request_obj = {
            "contents": [
                {"role": "user", "parts": [{"text": req.user_text}]},
            ],
            "generationConfig": {
                "temperature": getattr(self.profile, "temperature", 0),
                "maxOutputTokens": getattr(self.profile, "max_tokens", 8192),
            },
        }
        if req.system:
            request_obj["systemInstruction"] = {"parts": [{"text": req.system}]}
        # Pass-through extra params (e.g. reasoning_effort doesn't apply to
        # Gemini batch; we leave the door open for future fields).
        return {
            "request": request_obj,
            "metadata": {"key": req.custom_id},
        }

    async def submit(
        self, requests: list[BatchRequest], *, client: httpx.AsyncClient
    ) -> str:
        if not requests:
            raise ValueError("submit() requires at least one request")
        if len(requests) > self.INLINE_LIMIT:
            raise ValueError(
                f"Gemini inline batch limit is {self.INLINE_LIMIT}; got "
                f"{len(requests)}. Split into multiple batches."
            )
        payload = {
            "batch": {
                "displayName": f"m3-enrich-batch-{int(time.time())}",
                "inputConfig": {
                    "requests": {
                        "requests": [self._build_one(r) for r in requests],
                    },
                },
            },
        }
        # Auth via x-goog-api-key header rather than ?key=<token> on the URL.
        # httpx INFO-level request logs include the full URL, which would
        # otherwise leak the API key into log files; using a header keeps it
        # out of the request line. NOTE: the native v1beta endpoint
        # (/v1beta/models/...:batchGenerateContent) does NOT accept
        # Authorization: Bearer (returns 401 "Expected OAuth 2 access token")
        # — that header form is only valid on the OAI-compat shim
        # (/v1beta/openai/...). For the native API key, Google's documented
        # header is x-goog-api-key.
        url = f"{self._base}/{self._model}:batchGenerateContent"
        headers = {"x-goog-api-key": self._token}
        r = await client.post(url, json=payload, headers=headers, timeout=120.0)
        if r.status_code != 200:
            raise RuntimeError(
                f"gemini batch submit http {r.status_code}: {r.text[:500]}"
            )
        data = r.json()
        bid = data.get("name") or (data.get("metadata") or {}).get("name")
        if not bid:
            raise RuntimeError(f"gemini batch submit returned no name: {data}")
        return bid

    async def poll(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> BatchStatus:
        # See note on x-goog-api-key in submit() — same rationale here.
        url = f"{self._base}/{batch_id}"
        headers = {"x-goog-api-key": self._token}
        r = await client.get(url, headers=headers, timeout=30.0)
        if r.status_code != 200:
            raise RuntimeError(
                f"gemini batch poll http {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        meta = data.get("metadata", {}) or {}
        gstate = meta.get("state", "BATCH_STATE_PENDING")
        # Map Gemini state -> our protocol state
        if gstate == "BATCH_STATE_SUCCEEDED":
            state = "ended"
        elif gstate in ("BATCH_STATE_FAILED", "BATCH_STATE_EXPIRED"):
            state = "failed"
        elif gstate == "BATCH_STATE_CANCELLED":
            state = "canceled"
        else:
            # PENDING, RUNNING, UNSPECIFIED
            state = "in_progress"
        stats = meta.get("batchStats", {}) or {}
        # request_count includes total; use successfulRequestCount as the
        # done counter and pendingRequestCount as in-flight.
        rc_total = int(stats.get("requestCount") or 0)
        n_succ = int(stats.get("successfulRequestCount") or 0)
        n_fail = int(stats.get("failedRequestCount") or 0)
        n_pend = int(stats.get("pendingRequestCount") or
                     max(0, rc_total - n_succ - n_fail))
        return BatchStatus(
            batch_id=batch_id,
            state=state,
            n_processing=n_pend,
            n_succeeded=n_succ,
            n_errored=n_fail,
            n_canceled=0,
            n_expired=1 if gstate == "BATCH_STATE_EXPIRED" else 0,
        )

    async def fetch_results(
        self, batch_id: str, *, client: httpx.AsyncClient
    ) -> AsyncIterator[BatchResult]:
        # Gemini returns results inline on the batch resource itself.
        # See note on x-goog-api-key in submit() — same rationale here.
        url = f"{self._base}/{batch_id}"
        headers = {"x-goog-api-key": self._token}
        r = await client.get(url, headers=headers, timeout=120.0)
        if r.status_code != 200:
            raise RuntimeError(
                f"gemini batch fetch http {r.status_code}: {r.text[:300]}"
            )
        data = r.json()
        # Output lives at metadata.output.inlinedResponses.inlinedResponses
        # (also mirrored at response.inlinedResponses.inlinedResponses).
        meta = data.get("metadata", {}) or {}
        out = (meta.get("output", {}) or {}).get("inlinedResponses", {}) or {}
        items = out.get("inlinedResponses", []) or []
        for item in items:
            yield self._parse_one(item)

    def _parse_one(self, rec: dict) -> BatchResult:
        cid = (rec.get("metadata") or {}).get("key", "")
        # Either rec.response (success) or rec.error (failure).
        if "error" in rec:
            err = rec["error"] or {}
            etype = err.get("status", "unknown") or err.get("code", "unknown")
            emsg = err.get("message", "")
            return BatchResult(
                custom_id=cid,
                succeeded=False,
                error=f"{etype}: {emsg}"[:500] if emsg else str(etype),
            )
        resp = rec.get("response", {}) or {}
        cand = (resp.get("candidates") or [{}])[0]
        finish = cand.get("finishReason", "")
        text = ""
        for part in (cand.get("content", {}) or {}).get("parts", []) or []:
            if isinstance(part, dict) and "text" in part:
                text += part["text"]
        u = resp.get("usageMetadata", {}) or {}
        # Gemini reports thoughtsTokenCount separately for thinking models;
        # roll it into output side so cost tracking is accurate.
        tokens_out = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
        succeeded = bool(text) or finish in ("STOP", "MAX_TOKENS")
        if not succeeded:
            return BatchResult(
                custom_id=cid,
                succeeded=False,
                error=f"finish_reason={finish}; no text",
            )
        return BatchResult(
            custom_id=cid,
            succeeded=True,
            text=text,
            usage=BatchUsage(
                tokens_in=int(u.get("promptTokenCount") or 0),
                tokens_out=tokens_out,
                cache_read_tokens=int(u.get("cachedContentTokenCount") or 0),
                cache_write_tokens=0,
                service_tier="batch",
            ),
        )


# ── Helper: pick runner from profile.backend ───────────────────────────────

def make_runner(profile, *, token: str) -> BatchRunner:
    """Factory: return the right BatchRunner for profile.backend.

    Dispatch:
      - backend='anthropic'                 -> AnthropicBatchRunner
      - backend='openai' + Google URL host  -> GeminiBatchRunner (OAI shim)
      - other 'openai' backends              -> NotImplementedError

    Reason: 'openai' backend covers many vendors (real OpenAI, Gemini OAI
    shim, xAI, etc.). Each has its own batch API. We dispatch on URL host
    to disambiguate.
    """
    backend = getattr(profile, "backend", "openai")
    if backend == "anthropic":
        return AnthropicBatchRunner(profile, token=token)
    if backend == "openai":
        url = getattr(profile, "url", "") or ""
        if "generativelanguage.googleapis.com" in url:
            return GeminiBatchRunner(profile, token=token)
    raise NotImplementedError(
        f"batch_runner.make_runner: backend {backend!r} (url={getattr(profile, 'url', '')!r}) "
        f"not implemented yet. Currently supports: anthropic, gemini-via-OAI-shim. "
        f"Use the live (non-batch) path for other backends."
    )


# ── Convenience: high-level submit-poll-fetch loop ─────────────────────────

async def run_to_completion_chunked(
    runner: BatchRunner,
    requests: list[BatchRequest],
    *,
    client: httpx.AsyncClient,
    poll_interval_s: float = 30.0,
    max_wait_s_per_slice: float = 24 * 3600.0,
    on_status: Optional[Callable[..., Any]] = None,
    on_slice_start: Optional[Callable[..., Any]] = None,
    on_slice_end: Optional[Callable[..., Any]] = None,
):
    """Like run_to_completion, but transparently splits requests into
    slices of size <= runner.max_batch_size, submits each slice serially,
    and yields (batch_id, results) per slice as each completes.

    Use when len(requests) may exceed the runner's per-batch limit
    (Gemini=100, Anthropic=100K). Caller streams results as they land
    instead of waiting for the entire workload to finish — useful for
    state-machine update loops that ingest as soon as a slice ends.

    Yields tuple (slice_idx, batch_id, results) per completed slice.

    Raises TimeoutError per-slice (max_wait_s_per_slice).
    Raises RuntimeError per-slice on submit/poll/fetch HTTP failures —
    callers should catch and decide whether to retry or abort.
    """
    max_size = getattr(runner, "max_batch_size", 100_000)
    n_slices = (len(requests) + max_size - 1) // max_size
    for i in range(n_slices):
        slice_requests = requests[i * max_size : (i + 1) * max_size]
        if on_slice_start:
            try:
                on_slice_start(i, n_slices, len(slice_requests))
            except Exception:  # noqa: BLE001
                pass
        bid, results = await run_to_completion(
            runner, slice_requests, client=client,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s_per_slice,
            on_status=on_status,
        )
        if on_slice_end:
            try:
                on_slice_end(i, n_slices, bid, len(results))
            except Exception:  # noqa: BLE001
                pass
        yield i, bid, results


async def run_to_completion(
    runner: BatchRunner,
    requests: list[BatchRequest],
    *,
    client: httpx.AsyncClient,
    poll_interval_s: float = 30.0,
    max_wait_s: float = 24 * 3600.0,
    on_status: Optional[Callable[..., Any]] = None,
) -> tuple[str, list[BatchResult]]:
    """Submit a batch, poll until it ends, fetch all results, return them.

    Convenience wrapper for callers that just want results without
    managing the lifecycle themselves. Returns (batch_id, results).

    Pass on_status=callable(BatchStatus) to receive periodic progress
    callbacks during polling.

    Raises TimeoutError if max_wait_s elapses before the batch ends.
    Raises RuntimeError on submit/poll/fetch HTTP failures.
    """
    batch_id = await runner.submit(requests, client=client)
    deadline = time.monotonic() + max_wait_s
    while True:
        status = await runner.poll(batch_id, client=client)
        if on_status:
            try:
                on_status(status)
            except Exception:  # noqa: BLE001
                pass
        if status.state == "ended":
            break
        if status.state in ("canceled", "failed"):
            raise RuntimeError(
                f"batch {batch_id} ended in non-success state {status.state!r}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"batch {batch_id} did not complete within {max_wait_s}s"
            )
        await asyncio.sleep(poll_interval_s)
    results: list[BatchResult] = []
    async for r in runner.fetch_results(batch_id, client=client):
        results.append(r)
    return batch_id, results
