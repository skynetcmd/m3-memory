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

  mmr_rerank_scored — CLEAN. This is the policy-aware variant that mirrors
                    m3-memory's ACTUAL retrieval loop (memory_core.py
                    ~3489-3519): it ranks on a caller-supplied blended
                    relevance score (not cosine-to-query) and force-seeds
                    index 0 unconditionally. With ``force_seed_first=True``
                    and a descending-sorted relevance input it reproduces the
                    Python selection-index sequence EXACTLY — including the
                    case where the max-relevance item is NOT the max-cosine
                    item, which is precisely where the generic ``mmr_rerank``
                    diverges. Verified for k<n, k==n, k>n, and tie cases.

  enforce_displacement_guard — CLEAN. Faithful port of
                    ``_enforce_expansion_displacement_guard`` (memory_core.py
                    ~515-573). Same protected_ranks/margin defaults (3, 2.0),
                    same find-next-primary walk, same
                    ``score > 0 and primary > 0 and score >= margin*primary``
                    test, same swap, same no-op conditions, idempotent.

TOLERANCE: ``abs(rust - py) < 1e-5``. Both implementations cast inputs to
float32 (Python via ``numpy.float32``, Rust via ``f32``), and accumulate
dot/norm sums in f32. float32 has ~7 significant decimal digits; over a
1024-dim reduction the accumulated rounding error sits well under 1e-5 but
far above 1e-9. Demanding 1e-9 would be a false failure caused purely by
float32 representation, not by an implementation divergence.
"""


import pytest

m3_core_rs = pytest.importorskip("m3_core_rs")
np = pytest.importorskip("numpy")

# conftest.py puts bin/ on sys.path.
from embedding_utils import batch_cosine as py_batch_cosine
from embedding_utils import cosine as py_cosine

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


# --------------------------------------------------------------------------
# 5. mmr_rerank_scored parity — the policy-aware variant
# --------------------------------------------------------------------------
def py_mmr_scored_select(relevance, candidates, lambda_, k, force_seed_first):
    """Faithful inline replica of memory_core.py MMR loop (lines ~3489-3519).

    Operates on a caller-supplied ``relevance`` score per candidate (the
    blended FTS+vector rank score, NOT cosine-to-query). When
    ``force_seed_first`` is True, ``selected = [pre_ranked[0]]`` — index 0 is
    seeded unconditionally (caller pre-sorts descending by relevance). Then
    greedily: ``mmr_score = lambda*c_score - (1-lambda)*max_sim_to_selected``.

    Returns selected indices in selection order.
    """
    n = len(candidates)
    k = min(k, n)
    if k <= 0:
        return []
    selected = []
    remaining = list(range(n))
    if force_seed_first:
        selected.append(remaining.pop(0))
    while remaining and len(selected) < k:
        best_idx, best_mmr = 0, -float("inf")
        for ci, cand_i in enumerate(remaining):
            c_score = relevance[cand_i]
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


# Candidate vectors + a blended relevance score per candidate. Crucially the
# relevance ranking is NOT the cosine-to-query ranking — index 0 has the
# highest relevance but is NOT the max-cosine-to-anything item. This is the
# exact shape where the generic mmr_rerank diverges from production.
_SCORED_CANDS = [
    [0.0, 1.0, 0.0],   # idx 0
    [1.0, 0.0, 0.0],   # idx 1
    [0.9, 0.1, 0.0],   # idx 2 — near-duplicate of idx 1
    [0.0, 0.0, 1.0],   # idx 3
    [0.1, 0.1, 0.95],  # idx 4 — near-duplicate of idx 3
]
# Pre-sorted descending by relevance, as the production call site guarantees.
_SCORED_RELEVANCE = [0.95, 0.80, 0.70, 0.60, 0.40]


@pytest.mark.parametrize("k", [1, 2, 3, 5, 8])
def test_mmr_scored_parity_force_seed(k):
    """force_seed_first=True must reproduce the Python loop exactly."""
    rust = list(
        m3_core_rs.mmr_rerank_scored(
            _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, k, True
        )
    )
    py = py_mmr_scored_select(
        _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, k, True
    )
    assert rust == py


def test_mmr_scored_force_seed_pins_index_zero():
    """Index 0 is always selected first under force_seed_first, even though
    it is neither the max-cosine nor uniquely separable item."""
    rust = list(
        m3_core_rs.mmr_rerank_scored(
            _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, 5, True
        )
    )
    assert rust[0] == 0


def test_mmr_scored_max_relevance_not_max_cosine():
    """The divergence case: max-relevance item != max-cosine item.

    Generic mmr_rerank would seed on cosine-to-query; mmr_rerank_scored with
    force_seed_first seeds on relevance index 0. Pin that they differ in
    seeding and that the scored variant matches the Python policy loop.
    """
    relevance = [0.9, 0.5, 0.4]
    cands = [[0.0, 1.0], [1.0, 0.0], [0.95, 0.05]]
    rust = list(
        m3_core_rs.mmr_rerank_scored(relevance, cands, _MMR_LAMBDA, 3, True)
    )
    py = py_mmr_scored_select(relevance, cands, _MMR_LAMBDA, 3, True)
    assert rust == py
    assert rust[0] == 0  # relevance-seeded, not cosine-seeded


def test_mmr_scored_no_force_seed_pure_greedy():
    """force_seed_first=False → pure greedy; first pick = argmax relevance
    (selected empty → max_sim=0, so mmr reduces to lambda*relevance)."""
    rust = list(
        m3_core_rs.mmr_rerank_scored(
            _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, 5, False
        )
    )
    py = py_mmr_scored_select(
        _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, 5, False
    )
    assert rust == py
    assert rust[0] == 0  # argmax relevance


def test_mmr_scored_tie_case():
    """Tie in relevance: both implementations break ties by original index
    order (Python's stable argmax / pop, Rust's ordered remove)."""
    relevance = [0.5, 0.5, 0.5]
    cands = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    rust = list(
        m3_core_rs.mmr_rerank_scored(relevance, cands, _MMR_LAMBDA, 3, True)
    )
    py = py_mmr_scored_select(relevance, cands, _MMR_LAMBDA, 3, True)
    assert rust == py


def test_mmr_scored_k_zero():
    assert list(
        m3_core_rs.mmr_rerank_scored(
            _SCORED_RELEVANCE, _SCORED_CANDS, _MMR_LAMBDA, 0, True
        )
    ) == []


# --------------------------------------------------------------------------
# 6. enforce_displacement_guard parity
# --------------------------------------------------------------------------
EXPANSION_PROTECTED_RANKS = 3
EXPANSION_DISPLACEMENT_MARGIN = 2.0


def py_enforce_displacement_guard(items, protected_ranks, margin):
    """Inline replica of memory_core.py ``_enforce_expansion_displacement_guard``.

    ``items`` is a list of ``(score, is_expansion)`` tuples in ranked order.
    Returns a reordered list. No-op if protected_ranks <= 0 or margin <= 1.0.
    """
    if not items or protected_ranks <= 0 or margin <= 1.0:
        return list(items)
    work = list(items)
    n = len(work)
    limit = min(protected_ranks, n)
    for rank in range(limit):
        score, is_exp = work[rank]
        if not is_exp:
            continue
        next_primary_idx = None
        for j in range(rank + 1, n):
            if not work[j][1]:
                next_primary_idx = j
                break
        if next_primary_idx is None:
            continue
        primary_score, _ = work[next_primary_idx]
        if score > 0 and primary_score > 0 and score >= margin * primary_score:
            continue
        work[rank], work[next_primary_idx] = work[next_primary_idx], work[rank]
    return work


def _rows_eq(a, b):
    """Compare (score, is_expansion) lists; score within TOL (f32 round-trip)."""
    if len(a) != len(b):
        return False
    return all(
        abs(sa - sb) < TOL and ea == eb
        for (sa, ea), (sb, eb) in zip(a, b)
    )


def _guard(items, protected_ranks=EXPANSION_PROTECTED_RANKS,
           margin=EXPANSION_DISPLACEMENT_MARGIN):
    # The Rust binding returns an index permutation, not reordered rows — so
    # callers can map back to their own row objects even when (score, flag)
    # pairs collide. Apply it to recover the reordered list for comparison.
    perm = m3_core_rs.enforce_displacement_guard(items, protected_ranks, margin)
    rust = [items[i] for i in perm]
    py = py_enforce_displacement_guard(items, protected_ranks, margin)
    assert _rows_eq(rust, py), f"{rust} != {py}"
    return rust


def test_guard_expansion_rank0_fails_margin_swaps():
    items = [(1.0, True), (0.9, False), (0.5, False)]
    out = _guard(items)
    assert _rows_eq(out, [(0.9, False), (1.0, True), (0.5, False)])


def test_guard_expansion_passes_margin_stays():
    items = [(3.0, True), (1.0, False), (0.5, False)]
    out = _guard(items)
    assert _rows_eq(out, [(3.0, True), (1.0, False), (0.5, False)])


def test_guard_nonpositive_scores_primary_wins():
    # expansion score <= 0 → no clean ratio → primary wins.
    items = [(-1.0, True), (-2.0, False)]
    out = _guard(items)
    assert _rows_eq(out, [(-2.0, False), (-1.0, True)])
    # positive expansion, zero primary → still primary wins.
    items2 = [(5.0, True), (0.0, False)]
    out2 = _guard(items2)
    assert _rows_eq(out2, [(0.0, False), (5.0, True)])


def test_guard_margin_le_one_noop():
    items = [(1.0, True), (0.9, False)]
    assert _rows_eq(_guard(items, margin=1.0), items)
    assert _rows_eq(_guard(items, margin=0.5), items)


def test_guard_protected_ranks_zero_noop():
    items = [(1.0, True), (0.9, False)]
    assert _rows_eq(_guard(items, protected_ranks=0), items)


def test_guard_idempotent():
    items = [(1.0, True), (0.9, False), (0.8, True), (0.4, False)]
    once = _guard(items)
    twice = _guard(once)
    assert _rows_eq(once, twice)


def test_guard_no_primary_below():
    # all-expansion list: nothing to swap with, order preserved.
    items = [(1.0, True), (0.5, True), (0.3, True)]
    assert _rows_eq(_guard(items), items)


def test_guard_empty():
    assert _guard([]) == []
