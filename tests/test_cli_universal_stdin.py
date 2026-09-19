"""Every CLI tool accepts a piped JSON object, not just the complex ten.

⚠ THE GAP: `--json` / `--json-file` were added only under `if complex_:`, which
is true for 10 of 118 tools (those with an object or array-of-object parameter).
So the obvious UNIX thing —

    echo '{"query":"postgres","k":3}' | m3 memory memory_search --json-file -

— failed on almost the entire surface with "unrecognized arguments". The flags
cost nothing on a simple tool, and piping is not a feature that should depend on
a parameter's JSON type.

Two consequences had to be handled, and both are pinned below:

1. MERGE, DON'T REPLACE. A simple tool builds its args from declared flags. The
   piped object is overlaid on those rather than substituted for them, so
   `--query x` and a piped `{"k": 3}` compose. An explicit flag wins on
   conflict: it is the more deliberate statement, and it lets a script pipe a
   base object and override one field per call.

2. REQUIRED ARGS MOVED OFF ARGPARSE. A required value can now arrive by flag or
   inside the JSON, and argparse only sees the former — it would reject the call
   before the JSON was ever read. Requirements are checked after the merge, so
   either route satisfies them and a real omission still fails loudly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _run(args, stdin_text=None, timeout=120):
    """Invoke the WORKING-TREE cli, never the installed `m3` on PATH.

    ⚠ `m3` resolves to the pipx-installed console script, which is a different
    (older) copy. Testing through it silently validates the wrong code — it is
    what made this change look broken for several minutes during development.
    """
    return subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", *args],
        input=stdin_text, capture_output=True, text=True,
        cwd=_REPO, timeout=timeout,
    )


# ── the flags exist everywhere ───────────────────────────────────────────────

def test_a_simple_tool_advertises_json_and_json_file():
    """memory_search has no object parameter, so it was previously excluded."""
    out = _run(["memory", "memory_search", "--help"]).stdout
    assert "--json " in out or "--json OBJ" in out
    assert "--json-file" in out


def test_the_help_names_stdin_explicitly():
    out = _run(["memory", "memory_search", "--help"]).stdout
    assert "stdin" in out.lower(), (
        "the help does not mention stdin, so the '-' convention is undiscoverable"
    )


# ── piping works ─────────────────────────────────────────────────────────────

def test_a_piped_object_drives_a_simple_tool():
    r = _run(["memory", "memory_search", "--json-file", "-"],
             stdin_text=json.dumps({"query": "postgres", "k": 2}))
    assert r.returncode == 0, r.stderr[-400:]
    assert "results" in r.stdout


def test_flags_still_work_unchanged():
    """The change must not cost the existing, overwhelmingly common path."""
    r = _run(["memory", "memory_search", "--query", "postgres", "--k", "1"])
    assert r.returncode == 0, r.stderr[-400:]
    assert "results" in r.stdout


def test_flags_and_piped_json_compose():
    """Neither silently wins: the object is the base, flags refine it.

    Asserted via --dry-run, which validates and echoes the RESOLVED arguments
    without executing. Checking result COUNTS instead would depend on the
    developer's corpus — an empty store returns "No results found" and the test
    becomes about seed data rather than about precedence.
    """
    r = _run(["memory", "memory_search", "--json-file", "-", "--k", "1",
              "--dry-run"],
             stdin_text=json.dumps({"query": "postgres", "k": 9}))
    assert r.returncode == 0, r.stderr[-400:]
    assert '"ok": true' in r.stdout.lower(), (
        f"the merged call did not validate: {r.stdout[-300:]}"
    )


# ── required arguments ───────────────────────────────────────────────────────

def test_a_required_arg_can_come_from_the_pipe():
    """⚠ THE CASE ARGPARSE CANNOT SEE.

    `query` is required. argparse rejects the call before any JSON is read, so
    leaving it argparse-required made the piped form impossible.
    """
    r = _run(["memory", "memory_search", "--json-file", "-"],
             stdin_text=json.dumps({"query": "postgres"}))
    assert r.returncode == 0, (
        f"a required arg supplied via the pipe was rejected: {r.stderr[-400:]}"
    )


def test_a_genuinely_missing_required_arg_still_fails_loudly():
    """Moving the check off argparse must not lose it (§3)."""
    r = _run(["memory", "memory_search", "--k", "1"])
    assert r.returncode != 0
    assert "requires --query" in r.stderr, (
        f"missing required arg did not produce a clear error: {r.stderr[-300:]}"
    )


def test_the_error_names_both_routes():
    """The message must say a pipe is an option, or users will not find it."""
    r = _run(["memory", "memory_search", "--k", "1"])
    assert "--json" in r.stderr and "--json-file" in r.stderr


# ── safety ───────────────────────────────────────────────────────────────────

def test_no_stdin_read_without_the_flag():
    """⚠ THE HANG GUARD.

    stdin is read ONLY when --json-file - is passed. If the CLI ever read it
    speculatively, every interactive invocation with no piped input would block
    forever waiting for EOF.
    """
    r = _run(["memory", "memory_search", "--query", "postgres", "--k", "1"],
             timeout=45)
    assert r.returncode == 0


def test_version_does_not_block():
    r = _run(["--version"], timeout=45)
    assert r.returncode == 0
    assert "m3-memory" in r.stdout


# ── malformed input ──────────────────────────────────────────────────────────

def test_invalid_json_on_stdin_is_reported():
    r = _run(["memory", "memory_search", "--json-file", "-"],
             stdin_text="{not valid json")
    assert r.returncode != 0
    assert "json" in r.stderr.lower()


def test_an_unknown_key_is_rejected_with_a_suggestion():
    """The existing difflib guard must still cover the piped path.

    A near-miss key silently discarded is how a call reports ok while doing the
    wrong thing — the defect that guard was written for.
    """
    r = _run(["memory", "memory_search", "--json-file", "-"],
             stdin_text=json.dumps({"query": "x", "kk": 3}))
    assert r.returncode != 0
    assert "unknown key" in r.stderr.lower()
    assert "did you mean" in r.stderr.lower()


@pytest.mark.parametrize("domain,tool", [
    ("memory", "memory_get"),
    ("memory", "memory_search"),
    ("memory", "memory_write"),
    ("files", "files_search"),
    ("tasks", "task_list"),
])
def test_json_file_reaches_tools_of_every_shape(domain, tool):
    """Spot-check across domains that the flag is genuinely universal.

    ⚠ CATALOG TOOLS ONLY. `m3 chatlog status` and friends are bespoke
    subcommands hand-written in cli.py, not generated from a ToolSpec, so they
    never had these flags and are out of scope here.
    """
    out = _run([domain, tool, "--help"]).stdout
    assert "--json-file" in out, f"{domain} {tool} is missing --json-file"


def test_an_explicit_json_null_is_a_missing_required_arg():
    """⚠ REACHABLE ONLY THROUGH THE PIPE.

    `{"query": null}` puts the key in the merged dict while supplying nothing,
    so a presence-only check passes it through and the impl raises a TypeError
    from inside the tool. Flags cannot produce this -- flag_args drops None --
    so the JSON route the merge added is what opened it.
    """
    r = _run(["memory", "memory_search", "--json-file", "-"],
             stdin_text=json.dumps({"query": None}))
    assert r.returncode != 0, (
        "an explicit null satisfied the required-arg check and reached the impl"
    )
    assert "requires --query" in r.stderr, (
        f"the null was rejected, but not with the clear CLI error: {r.stderr[-300:]}"
    )
