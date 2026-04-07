#!/usr/bin/env python3
"""
Claude Code status line — 2-line display
Line 1: identity + git + vim mode + time
Line 2: context bar + cost + duration + lines changed + LM Studio + MCP health + current focus
"""

import json, sys, subprocess, os, time, socket
from datetime import datetime
from pathlib import Path

# ── Parse stdin ────────────────────────────────────────────────────────────────
data = json.load(sys.stdin)
model      = data.get('model', {}).get('display_name', '?')
cwd        = data.get('workspace', {}).get('current_dir', os.getcwd())
ctx        = data.get('context_window', {})
cost_data  = data.get('cost', {})
vim_mode   = data.get('vim', {}).get('mode', '')

pct          = int(ctx.get('used_percentage', 0) or 0)
cost         = cost_data.get('total_cost_usd', 0) or 0
duration_ms  = cost_data.get('total_duration_ms', 0) or 0
lines_added  = cost_data.get('total_lines_added', 0) or 0
lines_removed= cost_data.get('total_lines_removed', 0) or 0
display_path = str(cwd).replace(os.path.expanduser('~'), '~')
now          = datetime.now().strftime('%H:%M:%S')

# ── ANSI colours ───────────────────────────────────────────────────────────────
R      = '\033[0m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
MAG    = '\033[35m'
YEL    = '\033[33m'
CYA    = '\033[36m'
GRN    = '\033[32m'
RED    = '\033[31m'
WHT    = '\033[37m'
ORANGE = '\033[38;5;208m'
BLUE   = '\033[34m'

# ── Context bar ────────────────────────────────────────────────────────────────
BAR_W  = 15
filled = int(pct * BAR_W / 100)
bar    = '█' * filled + '░' * (BAR_W - filled)
bar_c  = RED if pct >= 90 else YEL if pct >= 70 else GRN

# ── Duration ──────────────────────────────────────────────────────────────────
mins = duration_ms // 60000
secs = (duration_ms % 60000) // 1000

# ── Helpers: cached subprocess calls ─────────────────────────────────────────
def read_cache(path, max_age):
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            return json.loads(Path(path).read_text())
    except:
        pass
    return None

def write_cache(path, obj):
    try: Path(path).write_text(json.dumps(obj))
    except: pass

# ── Git status (5 s cache) ────────────────────────────────────────────────────
GIT_C = '/tmp/cc_sl_git.json'
git   = read_cache(GIT_C, 5)
if git is None:
    try:
        branch   = subprocess.check_output(['git','branch','--show-current'],
                       stderr=subprocess.DEVNULL, text=True).strip()
        staged   = subprocess.check_output(['git','diff','--cached','--name-only'],
                       stderr=subprocess.DEVNULL, text=True).strip()
        modified = subprocess.check_output(['git','diff','--name-only'],
                       stderr=subprocess.DEVNULL, text=True).strip()
        git = {
            'branch':   branch,
            'staged':   len([x for x in staged.split('\n') if x]),
            'modified': len([x for x in modified.split('\n') if x]),
        }
    except:
        git = {}
    write_cache(GIT_C, git)

# ── LM Studio health (5 s cache) ─────────────────────────────────────────────
LM_C   = '/tmp/cc_sl_lm.json'
lm_obj = read_cache(LM_C, 5)
if lm_obj is None:
    try:
        s = socket.create_connection(('127.0.0.1', 1234), timeout=0.5)
        s.close()
        lm_obj = {'ok': True}
    except:
        lm_obj = {'ok': False}
    write_cache(LM_C, lm_obj)
lm_ok = lm_obj.get('ok', False)

# ── MCP bridge health (30 s cache) ────────────────────────────────────────────
# MCP servers are managed by Claude Code via stdio — not visible to pgrep.
# Count configured mcpServers entries in settings.json instead.
MCP_C   = '/tmp/cc_sl_mcp.json'
mcp_obj = read_cache(MCP_C, 30)
if mcp_obj is None:
    try:
        # MCP servers are registered in ~/.claude.json (root), not ~/.claude/settings.json
        settings_path = Path.home() / '.claude.json'
        settings = json.loads(settings_path.read_text())
        count = len(settings.get('mcpServers', {}))
    except:
        count = 0
    mcp_obj = {'count': count}
    write_cache(MCP_C, mcp_obj)
mcp_count = mcp_obj.get('count', 0)

# ── Build line 1: identity | model | git | vim | time ────────────────────────
user = os.environ.get('USER', subprocess.check_output(['whoami'], text=True).strip())
host = subprocess.check_output(['hostname','-s'], text=True).strip()

git_part = ''
if git.get('branch'):
    s = f"{GRN}+{git['staged']}{R}"   if git.get('staged',   0) > 0 else ''
    m = f"{YEL}~{git['modified']}{R}" if git.get('modified', 0) > 0 else ''
    changes  = (' ' + ' '.join(filter(None, [s, m]))) if (s or m) else ''
    git_part = f"  {GRN}🌿 {git['branch']}{R}{changes}"

vim_part = f"  {ORANGE}[{vim_mode}]{R}" if vim_mode else ''

line1 = (
    f"{MAG}{user}{R}@{YEL}{host}{R}:{WHT}{display_path}{R}"
    f"  {DIM}│{R}  {CYA}{BOLD}{model}{R}"
    f"{git_part}"
    f"{vim_part}"
    f"  {DIM}│{R}  {WHT}{now}{R}"
)

# ── Build line 2: ctx bar | cost | time | lines | LM | MCP | focus ────────────
lm_ind  = f"{GRN}●{R}"  if lm_ok    else f"{RED}●{R}"
mcp_ind = f"{GRN}●{R}" if mcp_count > 0 else f"{RED}●{R}"

lines_part = (f"  {DIM}│{R}  {GRN}+{lines_added}{R} {RED}-{lines_removed}{R}"
              if (lines_added or lines_removed) else '')

line2 = (
    f"{bar_c}{bar}{R} {BOLD}{pct}%{R}"
    f"  {DIM}│{R}  {YEL}💰 ${cost:.3f}{R}"
    f"  {DIM}│{R}  ⏱️  {mins}m {secs:02d}s"
    f"{lines_part}"
    f"  {DIM}│{R}  localAI {lm_ind}  MCP {mcp_ind}{mcp_count}"
)

print(line1)
print(line2)
