"""Sync must warn when a node and the warehouse embed with different families.

Regression for the CDW pull that silently mixed vector spaces (bge-m3 vs
qwen3/nomic), making cosine ranking meaningless. The guardrail warns loudly (and
lets the sync proceed) so the operator converges the fleet on one family.

Crucially, the two tags of ONE model — the in-process GGUF and the LM Studio
server tag — must NOT look like a mismatch.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import pg_sync  # noqa: E402


class _FakeCur:
    def __init__(self, tags):
        self._tags = tags

    def execute(self, q):  # noqa: D401 — mimic DB-API
        self._q = q

    def fetchall(self):
        return [(t,) for t in self._tags]


def test_same_family_dual_tags_do_not_warn(caplog):
    local = _FakeCur(["bge-m3-GGUF-Q4_K_M.gguf", "text-embedding-bge-m3"])
    remote = _FakeCur(["text-embedding-bge-m3"])
    with caplog.at_level(logging.WARNING, logger=pg_sync.logger.name):
        pg_sync._warn_on_embed_family_mismatch(local, remote, "T")
    assert "MISMATCH" not in caplog.text


def test_different_families_warn(caplog):
    local = _FakeCur(["text-embedding-bge-m3"])
    remote = _FakeCur(["qwen3-embedding"])
    with caplog.at_level(logging.WARNING, logger=pg_sync.logger.name):
        pg_sync._warn_on_embed_family_mismatch(local, remote, "T")
    assert "EMBED-FAMILY MISMATCH" in caplog.text
    assert "bge-m3" in caplog.text and "qwen3" in caplog.text


def test_empty_store_does_not_warn(caplog):
    # A first sync into an empty warehouse must not cry mismatch.
    local = _FakeCur(["bge-m3"])
    remote = _FakeCur([])
    with caplog.at_level(logging.WARNING, logger=pg_sync.logger.name):
        pg_sync._warn_on_embed_family_mismatch(local, remote, "T")
    assert "MISMATCH" not in caplog.text


def test_family_scan_survives_query_error():
    class Boom:
        def execute(self, q):
            raise RuntimeError("no table")

        def fetchall(self):
            return []

    assert pg_sync._embed_families(Boom()) == set()
