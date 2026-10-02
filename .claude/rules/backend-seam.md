---
paths:
  - "bin/memory/backends/**/*.py"
  - "bin/memory/search_pg.py"
  - "bin/pg_sync.py"
  - "bin/pg_fdw_sync.py"
  - "bin/migrate_pg.py"
  - "tests/conftest.py"
---

# The backend seam

- **`M3_DB_BACKEND` is the ONLY backend selector** (default `sqlite`).
  `M3_PRIMARY_PG_URL` never selects the backend — it only says where PostgreSQL
  is. A lane that sets the DSN without the selector silently tests SQLite; that
  shipped once, and a CI job titled "PostgreSQL lane" had no PG coverage.
- **`conftest.pg_dsn()` precedence**: `M3_PRIMARY_PG_URL` > `M3_PG_URL`, and
  **never** `PG_URL` (that is production).
- **`requires_pg` gates on REACHABILITY**, not on the seam being PG. A DSN being
  *present* is not the cluster being *up*; treating it as such costs a connect
  timeout per test instead of a skip.
- **Selector memoization** (`_resolved_name`, `_backends`) is cleared by
  `_reset_for_tests()`. Anything that resolves a backend at import — module
  constants, class bodies, class setup — runs before the test sandbox can reach
  it and will bind the wrong store.
- **Vary the DSN SHAPE, not just the host.** Socket vs TCP, user vs no user,
  password vs peer are different code paths. `postgresql:///db` carries no host
  and no user; writing either into SQL as `NULL` is a syntax error. Three TCP
  boxes tested one shape three times and missed it.
