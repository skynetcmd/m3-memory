"""Installer for the m3-memory system payload.

The pip package ships thin: it exports a ``mcp-memory`` CLI but the actual
system code (``bin/memory_bridge.py`` plus the ~60 files it imports) lives
in the GitHub repo. This module clones or downloads that payload on demand
so a plain ``pip install m3-memory`` + ``mcp-memory install-m3`` is a
complete setup, no ``git clone`` step required from the user.

Resolution order for finding the bridge (see ``find_bridge``):

1. ``$M3_BRIDGE_PATH`` env var — power-user override, honored first.
2. ``~/.m3-memory/config.json`` — written by ``install_m3``.
3. Walk up from this file looking for a sibling ``bin/memory_bridge.py`` —
   catches the developer case where someone did ``pip install -e .`` from
   a clone of the repo.
4. None — caller prints a helpful error pointing at ``install-m3``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_URL = "https://github.com/skynetcmd/m3-memory.git"
TARBALL_URL_TEMPLATE = "https://github.com/skynetcmd/m3-memory/archive/refs/tags/{tag}.tar.gz"


def config_dir() -> Path:
    """Directory for per-user m3-memory state (config + default repo clone)."""
    return Path.home() / ".m3-memory"


def config_file() -> Path:
    return config_dir() / "config.json"


def default_repo_path() -> Path:
    return config_dir() / "repo"


def load_config() -> dict:
    """Return the saved config, or an empty dict if none exists or is malformed."""
    path = config_file()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def _developer_bridge() -> Optional[Path]:
    """Walk up from this file looking for a sibling ``bin/memory_bridge.py``.

    Returns the path if found (developer case: ``pip install -e .`` from a
    repo clone), else None.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "bin" / "memory_bridge.py"
        if candidate.is_file():
            return candidate
    return None


def find_bridge() -> Optional[Path]:
    """Locate ``memory_bridge.py`` using the four-step resolution order.

    Returns the absolute path if found, or None to signal "not installed."
    Callers should present an actionable message when None is returned.
    """
    # 1. Env var override.
    env = os.environ.get("M3_BRIDGE_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_file():
            return p

    # 2. Config file written by install_m3.
    cfg = load_config()
    bridge = cfg.get("bridge_path")
    if bridge:
        p = Path(bridge).expanduser().resolve()
        if p.is_file():
            return p

    # 3. Developer sibling case.
    dev = _developer_bridge()
    if dev:
        return dev

    return None


def _git_clone(tag: str, dest: Path) -> bool:
    """Shallow-clone REPO_URL at ``tag`` into ``dest``. Returns True on success,
    False if ``git`` is missing. Raises on any other subprocess failure."""
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", tag, REPO_URL, str(dest)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return True
    except FileNotFoundError:
        return False


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


def _download_tarball(tag: str, dest: Path) -> None:
    """Fallback to downloading a release tarball and extracting it into ``dest``.

    Intended for environments without git (CI, minimal containers, some
    Windows installs). The GitHub tarball's top-level dir is
    ``m3-memory-<tag-without-v>/`` — we strip that and move contents into
    ``dest`` so the layout matches a git clone.

    Extraction is filtered through ``_safe_tar_member`` to block the
    traditional tarslip / path-traversal / device-file attack classes.
    Python 3.12's built-in ``filter='data'`` would also work, but we
    support 3.11 so we roll our own filter.
    """
    url = TARBALL_URL_TEMPLATE.format(tag=tag)
    # Defense-in-depth: TARBALL_URL_TEMPLATE is a hardcoded constant that
    # pins the host to github.com/skynetcmd/m3-memory, but we revalidate
    # the fully-interpolated URL before the request anyway. A malicious
    # `tag` (e.g. one containing a scheme or authority) can't leak the
    # request to another host. This also silences SAST tools that flag
    # any `urlopen()` whose argument isn't a string literal (CWE-918).
    _TRUSTED_URL_PREFIX = "https://github.com/skynetcmd/m3-memory/archive/refs/tags/"
    if not url.startswith(_TRUSTED_URL_PREFIX):
        raise RuntimeError(
            f"refusing to fetch tarball from untrusted URL: {url!r} "
            f"(expected prefix {_TRUSTED_URL_PREFIX!r})"
        )
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        archive = tmp / "repo.tar.gz"
        print(f"  downloading {url}")
        with urllib.request.urlopen(url) as resp, archive.open("wb") as f:  # nosec B310 — trusted GitHub host, prefix-validated above
            shutil.copyfileobj(resp, f)
        with tarfile.open(archive, "r:gz") as tf:
            tmp_resolved = tmp.resolve()
            tf.extractall(tmp, filter=lambda m, _path: _safe_tar_member(m, tmp_resolved))  # nosec B202 - filter blocks tarslip
        # Find the single top-level dir extracted.
        top_level = [p for p in tmp.iterdir() if p.is_dir() and p.name.startswith("m3-memory-")]
        if len(top_level) != 1:
            raise RuntimeError(f"unexpected tarball layout (top-level dirs: {top_level})")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(top_level[0]), str(dest))


