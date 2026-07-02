import logging
import os
import time
from contextlib import contextmanager

from m3_core.gpu import _no_window
from m3_core.paths import get_m3_config_root
from m3_core.runtime import M3_CORE_RS_DISABLE

logger = logging.getLogger("M3_SDK")


@contextmanager
def migration_lock():
    """Acquires an exclusive atomic file lock for safe startup migrations.

    If the lock is held by another process, it block-waits (with a timeout of 120s)
    until the lock is released.
    """
    lock_path = os.path.join(get_m3_config_root(), ".migration.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    # Try native Rust advisory lock first
    if not M3_CORE_RS_DISABLE:
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "NativeMigrationLock"):
                lock = m3_core_rs.NativeMigrationLock(lock_path)
                acquired = lock.acquire(120)
                if not acquired:
                    raise RuntimeError(
                        f"Could not acquire native migration lock at {lock_path} within 120 seconds."
                    )
                try:
                    yield
                finally:
                    lock.release()
                return
        except Exception as e:
            if isinstance(e, RuntimeError) and "lock" in str(e):
                raise
            # Fall back to Python busy-wait lock on other errors

    fd = None
    start_time = time.time()
    acquired = False

    while time.time() - start_time < 120.0:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            # Stamp ownership so a future waiter can tell whether the holder is
            # still alive (PID liveness) or this is an orphaned lock to reclaim.
            try:
                os.write(fd, _lock_owner_stamp().encode("utf-8"))
            except OSError:
                pass  # stamping is best-effort; the lock itself is what matters
            acquired = True
            break
        except FileExistsError:
            # The lock exists. Before sleeping, check whether it's STALE — a
            # process that died holding it (crash / kill -9 / OOM) leaves the
            # file behind, which would otherwise wedge every migration for the
            # full 120s and then hard-error (the 2026-06-27 incident). Reclaim
            # it only when we can prove the owner is gone.
            if _reclaim_stale_lock(lock_path):
                continue  # reclaimed — retry os.open immediately, no sleep
            time.sleep(0.5)

    if not acquired:
        raise RuntimeError(
            f"Could not acquire migration lock at {lock_path} within 120 seconds. "
            "Another migration process may be hung. If you are sure no other process is migrating, "
            f"delete the lock file manually: {lock_path}"
        )

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.unlink(lock_path)
            except Exception:
                pass


# Hard ceiling: no legitimate startup migration runs longer than this. A lock
# older than this from an UNKNOWN/cross-host owner (whose PID we can't probe) is
# treated as stale. Generous so we never reclaim a genuinely-slow migration.
_MIGRATION_LOCK_MAX_AGE_S = 600.0


def _lock_owner_stamp() -> str:
    """Owner metadata written into the lock file: 'pid host epoch'."""
    import socket
    return f"{os.getpid()} {socket.gethostname()} {int(time.time())}"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. Conservative: on any uncertainty
    we return True (assume alive) so we never steal a live lock."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # No os.kill(pid, 0) semantics on Windows; query the task list.
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/fi", f"PID eq {pid}", "/nh"],
                capture_output=True, text=True, timeout=5, **_no_window(),
            )
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return True  # can't tell -> assume alive (safe)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user -> alive
    except OSError:
        return True  # unknown -> assume alive


def _reclaim_stale_lock(lock_path: str) -> bool:
    """Remove the lock file IFF its owner is provably gone. Returns True if a
    stale lock was reclaimed (caller should retry acquisition immediately).

    Decision rules (all fail SAFE — when unsure, leave the lock alone):
      - Same host + owner PID not alive  -> stale, reclaim.
      - Cross host (can't probe PID) + file older than the max-age ceiling
        -> stale, reclaim.
      - Unparseable/empty stamp + file older than the ceiling -> stale.
      - Otherwise -> not stale (return False; caller keeps waiting).
    """
    import socket
    try:
        raw = ""
        with open(lock_path, encoding="utf-8") as f:
            raw = f.read().strip()
        try:
            mtime = os.path.getmtime(lock_path)
        except OSError:
            mtime = 0.0
    except FileNotFoundError:
        return True  # vanished out from under us — effectively reclaimable
    except OSError:
        return False  # can't read -> don't touch it

    age = max(0.0, time.time() - mtime)
    parts = raw.split()
    pid: int = 0
    host = ""
    if len(parts) >= 2:
        try:
            pid = int(parts[0])
        except ValueError:
            pid = 0
        host = parts[1]

    this_host = socket.gethostname()
    stale = False
    if pid and host == this_host:
        # Same machine: authoritative liveness check.
        stale = not _pid_alive(pid)
    else:
        # Cross-host or unparseable: we cannot probe the PID, so fall back to a
        # generous age ceiling. Only reclaim something clearly abandoned.
        stale = age > _MIGRATION_LOCK_MAX_AGE_S

    if not stale:
        return False
    try:
        os.unlink(lock_path)
        logger.warning(
            "Reclaimed stale migration lock %s (owner pid=%s host=%s age=%.0fs)",
            lock_path, pid or "?", host or "?", age,
        )
        return True
    except FileNotFoundError:
        return True  # someone else reclaimed it first — fine, retry
    except OSError:
        return False
