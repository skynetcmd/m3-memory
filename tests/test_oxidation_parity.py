"""Parity harness — Rust ``m3_core_rs`` vs Python baselines (the §8 swap gate).

This file validates that the Rust extension implementations of cosine
similarity, batched cosine, and MMR reranking produce results numerically
equivalent to the Python baselines in ``bin/embedding_utils.py`` and the MMR
loop in ``bin/memory_core.py`` (~lines 3489-3519), BEFORE the Rust paths are
swapped in.

PARITY VERDICT (as of this harness):

  cosine          — CLEAN. Identical to ``embedding_utils.cosine`` within the
                    float32 tolerance below.
  cosine_batch    — CLEAN on homogeneous input. Two DOCUMENTED divergences:
                      (a) Ragged input: Rust RAISES ``ValueError`` on a
                          dimension mismatch; Python's ``batch_cosine``
                          silently filters mismatched rows to 0.0. Caller
                          code must keep doing dimension filtering BEFORE
                          handing a corpus to the Rust path. Covered by an
                          xfail-style assertion test below.
                      (b) Zero vectors: Python uses a ``1e-10`` norm floor,
                          Rust returns an exact ``0.0``. In practice both
                          yield 0.0 for a zero query/row (numerator is 0),
                          so this is a non-divergence — documented for
                          completeness.
  mmr_rerank      — CLEAN *only* when the Python ``pre_ranked`` scores are the
                    true cosine-to-query values, pre-sorted descending. Root
                    cause: Python force-seeds ``selected[0] = pre_ranked[0]``
                    (highest pre-rank score), while Rust internally seeds with
                    the highest cosine-to-query candidate. These coincide iff
                    pre_ranked[0] is the max-cosine candidate. The production
                    call site DOES pre-sort by score descending, but that
                    score is a blended rank score, NOT raw cosine-to-query —
                    so production MMR and Rust MMR are only equivalent when
                    the blended score happens to rank-agree with cosine. This
                    harness constructs the cosine-score case to prove the
                    core algorithm matches; the rank-score divergence is a
                    known, intentional behavioural difference, not a bug.

TOLERANCE: ``abs(rust - py) < 1e-5``. Both implementations cast inputs to
float32 (Python via ``numpy.float32``, Rust via ``f32``), and accumulate
dot/norm sums in f32. float32 has ~7 significant decimal digits; over a
1024-dim reduction the accumulated rounding error sits well under 1e-5 but
far above 1e-9. Demanding 1e-9 would be a false failure caused purely by
float32 representation, not by an implementation divergence.
"""

import math

import pytest

m3_core_rs = pytest.importorskip("m3_core_rs")
np = pytest.importorskip("numpy")

# conftest.py puts bin/ on sys.path.
from embedding_utils import cosine as py_cosine
from embedding_utils import batch_cosine as py_batch_cosine

TOL = 1e-5
_MMR_LAMBDA = 0.7


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _rand(dim, seed):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim).astype(np.float32).tolist()


def py_mmr_select(query, candidates, lambda_, k):
    """Replicate the memory_core.py MMR selection loop (lines ~3489-3519).

    The production loop is embedded in a large function and operates on
    ``(c_score, c_item)`` pairs where ``c_score`` is a pre-computed rank
    score. Here we extract just the selection logic and feed each
    candidate's TRUE cosine-to-query as its ``c_score`` so the result is
    directly comparable to ``m3_core_rs.mmr_rerank`` (which computes
    query similarity internally).

    Returns selected indices in selection order.
    """
    n = len(candidates)
    k = min(k, n)
    if k <= 0:
        return []
    # pre_ranked: indices sorted by cosine-to-query descending (stable).
    scores = [py_cosine(query, c) for c in candidates]
    pre_ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)

    # Python force-seeds the first (highest pre-rank score) candidate.
    selected = [pre_ranked[0]]
    remaining = list(pre_ranked[1:])
    while remaining and len(selected) < k:
        best_idx, best_mmr = 0, -float("inf")
        for ci, cand_i in enumerate(remaining):
            c_score = scores[cand_i]
            max_sim = max(
                (py_cosine(candidates[cand_i], candidates[s]) for s in selected),
                default=0.0,
            )
            mmr_score = lambda_ * c_score - (1 - lambda_) * max_sim
            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = ci
        selected.append(remaining.pop(best_idx))
    return selected


