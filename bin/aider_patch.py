"""
Aider statusline wrapper.

Monkey-patches prompt_toolkit.PromptSession BEFORE aider imports it,
injecting a bottom_toolbar that mirrors the Claude Code statusline:

  🌿 main +1 ~2  │  localAI ●  MCP ●4/4  │  14:09:21
"""
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit.formatted_text import ANSI

# ── In-process cache (avoids subprocess overhead on every keystroke) ──────────
_cache: dict = {}

def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    if key not in _cache or now - _cache[key][0] > ttl:
        _cache[key] = (now, fn())
    return _cache[key][1]

# ── Data collectors ───────────────────────────────────────────────────────────
def _git() -> dict:
    try:
        branch = subprocess.check_output(
            ['git', 'branch', '--show-current'],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        ).strip()
        staged = subprocess.check_output(
            ['git', 'diff', '--cached', '--name-only'],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        ).strip()
        modified = subprocess.check_output(
            ['git', 'diff', '--name-only'],
            stderr=subprocess.DEVNULL, text=True, timeout=1,
        ).strip()
        return {
            'branch':   branch,
            'staged':   len([x for x in staged.split('\n')   if x]),
            'modified': len([x for x in modified.split('\n') if x]),
        }
    except Exception:
        return {}

def _lm_ok() -> bool:
    try:
        s = socket.create_connection(('127.0.0.1', 1234), timeout=0.5)
        s.close()
        return True
    except Exception:
        return False

def _mcp_count() -> int:
    try:
        # Check project-specific settings first, then global
        paths = [
            Path.cwd() / '.gemini' / 'settings.json',
            Path.home() / '.gemini' / 'settings.json',
            Path.home() / '.claude' / 'settings.json'
        ]
        for p in paths:
            if p.exists():
                settings = json.loads(p.read_text())
                return len(settings.get('mcpServers', {}))
        return 0
    except Exception:
        return 0

# ── ANSI helpers ──────────────────────────────────────────────────────────────
GRN = '\033[32m'
YEL = '\033[33m'
RED = '\033[31m'
WHT = '\033[37m'
DIM = '\033[2m'
R   = '\033[0m'

# ── Toolbar callable (re-called by prompt_toolkit on every render) ─────────────
def toolbar():
    git = _cached('git', 5,  _git)
    lm  = _cached('lm',  5,  _lm_ok)
    mcp = _cached('mcp', 30, _mcp_count)
    now = datetime.now().strftime('%H:%M:%S')

    git_part = ''
    if git.get('branch'):
        s = f'{GRN}+{git["staged"]}{R}'   if git.get('staged',   0) > 0 else ''
        m = f'{YEL}~{git["modified"]}{R}' if git.get('modified', 0) > 0 else ''
        space = ' ' if (s or m) else ''
        git_part = f'{GRN}🌿 {git["branch"]}{R} {s}{m}{space}{DIM}│{R}  '

    lm_dot  = f'{GRN}●{R}' if lm             else f'{RED}●{R}'

    # We consolidated to 2 servers in our project, but we should report
    # whatever is actually configured in the settings.json we found.
    mcp_total = mcp
    mcp_dot = (f'{GRN}●{R}' if mcp > 0
          else f'{RED}●{R}')

    return ANSI(
        f' {git_part}'
        f'localAI {lm_dot}  MCP {mcp_dot}{mcp}/{mcp_total}'
        f'  {DIM}│{R}  {WHT}{now}{R}'
    )

# ── Patch prompt_toolkit BEFORE aider.io imports it ──────────────────────────
import prompt_toolkit.shortcuts as _pts  # type: ignore

_OrigSession = _pts.PromptSession

class _PatchedSession(_OrigSession):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('bottom_toolbar', toolbar)
        super().__init__(*args, **kwargs)

_pts.PromptSession = _PatchedSession

# ── Hand off to aider ─────────────────────────────────────────────────────────
sys.argv[0] = 'aider'
from aider.main import main  # noqa: E402  (must come after patch)

main()
