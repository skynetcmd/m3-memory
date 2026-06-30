"""Tests for the embedder-identity gate (_validate_identity) and its config.

A vector is only acceptable for the store if it came from the configured
("proper") embedder: right dimension, a compatible model tag, and (when
required) unit-length. A tier whose output fails identity must be treated as a
failed tier so the cascade tries the next one — never store a non-proper vector.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import memory.config as config
import memory.embed as me


def _unit(dim: int) -> list[float]:
    v = [0.0] * dim
    v[0] = 1.0  # L2 norm = 1
    return v


@pytest.fixture(autouse=True)
def _reset_identity_warn():
    me._IDENTITY_WARNED.clear()
    yield
    me._IDENTITY_WARNED.clear()


def test_proper_vector_and_tag_accepted():
    assert me._validate_identity(_unit(config.EMBED_DIM), config.EMBED_MODEL, "t") is True


def test_wrong_dimension_rejected():
    assert me._validate_identity([1.0, 0.0], config.EMBED_MODEL, "t") is False


def test_foreign_model_rejected():
    assert me._validate_identity(_unit(config.EMBED_DIM), "some-other-embed", "t") is False


def test_compatible_alias_accepted():
    # The tier-1 GGUF tag maps to the same space as the configured model.
    assert me._EMBED_GGUF_MODEL_TAG in me._compatible_model_names()
    assert me._validate_identity(_unit(config.EMBED_DIM), me._EMBED_GGUF_MODEL_TAG, "t") is True


def test_operator_compatible_models_env(monkeypatch):
    monkeypatch.setattr(config, "EMBED_COMPATIBLE_MODELS", ("legacy-embed-v1",))
    assert "legacy-embed-v1" in me._compatible_model_names()
    assert me._validate_identity(_unit(config.EMBED_DIM), "legacy-embed-v1", "t") is True


def test_non_unit_rejected_when_required(monkeypatch):
    monkeypatch.setattr(config, "EMBED_REQUIRE_UNIT_NORM", True)
    non_unit = [0.5] * config.EMBED_DIM  # norm = 0.5*sqrt(dim) >> 1
    assert me._validate_identity(non_unit, config.EMBED_MODEL, "t") is False


def test_non_unit_allowed_when_not_required(monkeypatch):
    monkeypatch.setattr(config, "EMBED_REQUIRE_UNIT_NORM", False)
    non_unit = [0.5] * config.EMBED_DIM
    # dim + model still match; norm not enforced -> accepted
    assert me._validate_identity(non_unit, config.EMBED_MODEL, "t") is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("require_unit", [True, False])
def test_non_finite_rejected_regardless_of_norm_policy(monkeypatch, bad, require_unit):
    """A NaN/inf component is never a valid embedding (it poisons every cosine
    distance). NaN in particular slips past the norm tolerance (NaN compares
    False to everything), so finite-ness must be enforced independently of the
    unit-norm policy."""
    monkeypatch.setattr(config, "EMBED_REQUIRE_UNIT_NORM", require_unit)
    vec = _unit(config.EMBED_DIM)
    vec[0] = bad
    assert me._validate_identity(vec, config.EMBED_MODEL, "t") is False
    # also caught when the bad vector is a sampled member of a bulk batch
    batch = [_unit(config.EMBED_DIM) for _ in range(5)]
    batch[0] = vec
    assert me._validate_identity(batch, config.EMBED_MODEL, "t") is False


def test_zero_vector_rejected_when_unit_required(monkeypatch):
    monkeypatch.setattr(config, "EMBED_REQUIRE_UNIT_NORM", True)
    assert me._validate_identity([0.0] * config.EMBED_DIM, config.EMBED_MODEL, "t") is False


def test_bulk_list_validated_by_sample():
    vecs = [_unit(config.EMBED_DIM) for _ in range(5)]
    assert me._validate_identity(vecs, config.EMBED_MODEL, "t") is True
    # one wrong-dim member in the sampled positions (0/mid/last) is caught
    vecs[0] = [1.0, 0.0]
    assert me._validate_identity(vecs, config.EMBED_MODEL, "t") is False


def test_empty_input_rejected():
    assert me._validate_identity([], config.EMBED_MODEL, "t") is False
    assert me._validate_identity(None, config.EMBED_MODEL, "t") is False


def test_norm_tolerance_respected(monkeypatch):
    monkeypatch.setattr(config, "EMBED_REQUIRE_UNIT_NORM", True)
    monkeypatch.setattr(config, "EMBED_NORM_TOL", 0.05)
    # norm 1.03 is within tol; 1.2 is not.
    near = _unit(config.EMBED_DIM)
    near[1] = math.sqrt(1.03**2 - 1.0)  # bump norm to ~1.03
    assert me._validate_identity(near, config.EMBED_MODEL, "t") is True
    far = _unit(config.EMBED_DIM)
    far[1] = math.sqrt(1.2**2 - 1.0)  # norm ~1.2
    assert me._validate_identity(far, config.EMBED_MODEL, "t") is False


def test_warns_once_per_reason():
    me._validate_identity([1.0, 0.0], config.EMBED_MODEL, "tierX")
    first = set(me._IDENTITY_WARNED)
    me._validate_identity([1.0, 0.0, 0.0], config.EMBED_MODEL, "tierX")  # same reason (dim)
    assert me._IDENTITY_WARNED == first  # no new warn key for the same reason


# ── _accept_bulk: assign proper vectors, keep failures in the miss set ──────────

def test_accept_bulk_assigns_proper_and_reports_misses():
    dim = config.EMBED_DIM
    out: list = [None, None, None]
    miss_indices = [0, 1, 2]
    vecs = [_unit(dim), None, _unit(dim)]  # row 1 missing
    still = me._accept_bulk(out, miss_indices, vecs, config.EMBED_MODEL, "t")
    assert out[0] == (_unit(dim), config.EMBED_MODEL)
    assert out[2] == (_unit(dim), config.EMBED_MODEL)
    assert out[1] is None
    assert still == [1]  # only the missing local index is reported


def test_accept_bulk_rejects_whole_foreign_batch():
    # A batch whose vectors fail identity (wrong dim) is rejected wholesale:
    # nothing is stored, every local index is reported as still-missing.
    out: list = [None, None]
    bad = [[1.0, 0.0], [1.0, 0.0]]  # dim 2, not EMBED_DIM
    still = me._accept_bulk(out, [0, 1], bad, config.EMBED_MODEL, "t")
    assert out == [None, None]
    assert still == [0, 1]


# ── Search read-path identity filter (SQL construction) ────────────────────────

@pytest.mark.asyncio
async def test_search_sql_includes_identity_filter(monkeypatch):
    """The semantic/hybrid search SQL must restrict the embeddings join to the
    proper identity: embed_model IN (compatible) AND dim = EMBED_DIM."""
    import memory_core

    async def fake_embed(_q):
        return (_unit(config.EMBED_DIM), config.EMBED_MODEL)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)

    captured: list[str] = []

    class _Cur:
        def fetchall(self): return []
        def fetchone(self): return None

    class _DB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append(sql)
            return _Cur()
        def commit(self): pass

    monkeypatch.setattr(memory_core, "_db", lambda: _DB())

    await memory_core.memory_search_scored_impl("anything", search_mode="semantic")
    assert captured, "expected at least one query"
    sql = " ".join(captured)
    assert "me.embed_model IN" in sql, "identity model filter missing from search SQL"
    assert "me.dim = ?" in sql, "identity dim filter missing from search SQL"
