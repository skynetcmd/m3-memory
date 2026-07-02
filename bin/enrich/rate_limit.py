"""Rate-limit detection and cascade — _estimate_cost_wall, _classify_observer_error,
_is_rate_limit_failure, _RateLimitCascade."""
from __future__ import annotations

import time
from typing import Optional

from slm_intent import Profile


def _estimate_cost_wall(profile: Profile, n_groups: int) -> tuple[Optional[str], Optional[str]]:
    """Rough cost + wall estimate based on the profile's known rate card."""
    # Per-call assumption: ~700 input + 400 output tokens.
    rates = {
        "claude-haiku-4-5":   (1.0,  5.0,  1.5),    # ($/M_in, $/M_out, sec/call)
        "claude-sonnet-4-6":  (3.0, 15.0,  2.0),
        "gpt-4o-mini":        (0.15, 0.60, 1.5),
        "gpt-4o":             (2.5, 10.0,  2.0),
        "gemini-2.5-flash":   (0.075, 0.30, 2.0),
        "gemini-2.5-pro":     (1.25, 5.0,  3.0),
    }
    rate = rates.get(profile.model)
    if rate is None:
        # Local — assume free, ~3s per call.
        return ("$0 (local)", f"~{n_groups * 3 / 60:.1f} min")
    in_rate, out_rate, sec = rate
    cost = n_groups * (700 * in_rate / 1_000_000 + 400 * out_rate / 1_000_000)
    wall = n_groups * sec / 60
    return (f"~${cost:.2f}", f"~{wall:.1f} min")


def _classify_observer_error(exc: BaseException) -> str:
    """Map an Observer exception to a stable error_class for the state table.

    Deterministic classes (json_decode/tokenizer/oversize/schema) skip retries;
    everything else gets exponential backoff. See estate.DETERMINISTIC_ERROR_CLASSES.
    """
    name = type(exc).__name__
    msg = str(exc)
    if "JSONDecode" in name or "json" in msg.lower() and "decode" in msg.lower():
        return "json_decode"
    if "tokenizer" in msg.lower() or "tokeniz" in msg.lower():
        return "tokenizer_error"
    if "too large" in msg.lower() or "context length" in msg.lower() or "max_tokens" in msg.lower():
        return "content_too_large"
    if "TimeoutException" in name or "ReadTimeout" in name or "timeout" in msg.lower():
        return "http_timeout"
    if "ConnectError" in name or "ConnectTimeout" in name:
        return "http_connect"
    if "HTTPStatusError" in name or "status_code" in msg.lower():
        return "http_status"
    return "other"


def _is_rate_limit_failure(error_class: str, last_error: str) -> bool:
    """True iff this failure is the signature of an upstream rate-limit /
    quota wall (429), as opposed to per-group bugs.

    Looks for `http_status` plus a 429 marker in the message. Cloud
    providers spell the marker different ways ("429", "Too Many Requests",
    "RESOURCE_EXHAUSTED", "exceeded your current quota") so we match
    several. Anything that's *just* http_status without those markers is
    treated as a real per-group failure (bad request, server error, etc.)."""
    if error_class != "http_status":
        return False
    low = (last_error or "").lower()
    return any(m in low for m in (
        "429",
        "too many requests",
        "resource_exhausted",
        "exceeded your current quota",
        "rate limit",
        "ratelimitexceeded",
    ))


class _RateLimitCascade:
    """Detects sustained upstream rate-limit cascades and arms an abort.

    Trip condition: `threshold` consecutive rate-limit failures within
    `window_s` seconds. "Consecutive" means no successful call landed
    in between — record_success() resets the counter. This catches the
    quota-wall pattern (one shared upstream limit means every concurrent
    call fails together) without false-firing on isolated 429s during
    normal operation.

    Default 10 in 60s matches the cascade we observed on Gemini paid-tier
    on 2026-05-01: once the daily quota tripped, every subsequent call
    failed in tight succession until the run completed.
    """

    def __init__(self, threshold: int = 10, window_s: float = 60.0):
        self.threshold = threshold
        self.window_s = window_s
        # Timestamps of the most recent consecutive rate-limit failures.
        # Cleared on any success.
        self._fails: list[float] = []

    def record_failure(self, error_class: str, last_error: str) -> None:
        if not _is_rate_limit_failure(error_class, last_error):
            return
        now = time.monotonic()
        self._fails.append(now)
        # Drop entries older than the window so the deque stays bounded.
        cutoff = now - self.window_s
        self._fails = [t for t in self._fails if t >= cutoff]

    def record_success(self) -> None:
        self._fails.clear()

    def should_abort(self) -> bool:
        return len(self._fails) >= self.threshold

    def summary(self) -> str:
        n = len(self._fails)
        if not n:
            return "no rate-limit failures recorded"
        span = self._fails[-1] - self._fails[0] if n > 1 else 0
        return f"{n} rate-limit failures in the last {span:.1f}s"
