"""Regression test: the embedded llama.cpp embed path must not trip
llama.cpp's silent fallback for CLS pooling / non-causal embedding.

Two warnings used to fire on EVERY embed call (209/209 in the original
repro), driven by:

  - `LlamaBatch::add_sequence(.., logits_all=false)` — wrong for CLS pooling;
    llama.cpp logs `init: embeddings required but some input tokens were not
    marked as outputs -> overriding`.
  - `LlamaContext::decode()` — wrong for non-causal BERT; llama.cpp logs
    `decode: cannot decode batches with this context (calling encode()
    instead)` and re-routes.

The fix in `crates/m3-embed-llamacpp/src/lib.rs` flips both. This test
captures stderr of a small subprocess workload, asserts neither warning
appears, and asserts the workload completes well under the pre-fix
per-call cost.

Skips cleanly if M3_TEST_GGUF is unset.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

GGUF = os.environ.get("M3_TEST_GGUF")


def _child_program() -> str:
    return textwrap.dedent(
        """
        import os, sys, threading, time
        import m3_core_rs

        gguf = os.environ['M3_TEST_GGUF']
        emb = m3_core_rs.EmbeddedEmbedder(gguf, warmup=True)

        # Prime: one embed before timing so model + worker contexts are
        # fully resident. The fallback warnings (if any) would still fire
        # on every subsequent call, so they're caught regardless.
        prime_call = emb.embed_batch if hasattr(emb, 'embed_batch') else emb.embed
        prime_call(['prime'])

        # 2 threads x 5 calls x 4-text batch = 10 embed calls, 40 texts.
        N_THREADS = 2
        N_CALLS = 5
        BATCH = ['hello world ' + str(i) for i in range(4)]

        def worker():
            for _ in range(N_CALLS):
                if hasattr(emb, 'embed_batch'):
                    emb.embed_batch(BATCH)
                else:
                    emb.embed(BATCH)

        ths = [threading.Thread(target=worker, name=f't{i}') for i in range(N_THREADS)]
        t0 = time.perf_counter()
        for t in ths: t.start()
        for t in ths: t.join()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        sys.stdout.write(f'OK elapsed_ms={elapsed_ms:.1f}\\n')
        sys.stdout.flush()
        """
    )


@pytest.mark.skipif(not GGUF, reason="M3_TEST_GGUF unset")
def test_no_llama_fallback_warnings_in_stderr() -> None:
    """Run a 10-call concurrent workload in a subprocess; assert clean stderr."""
    env = os.environ.copy()
    env["M3_TEST_GGUF"] = GGUF or ""
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [sys.executable, "-c", _child_program()],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    # Embed-only elapsed is reported by the child after subtracting model
    # load + warmup + prime cost — that's the wall-time we want to bound.
    elapsed_ms = 0.0
    for line in proc.stdout.splitlines():
        if "elapsed_ms=" in line:
            try:
                elapsed_ms = float(line.split("elapsed_ms=", 1)[1].strip())
            except ValueError:
                pass

    assert proc.returncode == 0, (
        f"child failed (rc={proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK" in proc.stdout, f"child did not report OK: {proc.stdout!r}"

    stderr = proc.stderr
    overriding_count = stderr.count("init: embeddings required")
    decode_fallback_count = stderr.count("cannot decode batches with this context")

    assert overriding_count == 0, (
        f"CLS-pool fix regressed: 'init: embeddings required ... overriding' "
        f"appeared {overriding_count}x in stderr (should be 0). "
        f"Check add_sequence(.., logits_all=true) in m3-embed-llamacpp."
    )
    assert decode_fallback_count == 0, (
        f"encode-vs-decode fix regressed: 'cannot decode batches' appeared "
        f"{decode_fallback_count}x in stderr (should be 0). "
        f"Check ctx.encode(batch) in m3-embed-llamacpp."
    )
    assert elapsed_ms < 5000.0, (
        f"throughput regression: 10 concurrent embed calls took "
        f"{elapsed_ms:.0f} ms (pre-fix ~10s+ under load, post-fix should "
        f"be well under 5s). Suspected per-call allocator churn."
    )
