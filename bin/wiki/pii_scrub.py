"""Deterministic PII scrub for COMPILED SYNTHESIS PROSE (not source memories).

Why a code-layer scrub and not a prompt instruction: the v1/v2/v3 prompt A/B
(2026-07-24) showed a local 9B model reproduces absolute paths and usernames
verbatim under any "keep the specifics" pressure, and an explicit "abstract these"
prompt line does NOT reliably suppress it. Redaction is a safety boundary
(§3 fail-safe, §6 content safety at the boundary), so it must be enforced in
code, not left to the model.

ASYMMETRY (the key design point): only the SYNTHESIS is scrubbed. The linked
source memories keep full fidelity — paths, hostnames, usernames — because they
are the system of record you trace back to via the `consolidates` edges. The
synthesis is the redacted, shareable *view*; the memories behind it are complete.

Two layers:
  1. Reuse `chatlog_redaction.scrub` for SECRETS + classic PII (API keys, bearer
     tokens, GitHub PATs, email, phone, SSN) — already tested, don't reinvent.
  2. Add the gap the chatlog scrubber doesn't cover and that a memory corpus
     (unlike a code repo) contains: absolute filesystem PATHS -> basename, and
     personal USERNAMES / HOSTNAMES -> neutral placeholder. Configurable.

Fail-safe: on any error the caller keeps the UNSCRUBBED prose? NO — the opposite.
A scrub error must never let raw PII through, but it also must not lose the page.
scrub_prose returns (text, n) and never raises; a pattern that can't compile is
skipped with the rest still applied. The caller decides policy; the default here
is best-effort-but-applied.
"""
from __future__ import annotations

import os
import re

# ── absolute-path abstraction ────────────────────────────────────────────────
# Windows (C:\Users\name\..., C:/Users/name/...) and POSIX (/home/name/...,
# /Users/name/...). Replace the whole path with its final component, so
# "C:/Users/alice/.m3/engine/agent_memory.db" -> "agent_memory.db" — the
# technically-useful part survives, the personal prefix does not.
_WIN_ABS = re.compile(r"[A-Za-z]:[\\/](?:[^\s\\/]+[\\/])*([^\s\\/]+)")
_POSIX_HOME = re.compile(r"/(?:home|Users)/[^\s/]+/(?:[^\s/]+/)*([^\s/]+)")


def _basename_win(m: "re.Match") -> str:
    return m.group(1)


def _basename_posix(m: "re.Match") -> str:
    return m.group(1)


# ── username / hostname placeholders ─────────────────────────────────────────
# These are deployment-specific VALUES (not a pattern), so they come ENTIRELY
# from local config — there is no hardcoded default, because a shipped default
# would (a) leak the author's own username into a public repo and (b) be wrong
# for every other deployment. Each install lists its own usernames/hostnames in
# M3_CONFIG_ROOT/.wiki_pii.json. A bare username appearing as a word ("by <name>",
# "user <name>") is the leak the prompt A/B surfaced; the config catches it.
_DEFAULT_SELF_NAMES = ()  # no default — supply names via .wiki_pii.json (see below)
_CONFIG_BASENAME = ".wiki_pii.json"


def _config_root() -> str:
    root = os.environ.get("M3_CONFIG_ROOT")
    if root:
        return root
    mem = os.environ.get("M3_MEMORY_ROOT")
    if mem:
        return os.path.join(mem, "config")
    return os.path.join(os.path.expanduser("~"), ".m3", "config")


def _self_names() -> "tuple[str, ...]":
    """Personal usernames/hostnames to redact. Default + optional config file
    M3_CONFIG_ROOT/.wiki_pii.json  {"names": ["alice", "alice-laptop"]}. A code-resolved
    file, not an env var (§3: headless launchers inherit no shell env)."""
    import json
    names: set[str] = set(_DEFAULT_SELF_NAMES)
    path = os.path.join(_config_root(), _CONFIG_BASENAME)
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                extra = json.load(f).get("names")
            if isinstance(extra, list):
                names.update(str(x) for x in extra if x)
    except Exception:
        pass  # config trouble never weakens the scrub — defaults still apply
    return tuple(sorted(names, key=len, reverse=True))  # longest first


def _redact_secrets(text: str) -> "tuple[str, int]":
    """Reuse the tested chatlog secret/PII scrubber. Returns (text, n)."""
    try:
        from chatlog_redaction import scrub
    except Exception:
        return text, 0
    # Enable the full secret set + classic PII for a shareable artifact.
    cfg = {
        "enabled": True,
        "patterns": ["api_keys", "bearer_tokens", "jwt", "aws_keys",
                     "github_tokens", "pii"],
        "custom_regex": [],
        "redact_pii": True,
    }
    try:
        scrubbed, n, _groups = scrub(text, cfg)
        return scrubbed, n
    except Exception:
        return text, 0


def scrub_prose(text: str) -> "tuple[str, int]":
    """Scrub a COMPILED SYNTHESIS for sharing. Returns (scrubbed_text, count).
    Never raises — a shareable artifact must not carry raw PII, and must not be
    lost to a scrub error either. Applied to the synthesis only; sources stay
    full-fidelity."""
    if not text:
        return text, 0
    total = 0

    # 1) secrets + classic PII (reused, tested).
    text, n = _redact_secrets(text)
    total += n

    # 2) absolute paths -> basename.
    text, n = _WIN_ABS.subn(_basename_win, text)
    total += n
    text, n = _POSIX_HOME.subn(_basename_posix, text)
    total += n

    # 3) personal usernames/hostnames -> placeholder, whole-word only so we don't
    # maul an unrelated substring.
    for name in _self_names():
        pat = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
        text, n = pat.subn("[user]", text)
        total += n

    return text, total
