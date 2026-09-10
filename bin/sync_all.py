#!/usr/bin/env python3
"""
sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL).
Runs pg_sync.py once per configured DB. Offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable or DB absent.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)

DB list:
    Repo default: `memory/agent_memory.db`. The agent_memory manifest sweeps
    both `main` and `chatlog` targets internally, so chatlog data gets synced
    in the same pass without listing it separately. Bench DBs and other
    custom databases are NOT auto-detected — set M3_SYNC_DBS to include them.

    Example self-host override:
        M3_SYNC_DBS=memory/agent_memory.db:../m3-memory-bench/data/agent_bench.db
"""
import argparse
import logging
import os
import pathlib
import socket
import subprocess
import sys

from m3_sdk import getenv_compat

IS_WIN = sys.platform == "win32"

# Subprocess timeout for pg_sync.py. First-run full syncs can take several
# minutes; delta syncs are much faster. Override with M3_PG_SYNC_TIMEOUT (seconds).
PG_SYNC_TIMEOUT = int(os.environ.get("M3_PG_SYNC_TIMEOUT", "600"))

BASE    = pathlib.Path(__file__).parent.parent.resolve()
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync_all.log"


def _resolve_python() -> str:
    """The interpreter to run the pg_sync.py subprocess with. Use the CURRENT
    interpreter (sys.executable) — it is correct for every install layout: a
    pipx venv (no in-tree .venv), an editable/dev tree, or a system install.
    The old hardcoded BASE/.venv guess only existed in a dev checkout and 404'd
    for pipx users (FileNotFoundError [WinError 2] -> the scheduled sync task
    failed silently). Fall back to the .venv guess only if sys.executable is
    somehow unavailable."""
    exe = sys.executable
    if exe and pathlib.Path(exe).exists():
        return exe
    guess = BASE / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return str(guess)


PY = _resolve_python()
TARGET_IP = getenv_compat("M3_POSTGRES_SERVER", "POSTGRES_SERVER", getenv_compat("M3_SYNC_TARGET_IP", "SYNC_TARGET_IP", ""))

# Logging is configured in main() via setup_task_runtime so scheduled-task
# runs self-log without a shell `>>` redirect. This minimal fallback keeps
# `log` usable if a caller imports this module without running main().
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sync_all")

# ── DB list resolution ────────────────────────────────────────────────────────
# Repo default: sync the production memory DB. The agent_memory.yaml manifest
# already sweeps BOTH `main` (agent_memory.db) and `chatlog` (agent_chatlog.db)
# targets in a single pg_sync invocation, so we don't list chatlog separately —
# doing so would either re-sync the same data or fail on a missing manifest.
# Anything beyond this (bench DBs, custom layouts) is self-host territory —
# users wire it up themselves via M3_SYNC_DBS.
_DEFAULT_DBS = [
    "memory/agent_memory.db",
]


def _resolve_dbs() -> list[pathlib.Path]:
    """Return list of DB paths to sync.

    Priority:
      1. M3_SYNC_DBS env var (explicit override; colon- or comma-separated paths).
      2. m3_sdk.resolve_db_path() — the Homecoming-aware resolver (honours
         M3_ENGINE_ROOT / M3_MEMORY_ROOT / M3_DATABASE).
      3. Repo-relative fallback (memory/agent_memory.db) if m3_sdk is not importable.

    Bench DBs and any other databases are NOT auto-detected — set M3_SYNC_DBS
    if you self-host a custom layout.
    """
    raw = os.environ.get("M3_SYNC_DBS", "")
    if raw:
        parts = [p.strip() for p in raw.replace(",", ":").split(":") if p.strip()]
        resolved = []
        for p in parts:
            path = pathlib.Path(p)
            if not path.is_absolute():
                path = BASE / path
            resolved.append(path.resolve())
        return resolved

    # No explicit override — use the Homecoming-aware DB resolver so we always
    # sync the live database.
    try:
        from m3_sdk import resolve_db_path
        return [pathlib.Path(resolve_db_path()).resolve()]
    except Exception as exc:
        log.warning(f"m3_sdk.resolve_db_path() unavailable ({exc}); falling back to repo default")
        path = BASE / _DEFAULT_DBS[0]
        return [path.resolve()]


