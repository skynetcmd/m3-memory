"""PII scrub for compiled synthesis prose (bin/wiki/pii_scrub.py).

Locks in the exact leaks the v1/v2/v3 prompt A/B surfaced (2026-07-24): a local
model reproduced `C:/Users/bhaba/.m3/engine/agent_memory.db` and "by bhaba"
verbatim. Redaction is a code boundary, not a prompt instruction.
"""
import os
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import pii_scrub as P  # noqa: E402

# ── the exact A/B leaks ──────────────────────────────────────────────────────

def test_windows_abspath_becomes_basename():
    text = "direct SQLite queries to C:/Users/bhaba/.m3/engine/agent_memory.db returned instantly"
    out, n = P.scrub_prose(text)
    assert "C:/Users/bhaba" not in out
    assert "agent_memory.db" in out  # the useful part survives
    assert n >= 1


def test_windows_backslash_abspath():
    out, _ = P.scrub_prose(r"the pin at C:\Users\bhaba\pipx\venvs\m3-memory overrode it")
    assert "bhaba" not in out
    assert "m3-memory" in out


def test_posix_home_path_becomes_basename():
    out, _ = P.scrub_prose("installed at /home/bhaba/.local/bin/m3 for the loop")
    assert "/home/bhaba" not in out
    assert "m3" in out


def test_username_attribution_redacted():
    out, n = P.scrub_prose("This diagnosis was formalized in an analysis dated 2026-07-13 by bhaba.")
    assert "bhaba" not in out
    assert "[user]" in out
    assert "2026-07-13" in out  # the date is fine, only the name goes


def test_username_is_whole_word_only():
    """Must not maul an unrelated substring that merely contains the name."""
    out, _ = P.scrub_prose("the abhabala module is unrelated")
    assert "abhabala" in out  # 'bhaba' inside a word is NOT redacted


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
    """A deployment can add its own usernames/hostnames via config."""
    import json
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / ".wiki_pii.json").write_text(json.dumps({"names": ["skypc"]}))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(cfg))
    out, _ = P.scrub_prose("ran on host skypc overnight")
    assert "skypc" not in out and "[user]" in out


def test_source_memory_fidelity_is_the_synthesis_only():
    """Documents the asymmetry: scrub_prose is for the synthesis. This test just
    asserts the function is pure (returns scrubbed copy, doesn't mutate a source)
    — sources are never passed through it in the writer."""
    src = "C:/Users/bhaba/secret.db"  # a source memory's content
    scrubbed, _ = P.scrub_prose(src)
    assert src == "C:/Users/bhaba/secret.db"  # original string object untouched
    assert scrubbed != src  # a NEW scrubbed value is returned
