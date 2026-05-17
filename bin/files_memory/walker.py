"""Directory walker for files_ingest.

Walks a root path, yields WalkEntry records for every file that passes
the filter pipeline. Pure generator — does NOT touch the DB.

Filter pipeline (applied in order, cheap-first):
  1. Directory ignore set (skip whole subtrees)
  2. .gitignore + .m3ignore (per-directory pathspec)
  3. Symlink policy (off by default)
  4. --include / --exclude glob filters
  5. Extension ignore set
  6. Size cap (--force overrides)
  7. Binary sniff (NUL byte + non-printable ratio)

Each WalkEntry carries enough metadata for the ingester to decide
re-ingestion without re-walking. The walker is intentionally pure: it
emits, the orchestrator decides.

Public API:
    walk(root, **opts) -> Iterator[WalkEntry]
    WalkEntry           — dataclass
    WalkStats           — counters returned at end (via stats arg)
"""
from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import config
from .identity import filetype_for, looks_binary

logger = logging.getLogger("files_memory.walker")


@dataclass
class WalkEntry:
    """One file that survived all filters, ready for chunking/ingestion."""
    path: str                # absolute
    repo_relative: str | None
    filename: str
    filetype: str
    size_bytes: int
    mtime: float             # POSIX timestamp
    ctime: float             # POSIX timestamp (or birthtime where supported)


@dataclass
class WalkStats:
    """Counters surfaced to the caller after a walk completes."""
    files_seen: int = 0
    files_yielded: int = 0
    skipped_size: int = 0
    skipped_binary: int = 0
    skipped_ext: int = 0
    skipped_glob: int = 0
    skipped_gitignore: int = 0
    skipped_symlink: int = 0
    skipped_unreadable: int = 0
    dirs_seen: int = 0
    dirs_skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# gitignore-style matcher (minimal — no negation, no nested directives)
# ──────────────────────────────────────────────────────────────────────────────
class _IgnoreMatcher:
    """Minimal gitignore-flavored matcher.

    Supports: literal names, glob wildcards, leading slash (root-anchored),
    trailing slash (directory-only). Does NOT support negation (!pattern)
    or nested gitignore inheritance. Good enough for the 90% case.

    For phase 2, swap in `pathspec` library if anyone hits edge cases.
    """

    def __init__(self, root: str, patterns: list[str]):
        self.root = os.path.abspath(root)
        self.patterns: list[tuple[str, bool, bool]] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            anchored = line.startswith("/")
            if anchored:
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            self.patterns.append((line, anchored, dir_only))

    def matches(self, abs_path: str, is_dir: bool) -> bool:
        try:
            rel = os.path.relpath(abs_path, self.root).replace(os.sep, "/")
        except ValueError:
            return False
        if rel == "." or rel.startswith(".."):
            return False
        basename = os.path.basename(abs_path)
        for pat, anchored, dir_only in self.patterns:
            if dir_only and not is_dir:
                continue
            if anchored:
                if fnmatch.fnmatch(rel, pat):
                    return True
            else:
                # Match against basename OR any path component sequence
                if fnmatch.fnmatch(basename, pat):
                    return True
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, f"*/{pat}") \
                        or fnmatch.fnmatch(rel, f"{pat}/*") or fnmatch.fnmatch(rel, f"*/{pat}/*"):
                    return True
        return False


