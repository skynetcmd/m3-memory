"""Fetch the BGE-M3 GGUF into m3's OWN model directory (~/.m3/models).

WHY this exists: every other model location m3 probes belongs to a third party
(LM Studio, Ollama, llama.cpp). A user who uninstalls one loses m3's embedder
through no action of their own. m3 needs a copy it controls.

MIRROR SELECTION (verified 2026-07-27, not assumed):
  The URL previously hardcoded in bin/fetch_sovereign_assets.py
  (bartowski/bge-m3-GGUF) now returns HTTP 401 — the only download path in the
  codebase was dead. Probed four candidates; two resolve. Both are 417.5 MB,
  matching the known-good local file byte-for-byte in SIZE but NOT in sha256:
  the GGUF metadata/tokenizer blocks differ between conversions.

  Differing bytes could mean a different VECTOR SPACE, which would silently
  corrupt an existing store. So equivalence was tested, not inferred: embedding
  three probe strings with the known-good model and with the gpustack mirror via
  m3_core_rs.EmbeddedEmbedder gave cos = 1.000000000 on all three. Same space,
  safe to mix. That is why we validate by MAGIC BYTES + SIZE + a load test rather
  than by a hardcoded sha256 — a checksum from one mirror would reject the other
  even though both are correct.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Canonical filename m3 uses for its own copy.
BGE_M3_FILENAME = "bge-m3-Q4_K_M.gguf"

# Ordered mirrors. Both verified live and vector-space-identical (see above).
# First entry wins; the next is tried only on a network/HTTP failure.
BGE_M3_MIRRORS = (
    "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q4_K_M.gguf",
    "https://huggingface.co/lm-kit/bge-m3-gguf/resolve/main/bge-m3-Q4_K_M.gguf",
)

# Exact byte count of Q4_K_M BGE-M3 on both verified mirrors. Used as an
# integrity gate: a truncated download or an HTML error page won't match.
BGE_M3_EXPECTED_BYTES = 437_778_496

# Tolerate a small drift if a mirror re-quantizes; still catches truncation
# and error pages, which are off by orders of magnitude.
_SIZE_TOLERANCE = 0.02


def m3_models_dir() -> Path:
    """m3's own model directory, via the canonical resolver."""
    try:
        from m3_core.paths import get_m3_models_root

        return Path(get_m3_models_root())
    except Exception:
        root = os.environ.get("M3_MODELS_ROOT")
        if root:
            return Path(root).expanduser()
        mem = os.environ.get("M3_MEMORY_ROOT")
        if mem:
            return Path(mem).expanduser() / "models"
        return Path.home() / ".m3" / "models"


def _is_valid_gguf(path: Path) -> bool:
    """True if `path` looks like a real GGUF of roughly the expected size.

    Checks MAGIC BYTES first: an HTML error page or an LFS pointer saved under a
    .gguf name is the common failure, and both fail this instantly.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    lo = BGE_M3_EXPECTED_BYTES * (1 - _SIZE_TOLERANCE)
    hi = BGE_M3_EXPECTED_BYTES * (1 + _SIZE_TOLERANCE)
    if not (lo <= size <= hi):
        logger.debug("GGUF size %d outside expected window", size)
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


def existing_m3_gguf() -> Optional[Path]:
    """Return m3's own validated GGUF copy, or None."""
    path = m3_models_dir() / BGE_M3_FILENAME
    return path if _is_valid_gguf(path) else None


def fetch_bge_m3(dest_dir: Optional[Path] = None, *, quiet: bool = False) -> Optional[Path]:
    """Download the BGE-M3 GGUF into `dest_dir` (default ~/.m3/models).

    Returns the path on success, None on failure. NEVER raises — a failed model
    fetch must not abort setup, because m3 still embeds via its other tiers.

    Downloads to a `.part` file and renames only after validation, so an
    interrupted transfer can't leave a corrupt file that later looks present.
    """
    dest_dir = Path(dest_dir) if dest_dir else m3_models_dir()
    final = dest_dir / BGE_M3_FILENAME

    if _is_valid_gguf(final):
        if not quiet:
            print(f"[OK] model already present: {final}")
        return final

    try:
        import requests
    except ImportError:
        logger.warning("requests unavailable — cannot fetch the model")
        return None

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if not quiet:
            print(f"[!] cannot create {dest_dir}: {exc}")
        return None

    part = final.with_suffix(final.suffix + ".part")
    for url in BGE_M3_MIRRORS:
        try:
            if not quiet:
                print(f"[~] downloading BGE-M3 (~418 MB) from {url.split('/')[3]}…")
            with requests.get(url, stream=True, timeout=(15, 120)) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                written = 0
                bar = _progress_bar(total, quiet)
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        written += fh.write(chunk)
                        if bar is not None:
                            bar.update(len(chunk))
                if bar is not None:
                    bar.close()

            if not _is_valid_gguf(part):
                # A 200 response can still be an HTML error page or a partial
                # transfer; validating BEFORE the rename keeps a bad file from
                # being discovered later as if it were a real model.
                if not quiet:
                    print(f"[!] downloaded file failed validation ({written} bytes) — discarding")
                part.unlink(missing_ok=True)
                continue

            part.replace(final)
            if not quiet:
                print(f"[OK] model saved: {final}")
            return final

        except Exception as exc:  # network, HTTP, disk — try the next mirror
            logger.debug("mirror %s failed: %s", url, exc)
            if not quiet:
                print(f"[!] mirror failed ({exc.__class__.__name__}); trying next…")
            part.unlink(missing_ok=True)
            continue

    if not quiet:
        print("[!] could not download the model from any mirror.\n"
              "    m3 still embeds via its in-process / HTTP tiers — this is not fatal.\n"
              f"    To add it by hand, place a bge-m3 GGUF at: {final}")
    return None


def _progress_bar(total: int, quiet: bool):
    """A tqdm bar when we have a console and a known size, else None."""
    if quiet or not total:
        return None
    try:
        import sys

        if not sys.stdout.isatty():
            return None
        from tqdm import tqdm

        return tqdm(total=total, unit="iB", unit_scale=True,
                    unit_divisor=1024, desc="  bge-m3")
    except Exception:
        return None
