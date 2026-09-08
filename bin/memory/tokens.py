"""Token budgeting for the embed pipeline — the single authority on "how long
is this text, in the units the embedder actually cares about".

Split out per DESIGN_PHILOSOPHIES §2: pure functions over their arguments, no
module-level MUTABLE state (the constants below are read once from os.environ
and never reassigned), so this module is safe to unit-test with no database, no
embedder and no network. Do NOT import `embed` or `chunking` here — both import
this, and the reverse edge would be a cycle.

WHY THIS MODULE EXISTS
----------------------
Every length guard in m3 measured CHARACTERS or BYTES and compared the result
against a TOKEN ceiling (bge-m3's n_ctx, 8192). Those units are not
interchangeable: measured against the real bge-m3 tokenizer, density spans 4x
across content a single corpus routinely mixes.

    content type      chars/token   bytes/token   tokens @28000 chars
    English prose         4.18          4.18          6,699   ok
    Python code           2.96          3.15          9,459   OVER
    logs / stack traces   2.26          2.26         12,389   OVER
    Chinese               1.66          4.88         16,867   OVER
    JSON                  1.63          1.63         17,178   OVER
    UUID lists            1.61          1.61         17,391   OVER
    base64                1.00          1.00         28,000   OVER

Only English prose was safe at the legacy defaults, which is not a coincidence:
`32768 == 8192 * 4` is the English ratio written down as a constant. Note JSON
and UUID lists are DENSER per character than Chinese, so this is not a CJK
problem — a corpus of code and structured logs hits it with no CJK at all.

THE ESTIMATOR AND ITS PROOF
---------------------------
`estimate_tokens` returns ``max(bytes/3, chars) + SPECIAL_TOKENS``. The `chars`
term is the load-bearing one: **bge-m3 uses SentencePiece, which cannot emit
more CONTENT tokens than there are characters**, so `chars` is a hard upper
bound on the content for ANY input, not merely for sampled ones.

The ``+ SPECIAL_TOKENS`` is not decoration. bge-m3 wraps every sequence in
BOS+EOS (`AddBos::Always` in the Rust embedder), so a text of N characters can
tokenize to N+2. Measured: 8000 chars of base64 -> 12,002 actual tokens against
a bare-`chars` estimate of 12,000. **A "provably safe" bound that omits the
special tokens under-estimates by exactly 2 — enough to overflow a row sitting
on the boundary.** The bound is on content; the frame has to be added back.

Formulas that look reasonable and are NOT safe, measured against the real
tokenizer (each under-estimates base64, which would still overflow n_ctx):

    max(bytes/4, chars/1.1)       worst ratio 0.91   UNSAFE
    max(bytes/3, chars/1.05)      worst ratio 0.95   UNSAFE
    max(bytes/3, chars)           worst ratio 1.00   UNSAFE by 2 tokens
    max(bytes/3, chars) + 2       worst ratio >1.00  SAFE

The cost of safety is over-chunking English by ~3.7x. That is why exact
counting (`count_tokens`, backed by the Rust core's tokenizer) matters: it
removes the penalty. The estimator is the floor that keeps the system CORRECT
with no wheel, no server and no network (§1 offline-capable); exactness is an
optimisation layered on top, never a requirement.

⚠ MODEL ASSUMPTION: the `chars` ceiling holds for SentencePiece/bge-m3. A
byte-level BPE tokenizer CAN emit more than one token per character, so a model
swap must revisit this bound. `tests/test_token_budget.py` asserts the
assumption so the swap fails loudly instead of silently under-estimating.
"""
from __future__ import annotations

import os
from typing import Callable, Optional, Sequence

from . import config

# ──────────────────────────────────────────────────────────────────────────────
# Budget constants (read-only config: set once at import, never reassigned)
# ──────────────────────────────────────────────────────────────────────────────
# The model's hard ceiling. Rows above this cannot be embedded whole at all.
# Mirrors M3_EMBED_CTX, which the Rust core reads for the same purpose
# (crates/m3-core-py/src/config.rs: embed_ctx(), default 8192).
#
# ⚠ Do NOT raise this to "fit more in". bge-m3 is TRAINED to 8192 positions, so
# beyond that you are extrapolating past the model rather than gaining capacity,
# and KV cost is ~96 KB/token — 16384 takes a 4-stream pool from ~3.0 GB to
# ~6.0 GB for embeddings that are getting worse, not better.
TOKEN_CEILING: int = int(os.environ.get("M3_EMBED_CTX", "8192"))

# The target we chunk to, leaving headroom under the ceiling for:
#   - anchor augmentation (`[2026-05-16, 2026-Q2] ` prepended at write time;
#     measured max 3 anchors ~= 38 chars in the live corpus, but the field is
#     unbounded in principle, which is why the budget is computed on the
#     POST-augmentation text — see memory/write.py),
#   - the estimator's own slack on mixed-density content.
TOKEN_BUDGET: int = int(os.environ.get("M3_EMBED_TOKEN_BUDGET", "7000"))