def _load_ignore_patterns(root: str) -> list[str]:
    """Collect patterns from <root>/.gitignore and <root>/.m3ignore."""
    pats: list[str] = []
    for name in (".gitignore", ".m3ignore"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    pats.extend(f.read().splitlines())
            except OSError as e:
                logger.warning("walker: could not read %s: %s", p, e)
    return pats


# ──────────────────────────────────────────────────────────────────────────────
# Walk
# ──────────────────────────────────────────────────────────────────────────────
def walk(
    root: str | Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_depth: int | None = None,
    follow_symlinks: bool | None = None,
    max_file_bytes: int | None = None,
    force_size: bool = False,
    skip_binary_sniff: bool = False,
    stats: WalkStats | None = None,
    repo_root: str | None = None,
    extra_dir_ignores: set[str] | None = None,
) -> Iterator[WalkEntry]:
    """Walk `root` and yield WalkEntry for every file that passes filters.

    Args:
        root: directory to walk.
        include: glob patterns; if set, ONLY matching files are yielded.
        exclude: glob patterns; matching files are skipped (overrides include).
        max_depth: max directory depth from root (0 = root only).
        follow_symlinks: override config.FILES_FOLLOW_SYMLINKS.
        max_file_bytes: per-file size cap (override config default).
        force_size: bypass the size cap entirely.
        skip_binary_sniff: skip the NUL-byte / non-printable sniff.
        stats: optional WalkStats to populate with counters.
        repo_root: if set, repo_relative paths are computed from this root
            rather than `root` itself. Useful when ingesting a subdir of a
            larger repo and you want consistent identity.
        extra_dir_ignores: additional directory names to skip (joined with
            config.BUILTIN_DIR_IGNORES).

    Yields:
        WalkEntry, one per surviving file.
    """
    root_abs = os.path.abspath(str(root))
    if not os.path.isdir(root_abs):
        raise NotADirectoryError(f"walk root is not a directory: {root_abs}")

    if follow_symlinks is None:
        follow_symlinks = config.FILES_FOLLOW_SYMLINKS
    if max_file_bytes is None:
        max_file_bytes = config.FILES_MAX_FILE_BYTES
    if stats is None:
        stats = WalkStats()

    dir_ignores = set(config.BUILTIN_DIR_IGNORES)
    if extra_dir_ignores:
        dir_ignores |= set(extra_dir_ignores)

    repo_root_abs = os.path.abspath(repo_root) if repo_root else root_abs
    matcher = _IgnoreMatcher(root_abs, _load_ignore_patterns(root_abs))

    include_globs = include or []
    exclude_globs = exclude or []

    def _included(path: str) -> bool:
        if not include_globs:
            return True
        basename = os.path.basename(path)
        return any(fnmatch.fnmatch(basename, g) or fnmatch.fnmatch(path, g) for g in include_globs)

    def _excluded(path: str) -> bool:
        if not exclude_globs:
            return False
        basename = os.path.basename(path)
        return any(fnmatch.fnmatch(basename, g) or fnmatch.fnmatch(path, g) for g in exclude_globs)

    def _recurse(dirpath: str, depth: int) -> Iterator[WalkEntry]:
        if max_depth is not None and depth > max_depth:
            return
        try:
            entries = list(os.scandir(dirpath))
        except OSError as e:
            stats.errors.append(f"scandir failed at {dirpath}: {e}")
            stats.dirs_skipped += 1
            return

        for entry in entries:
            full = entry.path
            try:
                is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                is_symlink = entry.is_symlink()
            except OSError as e:
                stats.errors.append(f"stat failed at {full}: {e}")
                stats.skipped_unreadable += 1
                continue

            if is_symlink and not follow_symlinks:
                stats.skipped_symlink += 1
                continue

            if is_dir:
                stats.dirs_seen += 1
                bn = os.path.basename(full)
                if bn in dir_ignores:
                    stats.dirs_skipped += 1
                    continue
                if matcher.matches(full, is_dir=True):
                    stats.dirs_skipped += 1
                    stats.skipped_gitignore += 1
                    continue
                yield from _recurse(full, depth + 1)
                continue

            # File path
            stats.files_seen += 1
            bn = os.path.basename(full)
            ext = os.path.splitext(bn)[1].lower()

            if ext in config.BUILTIN_EXT_IGNORES:
                stats.skipped_ext += 1
                continue

            if matcher.matches(full, is_dir=False):
                stats.skipped_gitignore += 1
                continue

            if not _included(full):
                stats.skipped_glob += 1
                continue
            if _excluded(full):
                stats.skipped_glob += 1
                continue

            try:
                st = entry.stat(follow_symlinks=follow_symlinks)
            except OSError as e:
                stats.errors.append(f"stat failed at {full}: {e}")
                stats.skipped_unreadable += 1
                continue

            if st.st_size == 0:
                # Empty file: silently skip (per plan §13 — failure table).
                continue

            if not force_size and st.st_size > max_file_bytes:
                stats.skipped_size += 1
                logger.debug("walker: skipping oversized file %s (%d bytes)", full, st.st_size)
                continue

            if not skip_binary_sniff and looks_binary(full):
                stats.skipped_binary += 1
                continue

            try:
                rel = os.path.relpath(full, repo_root_abs)
            except ValueError:
                rel = None  # different drive on windows

            # Windows exposes birthtime via st_birthtime on some FS; fall back
            # to ctime which is "metadata change time" on POSIX, "creation
            # time" on Windows. Good enough for our metadata field.
            ctime = getattr(st, "st_birthtime", None) or st.st_ctime

            stats.files_yielded += 1
            yield WalkEntry(
                path=os.path.abspath(full),
                repo_relative=rel,
                filename=bn,
                filetype=filetype_for(full),
                size_bytes=st.st_size,
                mtime=st.st_mtime,
                ctime=ctime,
            )

    yield from _recurse(root_abs, depth=0)
