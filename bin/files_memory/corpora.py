"""Multi-corpus management for files-memory — phase 4.

Logical multi-corpus: a single `files.db` holds many corpora, distinguished
by `file_nodes.corpus_id`. Per-corpus defaults live in the
`corpus_settings` table (added in schema v2). This module owns:

  - corpus_create:   register a new corpus + optional default overrides
  - corpus_list:     enumerate corpora with row counts
  - corpus_get:      fetch a single corpus's settings
  - corpus_set:      update settings for an existing corpus
  - corpus_delete:   remove a corpus + cascade-delete its file_nodes
                     (DANGEROUS — opt-in via cascade=True; default refuses
                     when files exist)
  - resolve_default_corpus: cli flag > env > corpus_settings.default >
                            global default

Per-corpus settings (free-form JSON, recognized keys):
  extract_mode      — 'none'|'inline'|'queue' (overrides global default)
  scope             — promotion default scope override
  description       — human-readable label
  default           — true marks this corpus as the default for the
                      installation (only one row can have default=true)
  created_at        — set automatically on insert
  retention_days    — phase-5 hook; advisory only today
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from . import config
from .config import files_table
from .db import _db

logger = logging.getLogger("files_memory.corpora")


# ──────────────────────────────────────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class CorpusInfo:
    """Snapshot of a corpus."""
    corpus_id: str
    settings: dict
    file_node_count: int
    leaf_count: int
    created_at: Optional[str]
    is_default: bool


# ──────────────────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────────────────
def resolve_default_corpus(
    *,
    cli_corpus: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Resolution order:
      1. cli_corpus arg (explicit)
      2. M3_FILES_CORPUS env var
      3. corpus_settings row with settings.default == true
      4. config.FILES_DEFAULT_CORPUS (typically 'default')
    """
    if cli_corpus:
        return cli_corpus
    env = os.environ.get("M3_FILES_CORPUS", "").strip()
    if env:
        return env
    try:
        from memory.backends import dialect as _dialect
        _d = _dialect()
        with _db(db_path) as conn:
            row = conn.execute(
                f"SELECT corpus_id, settings FROM {files_table('corpus_settings')} "
                f"WHERE {_d.json_extract_int('settings', 'default')} = 1 "
                f"LIMIT 1"
            ).fetchone()
            if row:
                return row["corpus_id"]
    except Exception as e:  # noqa: BLE001 — degrade to the configured default
        logger.debug("resolve_default_corpus: db lookup failed: %s", e)
    return config.FILES_DEFAULT_CORPUS


# ──────────────────────────────────────────────────────────────────────────────
# Corpus CRUD
# ──────────────────────────────────────────────────────────────────────────────
_RECOGNIZED_SETTING_KEYS = {
    "extract_mode", "scope", "description", "default",
    "retention_days", "created_at",
}


def corpus_create(
    corpus_id: str,
    *,
    description: Optional[str] = None,
    extract_mode: Optional[str] = None,
    scope: Optional[str] = None,
    default: bool = False,
    db_path: Optional[str] = None,
) -> CorpusInfo:
    """Register a new corpus. Errors if corpus_id already exists.

    Setting `default=True` unsets any other corpus's default flag in the
    same transaction.
    """
    if not corpus_id or not corpus_id.strip():
        raise ValueError("corpus_id must be a non-empty string")
    corpus_id = corpus_id.strip()

    if extract_mode is not None and extract_mode not in {"none", "inline", "queue"}:
        raise ValueError(
            f"extract_mode must be 'none'|'inline'|'queue', got: {extract_mode!r}"
        )

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    settings: dict = {"created_at": now}
    if description is not None:
        settings["description"] = description
    if extract_mode is not None:
        settings["extract_mode"] = extract_mode
    if scope is not None:
        settings["scope"] = scope
    if default:
        settings["default"] = True

    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    with _db(db_path) as conn:
        existing = conn.execute(
            f"SELECT corpus_id FROM {files_table('corpus_settings')} WHERE corpus_id = {_p}",
            (corpus_id,),
        ).fetchone()
        if existing:
            raise ValueError(f"corpus already exists: {corpus_id!r}")

        if default:
            # Clear any other default before setting this one.
            _clear_default_flag(conn)

        conn.execute(
            f"INSERT INTO {files_table('corpus_settings')}(corpus_id, settings) "
            f"VALUES ({_p}, {_p})",
            (corpus_id, json.dumps(settings)),
        )

    return _build_corpus_info(corpus_id, settings, file_node_count=0, leaf_count=0,
                              created_at=now, is_default=default)