# Overlap between adjacent windows, in tokens. Preserves the legacy ~28%
# ratio (8000/28000 chars) so retrieval across a chunk seam does not degrade.
TOKEN_OVERLAP: int = int(os.environ.get("M3_EMBED_TOKEN_OVERLAP", "2000"))

# Bytes-per-token divisor for the estimator's byte term. Deliberately 3 (not 4):
# 4 is the English ratio and under-estimates every denser content type.
_BYTES_PER_TOKEN_FLOOR: int = 3

# Special tokens bge-m3 wraps every sequence in (BOS + EOS; `AddBos::Always` in
# crates/m3-embed-llamacpp). Measured exactly 2, independent of input length —
# including for the empty string, which encodes to 2 tokens, not 0. They count
# against n_ctx like any other token, so the estimator must include them or a
# row on the boundary overflows by exactly this much.
SPECIAL_TOKENS: int = 2


def estimate_tokens(text: str) -> int:
    """Conservative token count that provably never under-estimates (bge-m3).

    ``max(bytes/3, chars) + SPECIAL_TOKENS`` — see the module docstring for the
    proof and for the measured formulas that are NOT safe. Pure arithmetic:
    ~5 us per 1024 rows, versus ~1,592 ms for exact tokenization in Python
    (300x), which is why this is the always-available floor rather than a
    fallback of last resort.

    Empty input still costs ``SPECIAL_TOKENS`` — the frame is emitted even with
    no content, and reporting 0 would be a lie the caller might budget against
    (§3: a zero is only correct when it is TRUE).
    """
    if not text:
        return SPECIAL_TOKENS
    n_bytes = len(text.encode("utf-8"))
    n_chars = len(text)
    return max(n_bytes // _BYTES_PER_TOKEN_FLOOR, n_chars) + SPECIAL_TOKENS


def _rust_count_tokens(texts: Sequence[str]) -> "Optional[list[int]]":
    """Exact counts from the Rust core, or None when it cannot serve them.

    Routed through ``config.m3_core_rs`` (NOT a direct ``import m3_core_rs``) so
    ``M3_CORE_RS_DISABLE=1`` covers this path like every other oxidized one — a
    direct import would silently bypass the kill switch and leave the system in
    a partially-oxidized state that no user actually runs.

    Probed with ``hasattr`` rather than catching AttributeError: an older
    installed wheel simply lacks the binding, and a bare except is what once
    masked a stale wheel falling back to Python for days (§3 fail loud).
    """
    rs = config.m3_core_rs
    if rs is None or not hasattr(rs, "count_tokens"):
        return None
    try:
        counts = rs.count_tokens(list(texts))
    except Exception:  # noqa: BLE001 — a genuine FFI hiccup falls back to the estimator
        return None
    if not isinstance(counts, (list, tuple)) or len(counts) != len(texts):
        return None  # shape mismatch: do not trust a partial answer
    return [int(c) for c in counts]


def count_tokens(
    texts: Sequence[str],
    *,
    exact_fn: "Optional[Callable[[Sequence[str]], Optional[list[int]]]]" = None,
) -> "list[int]":
    """Token counts for a batch — exact when reachable, conservative otherwise.

    Cascade, mirroring the embed path's own tier structure so the shape is
    familiar rather than novel:

        1. the Rust core's tokenizer (exact),
        2. ``exact_fn`` if the caller can supply one (e.g. an HTTP
           ``/tokenize/count`` on a standalone embed server),
        3. :func:`estimate_tokens` (conservative, always available).

    Every tier is OPTIONAL. Correctness never depends on the wheel, the server
    or the network (§1) — the tiers only make the budget tighter, which buys
    back the ~3.7x over-chunking the estimator costs on English prose.

    Always returns one count per input, in input order.
    """
    if not texts:
        return []
    counts = _rust_count_tokens(texts)
    if counts is not None:
        return counts
    if exact_fn is not None:
        try:
            counts = exact_fn(texts)
        except Exception:  # noqa: BLE001 — remote counter down: estimate instead
            counts = None
        if counts is not None and len(counts) == len(texts):
            return [int(c) for c in counts]
    return [estimate_tokens(t) for t in texts]


def token_budget(text: str) -> int:
    """Token length of a single text — the one call sites should use.

    Thin wrapper over :func:`count_tokens` so a guard reads as an intent
    (`token_budget(row) > TOKEN_CEILING`) rather than as an encoding detail.
    """
    return count_tokens([text])[0]


def fits(text: str, limit: "Optional[int]" = None) -> bool:
    """True if ``text`` is within ``limit`` tokens (default: :data:`TOKEN_BUDGET`)."""
    return token_budget(text) <= (TOKEN_BUDGET if limit is None else limit)
