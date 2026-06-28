"""Parity tests for file_content_sha256_batch (M4 ingest oxidation).

The batch hasher routes through native m3_core_rs.hash_files when available
(rayon-parallel) and falls back to a per-file Python loop. Either way its
per-path digest must equal the single-file file_content_sha256, and unreadable
paths must map to None rather than raising.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from files_memory.identity import file_content_sha256, file_content_sha256_batch  # noqa: E402


def test_batch_matches_single(tmp_path):
    files = []
    for i in range(5):
        p = tmp_path / f"f{i}.txt"
        p.write_bytes(f"content-{i}".encode() * (i + 1))
        files.append(str(p))

    batch = file_content_sha256_batch(files)
    for p in files:
        assert batch[p] == file_content_sha256(p)


def test_empty_input_returns_empty():
    assert file_content_sha256_batch([]) == {}


def test_unreadable_file_maps_to_none(tmp_path):
    good = tmp_path / "good.txt"
    good.write_bytes(b"ok")
    missing = str(tmp_path / "does_not_exist.txt")

    out = file_content_sha256_batch([str(good), missing])
    assert out[str(good)] == file_content_sha256(good)
    assert out[missing] is None


def test_fallback_path_matches_native(tmp_path, monkeypatch):
    """Forcing M3_CORE_RS_DISABLE=1 (pure-Python path) yields identical
    digests to the default path — the fallback is a true drop-in."""
    files = []
    for i in range(3):
        p = tmp_path / f"g{i}.bin"
        p.write_bytes(bytes(range(256)) * (i + 1))
        files.append(str(p))

    default_out = file_content_sha256_batch(files)

    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    forced_python = file_content_sha256_batch(files)

    assert default_out == forced_python
    for p in files:
        assert forced_python[p] == file_content_sha256(p)