def install_m3(repo_path: Optional[Path] = None, tag: Optional[str] = None, force: bool = False) -> Path:
    """Clone or download the m3-memory repo and record the bridge path in config.

    ``repo_path`` defaults to ``~/.m3-memory/repo``. ``tag`` defaults to
    ``v<m3_memory.__version__>`` so the cloned payload always matches the
    installed wheel. ``force=True`` wipes an existing clone before re-fetching.

    Returns the resolved path to ``bin/memory_bridge.py``. Raises RuntimeError
    if neither git nor the tarball fallback can fetch the repo.
    """
    from m3_memory import __version__

    if repo_path is None:
        repo_path = default_repo_path()
    repo_path = repo_path.expanduser().resolve()

    if tag is None:
        tag = f"v{__version__}"

    if repo_path.exists():
        if not force:
            raise RuntimeError(
                f"{repo_path} already exists. Run `mcp-memory install-m3 --force` to replace it, "
                f"or `mcp-memory update` to refresh to the current wheel version."
            )
        print(f"  removing existing {repo_path}")
        shutil.rmtree(repo_path)

    print(f"fetching m3-memory {tag} -> {repo_path}")
    if not _git_clone(tag, repo_path):
        print("  git not found; falling back to GitHub tarball")
        _download_tarball(tag, repo_path)

    bridge = repo_path / "bin" / "memory_bridge.py"
    if not bridge.is_file():
        raise RuntimeError(
            f"fetched repo but {bridge} not found. This usually means the "
            f"tag {tag!r} doesn't exist on GitHub yet. Check "
            f"https://github.com/skynetcmd/m3-memory/releases."
        )

    save_config({
        "repo_path": str(repo_path),
        "bridge_path": str(bridge),
        "version": __version__,
        "tag": tag,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    print(f"[OK] installed. bridge_path = {bridge}")
    print(f"  config written to {config_file()}")
    return bridge


def uninstall_m3(yes: bool = False) -> None:
    """Remove the cloned repo + config file. Idempotent."""
    cfg = load_config()
    repo_path = Path(cfg.get("repo_path", str(default_repo_path())))

    if not cfg and not repo_path.exists():
        print("nothing to uninstall (no config, no repo).")
        return

    if not yes:
        print(f"will remove:")
        print(f"  {repo_path}")
        print(f"  {config_file()}")
        resp = input("proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("aborted.")
            return

    if repo_path.exists():
        shutil.rmtree(repo_path, ignore_errors=True)
        print(f"  removed {repo_path}")
    if config_file().is_file():
        config_file().unlink()
        print(f"  removed {config_file()}")


def doctor() -> int:
    """Print diagnostic info and return 0 on healthy, 1 on missing payload."""
    from m3_memory import __version__

    print(f"m3-memory package version: {__version__}")
    print(f"config file:               {config_file()}")
    cfg = load_config()
    if cfg:
        print(f"  installed version:       {cfg.get('version', '?')}")
        print(f"  installed tag:           {cfg.get('tag', '?')}")
        print(f"  installed at:            {cfg.get('installed_at', '?')}")
        print(f"  repo_path:               {cfg.get('repo_path', '?')}")
    else:
        print("  (no config - system not installed via `mcp-memory install-m3`)")

    env = os.environ.get("M3_BRIDGE_PATH")
    if env:
        print(f"M3_BRIDGE_PATH (env):      {env}")
    else:
        print("M3_BRIDGE_PATH (env):      (unset)")

    dev = _developer_bridge()
    if dev:
        print(f"developer sibling bridge:  {dev}")
    else:
        print("developer sibling bridge:  (not found)")

    print()
    bridge = find_bridge()
    if bridge and bridge.is_file():
        print(f"[OK] resolved bridge: {bridge}")
        return 0
    print("[X] no bridge found. Run `mcp-memory install-m3` to fetch the system.")
    return 1
