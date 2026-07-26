"""Environment coherence probe — does every wired path point at THIS install?

m3's runtime is spread across artifacts that each bake an ABSOLUTE path at
install time: Claude Code hook commands (interpreter + script), the console
launchers on PATH, the payload directory, and the venv holding it. Nothing
reconciles them afterwards. A reinstall, a relocation, or a second install via
a different mechanism moves one and leaves the others pointing at where it used
to be — and every one of those failures is SILENT.

The chatlog hooks are the highest-stakes case. They look like:

    <venv>/Scripts/python.exe <venv>/…/m3_memory/bin/hooks/chatlog/…py

Both halves live inside the pipx venv, which every reinstall churns. If either
goes stale the hook fails per-turn with no surfaced error and conversation
capture stops. That is the documented 48-hour context-loss failure in CLAUDE.md,
and until now nothing checked for it: agent_paths_probe covers MCP *server*
configs (mcpServers / OpenCode / Hermes), never Claude Code's `hooks` block.

What this probe checks, all by reading the real artifact rather than a config
flag that merely claims the thing is true:

  1. Every m3-owned hook command's interpreter and script EXIST on disk.
  2. Each hook's interpreter belongs to the same install as the running payload
     (a hook pinned to an old venv runs different code than `m3` does).
  3. Hooks that need root pinning carry M3_ENGINE_ROOT / M3_CONFIG_ROOT. Claude
     Code re-resolves server env only on restart but reloads hooks live, so an
     unpinned hook can write turns to a DIFFERENT engine root than the server
     reads — the split-brain hazard in CLAUDE.md.
  4. Config that claims a hook is wired is corroborated against settings.json.
     `chatlog_status` reports "wired": spec.enabled — the config flag echoed
     back, NOT evidence the hook exists. Same class of bug as the permanently-on
     "silent 24h+" warning: a field that asserts instead of measures.

DETECTION ONLY by default. Repair is behind `m3 doctor --fix --fix-hooks`,
because settings.json is the USER'S file — it holds non-m3 hooks, and a bad
merge breaks their whole Claude Code setup, not just m3. See repair() for the
backup-and-diff contract.

Bumps the doctor exit code on a broken hook: capture being silently dead is a
real degraded state, not a preference. Cross-platform, network-free, never
raises.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

# The installer marks its own settings.json entries by this substring appearing
# anywhere in the serialized entry (see chatlog_init._chatlog_entry_present).
# Detection and repair must use the SAME ownership test, or repair will either
# miss entries it should fix or touch entries it does not own.
OWNERSHIP_MARKER = "chatlog"

# Hook events m3 wires. SessionStart is capture-health only; PreCompact and Stop
# are the actual turn-capture path.
CAPTURE_EVENTS = ("PreCompact", "Stop")

# Roots that must be pinned inline in the hook command (CLAUDE.md split-brain).
REQUIRED_ROOT_PINS = ("M3_ENGINE_ROOT", "M3_CONFIG_ROOT")


def _claude_settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _load_settings() -> dict | None:
    """Parsed settings.json, or None when absent/unreadable.

    None means "cannot check" and must never be reported as healthy.
    """
    try:
        with open(_claude_settings_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — absent or malformed both mean "unknown"
        return None


def _m3_hook_entries(settings: dict) -> list[tuple[str, str]]:
    """[(event, command), …] for every m3-owned hook entry."""
    out: list[tuple[str, str]] = []
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries or []:
            for hook in entry.get("hooks") or []:
                command = str(hook.get("command", ""))
                if OWNERSHIP_MARKER in command.lower():
                    out.append((event, command))
    return out


def _extract_paths(command: str) -> list[str]:
    """Absolute filesystem paths referenced by a hook command string.

    Hook commands come in several shapes across platforms and install vintages
    (`powershell -File <ps1>`, `<python.exe> <script.py>`, `/bin/sh <sh>`), often
    behind `set VAR=… &&` prefixes. Rather than parse each shape, pull out
    anything that looks like an absolute path to a real file type — shape-
    agnostic, so a new launcher form does not silently go unchecked.
    """
    pattern = r'(?:[A-Za-z]:[\\/]|/)[^\s"\'&|;]+\.(?:exe|py|ps1|sh|cmd|bat)'
    seen: list[str] = []
    for match in re.findall(pattern, command):
        cleaned = match.strip().strip('"').strip("'")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def _payload_root() -> str:
    """Directory of the m3_memory package this process is running from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_installed_layout(payload_root: str) -> bool:
    """Is the running payload an INSTALLED package (vs. a repo checkout)?

    The same-install comparison is only meaningful for an installed layout. From
    a checkout the hooks correctly point at the installed venv — a different tree
    by design — so the comparison must be skipped rather than reported as a
    mismatch on every dev machine.
    """
    return "/site-packages/" in os.path.normcase(
        os.path.abspath(payload_root)
    ).replace("\\", "/")