def corpus_list(*, db_path: Optional[str] = None) -> list[CorpusInfo]:
    """Enumerate corpora with row counts. Includes corpora that have
    file_nodes but no corpus_settings row (returned with empty settings)."""
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    with _db(db_path) as conn:
        # Corpora that have settings rows
        settings_rows = {
            r["corpus_id"]: r["settings"]
            for r in conn.execute(
                f"SELECT corpus_id, settings FROM {files_table('corpus_settings')}",
            ).fetchall()
        }
        # Also pick up corpora that have file_nodes but no settings row.
        ids_in_files = {
            r["corpus_id"] for r in conn.execute(
                f"SELECT DISTINCT corpus_id FROM {files_table('file_nodes')} "
                f"WHERE corpus_id IS NOT NULL",
            ).fetchall()
        }
        all_ids = set(settings_rows.keys()) | ids_in_files

        out: list[CorpusInfo] = []
        for cid in sorted(all_ids):
            settings = _safe_json(settings_rows.get(cid)) or {}
            file_n = conn.execute(
                f"SELECT COUNT(*) FROM {files_table('file_nodes')} WHERE corpus_id = {_p} "
                f"AND superseded_by IS NULL",
                (cid,),
            ).fetchone()[0]
            leaf_n = conn.execute(
                f"SELECT COUNT(*) FROM {files_table('leaves')} l "
                f"JOIN {files_table('file_nodes')} fn ON fn.uuid = l.file_node "
                f"WHERE fn.corpus_id = {_p} AND fn.superseded_by IS NULL "
                f"AND l.superseded_by IS NULL",
                (cid,),
            ).fetchone()[0]
            out.append(_build_corpus_info(
                cid, settings, file_n, leaf_n,
                created_at=settings.get("created_at"),
                is_default=bool(settings.get("default")),
            ))
        return out


def corpus_get(corpus_id: str, *, db_path: Optional[str] = None) -> Optional[CorpusInfo]:
    """Fetch a single corpus's info. None if no settings row AND no file_nodes."""
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    with _db(db_path) as conn:
        row = conn.execute(
            f"SELECT settings FROM {files_table('corpus_settings')} WHERE corpus_id = {_p}",
            (corpus_id,),
        ).fetchone()
        settings = _safe_json(row["settings"]) if row else None

        file_n = conn.execute(
            f"SELECT COUNT(*) FROM {files_table('file_nodes')} WHERE corpus_id = {_p} "
            f"AND superseded_by IS NULL",
            (corpus_id,),
        ).fetchone()[0]
        leaf_n = conn.execute(
            f"SELECT COUNT(*) FROM {files_table('leaves')} l "
            f"JOIN {files_table('file_nodes')} fn ON fn.uuid = l.file_node "
            f"WHERE fn.corpus_id = {_p} AND fn.superseded_by IS NULL "
            f"AND l.superseded_by IS NULL",
            (corpus_id,),
        ).fetchone()[0]

        if settings is None and file_n == 0:
            return None

        settings = settings or {}
        return _build_corpus_info(
            corpus_id, settings, file_n, leaf_n,
            created_at=settings.get("created_at"),
            is_default=bool(settings.get("default")),
        )


def corpus_set(
    corpus_id: str,
    *,
    description: Optional[str] = None,
    extract_mode: Optional[str] = None,
    scope: Optional[str] = None,
    default: Optional[bool] = None,
    retention_days: Optional[int] = None,
    db_path: Optional[str] = None,
) -> CorpusInfo:
    """Update settings for an existing corpus. None args are no-ops.

    Creates the corpus_settings row if it doesn't exist (e.g. when a corpus
    has file_nodes but no settings — auto-created on first ingest into a
    new corpus_id, settings can be filled in later).
    """
    if extract_mode is not None and extract_mode not in {"none", "inline", "queue"}:
        raise ValueError(
            f"extract_mode must be 'none'|'inline'|'queue', got: {extract_mode!r}"
        )

    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    with _db(db_path) as conn:
        row = conn.execute(
            f"SELECT settings FROM {files_table('corpus_settings')} WHERE corpus_id = {_p}",
            (corpus_id,),
        ).fetchone()
        if row is None:
            settings: dict[str, object] = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            is_insert = True
        else:
            settings = _safe_json(row["settings"]) or {}
            is_insert = False

        if description is not None:
            settings["description"] = description
        if extract_mode is not None:
            settings["extract_mode"] = extract_mode
        if scope is not None:
            settings["scope"] = scope
        if retention_days is not None:
            settings["retention_days"] = int(retention_days)
        if default is True:
            _clear_default_flag(conn, except_corpus_id=corpus_id)
            settings["default"] = True
        elif default is False:
            settings.pop("default", None)

        settings_json = json.dumps(settings)
        if is_insert:
            conn.execute(
                f"INSERT INTO {files_table('corpus_settings')}(corpus_id, settings) "
                f"VALUES ({_p}, {_p})",
                (corpus_id, settings_json),
            )
        else:
            conn.execute(
                f"UPDATE {files_table('corpus_settings')} SET settings = {_p}, "
                f"updated_at = {_d.now()} WHERE corpus_id = {_p}",
                (settings_json, corpus_id),
            )

    # Recompute info after the write
    info = corpus_get(corpus_id, db_path=db_path)
    assert info is not None  # we just wrote it
    return info


