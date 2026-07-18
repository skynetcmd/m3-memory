# Design: Streamlined Test-Suite Gating & Structure

> **Status:** proposed (review before implementation)
> **Scope:** how tests are *gated, selected, and isolated* — NOT a rewrite of the
> 2,300 test bodies. The goal is a small, explicit gating layer that replaces
> today's scattered, hand-rolled skip logic and makes tiers CI-selectable.

## Current state (measured)

- **181 test files**, one monolithic `tests/conftest.py` (559 lines, 6 autouse
  fixtures + 16 module-level helpers).
- **CI runs bare `pytest tests/`** — no tier selection, no `-m`, no xdist.
- **Only 2 custom markers** (`slow`, `real_is_current`); `slow` is declared but
  **never used to gate** (no `-m 'not slow'` anywhere).
- **No `--strict-markers`** → a typo'd marker silently no-ops.
- **14 `*_live.py` files gate on live services by HAND** — each re-derives a DSN
  from `M3_PRIMARY_PG_URL/M3_PG_URL`, defines its own `_reachable()` probe, and
  sets a module `pytestmark = skipif(...)`. On CI (no PG, no embedder) **all of
  them silently skip** — so integration coverage runs *only* on a dev box with
  services up, and is enforced *nowhere*.
- **21 ad-hoc `skipif`s** re-derive availability (GGUF present, platform, native
  wheel, files-db present) inline.

### The core problems

1. **Gating is implicit and duplicated.** "Is this an integration test?" is
   answered by *filename convention* (`*_live.py`) and *copy-pasted skip code*,
   not a marker. You cannot say `pytest -m "not integration"` or, in CI,
   `-m integration` to *require* the PG suite when a cluster IS provisioned.
2. **Integration tests are enforced nowhere.** They skip on CI and depend on a
   developer remembering to run them locally with services up.
3. **Isolation gaps cause leaks** (this session: backup-dir leak, CDW-env leak) —
   because per-test isolation was piecemeal. (Being fixed by `m3_sandbox`, see
   below; this doc records the target end-state.)
4. **No fast inner loop.** No `unit`-only selection, so a one-line change reruns
   everything including the slow/native paths.

## Target design

### 0. The full gating inventory (measured across all 181 files)

Five distinct "is X available?" idioms, each hand-rolled per file, none sharing a
helper — this is the real suite-wide problem, not just PG:

| Capability | Files | Today's mechanism | Defects found |
|---|---|---|---|
| **PostgreSQL** | ~12 | verbatim-copied `_dsn()`+`_reachable()`+`pytestmark` (~130 dup lines) | 1 file's skip reason says `M3_PG_URL` not `M3_PRIMARY_PG_URL`; `test_backend_conformance` gates on *presence* not *reachability* (inconsistent) |
| **native `m3_core_rs`** | ~6 | **3 different idioms**: `importorskip`, in-body `pytest.skip`, custom `_has_native_governor()` | same condition expressed 3 ways |
| **GGUF model** | 2 | hand-rolled `skipif` | **two different env vars** for the same thing: `M3_TEST_GGUF` vs `M3_EMBED_GGUF` |
| **`files_database.db`** | 3 | `_FILES_DB.is_file()` re-derived per file | some files derive the path but don't guard |
| **embedder** | 1 gates / ~8 mock | shared `embed_backend_reachable()` **exists but only `test_doctor` uses it**; the rest mock the transport (correctly) | helper under-used; smoke scripts re-derive their own probe |
| **optional extras** | ~7 | `pytest.importorskip("crewai"/"fastapi"/"langchain_core"/…)` | consistent-ish; leave as-is or wrap |

Plus: **platform** appears in two unrelated roles — real `skipif(sys.platform…)`
gates (~3 files) vs. `monkeypatch.setattr(sys,"platform",…)` *simulations* (~8
files). The redesign touches only the former; simulations stay.

Also: 18 non-`test_`-prefixed scripts (`bench_*`, `smoke_*`, `integration_*`, …)
are **never collected** (they self-gate + `return 0` as `__main__`). Out of scope.

### 1. A small, strict marker taxonomy (the gate)

Register in `pyproject.toml` with `--strict-markers` (typos then FAIL):

One marker per capability, each backed by ONE conftest probe — covering all five
idioms from §0, not just PG:

| Marker | Replaces (today) | Backed by conftest probe |
|---|---|---|
| *(none)* = **unit** | the ~150 hermetic files | — (must pass everywhere) |
| `requires_pg` | 12 copy-pasted `_dsn()`/`_reachable()` blocks | `pg_dsn()` + `_pg_reachable()` (one place, correct precedence) |
| `requires_embedder` | 1 gate + smoke re-derivations | existing `embed_backend_reachable()` |
| `requires_native` | 3 idioms (`importorskip`/`skip`/`_has_native_governor`) | `_native_wheel_present()` |
| `requires_gguf` | 2 files, **2 different env vars** | one probe + **one canonical env var** |
| `requires_files_db` | 3 files re-deriving the path | `_files_db_present()` |
| `slow` | declared, ~unused | (kept; `-m "not slow"` fast loop) |
| `platform_darwin`/`platform_win` | ~3 real `skipif` gates | (simulations via monkeypatch stay untouched) |

All `requires_*` imply an `integration` umbrella. CI's hermetic lane runs
`-m "not integration"`; the umbrella means one expression excludes every live
gate at once — the thing that's **impossible today** (§4).

This also fixes the §0 defects as a side effect: one `pg_dsn()` kills the typo'd
skip reason and the presence-vs-reachability split; one `requires_gguf` probe
ends the `M3_TEST_GGUF`/`M3_EMBED_GGUF` divergence; one `requires_native` ends
the 3-idiom drift. **Consolidation isn't just less code — it removes latent
inconsistency bugs** (§3 fail-loud/consistent).

### 2. Availability fixtures replace hand-rolled skips (DRY the probe)

One canonical probe per service, in conftest, that a marker auto-applies — so a
test says *what it needs*, never *how to detect it*:

```python
# conftest.py — single source of truth for the DSN precedence rule
def pg_dsn() -> str | None:
    # M3_PRIMARY_PG_URL > M3_PG_URL — NEVER PG_URL (PG_URL points at PROD; see
    # CLAUDE.md / the pg-url-split memory). Centralizing it here means the
    # precedence can't drift across 19 files.
    return (os.environ.get("M3_PRIMARY_PG_URL")
            or os.environ.get("M3_PG_URL") or "").strip() or None

def _pg_reachable() -> bool: ...      # one probe, cached per session
def _embed_reachable() -> bool: ...   # wraps existing embed_backend_reachable()

@pytest.fixture
def pg_url() -> str:
    dsn = pg_dsn()
    if not dsn or not _pg_reachable():
        pytest.skip("no reachable PostgreSQL — set M3_PRIMARY_PG_URL to a throwaway cluster")
    return dsn
```

A marker hook auto-skips when the service is absent, so files drop their
`pytestmark = skipif(...)` boilerplate entirely:

```python
# conftest.py
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "pg_live" in item.keywords and not _pg_reachable():
            item.add_marker(pytest.mark.skip(reason="no reachable PostgreSQL"))
        if "embed_live" in item.keywords and not _embed_reachable():
            item.add_marker(pytest.mark.skip(reason="no reachable embedder"))
        if "native" in item.keywords and not _native_wheel_present():
            item.add_marker(pytest.mark.skip(reason="m3_core_rs wheel not installed"))
```

Net effect: a `*_pg_live.py` file becomes `pytestmark = pytest.mark.pg_live` at
the top and requests the `pg_url` fixture — no DSN parsing, no reachability
helper, no skipif. **~19 files × ~15 lines of identical boilerplate deleted.**

### 3. `m3_sandbox`: one hermetic-environment fixture (isolation)

Already implemented this session. The single autouse authority for a hermetic
env: pins all three roots (`M3_ENGINE_ROOT`/`M3_CONFIG_ROOT`/`M3_MEMORY_ROOT`) to
per-test tmp and clears the leaky backend/PG/CDW env (`_SANDBOX_CLEAR_ENV`). It
replaced the old `_isolate_engine_root` + the env half of
`_reset_storage_backend_cache`, and closed the backup-dir + CDW-env leak classes.
The 4 remaining autouse fixtures (`_restore_memory_modules`, `_guard_thread_leaks`,
`_close_db_pools`, embed-cache reset) are distinct *state* resets, each guarding a
cited incident — kept separate by design.

### 4. CI: tiered invocation (enforce what's currently unenforced)

Replace bare `pytest tests/` with tiers:

```yaml
# always, every push — the hermetic gate:
- run: pytest -m "not integration" --strict-markers
# native-wheel job (has the wheel installed):
- run: pytest -m "native" --strict-markers
# services job (Postgres in `services:`, embedder started):
  services: { postgres: ... }
  env: { M3_PRIMARY_PG_URL: postgresql://…@localhost/m3_test }
- run: pytest -m "integration" --strict-markers
```

This is the biggest correctness win: **integration tests become enforced** on a
job that provisions the services, instead of silently skipping forever.

