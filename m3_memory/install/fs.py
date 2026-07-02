"""Pure filesystem / sqlite helpers used by the installer.

Extracted verbatim from installer.py. None of these are monkeypatch targets
and none call any patched function, so they're safe to live in their own
module. installer.py re-imports them so `installer.<name>` still resolves.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile  # noqa: F401 - referenced only in string type annotations below
from pathlib import Path


def _robust_rmtree(path, retries: int = 5, delay: float = 0.3) -> None:
    """Delete a directory tree, resilient to the two Windows failure modes that
    make a bare shutil.rmtree abort an install:

      1. Read-only files (git packs its objects read-only) -> os.unlink raises
         PermissionError [WinError 5]. We clear the read-only bit and retry.
      2. Transient locks (git gc / antivirus briefly holding a pack file) ->
         a few retries with a short backoff clear them.

    Works across Python 3.11–3.14 by retrying at the top level rather than using
    the onerror/onexc callback (whose signature changed in 3.12). Raises the last
    error if the tree is still undeletable after all retries — so a genuine
    permission problem still surfaces loudly (§3), but a read-only pack or a
    momentary lock no longer turns a successful update into a false failure.

    NOTE: this does NOT resolve a file that is genuinely OPEN by another process
    (a running m3 server holding agent_memory.db, WinError 32) — retrying can't
    unlock that while it's open. That case is handled upstream by stopping the
    server first (`--force-kill-mcp`) and, going forward, by the decoupled-roots
    default that keeps the DB OUT of the repo dir being replaced. See the
    known-issues note in docs/install_windows.md.
    """
    import stat
    import time

    path = str(path)
    if not os.path.exists(path):
        return
    last_exc: "Exception | None" = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except (PermissionError, OSError) as e:
            last_exc = e
            # Best-effort: strip read-only bits across the tree, then retry.
            for root, dirs, files in os.walk(path):
                for name in dirs + files:
                    try:
                        os.chmod(os.path.join(root, name), stat.S_IWRITE)
                    except OSError:
                        pass
            if attempt < retries - 1:
                time.sleep(delay)
    if os.path.exists(path) and last_exc is not None:
        raise last_exc


def _drain_wal(src: Path) -> None:
    """Best-effort checkpoint of a DB's WAL into its main file BEFORE we snapshot
    and delete the old repo (CLAUDE.md §10 WAL discipline).

    Folding the `-wal` back into the `.db` means (a) the copy below sees a
    complete file even if the backup path degrades, and (b) the `-wal`/`-shm`
    sidecars shrink/close, removing one more open-file lock that could trip the
    repo delete (WinError 32). Best-effort only: if a running server holds the
    DB (checkpoint returns busy) we don't fail — the backup API still captures
    committed WAL pages, so no data is lost; we just couldn't shrink the source.
    """
    try:
        conn = sqlite3.connect(str(src), timeout=5)
        try:
            # TRUNCATE resets the WAL file to zero after checkpointing. On a busy
            # DB this may checkpoint only partially; that's fine — non-fatal.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # locked / not-WAL / not-a-DB — the backup below still preserves data


def _safe_copy_sqlite(src: Path, dst: Path) -> None:
    """Copy a SQLite database WAL-safely via the Online Backup API.

    A plain file copy of a DB the m3 MCP server has open can miss pages still in
    the `-wal` file or capture a torn write. We first drain the source WAL
    (best-effort checkpoint), then use the backup API — which produces a
    transactionally-consistent single-file snapshot (it also folds any remaining
    committed WAL pages into the destination). Falls back to a plain copy only if
    the source isn't a valid SQLite file (e.g. a 0-byte placeholder) so non-DB
    `.db` files still get preserved rather than dropped.
    """
    _drain_wal(src)
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                with dst_conn:
                    src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error:
        # Not a usable SQLite DB (corrupt / empty / locked-exclusive) — fall
        # back to a byte copy so we still preserve *something* rather than lose
        # the file entirely.
        shutil.copy2(src, dst)


def _safe_tar_member(member: "tarfile.TarInfo", dest_root: Path) -> "tarfile.TarInfo | None":
    """Per-member filter for tarfile.extractall.

    Blocks the classic path-traversal vectors:
      - absolute paths (`/etc/passwd`)
      - parent-dir escapes (`../../something`)
      - symlinks or hardlinks that point outside dest_root
      - device files, fifos, and other non-regular non-dir entries

    Returns the member unchanged if safe, or None to drop it (extractall
    skips filter-None entries). Raising would abort the whole extraction
    which is too aggressive for a GitHub tarball that may carry innocuous
    unusual entries; dropping is defensive but recoverable.
    """
    name = member.name
    # Reject absolute paths outright.
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        return None
    # Normalize the member's target path and confirm it stays under dest_root.
    resolved = (dest_root / name).resolve()
    try:
        resolved.relative_to(dest_root.resolve())
    except ValueError:
        return None
    # Only allow regular files, directories, and links whose targets ALSO
    # resolve safely. Block devices, fifos, character/block specials.
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        return None
    if member.issym() or member.islnk():
        link_target = (resolved.parent / member.linkname).resolve()
        try:
            link_target.relative_to(dest_root.resolve())
        except ValueError:
            return None
    return member
