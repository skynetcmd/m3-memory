---
tool: bin/m3_sdk.py
sha1: df407b45be43
mtime_utc: 2026-04-18T03:45:31.260359+00:00
generated_utc: 2026-04-18T05:16:53.124247+00:00
private: false
---

# bin/m3_sdk.py

## Purpose

Shared SDK for M3 memory system. Provides SQLite connection pooling, resilient HTTP client with circuit-breaker pattern, PostgreSQL warehouse access, and environment bootstrapping. Used as library by CLI tools.

## Entry points / Public API

- `M3Context` class (line 42) — Runtime context manager. Initializes DB pool, HTTP client, secret resolution.
- `M3Context.__init__(db_path=None)` (line 43) — Load `.env`, configure DB path, create pool.
- `M3Context.get_sqlite_conn()` (line 162) — Context manager yielding pooled SQLite connection.
- `M3Context.request_with_retry(method, url, retries=3, **kwargs)` (line 138) — Async HTTP with exponential backoff + circuit breaker.
- `M3Context.pg_connection()` (line 177) — Context manager for PostgreSQL warehouse (2 retries, 10s timeout).
- `M3Context.get_secret(service)` (line 173) — Resolve API keys via `auth_utils.get_api_key()`.
- `M3Context.get_setting(key, default=None)` (line 56) — Read environ or `.env`.
- `M3Context.get_path(relative_path)` (line 53) — Resolve project-relative paths.
- `M3Context.get_async_client()` (line 110) — Shared `httpx.AsyncClient` (HTTP/2, 4800s read timeout).
- `resolve_venv_python()` (line 35) — Cross-platform path to project `.venv/bin/python`.

## CLI flags / arguments

_(no CLI surface — invoked as a library/module.)_

## Environment variables read

- `M3_MEMORY_ROOT` (default: script parent) — Project root path.
- `DB_POOL_SIZE` (default: 5) — SQLite connection pool size.
- `DB_POOL_TIMEOUT` (default: 10s) — SQLite acquire timeout.
- `PG_URL` (env or keychain) — PostgreSQL connection string (warehouse).

## Calls INTO this repo (intra-repo imports)

- `auth_utils.get_api_key(service)` — Resolve secrets.

## Calls OUT (external side-channels)

**HTTP**
- `httpx.AsyncClient()` — Async HTTP/2 with 5s connect, 4800s read, 10s write timeout.

**SQLite**
- `sqlite3.connect(self.db_path)` — WAL mode, PRAGMA journal_mode=WAL, synchronous=NORMAL, foreign_keys ON, 64MB cache, 512MB mmap.

**PostgreSQL**
- `psycopg2.connect(PG_URL)` — 10s timeout, 2 retry attempts.

## File dependencies

- `.env` (loaded once at init)
- `memory/agent_memory.db` (default DB path)

## Re-validation

If `sha1` above differs from current file, inventory is stale — re-read tool and regenerate via `python bin/gen_tool_inventory.py`.