### 5. Fast inner loop (developer ergonomics)

A `[tool.pytest]` addopts default of `-m "not slow"` for the bare invocation, or a
tiny `make test` / `make test-all` split, so the common case is fast and the full
run is one flag away.

## What this does NOT change

- **Test bodies / assertions** — untouched.
- **The 4 state-reset autouse fixtures** — each guards a distinct documented
  failure mode (module-identity, thread-leak, pool-leak, embed-cache); these are
  essential complexity, not churn targets (§2/§3).
- **Per-file `M3_DATABASE` / specific-root setup** — most is test-specific (the
  test asserts on the derived path); it legitimately *overrides* the sandbox
  default and stays. (An earlier estimate of "129 removable lines" was wrong on
  inspection — see below.)

## Tenet check (§12c)

- **§1 cross-platform / backend-agnostic:** markers gate PG/embed/native without
  assuming any are present; the DSN precedence rule is centralized once.
- **§3 fail-loud/safe:** `--strict-markers` turns a silent typo'd marker into a
  failure; availability fixtures skip with a clear reason, never a false pass.
- **§11 bench/regression discipline:** integration tiers become CI-enforced
  rather than dev-box-only.
- **§2 one feature per PR:** ship in stages — (a) markers + strict + sandbox
  (done) → (b) DRY the live-probes into fixtures + delete boilerplate → (c) CI
  tiering. Each is independently reviewable.

## Implementation notes (what actually shipped vs. the plan)

Two honest scope corrections made during implementation, recorded so the doc
matches reality (§3: never paper over a divergence):

- **PG / gguf / files_db — consolidated as designed.** 13 PG files → one
  `requires_pg` marker + `pg_dsn()`/`pg_url` in conftest (~130 dup lines gone,
  and the typo'd skip reason + presence-vs-reachability inconsistency fixed as a
  side effect). gguf's two-env-var split collapsed to the canonical
  `M3_TEST_GGUF` via `requires_gguf`. files_db's per-file `is_file()` skips →
  `requires_files_db`.
- **native — deliberately NOT force-consolidated.** On inspection the "3 idioms"
  gate on *three genuinely different conditions*: `importorskip("m3_core_rs")`
  (importable — and the file imports+uses it on the next line, so `importorskip`
  is the CORRECT tool, not a marker), `hasattr(m3_core_rs, "Governor")` (a
  stricter capability check), and `graph_mod.config.m3_core_rs is None` (respects
  the `M3_CORE_RS_DISABLE` runtime flag). Collapsing them into one
  `requires_native` probe would *change what each test gates on* — a correctness
  regression masquerading as cleanup. The marker is registered and available, but
  applied to nothing yet; the idiomatic `importorskip` sites stay. (§12c: don't
  make a damaging "simplification.")
- **embedder — already correct.** ~8 "embedder" tests MOCK the transport and run
  hermetically; only `test_doctor` needs a live one and already used the shared
  `embed_backend_reachable()`. No boilerplate to remove.

Result: `integration` umbrella auto-applies to 53 tests (50 pg + 1 gguf + 2
files_db); `-m "not integration"` is the clean hermetic CI lane;
`--strict-markers` makes a typo'd marker a hard error.

### Sandbox / shipped-payload discovery caveat (regression found + fixed)

Pinning `M3_MEMORY_ROOT` to tmp (the backup-leak fix) also blinds discovery of
*shipped read-only payload* that derives from `<M3_MEMORY_ROOT>/config/` — the
SLM profiles (`slm_intent._profile_search_dirs → <root>/config/slm`) stopped
resolving, so 2 `test_m3_enrich` tests that rely on the real `enrich_local_qwen`
profile failed under the full run. Fix: `m3_sandbox` points
`M3_SLM_PROFILES_DIR` at the repo's real `config/slm`, so state-root isolation
doesn't blind payload discovery. Tests that manage their own profile dir
(`test_slm_intent`) set that var themselves and override the sandbox default.
General principle: **isolate per-test STATE roots to tmp, but keep read-only
SHIPPED payload (profiles, migrations, templates) discoverable** — a root-pin
that also hides payload is the failure mode to watch for when extending the
sandbox.

## Open questions for review

1. **CI services** — provision Postgres + an embedder in a CI job (real
   integration enforcement), or keep integration dev-box-only and just make it
   *selectable*? The former is the real win but adds CI cost/complexity.
2. **Rollout order** — markers+fixtures first (mechanical, low-risk), CI tiering
   second? Or land the CI job together so the markers have teeth immediately?
3. **`slow` default** — make `-m "not slow"` the default local invocation, or
   leave the full run as default and document the fast path?