def _same_install(path: str, payload_root: str) -> bool:
    """Is `path` inside the same install tree as the running payload?

    Compared case-insensitively with normalized separators: hook commands are
    written with forward slashes on Windows while os.path yields backslashes,
    so a naive comparison reports a false mismatch on every Windows box.
    """
    def norm(p: str) -> str:
        return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")

    # Anchor on the VENV ROOT specifically, not "some ancestor". The interpreter
    # lives in <venv>/Scripts (or <venv>/bin) while the payload lives in
    # <venv>/Lib/site-packages/m3_memory, so the shared ancestor is the venv.
    # Walking up a fixed number of levels instead would climb PAST the venv into
    # its parent directory, and every sibling venv there would then look like
    # "the same install" — the check would pass on exactly the mismatch it is
    # meant to catch.
    payload = norm(payload_root)
    marker = "/site-packages/"
    idx = payload.find(marker)
    if idx == -1:
        # Running from a REPO CHECKOUT, not an installed layout. The hooks
        # legitimately point at the installed venv, which is a different tree by
        # design, so "same install" is not a meaningful question here — the
        # doctor is simply being run from source. Returning False would flag
        # every correctly-wired hook on every dev machine. Treat as
        # not-comparable (the caller skips the check).
        return True

    # <venv>/Lib/site-packages/... -> <venv>
    anchor = os.path.dirname(payload[:idx])
    return norm(path).startswith(anchor + "/")


def check() -> dict:
    """Pure detection. Returns {'status', 'findings', 'hooks_seen'}.

    status is one of:
      'ok'       — every wired path verified against disk
      'unknown'  — settings.json absent/unreadable; nothing was verified
      'issues'   — at least one finding
    """
    settings = _load_settings()
    if settings is None:
        return {"status": "unknown", "findings": [], "hooks_seen": 0}

    entries = _m3_hook_entries(settings)
    payload_root = _payload_root()
    findings: list[dict] = []

    for event, command in entries:
        paths = _extract_paths(command)
        if not paths:
            findings.append({
                "kind": "unparsable", "event": event,
                "detail": "no filesystem path found in the hook command; "
                          "cannot verify it points at a real script",
            })
            continue

        for path in paths:
            if not os.path.exists(path):
                findings.append({
                    "kind": "dead_path", "event": event, "path": path,
                    "detail": "hook target does not exist — this hook fails "
                              "silently on every fire, so capture is DEAD",
                })
            elif (path.lower().endswith(".exe")
                    and _is_installed_layout(payload_root)
                    and not _same_install(path, payload_root)):
                findings.append({
                    "kind": "foreign_interpreter", "event": event, "path": path,
                    "detail": "hook runs an interpreter outside the running "
                              "payload's install — it executes different m3 code",
                })

        if event in CAPTURE_EVENTS:
            missing = [v for v in REQUIRED_ROOT_PINS if v not in command]
            if missing:
                findings.append({
                    "kind": "unpinned_roots", "event": event,
                    "detail": "missing inline " + ", ".join(missing) +
                              " — hooks reload live but server env re-resolves "
                              "only on restart, so turns can be written to a "
                              "different engine root than the server reads",
                })

    # Corroborate the config's own claim against the file. `wired` in
    # chatlog_status is spec.enabled echoed back, not evidence.
    try:
        import chatlog_config
        cfg = chatlog_config.resolve_config()
        claimed = {n for n, s in cfg.host_agents.items() if s.enabled}
        if "claude-code" in claimed and not entries:
            findings.append({
                "kind": "claimed_not_wired", "event": "claude-code",
                "detail": "config reports the claude-code hook enabled, but "
                          "settings.json contains no m3 hook entry — capture "
                          "is not actually running",
            })
    except Exception:  # noqa: BLE001 — config is optional context, not the check
        pass

    return {
        "status": "issues" if findings else "ok",
        "findings": findings,
        "hooks_seen": len(entries),
    }