# --------------------------------------------------------------------------
# 1. cosine parity
# --------------------------------------------------------------------------
@pytest.mark.parametrize("dim,seed", [(4, 1), (128, 2), (1024, 3)])
def test_cosine_random(dim, seed):
    a = _rand(dim, seed)
    b = _rand(dim, seed + 1000)
    assert abs(m3_core_rs.cosine(a, b) - py_cosine(a, b)) < TOL


def test_cosine_identical():
    a = _rand(64, 42)
    r = m3_core_rs.cosine(a, a)
    assert abs(r - py_cosine(a, a)) < TOL
    assert abs(r - 1.0) < TOL


def test_cosine_orthogonal():
    a = [1.0, 0.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0, 0.0]
    r = m3_core_rs.cosine(a, b)
    assert abs(r - py_cosine(a, b)) < TOL
    assert abs(r - 0.0) < TOL


def test_cosine_opposite():
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]
    r = m3_core_rs.cosine(a, b)
    assert abs(r - py_cosine(a, b)) < TOL
    assert abs(r - (-1.0)) < TOL


def test_cosine_zero_vector():
    """Zero vector → denom is 0; both sides must return exactly 0.0."""
    a = [0.0, 0.0, 0.0]
    b = [1.0, 2.0, 3.0]
    assert m3_core_rs.cosine(a, b) == 0.0
    assert py_cosine(a, b) == 0.0
    assert m3_core_rs.cosine(a, a) == 0.0
    assert py_cosine(a, a) == 0.0


# --------------------------------------------------------------------------
# 2. cosine_batch parity
# --------------------------------------------------------------------------
@pytest.mark.parametrize("dim,n,seed", [(4, 3, 10), (128, 8, 11), (1024, 5, 12)])
def test_cosine_batch_homogeneous(dim, n, seed):
    query = _rand(dim, seed)
    corpus = [_rand(dim, seed + 100 + i) for i in range(n)]
    rust = m3_core_rs.cosine_batch(query, corpus)
    py = py_batch_cosine(query, corpus)
    assert len(rust) == len(py) == n
    for r, p in zip(rust, py):
        assert abs(r - p) < TOL


def test_cosine_batch_ragged_DIVERGENCE():
    """DOCUMENTED DIVERGENCE: ragged corpus.

    Python ``batch_cosine`` silently filters dimension-mismatched rows and
    scores them 0.0. Rust ``cosine_batch`` RAISES ``ValueError`` instead.
    This is a real behavioural divergence — the caller MUST keep doing
    dimension filtering before handing a corpus to the Rust path.
    """
    query = [1.0, 0.0, 0.0]
    ragged = [[1.0, 0.0, 0.0], [1.0, 0.0]]  # second row is dim-2

    # Python: tolerates it, mismatched row scored 0.0.
    py = py_batch_cosine(query, ragged)
    assert py[1] == 0.0
    assert abs(py[0] - 1.0) < TOL

    # Rust: raises.
    with pytest.raises(ValueError):
        m3_core_rs.cosine_batch(query, ragged)


def test_cosine_batch_zero_vectors():
    """Zero query / zero row: Python 1e-10 norm floor vs Rust exact 0.0.

    Numerator is 0 in both cases, so both yield exactly 0.0 — the floor
    never actually manifests as a visible difference here. Documented as a
    non-divergence.
    """
    # zero row in corpus
    query = [1.0, 1.0]
    corpus = [[0.0, 0.0], [1.0, 1.0]]
    rust = m3_core_rs.cosine_batch(query, corpus)
    py = py_batch_cosine(query, corpus)
    assert rust[0] == 0.0
    assert abs(rust[0] - py[0]) < TOL
    assert abs(rust[1] - py[1]) < TOL

    # zero query
    rust_zq = m3_core_rs.cosine_batch([0.0, 0.0], [[1.0, 1.0]])
    py_zq = py_batch_cosine([0.0, 0.0], [[1.0, 1.0]])
    assert rust_zq[0] == 0.0
    assert abs(rust_zq[0] - py_zq[0]) < TOL


def test_cosine_batch_empty_corpus():
    assert m3_core_rs.cosine_batch([1.0, 2.0], []) == []
    assert py_batch_cosine([1.0, 2.0], []) == []


