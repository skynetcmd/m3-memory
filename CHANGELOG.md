# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2026.6.1.0] - 2026-06-01

### Added
- **Polars-accelerated bitemporal history** (`bin/memory/history.py`): High-performance
  columnar grouping and delta analysis for bitemporal memory timelines. Pure-Python
  fallback included; Polars is an optional performance dependency.
- **Doctor quick-repair mode** (`m3 doctor --fix`): Full CLI dispatch for auto-healing
  SQLite migrations, FTS5 index rebuilds, and bitemporal cohesion checks. `--dry-run`
  flag available to preview repairs without applying them.
- **SDK oxidation — native FFI shims** (`bin/m3_sdk.py`): Rust-backed implementations
  of system telemetry (`sysinfo`), advisory file locking (`fs2`), and atomic circuit
  breakers via PyO3. All shims are lazy-import-guarded behind `M3_CORE_RS_DISABLE`
  for environments without the native extension.
- **Decoupled config/engine roots**: `~/.m3/config` and `~/.m3/engine` are now
  independently relocatable via environment variables.
- **`bin/memory/history.py`**: New module for bitemporal delta grouping, timeline
  auditing, and Polars DataFrame integration.

### Changed
- `bin/m3_sdk.py`: `get_system_telemetry` routes through native sysinfo shim when
  available, falling back to `psutil` gracefully.
- `bin/memory_core.py`: Lazy shims added for history analytics and oxidation paths.

### Tests
- 31+ new tests across `test_doctor.py`, `test_sdk_oxidation.py`,
  `test_sqlite_vec_integration.py`, and `test_memory_history.py`.

## [2026.5.30.2] - 2026-05-30

### Added
- Setup wizard decoupled roots and dynamic plugin architecture lazy-loading.
- `sqlite-vec` integration and full FFI parity re-exports in `memory_core`.

### Fixed
- Restored missing public FFI re-exports (`os`, `_infer_change_agent_util`) in
  `memory_core`.

## [2026.5.30.1] - 2026-05-30

### Added
- Initial M3-v3 milestone 2 implementation: multi-session state, M3Context governor,
  FIPS 140-3 boundary compliance, hybrid search (FTS5 + vector + MMR), GDPR export.
