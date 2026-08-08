"""Secret-resolver seam: one canonical, headless-safe token resolver.

The LM token (and friends) must resolve through ONE shared path
(`m3_sdk.get_secret` -> `auth_utils.get_api_key`: env -> keyring -> keychain ->
vault, LM_STUDIO_API_KEY alias) so it works in headless launchers that never
inherit the shell env (§3). These tests pin that facade, the file-extraction
LLM-endpoint fallback, and guard against re-introducing hand-rolled/env-only
token reads (the fragmentation that caused the headless bug).
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BIN = _ROOT / "bin"
for _p in (str(_ROOT), str(_BIN)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── facade: m3_sdk.get_secret is the one entry point ────────────────────────────

def test_m3_sdk_exposes_get_secret(monkeypatch):
    import m3_sdk
    assert callable(m3_sdk.get_secret)
    monkeypatch.setenv("SOME_TEST_SECRET", "s3cr3t")
    assert m3_sdk.get_secret("SOME_TEST_SECRET") == "s3cr3t"


def test_get_secret_lm_alias(monkeypatch):
    # The LM_API_TOKEN -> LM_STUDIO_API_KEY alias lives in the shared resolver.
    import m3_sdk
    monkeypatch.delenv("LM_API_TOKEN", raising=False)
    monkeypatch.setenv("LM_STUDIO_API_KEY", "tok-alias")
    assert m3_sdk.get_secret("LM_API_TOKEN") == "tok-alias"


# ── file-extraction resolves an LLM via the shared llm_failover fallback ─────────

def test_extract_endpoint_falls_back_to_shared_resolver(monkeypatch):
    for v in ("M3_FILES_EXTRACT_URL", "M3_FILES_SUMMARY_URL", "M3_LMSTUDIO_URL",
              "M3_LLM_URL", "M3_LLM_ENDPOINTS_CSV", "LLM_ENDPOINTS_CSV"):
        monkeypatch.delenv(v, raising=False)
    from files_memory.extract import _llm_endpoint, llm_available
    # With no override, it must fall back to llm_failover's default (LM Studio),
    # not report "no LLM" — the whole point of the seam fix.
    ep = _llm_endpoint()
    assert ep and "1234" in ep, f"expected LM Studio fallback, got {ep!r}"
    assert llm_available() is True


def test_extract_endpoint_override_wins(monkeypatch):
    monkeypatch.setenv("M3_FILES_EXTRACT_URL", "http://example.test:9/v1")
    from files_memory.extract import _llm_endpoint
    assert _llm_endpoint() == "http://example.test:9/v1"


def test_llm_auth_headers_resolves_token(monkeypatch):
    monkeypatch.setenv("LM_API_TOKEN", "tok-xyz")
    from files_memory.config import llm_auth_headers
    assert llm_auth_headers() == {"Authorization": "Bearer tok-xyz"}


# ── guard: no module reinvents the token seam (env-only / hand-rolled keychain) ──

_ALLOWED = {"auth_utils.py"}  # the resolver itself owns the native keychain path


def test_no_handrolled_lm_keychain_subprocess():
    """No module may shell out to `security find-generic-password` for the LM
    Studio key — that reinvents the cross-platform resolver env-only/macOS-only.
    Use m3_sdk.get_secret / auth_utils.get_api_key instead. Matches the actual
    subprocess arg-list form so a docstring MENTIONING the old pattern is fine."""
    # e.g. ["security", "find-generic-password", ...] near LM_STUDIO_API_KEY
    call = re.compile(r"""["']security["']\s*,\s*["']find-generic-password""")
    offenders = []
    for py in _BIN.rglob("*.py"):
        if py.name in _ALLOWED or py.name.startswith("test_"):
            continue
        txt = py.read_text(encoding="utf-8", errors="replace")
        if call.search(txt) and "LM_STUDIO_API_KEY" in txt:
            offenders.append(str(py.relative_to(_BIN)))
    assert not offenders, (
        "hand-rolled LM keychain lookup (use m3_sdk.get_secret): " + ", ".join(offenders)
    )


def test_migrated_sites_route_through_resolver():
    """The previously-fragmented sites now go through the canonical resolver.
    (A bare os.environ read may survive only inside an except-branch fallback.)"""
    checks = {
        "macbook_status_server.py": "get_secret",
        "mission_control.py": "get_secret",
        "promote_pipeline.py": "get_secret",
        "files_memory/config.py": "get_api_key",   # low-level: impl directly
        "llm_failover.py": "get_api_key",           # low-level: impl directly
    }
    for rel, needle in checks.items():
        txt = (_BIN / rel).read_text(encoding="utf-8")
        assert needle in txt, f"{rel} should resolve the LM token via {needle}(...)"
        # And must not shell out for the key anymore.
        assert '"find-generic-password"' not in txt and "'find-generic-password'" not in txt, \
            f"{rel} still shells out to security find-generic-password"
