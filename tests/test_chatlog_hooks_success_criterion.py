"""Regression tests for the chatlog exit-hook success criterion.

Two real false-positive bugs have hit these hooks, both causing bogus
``~/.m3/unsaved_chats/`` spill files + a "M3 CHATLOG NOT SAVED" scream while
ingest was actually succeeding:

  1. The PRETTY-JSON parser bug (2026-06-04..13, ~1485 bogus files): the parser
     scanned for a line *starting with* "{" and json.loads'd that single line,
     which is just "{" for pretty-printed JSON, so it always reported written=0.
     Fixed in claude_code_precompact.py with _extract_last_json_object(); this
     test pins that the fix is present in ALL THREE hooks.

  2. The written==0-is-failure bug (observed 2026-06-27): the live MCP server
     captures turns itself via chatlog_write, so by the time a PreCompact/Stop/
     SessionEnd hook runs its own ingest, the turns are ALREADY in the DB ->
     ingest legitimately reports written=0, skipped=N (all deduped). That is a
     SUCCESS, not a loss. Treating written==0 as failure screamed every time the
     live capture beat the hook. Success must be REACHABILITY (failed==0, no
     error, something seen), not written>0.

These hooks are three independent hand-maintained copies with a "# Shared core
(parity ...)" comment that promised parity but did not enforce it — which is
exactly why fix #1 reached only the claude hook. The parity guard below makes
divergence a test failure.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HOOK_DIR = os.path.join(os.path.dirname(__file__), "..", "bin", "hooks", "chatlog")
HOOKS = ("claude_code_precompact", "gemini_cli_onexit", "opencode_session_end")


def _load(name):
    path = os.path.join(HOOK_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("_m3_hook_" + name, os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Pretty-printed (multi-line) JSON exactly as chatlog_ingest.py emits it, with
# log lines interleaved before it — the shape that broke the old parser.
def _fake_ingest_stdout(written, skipped, failed=0, error=None, with_logs=True):
    import json
    payload = {"written": written, "skipped": skipped, "spilled": 0,
               "failed": failed, "errors": [], "session_id": "abc123"}
    if error is not None:
        payload["error"] = error
    body = json.dumps(payload, indent=2)
    if with_logs:
        return ("2026-06-27 22:59:37 - M3_SDK - INFO - SQLite pool ready\n"
                "2026-06-27 22:59:37 - chatlog_ingest - INFO - Ingested foo.jsonl: "
                f"written={written}, skipped={skipped}\n" + body)
    return body


class _FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _patch_subprocess(monkeypatch, mod, stdout, returncode=0):
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(stdout, returncode))


# ── Parser regression (bug #1): pretty-printed JSON must parse ────────────────

def test_extract_last_json_object_present_in_all_hooks():
    for name in HOOKS:
        mod = _load(name)
        assert hasattr(mod, "_extract_last_json_object"), (
            f"{name} is missing the robust parser — it will hit the pretty-JSON "
            "bug that wrote ~1485 bogus spill files")


def test_pretty_json_parses_in_all_hooks():
    pretty = _fake_ingest_stdout(written=9, skipped=1673)
    for name in HOOKS:
        mod = _load(name)
        obj = mod._extract_last_json_object(pretty)
        assert obj is not None, f"{name} failed to parse pretty-printed ingest JSON"
        assert obj["written"] == 9 and obj["skipped"] == 1673


def test_run_ingest_returns_five_tuple_in_all_hooks(monkeypatch):
    """run_ingest must surface skipped+failed (not just written+error) so the
    caller can tell 'deduped success' from 'real failure'."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=0, skipped=1682))
        written, skipped, failed, error, rc = mod.run_ingest("py", mod.Path("ingest.py"), [])
        assert (written, skipped, failed, error) == (0, 1682, 0, None)


# ── Success-criterion regression (bug #2): written==0 + skipped>0 is SUCCESS ──

