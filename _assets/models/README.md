# `_assets/models/` — Bundled embedder model files

The sovereign CPU embedder (m3-embed-server on port 8082) needs a GGUF model
file. m3 ships **bge-m3-Q4_K_M.gguf** here, tracked via **Git LFS** because
the file is ~438 MB — far too big for a plain Git blob.

## What's bundled

| File | Size | Purpose |
|---|---|---|
| `bge-m3-Q4_K_M.gguf` | ~438 MB | BGE-M3 embedding model, Q4_K_M quant. The sovereign default. |

The Q4_K_M quant keeps full BGE-M3 retrieval quality while staying small
enough to run on CPU at 30–80 embeddings/sec on modern hardware.

## How LFS works here

- `.gitattributes` declares `_assets/models/*.gguf filter=lfs ...`, so any
  GGUF added to this directory is automatically tracked via Git LFS.
- A normal `git clone` without LFS gives you a **pointer file** (~130 bytes,
  starts with `version https://git-lfs.github.com/...`) instead of the real
  bytes.
- `m3 embedder install` detects pointer files and prints actionable
  guidance: install LFS, then `git lfs pull`.

## Materializing the model

```bash
# One-time per machine — installs LFS hooks into your Git config:
git lfs install

# Inside the m3-memory checkout — fetches the actual bytes:
git lfs pull
```

After that the file is fully materialized at `_assets/models/bge-m3-Q4_K_M.gguf`
and `m3 embedder install` will pick it up automatically.

## Why LFS, not pip-bundle or lazy-download

We considered three options and picked LFS:

| Option | Why not |
|---|---|
| **Bundle in the wheel** | Wheel size ~440 MB; PyPI has a 100 MB per-file cap by default. Asking for a waiver per release is friction. |
| **Lazy fetch from HuggingFace on first `m3 embedder install`** | Not air-gapped without an extra dance. Source-of-truth is a third-party CDN. |
| **Git LFS (chosen)** | Repo stays small for users who don't need it (skip LFS pull). Source-of-truth is our repo. Air-gapped by default for anyone who runs `git lfs pull` once on a connected machine and `scp`s the checkout. |

## Adding more models

To bundle another GGUF (e.g. a reranker model), just put the file in this
directory and commit. The `*.gguf` LFS filter pattern catches it
automatically — no `.gitattributes` change needed.

If you bundle a non-GGUF asset that's large, add a new pattern under
`_assets/models/` in `.gitattributes` (e.g. `*.safetensors filter=lfs ...`).

## Cross-references

- `m3_memory/embedder_admin.py` — `_find_bundled_gguf()` does the resolution.
- `docs/EMBED_DEPLOYMENT.md` — full embedder architecture, build matrix.
- `docs/SOVEREIGN_DEPLOYMENT.md` — air-gapped install recipe.