def repair(dry_run: bool = True) -> dict:
    """Re-wire m3-owned hook entries. Behind `--fix --fix-hooks`.

    Contract, in order:
      1. Never touch an entry that fails the ownership test — the user's own
         hooks in the same file must survive untouched.
      2. Always write a timestamped backup of settings.json first.
      3. dry_run=True (the DEFAULT) reports the intended change and writes
         nothing, so the operator sees the diff before consenting.

    Repair delegates to the installer's canonical writer rather than editing
    JSON here: that path already owns idempotent merge + statusLine handling,
    and a second writer would be a second thing to keep in sync.
    """
    result: dict = {"dry_run": dry_run, "backup": None, "actions": []}
    findings = check()["findings"]
    if not findings:
        result["actions"].append({"action": "rewire", "status": "skipped",
                                  "detail": "no hook issues to repair"})
        return result

    if dry_run:
        result["actions"].append({
            "action": "rewire", "status": "skipped",
            "detail": f"dry_run=True; would back up settings.json and re-wire "
                      f"{len(findings)} issue(s) via chatlog_init",
        })
        return result

    settings_path = _claude_settings_path()
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{settings_path}.m3-backup-{stamp}"
        shutil.copy2(settings_path, backup)
        result["backup"] = backup
    except Exception as e:  # noqa: BLE001 — no backup, no write
        result["actions"].append({
            "action": "backup", "status": "error",
            "detail": f"could not back up settings.json ({e}); refusing to "
                      "modify it without a backup",
        })
        return result

    try:
        import chatlog_config
        import chatlog_init
        # apply_claude_settings is the canonical, idempotent writer (it owns the
        # merge + statusLine handling). It skips events that already hold a
        # chatlog-owned entry, so stale entries must be dropped first or the
        # re-wire is a no-op on exactly the hooks that are broken.
        settings = _load_settings() or {}
        removed = 0
        for event, entries in list((settings.get("hooks") or {}).items()):
            kept = [
                e for e in entries or []
                if OWNERSHIP_MARKER not in json.dumps(e).lower()
            ]
            removed += len(entries or []) - len(kept)
            if kept:
                settings["hooks"][event] = kept
            else:
                settings["hooks"].pop(event, None)
        if removed:
            with open(_claude_settings_path(), "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2)

        changed, msg = chatlog_init.apply_claude_settings(
            chatlog_config.resolve_config()
        )
        result["actions"].append({
            "action": "rewire", "status": "ok",
            "detail": f"dropped {removed} stale m3 entr(ies), re-wired via "
                      f"apply_claude_settings ({msg}); backup at {result['backup']}",
        })
    except Exception as e:  # noqa: BLE001
        result["actions"].append({
            "action": "rewire", "status": "error",
            "detail": f"{type(e).__name__}: {e}; settings.json unchanged at "
                      f"{result['backup']}",
        })
    return result


def run(brief: bool = False, fix: bool = False) -> int:
    """Print findings; return 1 when capture is broken, else 0."""
    try:
        res = check()
    except Exception as e:  # noqa: BLE001 — the doctor must never die on a probe
        if not brief:
            print(f"  environment: probe failed (non-fatal): {type(e).__name__}: {e}")
        return 0

    if res["status"] == "unknown":
        # Not "healthy" — nothing was verified. Saying OK here would be the
        # same lie this probe exists to catch.
        print("⚠️  chatlog hooks: SKIPPED — ~/.claude/settings.json absent or "
              "unreadable; hook wiring was NOT verified")
        return 0

    if res["status"] == "ok":
        # `brief` means ONE LINE, not silence — see entrypoint_probe.run.
        print(f"✅ chatlog hooks: OK ({res['hooks_seen']} verified against disk)")
        return 0

    print("⚠️  chatlog hooks: [FAIL] wiring does not match this install — CAPTURE MAY BE DEAD")
    for f in res["findings"]:
        where = f.get("path") or f.get("event")
        print(f"    - {f['event']} ({f['kind']}): {where}")
        print(f"      {f['detail']}")

    if fix:
        rep = repair(dry_run=False)
        for act in rep["actions"]:
            print(f"      [{act['status']}] {act['action']}: {act['detail']}")
    else:
        print("      fix: m3 doctor --fix --fix-hooks")
        print("           (backs up settings.json first; only m3-owned entries "
              "are touched)")

    return 1
