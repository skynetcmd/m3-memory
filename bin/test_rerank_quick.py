#!/usr/bin/env python3
"""Quick-feedback rerank smoke. Designed to fail in <30 seconds when broken.

Catches:
  - Default-off invariant break (rerank=False NOT byte-identical to pre-feature)
  - Lazy-load regression (importing memory_core triggering sentence_transformers)
  - Empty-content row crash (rerank chokes on rows with no text)
  - blend=0.0 no-op semantics break
  - Top-K monotonicity break (rerank produces non-monotonic scores)
  - sentence_transformers not installed (fails clearly, not silently)

Does NOT test:
  - End-to-end retrieval quality (that's the bench sweep's job)
  - Real model accuracy (orthogonal concern)
  - GPU vs CPU correctness (the underlying lib's job)

Run:
    python bin/test_rerank_quick.py

Exit: 0 = all pass; non-zero = first failure with diagnostic output.
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

REPO_BIN = Path(__file__).resolve().parent
if str(REPO_BIN) not in sys.path:
    sys.path.insert(0, str(REPO_BIN))


def _hr(label: str) -> None:
    print(f"\n--- {label} ---", flush=True)


def test_lazy_load_invariant() -> None:
    """Importing memory_core must NOT import sentence_transformers."""
    _hr("test_lazy_load_invariant")
    # Fresh module state — clear any prior import
    for mod in list(sys.modules):
        if mod.startswith("memory_core") or mod.startswith("sentence_transformers"):
            del sys.modules[mod]
    t0 = time.monotonic()
    import memory_core  # noqa: F401
    elapsed = time.monotonic() - t0
    in_modules = any(m.startswith("sentence_transformers") for m in sys.modules)
    print(f"  memory_core import time: {elapsed*1000:.1f}ms")
    print(f"  sentence_transformers loaded by import: {in_modules}")
    assert not in_modules, (
        "REGRESSION: importing memory_core triggered sentence_transformers import. "
        "This breaks cold-start for all callers that don't use rerank."
    )


def test_apply_rerank_default_off() -> None:
    """When blend=0.0, _apply_rerank returns input unmodified."""
    _hr("test_apply_rerank_default_off (blend=0.0 no-op)")
    import memory_core as mc
    hits = [
        (0.9, {"id": "a", "content": "user said apples"}),
        (0.8, {"id": "b", "content": "user said bananas"}),
        (0.7, {"id": "c", "content": "user said cherries"}),
    ]
    out = mc._apply_rerank(
        hits, "fruit",
        pool_k=10, final_k=3,
        model_name=mc.DEFAULT_RERANK_MODEL,
        blend=0.0,
    )
    print(f"  input top-1 score: {hits[0][0]}, output top-1: {out[0][0]}")
    assert out == hits[:3], (
        f"REGRESSION: blend=0.0 should be no-op. input={hits[:3]} output={out}"
    )


def test_apply_rerank_basic() -> None:
    """Rerank produces top-K with monotonic scores when blend=1.0."""
    _hr("test_apply_rerank_basic (blend=1.0, real CE call)")
    import memory_core as mc
    hits = [
        (0.5, {"id": "a", "content": "I love cooking pasta with garlic and olive oil."}),
        (0.5, {"id": "b", "content": "My favorite hobby is rock climbing on weekends."}),
        (0.5, {"id": "c", "content": "Pasta carbonara is simple but classic."}),
        (0.5, {"id": "d", "content": "I bought a new bicycle last week."}),
        (0.5, {"id": "e", "content": "The pasta was overcooked at the restaurant."}),
    ]
    t0 = time.monotonic()
    out = mc._apply_rerank(
        hits, "What did I cook?",
        pool_k=5, final_k=3,
        model_name=mc.DEFAULT_RERANK_MODEL,
        blend=1.0,
    )
    elapsed = time.monotonic() - t0
    print(f"  rerank wall: {elapsed*1000:.0f}ms (cold-load + 5 pairs)")
    assert len(out) == 3, f"expected 3 hits, got {len(out)}"
    # Monotonic
    for i in range(len(out) - 1):
        assert out[i][0] >= out[i + 1][0], (
            f"non-monotonic: {out[i][0]} < {out[i + 1][0]}"
        )
    # Top hit should be a pasta-related row (a, c, or e). Not a guarantee but
    # close to one for any non-broken cross-encoder.
    top_id = out[0][1]["id"]
    print(f"  top-1 id: {top_id}, score: {out[0][0]:.4f}")
    assert top_id in {"a", "c", "e"}, (
        f"top-1 should be pasta-related (a/c/e), got {top_id}"
    )


def test_apply_rerank_empty_content() -> None:
    """Rows with empty content don't crash — they keep hybrid score (CE=0)."""
    _hr("test_apply_rerank_empty_content")
    import memory_core as mc
    hits = [
        (0.9, {"id": "x", "content": ""}),       # empty content
        (0.8, {"id": "y", "content": "valid text about topic"}),
        (0.7, {"id": "z"}),                       # missing content key
    ]
    out = mc._apply_rerank(
        hits, "topic",
        pool_k=3, final_k=3,
        model_name=mc.DEFAULT_RERANK_MODEL,
        blend=0.5,
    )
    print(f"  output count: {len(out)} (expected 3)")
    assert len(out) == 3
    ids = {item.get("id") for _, item in out}
    assert ids == {"x", "y", "z"}, f"missing ids: {ids}"


def test_apply_rerank_pool_smaller_than_k() -> None:
    """When pool_k < final_k, never truncate below final_k."""
    _hr("test_apply_rerank_pool_smaller_than_k")
    import memory_core as mc
    hits = [(0.9 - i*0.1, {"id": chr(ord("a") + i), "content": f"row {i}"}) for i in range(5)]
    out = mc._apply_rerank(
        hits, "row",
        pool_k=2, final_k=5,  # pool < final
        model_name=mc.DEFAULT_RERANK_MODEL,
        blend=1.0,
    )
    print(f"  pool_k=2 final_k=5: got {len(out)} hits (must be >=5)")
    assert len(out) == 5, f"floor violated: pool_k=2, final_k=5, got {len(out)}"


def test_lazy_load_after_rerank_call() -> None:
    """After first _apply_rerank call, sentence_transformers IS loaded."""
    _hr("test_lazy_load_after_rerank_call")
    in_modules = any(m.startswith("sentence_transformers") for m in sys.modules)
    print(f"  sentence_transformers loaded after rerank: {in_modules}")
    assert in_modules, (
        "REGRESSION: _apply_rerank ran but sentence_transformers not in sys.modules. "
        "Lazy-load chain may be broken."
    )


def main() -> int:
    overall_t0 = time.monotonic()
    tests = [
        test_lazy_load_invariant,
        test_apply_rerank_default_off,
        test_apply_rerank_basic,
        test_apply_rerank_empty_content,
        test_apply_rerank_pool_smaller_than_k,
        test_lazy_load_after_rerank_call,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS")
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    elapsed = time.monotonic() - overall_t0
    print(f"\n{'=' * 60}")
    print(f"Total wall: {elapsed:.1f}s")
    if failed == 0:
        print(f"ALL {len(tests)} TESTS PASSED")
        return 0
    print(f"FAILED {failed}/{len(tests)} TESTS")
    return 1


if __name__ == "__main__":
    sys.exit(main())
