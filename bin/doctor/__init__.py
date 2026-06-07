"""m3-memory doctor — health probes and DB repair, split per concern.

Narrow modules, plus a thin CLI dispatcher (`memory_doctor.py` at
`bin/` keeps its name for backward compatibility):

- `db_repair`           — legacy DB repair (timestamps, relationships, JSON)
- `cascade_probe`       — wrapper around `memory.doctor.memory_doctor_impl`
- `embed_server_probe`  — shells out to the Rust `m3-embed-server doctor`
- `oxidation_probe`     — reports m3_core_rs presence / staleness (report-only)

Each module's public entry point returns an int exit code (0=pass,
non-zero=fail-this-phase). The CLI aggregates the worst code across
phases the operator didn't opt out of.

Design: one file per concern, under ~100 LOC each. Single
responsibility. Easy to test, easy to plug a new probe in.
"""
from __future__ import annotations
