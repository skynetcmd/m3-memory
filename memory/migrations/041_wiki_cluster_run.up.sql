-- 041_wiki_cluster_run.up.sql
-- Wiki compile provenance + membership cache (plan G1,
-- ~/.m3-private/plans/WIKI_SYNTHESIS_MEMORY_TYPE.md).
--
-- Follows 040.
--
-- Two tables with DIFFERENT natures (do not conflate them):
--
--   cluster_run     PROVENANCE — a compile decision made at a moment. NOT
--                   rebuildable (recompute cannot reproduce what the clusters
--                   WERE at compile time, under which selection). Pruned only
--                   when nothing references it: a synthesis's compiler.run_id
--                   would dangle otherwise, breaking 4b snapshot pinning and
--                   S5 `--as-of`.
--   cluster_members CACHE — (run_id, cluster_key, memory_id). Rebuildable
--                   (~1.3 ms) and prunable generationally (run N+1 completes =>
--                   drop run N's members). Stores UUIDs ONLY, never content, so
--                   the table itself holds no personal data and a GDPR hit is
--                   detectable by scanning memory_id.
--
-- Why membership must be FROZEN at compile time (the correctness payoff): a
-- synthesis's own `consolidates` edges perturb modularity, so recomputing a past
-- run's clustering can shift SOURCE membership and re-supersede an unchanged
-- topic (the residual 2/13 idempotence churn). cluster_members records what the
-- membership WAS, so a cross-run skip-check compares against the frozen set, not
-- a freshly (and differently) recomputed one.
--
-- cluster_members ALSO doubles as a compile ledger / skip index (plan line 742):
-- alongside each row we keep the cluster's compiled `cluster_hash` -> its head
-- synthesis id, so an unchanged cluster is recognized by hash lookup WITHOUT
-- loading its head synthesis row. This is an OPTIMIZATION + resume/loop guard,
-- NOT a correctness fix (the single-pass writer cannot loop).
--
-- NOTE (2026-07-24): the plan's G1 text lists `use_networkx` as a stored
-- reproducibility parameter. networkx became a BASE dependency this session and
-- clustering is now always-networkx, so `use_networkx` is a constant, not a
-- parameter — it is intentionally NOT a column here. `entity_comention` and
-- `entity_max_degree` remain real reproducibility parameters and are stored.
--
-- Readers resolve ONLY completed runs (completed_at IS NOT NULL). A crashed
-- compile leaves an in_progress row that is invisible — atomicity, not deletion,
-- solves the partial-readable-table concern (G1).
--
-- GDPR (G2): gdpr_forget scans cluster_members for the erased UUID; a hit
-- records the erased id + count + timestamps ON THE RUN ROW (accumulating) and
-- flushes that run's cached membership. The run row survives the flush, so WHY
-- the data is gone outlasts the data. Retaining the UUID matches documented m3
-- posture (gdpr_requests keeps subject_id to demonstrate Art. 5(2) compliance).

CREATE TABLE IF NOT EXISTS cluster_run (
    id                   TEXT PRIMARY KEY,
    seq                  INTEGER NOT NULL,        -- monotonic per-store sequence
    completed_at         TEXT,                    -- NULL while in progress => invisible to readers
    as_of                TEXT,                    -- bitemporal selection point (S5 --as-of), NULL = live
    scope                TEXT,                    -- S1 tenancy selector the run compiled under
    user_id              TEXT,
    importance_threshold REAL,                    -- core-memory floor at compile time
    entity_comention     INTEGER DEFAULT 0,       -- reproducibility param (0/1)
    entity_max_degree    INTEGER,                 -- ENTITY_MAX_DEGREE cap in effect
    cluster_count        INTEGER DEFAULT 0,       -- clusters this run produced (audit)
    -- GDPR G2: erasure record + cache-invalidation signal (accumulating).
    erased_member_ids    TEXT,                    -- JSON array of erased UUIDs; NULL until first hit
    erased_count         INTEGER DEFAULT 0,
    first_erased_at      TEXT,
    last_erased_at       TEXT,
    membership_flushed   INTEGER DEFAULT 0,       -- 1 once this run's cluster_members were flushed
    page_hash_digest     TEXT,                    -- JSON {cluster_key: page_hash}; a hash is not PII, survives erasure -> "these topics differ"
    created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_cluster_run_seq ON cluster_run(seq);
-- Readers resolve the latest COMPLETED run cheaply.
CREATE INDEX IF NOT EXISTS idx_cluster_run_completed
    ON cluster_run(completed_at) WHERE completed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS cluster_members (
    run_id            TEXT NOT NULL,
    cluster_key       TEXT NOT NULL,
    memory_id         TEXT NOT NULL,              -- UUID ONLY, never content
    cluster_hash      TEXT,                       -- compile-time hash of the cluster (skip index)
    head_synthesis_id TEXT,                       -- the synthesis this cluster compiled to
    PRIMARY KEY (run_id, cluster_key, memory_id)
);

-- GDPR scan: find every run/cluster a given erased UUID participated in.
CREATE INDEX IF NOT EXISTS idx_cluster_members_memory ON cluster_members(memory_id);
-- Generational flush: drop an entire run's membership by run_id.
CREATE INDEX IF NOT EXISTS idx_cluster_members_run ON cluster_members(run_id);
-- Skip index: recognize an unchanged cluster by its hash without loading the head.
CREATE INDEX IF NOT EXISTS idx_cluster_members_hash ON cluster_members(cluster_hash);