# --------------------------------------------------------------------------
# 3. MMR parity
# --------------------------------------------------------------------------
# Test corpus: query + candidates with distinct, well-separated cosine-to-query
# values so there are no accidental ties in the pre-ranking.
_MMR_QUERY = [1.0, 0.0, 0.0]
_MMR_CANDS = [
    [1.0, 0.0, 0.0],     # cos 1.00
    [0.9, 0.4, 0.0],     # cos ~0.91
    [0.5, 0.85, 0.0],    # cos ~0.51
    [0.2, 0.9, 0.3],     # cos ~0.21
    [0.0, 0.7, 0.7],     # cos 0.00
]


@pytest.mark.parametrize("k", [1, 2, 3, 5, 8])
def test_mmr_parity(k):
    """Selected index sequences must match for k<n, k==n, k>n.

    Comparable because the Python helper feeds true cosine-to-query as the
    pre-rank score and pre-sorts descending, matching how Rust seeds.
    """
    rust = list(m3_core_rs.mmr_rerank(_MMR_QUERY, _MMR_CANDS, _MMR_LAMBDA, k))
    py = py_mmr_select(_MMR_QUERY, _MMR_CANDS, _MMR_LAMBDA, k)
    assert rust == py


def test_mmr_k_zero():
    assert list(m3_core_rs.mmr_rerank(_MMR_QUERY, _MMR_CANDS, _MMR_LAMBDA, 0)) == []
    assert py_mmr_select(_MMR_QUERY, _MMR_CANDS, _MMR_LAMBDA, 0) == []


def test_mmr_tie_case():
    """Tie case: two candidates identical to the query (cos 1.0 each).

    Both implementations break the tie by original index order (stable
    sort / first-wins argmax), so index 0 is seeded and index 1 follows.
    If this assertion ever fails, tie-breaking has diverged — investigate
    before swapping.
    """
    query = [1.0, 0.0]
    cands = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    rust = list(m3_core_rs.mmr_rerank(query, cands, _MMR_LAMBDA, 3))
    py = py_mmr_select(query, cands, _MMR_LAMBDA, 3)
    assert rust == py
    # And the documented expectation:
    assert rust == [0, 1, 2]


def test_mmr_seed_is_max_cosine_not_index_zero():
    """Pin the seeding contract.

    Rust seeds with the highest-cosine candidate, NOT input index 0. Here
    index 2 has the highest cosine-to-query, so Rust picks it first. The
    Python production loop force-seeds pre_ranked[0]; parity holds ONLY
    because py_mmr_select pre-sorts by cosine descending. This test makes
    the seeding behaviour explicit so a future reader understands why the
    pre-sort in py_mmr_select is load-bearing.
    """
    query = [1.0, 0.0]
    cands = [[0.0, 1.0], [0.5, 0.5], [1.0, 0.0]]  # idx 2 = max cosine
    rust = list(m3_core_rs.mmr_rerank(query, cands, _MMR_LAMBDA, 3))
    assert rust[0] == 2
    assert rust == py_mmr_select(query, cands, _MMR_LAMBDA, 3)


# --------------------------------------------------------------------------
# 4. summary gate test
# --------------------------------------------------------------------------
def test_parity_summary():
    """Smoke-level end-to-end: every operation agrees on a shared fixture.

    This is the readable gate check — if it is green, cosine and
    cosine_batch are numerically parity-clean within TOL, and mmr_rerank is
    parity-clean given a cosine-scored, descending-sorted pre-ranking. See
    the module docstring for the full verdict and the two documented
    cosine_batch divergences (ragged input, zero-vector floor).
    """
    query = _rand(256, 7)
    corpus = [_rand(256, 7 + 50 + i) for i in range(6)]

    # cosine
    for c in corpus:
        assert abs(m3_core_rs.cosine(query, c) - py_cosine(query, c)) < TOL

    # cosine_batch
    rb = m3_core_rs.cosine_batch(query, corpus)
    pb = py_batch_cosine(query, corpus)
    assert all(abs(r - p) < TOL for r, p in zip(rb, pb))

    # mmr
    assert list(m3_core_rs.mmr_rerank(query, corpus, _MMR_LAMBDA, 4)) == \
        py_mmr_select(query, corpus, _MMR_LAMBDA, 4)