def test_all_deduped_does_not_scream(monkeypatch, tmp_path, capsys):
    """written=0, skipped=N (live MCP capture beat the hook) -> NO fallback file,
    NO scream, exit 0. This is the 2026-06-27 false-alarm."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=0, skipped=1682))
        # Redirect the fallback dir into tmp so we can assert nothing is written.
        unsaved = tmp_path / name / ".m3" / "unsaved_chats"
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
        monkeypatch.setattr(sys, "stdin", _StdinStub(name))

        rc = mod.main()
        assert rc == 0, f"{name}: all-deduped run must exit 0, got {rc}"
        assert not unsaved.exists() or not any(unsaved.iterdir()), (
            f"{name}: all-deduped run wrote a bogus spill file")
        err = capsys.readouterr().err
        assert "M3 CHATLOG NOT SAVED" not in err, (
            f"{name}: all-deduped run screamed a false alarm")


def test_genuine_failure_still_screams(monkeypatch, tmp_path, capsys):
    """error set OR failed>0 OR nothing-seen MUST still scream + spill — the
    safety net we must not weaken while fixing the false positive."""
    cases = [
        _fake_ingest_stdout(written=0, skipped=0, error="m3 unreachable"),
        _fake_ingest_stdout(written=0, skipped=0, failed=3),
        _fake_ingest_stdout(written=0, skipped=0),  # nothing seen at all
    ]
    for name in HOOKS:
        for stdout in cases:
            mod = _load(name)
            _patch_subprocess(monkeypatch, mod, stdout, returncode=0)
            unsaved = tmp_path / name / ".m3" / "unsaved_chats"
            monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
            monkeypatch.setattr(sys, "stdin", _StdinStub(name))

            rc = mod.main()
            assert rc != 0, f"{name}: genuine failure must exit non-zero"
            assert unsaved.exists() and any(unsaved.iterdir()), (
                f"{name}: genuine failure must write a spill file")
            err = capsys.readouterr().err
            assert "M3 CHATLOG NOT SAVED" in err, (
                f"{name}: genuine failure must scream")


def test_normal_write_succeeds_quietly(monkeypatch, tmp_path, capsys):
    """written>0 -> success, no scream, no spill (the happy path)."""
    for name in HOOKS:
        mod = _load(name)
        _patch_subprocess(monkeypatch, mod, _fake_ingest_stdout(written=9, skipped=1673))
        unsaved = tmp_path / name / ".m3" / "unsaved_chats"
        monkeypatch.setattr(mod.Path, "home", staticmethod(lambda p=tmp_path, n=name: p / n))
        monkeypatch.setattr(sys, "stdin", _StdinStub(name))

        rc = mod.main()
        assert rc == 0
        assert not unsaved.exists() or not any(unsaved.iterdir())
        assert "M3 CHATLOG NOT SAVED" not in capsys.readouterr().err


# ── Parity guard: the three hooks must not silently diverge again ─────────────

def test_parser_is_byte_identical_across_hooks():
    import inspect
    srcs = {name: inspect.getsource(_load(name)._extract_last_json_object) for name in HOOKS}
    ref = srcs[HOOKS[0]]
    for name in HOOKS[1:]:
        assert srcs[name] == ref, (
            f"{name}._extract_last_json_object diverged from {HOOKS[0]} — the "
            "'shared core (parity)' promise is broken; re-sync the parser")


class _StdinStub:
    """A minimal stdin whose .read() returns a valid hook envelope for `name`."""
    def __init__(self, name):
        import json
        event = {
            "claude_code_precompact": "PreCompact",
            "gemini_cli_onexit": "SessionEnd",
            "opencode_session_end": "SessionEnd",
        }[name]
        self._raw = json.dumps({
            "session_id": "abc123",
            "transcript_path": os.path.abspath(__file__),  # any existing file
            "cwd": os.getcwd(),
            "hook_event_name": event,
        })

    def read(self):
        return self._raw