def corpus_delete(
    corpus_id: str,
    *,
    cascade: bool = False,
    db_path: Optional[str] = None,
) -> dict:
    """Remove a corpus and (optionally) its file_nodes.

    Refuses when the corpus has file_nodes unless cascade=True.
    Cascade is DESTRUCTIVE — file_nodes, leaves, facts, embeddings,
    promotion_markers all go away.
    """
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    with _db(db_path) as conn:
        file_n = conn.execute(
            f"SELECT COUNT(*) FROM {files_table('file_nodes')} WHERE corpus_id = {_p}",
            (corpus_id,),
        ).fetchone()[0]

        if file_n > 0 and not cascade:
            raise ValueError(
                f"corpus {corpus_id!r} has {file_n} file_nodes; pass cascade=True to delete"
            )

        deleted = {"file_nodes": 0, "leaves": 0, "facts": 0, "settings": 0}
        if cascade and file_n > 0:
            uuids = [
                r["uuid"] for r in conn.execute(
                    f"SELECT uuid FROM {files_table('file_nodes')} WHERE corpus_id = {_p}",
                    (corpus_id,),
                ).fetchall()
            ]
            # ON DELETE CASCADE on leaves / facts / leaf_embeddings handles the rest.
            CHUNK = 500
            for start in range(0, len(uuids), CHUNK):
                chunk = uuids[start:start + CHUNK]
                placeholders = _d.placeholder(len(chunk))
                # Count what's about to be cascaded.
                deleted["leaves"] += conn.execute(
                    f"SELECT COUNT(*) FROM {files_table('leaves')} WHERE file_node IN ({placeholders})",
                    chunk,
                ).fetchone()[0]
                deleted["facts"] += conn.execute(
                    f"SELECT COUNT(*) FROM {files_table('facts')} WHERE file_node IN ({placeholders})",
                    chunk,
                ).fetchone()[0]
                conn.execute(
                    f"DELETE FROM {files_table('file_nodes')} WHERE uuid IN ({placeholders})", chunk,
                )
                deleted["file_nodes"] += len(chunk)

        settings_n = conn.execute(
            f"DELETE FROM {files_table('corpus_settings')} WHERE corpus_id = {_p}",
            (corpus_id,),
        ).rowcount or 0
        deleted["settings"] = settings_n

    return {"corpus_id": corpus_id, "deleted": deleted}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _build_corpus_info(
    corpus_id: str, settings: dict, file_node_count: int, leaf_count: int,
    created_at: Optional[str], is_default: bool,
) -> CorpusInfo:
    return CorpusInfo(
        corpus_id=corpus_id,
        settings=settings,
        file_node_count=file_node_count,
        leaf_count=leaf_count,
        created_at=created_at,
        is_default=is_default,
    )


def _clear_default_flag(conn, except_corpus_id: Optional[str] = None) -> None:
    """Drop the `default` key from every corpus_settings.settings row,
    optionally skipping one.

    The JSON mutation is done in PYTHON (read → pop → write back) rather than via
    SQLite's json_remove(): json_remove is a SQLite-only function with no portable
    equivalent, and `settings` is a TEXT column on both backends, so decoding /
    re-encoding here is correct and backend-uniform. json_extract_int() picks the
    rows that actually carry a `default` flag so we only rewrite those."""
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    sel = (
        f"SELECT corpus_id, settings FROM {files_table('corpus_settings')} "
        f"WHERE {_d.json_extract_int('settings', 'default')} = 1"
    )
    params: list = []
    if except_corpus_id is not None:
        sel += f" AND corpus_id != {_p}"
        params.append(except_corpus_id)
    rows = conn.execute(sel, params).fetchall()
    for r in rows:
        s = _safe_json(r["settings"]) or {}
        s.pop("default", None)
        conn.execute(
            f"UPDATE {files_table('corpus_settings')} "
            f"SET settings = {_p}, updated_at = {_d.now()} WHERE corpus_id = {_p}",
            (json.dumps(s), r["corpus_id"]),
        )


def _safe_json(s: Optional[str]) -> Optional[dict]:
    if not s:
        return None
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None
