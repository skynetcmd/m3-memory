"""DB prep / profile loading — _today, _resolve_db, _load_profile_with_path,
_ensure_migration_025, _backup_db."""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from m3_sdk import get_m3_root
from slm_intent import (
    Profile,
    _parse_profile,
    load_profile,
)
from slm_intent import (
    invalidate_cache as invalidate_profile_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROFILE = os.environ.get("M3_ENRICH_PROFILE", "enrich_local_qwen")
BACKUP_DIR = Path(get_m3_root()) / "backups"


def _today() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _resolve_db(arg_path: str | None, env_var: str, default_name: str) -> Path | None:
    """Pick the DB to use for one role (core or chatlog).

    Order: explicit --core-db / --chatlog-db arg > env > default sibling.
    Returns None if no file exists at the resolved path (caller skips).
    """
    if arg_path:
        p = Path(arg_path).expanduser().resolve()
        return p if p.exists() else None
    env_val = os.environ.get(env_var)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        return p if p.exists() else None
    p = REPO_ROOT / "memory" / default_name
    return p if p.exists() else None


def _load_profile_with_path(name: str | None, path: str | None) -> Profile:
    """Load a profile by name (config/slm/<name>.yaml) OR explicit path.

    --profile-path wins if both are set.
    """
    if path:
        pth = Path(path).expanduser().resolve()
        if not pth.exists():
            sys.exit(f"ERROR: profile path not found: {pth}")
        # Reuse slm_intent's parser, which validates required keys.
        return _parse_profile(pth.stem, pth)
    invalidate_profile_cache()
    prof = load_profile(name or DEFAULT_PROFILE)
    if not prof:
        sys.exit(
            f"ERROR: profile {name!r} not found in config/slm/. "
            f"Use --profile-path /full/path.yaml, or copy "
            f"config/slm/enrich_custom_stub.yaml to make your own."
        )
    return prof


def _ensure_migration_025(db_path: Path) -> None:
    """Apply migration 025 (observation_queue, reflector_queue, obs index)
    if not already present. Best-effort — existing migrate_memory.py path
    may fail on chatlog DBs that have a different schema chain; we fall
    back to direct DDL in that case.

    No-op on PostgreSQL: this replays a SQLite-dialect migration file via
    sqlite3.connect + executescript + sqlite_master, none of which apply to PG,
    where these tables are created by the pg_040 migration + ensure_schema()."""
    from memory.backends import active_backend
    if active_backend().name != "sqlite":
        return  # PG: observation_queue/reflector_queue come from migrations
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        # Check what's already present.
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('observation_queue','reflector_queue')"
        ).fetchall()
        existing = {r[0] for r in rows}

        # Apply migration 025 if either queue is missing.
        if not {'observation_queue', 'reflector_queue'}.issubset(existing):
            up_path = REPO_ROOT / "memory" / "migrations" / "025_observation_queue.up.sql"
            if up_path.exists():
                conn.executescript(up_path.read_text(encoding="utf-8"))
                conn.commit()
                print(f"[m3-enrich] applied migration 025 to {db_path.name}", flush=True)
    finally:
        conn.close()


def _backup_db(db_path: Path) -> Path:
    """Copy db_path into BACKUP_DIR with a timestamp suffix. Returns
    the backup file path. Idempotent within the same minute (silently
    overwrites if the same minute-stamp already exists)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M")
    dst = BACKUP_DIR / f"{db_path.stem}.pre-enrich.{stamp}.db"
    shutil.copy2(db_path, dst)
    return dst