# ── Network check ─────────────────────────────────────────────────────────────

def is_reachable(host: str, port: int = 5432, timeout: float = 3.0) -> bool:
    """TCP probe — faster and more reliable than ping across platforms."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── pg_sync runner ────────────────────────────────────────────────────────────

def run_pg_sync_for_db(db_path: pathlib.Path, dry_run: bool) -> bool:
    """Run pg_sync.py --db <path> for one database. Returns True on success."""
    if not db_path.exists():
        log.info(f"  skipping {db_path} — not present on this peer")
        return True  # not an error; this peer just doesn't have that DB

    if dry_run:
        log.info(f"[DRY-RUN] Would run pg_sync.py --db {db_path}")
        return True

    log.info(f"Running pg_sync.py --db {db_path} ...")
    try:
        from _task_runtime import no_window_kwargs
        result = subprocess.run(
            [str(PY), str(BASE / "bin" / "pg_sync.py"), "--db", str(db_path)],
            capture_output=True, text=True, timeout=PG_SYNC_TIMEOUT,
            **no_window_kwargs(),
        )
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log.info(f"  pg_sync[{db_path.stem}]: {line}")
        if result.returncode == 0:
            log.info(f"pg_sync completed for {db_path.stem}.")
            return True
        else:
            log.error(f"pg_sync exited with code {result.returncode} for {db_path.stem}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"pg_sync timed out after {PG_SYNC_TIMEOUT}s for {db_path.stem}")
        return False
    except Exception as e:
        log.error(f"pg_sync failed for {db_path.stem}: {type(e).__name__}: {e}")
        return False


# Why the FDW fast-path declined, for the refusal message downstream. Module
# state rather than a return value because _try_pg_fdw_fastpath's tri-state
# contract (True/False/None) is load-bearing at its call site and widening it to
# carry a reason would ripple further than this fix warrants.
_FDW_LAST_REASON: str = ""


def _set_fdw_reason(reason: str) -> None:
    """Record why the fast-path declined.

    A function rather than an inline `global`: two `global` statements in one
    function body is a SyntaxError, and the two fall-back handlers below both
    need to write this. Ruff does not catch that — only importing the module does.
    """
    global _FDW_LAST_REASON  # noqa: PLW0603 — deliberate module-level state
    _FDW_LAST_REASON = reason


def _try_pg_fdw_fastpath(dry_run: bool) -> "bool | None":
    """When the PRIMARY store is PostgreSQL, try the postgres_fdw fast-path
    (set-based server-side upserts) instead of the row-by-row SQLite<->PG bridge.

    Returns True/False on a completed fast-path run, or None to signal "not
    applicable / unavailable — fall back to the generic bridge" (SQLite/MariaDB
    primary, missing extension, unreachable warehouse). Never raises."""
    try:
        from memory.backends import active_backend
        if active_backend().name != "postgres":
            return None  # SQLite/MariaDB primary -> generic bridge
    except Exception:
        return None
    try:
        import pg_fdw_sync
        from m3_sdk import M3Context
        ctx = M3Context.for_db(None)
        warehouse_dsn = ctx.get_secret("PG_URL") or os.environ.get("M3_CDW_PG_URL")
        if not warehouse_dsn:
            # Deliberate skip, not a failure: sync is opt-in, and main() already
            # exits cleanly when SYNC_TARGET_IP is unset (:390). Returning True
            # keeps that contract. Worded to say WHY nothing happened, so this
            # does not read like the silent success the rest of this change
            # exists to remove — "nothing to sync" implied there was nothing TO
            # sync, when in fact nothing was CONFIGURED.
            log.info(
                "PG primary, but no warehouse DSN is configured "
                "(M3_CDW_PG_URL / PG_URL) — sync not set up on this peer; skipping."
            )
            return True
        # ── The PRIMARY store, not the warehouse ──────────────────────────────
        # `ctx.pg_connection()` is the WAREHOUSE role by contract: it resolves
        # M3_CDW_PG_URL > PG_URL and deliberately does NOT read M3_PG_URL (see
        # M3Context.pg_connection's docstring). Using it here opened the
        # warehouse as `primary_conn`, so the FDW fast-path wired postgres_fdw
        # from the warehouse back to ITSELF and synced the warehouse with itself
        # — the primary store was never touched, and the run reported success.
        #
        # The primary must come from the primary resolver, whose own docstring
        # states the invariant this violated: "the warehouse DSN must never reach
        # the primary store through env resolution."
        from m3_core.paths import resolve_primary_pg_dsn
        from memory.backends.postgres_backend import PostgresBackend

        primary_dsn = resolve_primary_pg_dsn()
        if not primary_dsn:
            log.warning(
                "PG primary selected (M3_DB_BACKEND=postgres) but no primary DSN "
                "resolved — set M3_PRIMARY_PG_URL (or M3_PG_URL). Falling back to "
                "the generic bridge."
            )
            return None

        # Watermarks stay LOCAL to this machine (per-peer cursors): a Mac, Linux
        # box, and PC each track their own push/pull against the shared CDW. On a
        # PG primary they live in the primary's own sync_watermarks table.
        #
        # ⚠ The same-database guard must be invoked EXPLICITLY here.
        # PostgresBackend.__init__ does `dsn or _resolve_dsn()`, so passing a DSN
        # BYPASSES _resolve_dsn — and with it _reject_same_as_warehouse. Relying
        # on construction to guard would silently re-permit the exact failure this
        # fix exists to close. Reuse the existing guard rather than writing a
        # second copy of the (host, port, dbname) comparison.
        from memory.backends.postgres_backend import _reject_same_as_warehouse

        try:
            _reject_same_as_warehouse(primary_dsn)
        except RuntimeError as e:
            log.error(f"Refusing the FDW fast-path: {e}")
            return False  # a real misconfiguration — do NOT fall through

        # try/finally: PostgresBackend owns a ThreadedConnectionPool, and this is a
        # short-lived cron process — leaking the pool would hold primary connections
        # open past the run on every hourly invocation.
        _primary_backend = PostgresBackend(dsn=primary_dsn)
        try:
            return _fdw_run(_primary_backend, pg_fdw_sync, warehouse_dsn, dry_run)
        finally:
            try:
                _primary_backend.close()
            except Exception:  # noqa: BLE001 — cleanup must not mask a real result
                pass
    except pg_fdw_sync.FdwUnavailable as e:
        _set_fdw_reason(f"FDW fast-path unavailable: {e}")
        log.info(f"FDW fast-path unavailable ({e}) — falling back to generic bridge.")
        return None
    except Exception as e:
        _set_fdw_reason(f"FDW fast-path errored: {type(e).__name__}: {e}")
        log.warning(f"FDW fast-path errored ({type(e).__name__}: {e}) — "
                    f"falling back to generic bridge.")
        return None


def _fdw_run(primary_backend, pg_fdw_sync, warehouse_dsn: str, dry_run: bool) -> bool:
    """Run the FDW sync against an already-resolved PRIMARY backend.

    Extracted from `_try_pg_fdw_fastpath` so the backend's connection pool can be
    closed in a `finally` there without wrapping the whole body in another level
    of try. Exceptions propagate: the caller owns the fall-back decision.
    """
    # Placeholders and the upsert clause come from the DIALECT, not hardcoded
    # `%s`. This block is PG-only *today* (the FDW fast-path requires it), but a
    # literal placeholder is a portability bug the moment a third backend lands
    # — and it costs nothing to ask the seam instead. `on_conflict_update`
    # renders identically on both current backends, which is the point: the call
    # site stops caring.
    dialect = primary_backend.dialect()
    p = dialect.param()
    upsert_clause = dialect.on_conflict_update("(direction)", ["last_synced_at"])

    with primary_backend.connection() as primary_conn:
        primary_conn.autocommit = False
        with primary_conn.cursor() as wc:
            # TODO(pg_053): this DDL is created ad-hoc here AND in pg_sync.py,
            # with different SQL at each site, and belongs in a migration. Phase 2
            # of the PG-local-sync work removes both copies.
            wc.execute("CREATE TABLE IF NOT EXISTS sync_watermarks "
                       "(direction TEXT PRIMARY KEY, last_synced_at TEXT)")

        def get_wm(direction: str):
            with primary_conn.cursor() as c:
                c.execute(
                    f"SELECT last_synced_at FROM sync_watermarks WHERE direction={p}",
                    (direction,))
                row = c.fetchone()
                return row[0] if row else None

        def set_wm(direction: str, ts: str):
            with primary_conn.cursor() as c:
                c.execute(
                    f"INSERT INTO sync_watermarks (direction, last_synced_at) "
                    f"VALUES ({p}, {p}) {upsert_clause}", (direction, ts))

        res = pg_fdw_sync.sync_pg_to_pg(primary_conn, warehouse_dsn,
                                        get_wm, set_wm, dry_run=dry_run)
        primary_conn.commit()
        log.info(f"PG->PG FDW fast-path completed: {res}")
        return True


def _local_backend_name() -> str:
    """The backend the LOCAL store uses, or 'sqlite' if it cannot be determined.

    Defaults to sqlite deliberately: that is the historical assumption, so an
    unresolvable backend keeps existing SQLite deployments working rather than
    refusing them (§3 — the refusal below must not fire on a healthy user).
    """
    try:
        from memory.backends import active_backend
        return active_backend().name
    except Exception:  # noqa: BLE001 — seam unavailable: assume the legacy shape
        return "sqlite"


def run_pg_sync(dry_run: bool) -> bool:
    """Run the warehouse sync. On a PostgreSQL primary, prefer the postgres_fdw
    fast-path; otherwise (or if it's unavailable) run the generic pg_sync.py
    bridge for each configured database. Returns True if all succeed."""
    fast = _try_pg_fdw_fastpath(dry_run)
    if fast is not None:
        return fast

    # ── The generic bridge is SQLite-only on the LOCAL side ───────────────────
    # bin/pg_sync.py opens the local store with sqlite3.connect(). On a
    # PostgreSQL primary that either finds no file, or — worse — opens a stale or
    # empty agent_memory.db, reads zero rows, writes zero rows, and reports
    # SUCCESS. A replication path that silently moves nothing is the failure mode
    # this refusal exists to remove: an operator sees a green hourly job while
    # the two stores drift apart.
    #
    # Reaching here on a PG primary always means the FDW fast-path declined, so
    # name that reason too — otherwise the operator is told the bridge is
    # unsupported without learning that the supported path was one probe away.
    backend = _local_backend_name()
    if backend != "sqlite":
        log.error(
            f"Local store is {backend!r} (M3_DB_BACKEND), but the generic sync "
            "bridge supports only a SQLite local store.\n"
            f"  {_FDW_LAST_REASON or 'FDW fast-path did not run.'}\n"
            "  Refusing to run rather than syncing an empty database.\n"
            "  Fix: ensure M3_PRIMARY_PG_URL points at the primary store and, for "
            "the fast path, ask an administrator for `CREATE EXTENSION postgres_fdw;` "
            "on it."
        )
        return False

    dbs = _resolve_dbs()
    log.info(f"pg_sync target DBs: {[str(d) for d in dbs]}")
    results = []
    for db in dbs:
        ok = run_pg_sync_for_db(db, dry_run)
        results.append(ok)
    return all(results)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hourly sync runner")
    parser.add_argument("--dry-run", action="store_true", help="Check connectivity only")
    sys.path.insert(0, str(BASE / "bin"))
    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg
    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(args.log_file or LOG_FILE, lock_name="sync_all")

    if args.database:
        # Pass-through env so the pg_sync subprocess inherits.
        os.environ["M3_DATABASE"] = args.database

    # sys.platform, not platform.system() (WMI-hang risk on Py3.14/Windows).
    _os = {"darwin": "Darwin", "win32": "Windows"}.get(sys.platform, "Linux")
    log.info(f"=== sync_all starting [{_os}] ===")

    if not TARGET_IP:
        log.info("SYNC_TARGET_IP not set — skipping sync.")
        sys.exit(0)

    if not is_reachable(TARGET_IP):
        log.warning(f"PostgreSQL data warehouse ({TARGET_IP}) unreachable — skipping sync (will retry next hour).")
        sys.exit(0)

    log.info(f"PostgreSQL data warehouse ({TARGET_IP}) reachable — running full sync.")

    pg_ok = run_pg_sync(args.dry_run)

    if pg_ok:
        log.info("=== sync_all complete: all systems synced ===")
        sys.exit(0)
    else:
        log.error(f"=== sync_all finished with errors: pg={pg_ok} ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
