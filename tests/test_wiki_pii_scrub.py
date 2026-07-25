"""PII scrub for compiled synthesis prose (bin/wiki/pii_scrub.py).

Locks in the exact leak class the v1/v2/v3 prompt A/B surfaced (2026-07-24): a
local model reproduced absolute home paths (`C:/Users/<name>/...`) and bare
username attributions ("by <name>") verbatim. Redaction is a code boundary, not a
prompt instruction.

Names are deployment-specific and come ONLY from config (`.wiki_pii.json`) — there
is no shipped default — so the name-redaction tests set a name via a temp config.
Path abstraction is pattern-based (no names needed) and uses a generic example.
"""
import json
import os
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import pii_scrub as P  # noqa: E402


def _with_names(tmp_path, monkeypatch, *names):
    """Point the scrubber at a temp config listing `names` to redact."""
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / ".wiki_pii.json").write_text(json.dumps({"names": list(names)}))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))


# ── path abstraction (pattern-based — no configured name needed) ─────────────

def test_windows_abspath_becomes_basename():
    text = "direct SQLite queries to C:/Users/alice/.m3/engine/agent_memory.db returned instantly"
    out, n = P.scrub_prose(text)
    assert "C:/Users/alice" not in out
    assert "agent_memory.db" in out  # the useful part survives
    assert n >= 1


def test_windows_backslash_abspath():
    out, _ = P.scrub_prose(r"the pin at C:\Users\alice\pipx\venvs\m3-memory overrode it")
    assert "alice" not in out
    assert "m3-memory" in out


def test_posix_home_path_becomes_basename():
    out, _ = P.scrub_prose("installed at /home/alice/.local/bin/m3 for the loop")
    assert "/home/alice" not in out
    assert "m3" in out


# ── username attribution (config-driven — no shipped default) ────────────────

def test_username_attribution_redacted(tmp_path, monkeypatch):
    _with_names(tmp_path, monkeypatch, "alice")
    out, n = P.scrub_prose("This diagnosis was formalized in an analysis dated 2026-07-13 by alice.")
    assert "alice" not in out
    assert "[user]" in out
    assert "2026-07-13" in out  # the date is fine, only the name goes


def test_username_is_whole_word_only(tmp_path, monkeypatch):
    """Must not maul an unrelated substring that merely contains the name."""
    _with_names(tmp_path, monkeypatch, "alice")
    out, _ = P.scrub_prose("the palicea module is unrelated")
    assert "palicea" in out  # 'alice' inside a word is NOT redacted


def test_no_names_configured_is_a_noop_for_names(tmp_path, monkeypatch):
    """With no config, no name is redacted — the default ships EMPTY so the
    author's username never leaks into the public repo."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))  # no .wiki_pii.json here
    out, _ = P.scrub_prose("this analysis by alice is fine")
    assert "alice" in out  # nothing configured → nothing name-redacted


# ── secrets reuse (chatlog_redaction) ────────────────────────────────────────

def test_email_redacted():
    out, n = P.scrub_prose("contact user@example.com for access")
    assert "user@example.com" not in out
    assert n >= 1


# ── safety contract ──────────────────────────────────────────────────────────

def test_never_raises_and_preserves_clean_text():
    clean = "# A page\n\nNothing sensitive here — just prose about `memory_search`."
    out, n = P.scrub_prose(clean)
    assert out == clean and n == 0


def test_empty_input():
    assert P.scrub_prose("") == ("", 0)
    assert P.scrub_prose(None) == (None, 0)


def test_config_extra_names(tmp_path, monkeypatch):
    """A deployment adds its own usernames/hostnames via config."""
    _with_names(tmp_path, monkeypatch, "somehost")
    out, _ = P.scrub_prose("ran on host somehost overnight")
    assert "somehost" not in out and "[user]" in out


def test_source_memory_fidelity_is_the_synthesis_only():
    """Documents the asymmetry: scrub_prose is for the synthesis. This test just
    asserts the function is pure (returns a scrubbed copy, doesn't mutate a
    source) — sources are never passed through it in the writer."""
    src = "C:/Users/alice/secret.db"  # a source memory's content
    scrubbed, _ = P.scrub_prose(src)
    assert src == "C:/Users/alice/secret.db"  # original string object untouched
    assert scrubbed != src  # a NEW scrubbed value is returned
