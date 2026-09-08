"""Detect stale m3 guidance copied into a user's own agent-instruction file.

m3 NEVER writes `CLAUDE.md` / `GEMINI.md` / `AGENTS.md` — those are the user's
files, mixing m3 guidance with their own conventions, and an installer that
rewrote them would clobber unrelated content. That boundary is correct.

The cost of it is a distribution gap: guidance users COPIED out of
`docs/AGENT_INSTRUCTIONS.md` never updates when we correct it. This probe closes
that gap by DETECTING known-wrong text and printing the fix. It reads; it never
writes.

Currently detected
------------------
**The MCP-disconnect false alarm.** Guidance shipped before 2026-09-07 told
agents to say, on any MCP disconnect:

    "Chatlog is NOT being captured. Design decisions ... will NOT be preserved."

That is false. Chatlog capture writes to the database through the ingest hook,
NOT through the MCP connection, so a dropped MCP session cannot lose turns.
Measured on a session with 17 disconnects: 610 turns captured, zero lost,
including turns written *during* a disconnect window; the m3 process never
restarted.

A warning that fires when nothing is wrong is worse than no warning — it trains
users to ignore the one that matters. Users still carrying this text get a false
alarm on every transient reconnect.

**The `hooks[*].enabled` false positive.** Older guidance told agents to warn
when that flag reads `false`. It records only whether a per-turn shell hook was
wired at init time, and reads `false` on HEALTHY installs where the
Stop-hook/MCP write path captures fine (confirmed 2026-06-13) — a permanent
false alarm. The data to trust is `capture.healthy` / `last_write_at`.
"""
from __future__ import annotations

import os
from pathlib import Path

# Files users are known to copy m3 guidance into. Home-scoped only: a
# project-local CLAUDE.md is the user's business and scanning the filesystem for
# them would be both slow and intrusive.
_CANDIDATES = (
    "CLAUDE.md",
    "GEMINI.md",
    "AGENTS.md",
    ".claude/CLAUDE.md",
    ".gemini/GEMINI.md",
)

# (id, needle, why-it-is-wrong, what-to-do). Needles are matched
# case-insensitively against the file text. Keep them SPECIFIC: a loose match
# would fire on a user's own prose that merely discusses the topic, and a probe
# that cries wolf about crying wolf helps nobody.
_STALE_PATTERNS = (
    (
        "mcp-disconnect-implies-loss",
        "chatlog is not being captured",
        "an MCP disconnect does NOT stop chatlog capture — the ingest hook "
        "writes to the DB directly, independent of the MCP connection",
        "replace that warning with: \"m3's MCP tools are unreachable, so I "
        "can't search or write memory right now. Chatlog capture is "
        "unaffected. Reconnect with `/mcp`.\"",
    ),
    (
        "hooks-enabled-false-alarm",
        "hook.enabled = false",
        "`hooks[*].enabled` reads false on HEALTHY installs (it records only "
        "init-time shell-hook wiring), so alarming on it is a permanent false "
        "positive",
        "trust `capture.healthy` and `last_write_at` from `chatlog_status` "
        "instead — the data, not the flag",
    ),
)


def _home_files() -> "list[Path]":
    home = Path(os.path.expanduser("~"))
    return [home / rel for rel in _CANDIDATES]


def check() -> "list[dict]":
    """Findings for stale guidance. Pure detection; no printing, no writing."""
    findings: "list[dict]" = []
    for path in _home_files():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except Exception:  # noqa: BLE001 — unreadable file is not our problem
            continue
        for pid, needle, why, fix in _STALE_PATTERNS:
            if needle in text:
                findings.append({
                    "path": str(path), "id": pid, "why": why, "fix": fix,
                })
    return findings


def run(brief: bool = False) -> int:
    """Print findings. Returns 0 ALWAYS — stale guidance is a correctness issue
    in the user's own notes, not a broken install, so it must not fail the
    doctor's exit code and break anyone's CI."""
    try:
        findings = check()
    except Exception as e:  # noqa: BLE001 — the doctor must never die on a probe
        if not brief:
            print(f"  agent guidance: probe failed (non-fatal): "
                  f"{type(e).__name__}: {e}")
        return 0

    if not findings:
        # ONE LINE in brief mode too. `brief` is the default, so gating the
        # healthy line behind --verbose makes the check invisible to nearly
        # every user -- and an invisible check reads as coverage while
        # providing none. See tests/test_doctor_brief_visibility.py.
        print("✅ agent guidance: OK (no stale m3 guidance found)")
        return 0

    print("⚠️  agent guidance: stale m3 text found in your instruction file(s)")
    print("    m3 never edits these files, so a correction we ship does not")
    print("    reach text you copied earlier. Update it by hand:")
    for f in findings:
        print(f"    - {f['path']}")
        print(f"      why: {f['why']}")
        print(f"      fix: {f['fix']}")
    print("    reference: docs/AGENT_INSTRUCTIONS.md "
          "(§ Silent Failure Detection)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
