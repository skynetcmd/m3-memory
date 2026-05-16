"""Integration smoke: dense-recovery path on the two known overflow rows.

READ-ONLY against the chatlog DB. Pulls the two memory_ids that we know
trip the bge-m3 8192-token ceiling at the default 28000-char window
size, runs them through _chunk_for_sliding_window + the new
_subdivide_dense_chunk path, and verifies every sub-chunk embeds
successfully.

No DB writes; this exercises the recovery LOGIC, not the SQL inserts.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

os.environ.setdefault("GGML_CUDA_DISABLE_GRAPHS", "1")
os.environ.setdefault(
    "M3_EMBED_GGUF",
    r"C:\Users\bhaba\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf",
)
os.environ.setdefault("M3_EMBED_STREAMS", "1")
os.environ.setdefault("M3_EMBED_CTX", "8192")
os.environ.setdefault("M3_EMBED_SEQ_MAX", "8")
os.environ.setdefault("M3_EMBED_N_BATCH", "8192")
os.environ.setdefault("M3_EMBED_N_UBATCH", "8192")

import memory_core as mc  # noqa: E402
import m3_core_rs  # noqa: E402


CHATLOG_DB = Path(r"C:\Users\bhaba\m3-memory\memory\agent_chatlog.db")
KNOWN_BAD_IDS = [
    "778e7500",  # 39331 chars, observed 16875 tokens at 28000c
    "7127bb1e",  # 30072 chars, observed 9735 tokens at 28000c
]


def find_full_ids() -> list[tuple[str, str]]:
    """Return (full_id, content) for the known-bad prefixes."""
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    out = []
    for prefix in KNOWN_BAD_IDS:
        cur.execute(
            "SELECT id, content FROM memory_items WHERE id LIKE ? AND is_deleted=0",
            (prefix + "%",),
        )
        row = cur.fetchone()
        if row:
            out.append(row)
        else:
            print(f"  WARN: prefix {prefix} not found")
    c.close()
    return out


def main():
    print(f"Config: MAX={mc.MAX_CHARS_PER_CHUNK} OVL={mc.MIN_OVERLAP_CHARS}")
    print(f"        DENSE_TARGET_TOKENS={mc.DENSE_TARGET_TOKENS} DENSE_MIN_SUB_CHARS={mc.DENSE_MIN_SUB_CHARS}")
    print(f"DB (read-only): {CHATLOG_DB}\n")

    rows = find_full_ids()
    if not rows:
        print("No known-bad rows found in DB; nothing to test.")
        sys.exit(1)

    print(f"Loading in-process Rust embedder...")
    t0 = time.perf_counter()
    embedder = m3_core_rs.EmbeddedEmbedder(os.environ["M3_EMBED_GGUF"])
    dim = embedder.embedding_dim()
    print(f"  loaded in {time.perf_counter()-t0:.1f}s, dim={dim}, backend={m3_core_rs.embed_backend_label()}\n")

    overall_pass = True

    for mid, content in rows:
        print(f"=== {mid[:8]} ({len(content)} chars) ===")
        # First, chunking
        chunks = mc._chunk_for_sliding_window(content)
        print(f"  initial chunks: {len(chunks)}")
        for ct, idx in chunks:
            print(f"    chunk[{idx}]: {len(ct)} chars")

        # For each chunk, try embedding. On dense overflow, run subdivide+retry.
        all_subs_succeed = True
        for ct, idx in chunks:
            try:
                v = embedder.embed([ct])[0]
                if v and len(v) == dim:
                    print(f"  chunk[{idx}]: OK (no recovery needed)")
                    continue
            except Exception as e:
                err = str(e)
                m = mc._DENSE_ERR_RE.search(err)
                if not m:
                    print(f"  chunk[{idx}]: NON-DENSE failure: {err[:120]}")
                    all_subs_succeed = False
                    continue
                observed_tokens = int(m.group(1))
                print(f"  chunk[{idx}]: dense overflow at {observed_tokens} tokens "
                      f"({len(ct)/observed_tokens:.2f} c/t)")
                subs = mc._subdivide_dense_chunk(ct, observed_tokens)
                print(f"    subdivided into {len(subs)} sub-chunks: "
                      f"{[len(s) for s in subs]}")
                # Try each sub-chunk
                for j, sub in enumerate(subs):
                    try:
                        sv = embedder.embed([sub])[0]
                        if sv and len(sv) == dim:
                            print(f"      sub[{j}]: OK ({len(sub)} chars)")
                        else:
                            print(f"      sub[{j}]: bad vec dim {len(sv) if sv else 'None'}")
                            all_subs_succeed = False
                    except Exception as se:
                        print(f"      sub[{j}]: STILL FAILED ({len(sub)} chars): {se}")
                        all_subs_succeed = False

        if all_subs_succeed:
            print(f"  RESULT: {mid[:8]} fully recovered\n")
        else:
            print(f"  RESULT: {mid[:8]} PARTIAL — some chunks/sub-chunks failed\n")
            overall_pass = False

    print("=" * 50)
    if overall_pass:
        print("ALL KNOWN-BAD ROWS RECOVERED SUCCESSFULLY")
        sys.exit(0)
    else:
        print("SOME ROWS STILL HAD UNRECOVERED FAILURES")
        sys.exit(1)


if __name__ == "__main__":
    main()
