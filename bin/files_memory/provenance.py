"""Original-vs-processed file provenance.

Many ingestable files are the *processed* output of a conversion pass
(`paper.pdf` → `paper.pdf.txt` via pdftotext, scan.png → scan.txt via
OCR, video.mp4 → transcript.txt via whisper). The user's mental model
points at the **original**, not the converted text we mined. This
module records the link so search results can cite the right artifact.

Two ways to declare the original-path link:

1. CLI flag at ingest time:
       files_ingest <path> --original-path <orig>
   Applied to every file in the walk. Useful for batch conversions
   where one rule covers many files.

2. Sidecar file next to the ingested file:
       paper.pdf.txt          ← the file we ingest
       paper.pdf.txt.m3meta.json  ← sidecar
   Sidecar JSON shape (all fields optional except original_path):
       {
         "original_path": "/abs/path/to/paper.pdf",
         "conversion": {
           "tool":       "pdftotext",
           "version":    "24.07.0",
           "params":     "-layout -nopgbrk",
           "converted_at": "2026-05-17T...",
           "checksum":   "<sha256 of original, optional>"
         },
         "m3_doc_id": "<optional explicit identity_key override>"
       }
   Sidecar wins over the CLI flag when both are present — it's per-file
   intent, the flag is bulk default.

No schema change: the data lives in file_nodes.metadata JSON under the
"provenance" key. Readers (files_search, files_get, files_index) check
for it and surface original_path when present.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("files_memory.provenance")


SIDECAR_SUFFIX = ".m3meta.json"


def find_sidecar(path: str) -> Optional[Path]:
    """Return the sidecar path if it exists, else None.

    The sidecar is at `<path>.m3meta.json`. E.g. `paper.pdf.txt` →
    `paper.pdf.txt.m3meta.json`.
    """
    sidecar = Path(str(path) + SIDECAR_SUFFIX)
    return sidecar if sidecar.is_file() else None


def load_sidecar(path: str) -> Optional[dict]:
    """Read and parse the sidecar JSON for `path`. None if missing/invalid.

    On parse error we log + return None rather than failing the ingest —
    a broken sidecar shouldn't lose the file.
    """
    sidecar = find_sidecar(path)
    if sidecar is None:
        return None
    try:
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("sidecar %s is not a JSON object; ignoring", sidecar)
            return None
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read sidecar %s: %s", sidecar, e)
        return None


def resolve_provenance(
    path: str,
    *,
    cli_original_path: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the provenance record for a file.

    Resolution order:
      1. Sidecar at <path>.m3meta.json (per-file, wins)
      2. CLI flag passed to files_ingest (bulk default)

    Returns None when neither applies — file is its own original.

    Returns a dict with at least 'original_path' (absolute, normalized).
    May include 'conversion' (dict), 'm3_doc_id' (str), or arbitrary
    additional keys preserved from the sidecar.
    """
    sidecar = load_sidecar(path)
    if sidecar:
        original = sidecar.get("original_path")
        if not original:
            # Sidecar exists but has no original_path — treat as no provenance.
            return None
        record = dict(sidecar)
        record["original_path"] = _normalize_original_path(original, ingested_path=path)
        record["_source"] = "sidecar"
        return record

    if cli_original_path:
        return {
            "original_path": _normalize_original_path(cli_original_path, ingested_path=path),
            "_source": "cli",
        }

    return None


def _normalize_original_path(original: str, *, ingested_path: str) -> str:
    """Return an absolute path. Relative originals resolve against the
    directory of `ingested_path` so the sidecar can be portable."""
    if os.path.isabs(original):
        return os.path.abspath(original)
    base = os.path.dirname(os.path.abspath(ingested_path))
    return os.path.abspath(os.path.join(base, original))


def original_path_for_metadata(metadata_json: str | dict | None) -> Optional[str]:
    """Convenience reader: pull original_path out of a file_node.metadata blob.

    Accepts either a JSON string (typical when reading from sqlite) or
    an already-decoded dict. Returns None if not set.
    """
    if not metadata_json:
        return None
    if isinstance(metadata_json, str):
        try:
            metadata_json = json.loads(metadata_json)
        except (ValueError, TypeError):
            return None
    if not isinstance(metadata_json, dict):
        return None
    prov = metadata_json.get("provenance")
    if not isinstance(prov, dict):
        return None
    return prov.get("original_path")
