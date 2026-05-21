"""Configuration for the files.db ingestion subsystem.

Re-exports the files_* knobs from `memory.config` (the canonical home for
all m3-memory path/env reads) plus files-store-specific constants that
don't make sense to put in the shared config.

Why the indirection: `memory.config` is the boundary at which env vars are
read at import time. Keeping FILES_DB_PATH there means every reader sees a
consistent value and there's a single grep target for "where is files.db?".
"""
from __future__ import annotations

import os

# Re-exports from memory.config — single source of truth.

# ──────────────────────────────────────────────────────────────────────────────
# Schema version
# ──────────────────────────────────────────────────────────────────────────────
# Bumped when the SQL DDL changes. files_memory.db._lazy_init compares this
# against schema_migrations.version and applies migrations in order. The
# initial schema is v1.
SCHEMA_VERSION: int = 3


# ──────────────────────────────────────────────────────────────────────────────
# Ingestion knobs (env-driven, sane defaults)
# ──────────────────────────────────────────────────────────────────────────────

# Hard cap on per-file size during walk. Files larger than this are skipped
# unless --force is passed. 10 MiB chosen to comfortably handle docs/PDFs
# while keeping a 1k-file ingest under 10 GiB worst-case.
FILES_MAX_FILE_BYTES: int = int(os.environ.get("M3_FILES_MAX_FILE_BYTES", str(10 * 1024 * 1024)))

# Per-leaf token cap. Leaves above this are truncated with a warning and
# `truncated=true` flag. The bge-m3 ctx default is 8192; we stay under that.
FILES_MAX_LEAF_TOKENS: int = int(os.environ.get("M3_FILES_MAX_LEAF_TOKENS", "7000"))

# Per-ingest file count cap. Override with --no-cap on the CLI.
FILES_MAX_FILES_PER_INGEST: int = int(os.environ.get("M3_FILES_MAX_FILES_PER_INGEST", "10000"))

# Follow symlinks during walk. Off by default to avoid filesystem loops.
FILES_FOLLOW_SYMLINKS: bool = os.environ.get("M3_FILES_FOLLOW_SYMLINKS", "0").lower() in (
    "1",
    "true",
    "yes",
)

# Default corpus_id used when --corpus is not passed. "default" is a valid
# sentinel; per-project users can override per ingest or via env.
FILES_DEFAULT_CORPUS: str = os.environ.get("M3_FILES_DEFAULT_CORPUS", "default")

# Default scope for promoted-to-memory items. Plan §14 Q3: "user" because
# file content is generally personal-knowledge shaped, not agent-private.
FILES_DEFAULT_SCOPE: str = os.environ.get("M3_FILES_DEFAULT_SCOPE", "user")


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline versioning (see plan §12)
# ──────────────────────────────────────────────────────────────────────────────
# Every ingestion_runs row records these so a stale-version sweep can target
# files ingested under outdated logic. Bump when behavior changes
# meaningfully — not on cosmetic edits.
INGESTER_VERSION: str = "p2.0.0"
# Chunker_version is a composite: bump any chunker, bump this. The dispatcher
# resolves the per-filetype chunker module's CHUNKER_VERSION and combines.
CHUNKER_DISPATCHER_VERSION: str = "p2.0.0"
# Extractor version: bumped any time the extraction prompt OR the post-
# processing logic changes meaningfully. Used by staleness review to find
# leaves extracted under outdated logic. None = no extraction yet.
EXTRACTOR_VERSION: str | None = "p2.0.0"

# Default extract mode when none is passed: 'none' = no extraction (P1
# behavior), 'inline' = sync per-leaf, 'queue' = defer to extract_pending.
# We keep 'none' as default so a fresh ingest doesn't surprise the user
# with a long LLM run. Pass --mode inline (or queue) explicitly.
DEFAULT_EXTRACT_MODE: str = os.environ.get("M3_FILES_DEFAULT_EXTRACT_MODE", "none")

# Per-leaf concurrency for inline extraction. Higher = faster but more
# memory + more pressure on the LLM endpoint. 2 is conservative; 4 fine
# on a local LM Studio / Ollama server.
EXTRACT_CONCURRENCY: int = int(os.environ.get("M3_FILES_EXTRACT_CONCURRENCY", "2"))

# Max attempts per leaf when extraction fails (transient errors). After
# this many failures the leaf is marked extraction_status='failed' and
# surfaced in staleness review.
EXTRACT_MAX_ATTEMPTS: int = int(os.environ.get("M3_FILES_EXTRACT_MAX_ATTEMPTS", "2"))

# Min characters in a leaf for extraction to be attempted. Tiny leaves
# rarely contain extractable facts; skip them to save LLM calls.
EXTRACT_MIN_LEAF_CHARS: int = int(os.environ.get("M3_FILES_EXTRACT_MIN_LEAF_CHARS", "120"))

# Promotion default scope when --scope not passed. See plan §14 Q3.
PROMOTION_DEFAULT_SCOPE: str = os.environ.get("M3_FILES_PROMOTION_SCOPE", "user")

# memory.db path for cross-store operations (entity link + promotion).
# Resolves via the active M3Context — this is just the default.
MEMORY_DB_PATH: str | None = os.environ.get("M3_DATABASE")  # None = M3Context default


# ──────────────────────────────────────────────────────────────────────────────
# Ignore patterns
# ──────────────────────────────────────────────────────────────────────────────
# Built-in directory ignore list, applied unconditionally. These are
# directories that almost never contain ingestable content; including them
# would multiply walk time and pollute search.
BUILTIN_DIR_IGNORES: frozenset[str] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "venv_gpu",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
    ".DS_Store",
    "_archived_flat_files_2026-05-09",
    "to_be_deleted",
})

# File-extension ignore (binary blobs, lockfiles, etc.).
BUILTIN_EXT_IGNORES: frozenset[str] = frozenset({
    ".lock",
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".o",
    ".a",
    ".class",
    ".jar",
    ".exe",
    ".bin",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".rar",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tiff",
    ".webp",
    ".ico",
    ".svg",
    ".mp3",
    ".mp4",
    ".wav",
    ".flac",
    ".ogg",
    ".avi",
    ".mov",
    ".mkv",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
})
