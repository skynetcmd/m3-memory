"""PG_URL split-by-role: resolvers, deprecation, and primary-store safeguards.

No database needed — these are pure env-resolution + guard tests. They lock in
the invariant that killed the footgun: the primary store and the data-warehouse
resolve from DISJOINT env vars, so neither can silently arm the other.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from m3_core import paths as _paths  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_pg_env(monkeypatch):
    for v in ("M3_PRIMARY_PG_URL", "M3_PG_URL", "M3_CDW_PG_URL", "PG_URL",
              "M3_PG_FORBIDDEN_HOSTS"):
        monkeypatch.delenv(v, raising=False)
    # reset the one-time deprecation-warning latch so each test sees a fresh warn
    _paths._reset_pg_url_deprecation_state_for_tests()
    yield


class TestPrimaryResolver:
    def test_prefers_primary_specific_var(self, monkeypatch):
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://p/primary")
        monkeypatch.setenv("M3_PG_URL", "postgresql://p/legacy")
        assert _paths.resolve_primary_pg_dsn() == "postgresql://p/primary"

    def test_falls_back_to_m3_pg_url(self, monkeypatch):
        monkeypatch.setenv("M3_PG_URL", "postgresql://p/legacy")
        assert _paths.resolve_primary_pg_dsn() == "postgresql://p/legacy"

    def test_never_reads_pg_url(self, monkeypatch):
        """The whole point: a warehouse PG_URL must not reach the primary store."""
        monkeypatch.setenv("PG_URL", "postgresql://warehouse/cdw")
        assert _paths.resolve_primary_pg_dsn() is None
        assert _paths.resolve_primary_pg_dsn("fallback") == "fallback"

    def test_never_reads_cdw_var(self, monkeypatch):
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://warehouse/cdw")
        assert _paths.resolve_primary_pg_dsn() is None


class TestCdwResolver:
    def test_prefers_cdw_var(self, monkeypatch):
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://w/cdw")
        monkeypatch.setenv("PG_URL", "postgresql://w/legacy")
        assert _paths.resolve_cdw_pg_dsn() == "postgresql://w/cdw"

    def test_falls_back_to_deprecated_pg_url_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("PG_URL", "postgresql://w/legacy")
        import logging

        with caplog.at_level(logging.WARNING, logger="M3_SDK"):
            assert _paths.resolve_cdw_pg_dsn() == "postgresql://w/legacy"
        assert any("PG_URL" in r.message and "M3_CDW_PG_URL" in r.message
                   for r in caplog.records)
        # and it is recorded for `m3 doctor`
        assert _paths.deprecated_env_in_use().get("PG_URL") == "M3_CDW_PG_URL"

    def test_never_reads_m3_pg_url(self, monkeypatch):
        """The warehouse must not read the primary-store var either."""
        monkeypatch.setenv("M3_PG_URL", "postgresql://p/primary")
        assert _paths.resolve_cdw_pg_dsn() is None


class TestRenameMaps:
    def test_pg_url_is_role_split_not_pure_namespacing(self):
        # PG_URL must NOT be in the pure-namespacing map (its new name isn't M3_PG_URL)
        assert "PG_URL" not in _paths.DEPRECATED_ENV_RENAMES
        assert _paths.ROLE_SPLIT_ENV_RENAMES["PG_URL"] == "M3_CDW_PG_URL"

    def test_all_env_renames_is_the_union(self):
        merged = _paths.all_env_renames()
        assert merged["PG_URL"] == "M3_CDW_PG_URL"
        # a known pure-namespacing entry is still present
        assert merged["CHROMA_BASE_URL"] == "M3_CHROMA_BASE_URL"


class TestInstallHardFail:
    def test_raises_when_pg_url_set(self, monkeypatch):
        monkeypatch.setenv("PG_URL", "postgresql://w/legacy")
        with pytest.raises(RuntimeError) as ei:
            _paths.assert_no_deprecated_pg_url_on_install()
        assert "M3_CDW_PG_URL" in str(ei.value)
        assert "M3_PRIMARY_PG_URL" in str(ei.value)

    def test_noop_when_unset(self):
        _paths.assert_no_deprecated_pg_url_on_install()  # must not raise


class TestPrimaryBackendGuards:
    """Forbidden-host + same-as-warehouse guards in postgres_backend._resolve_dsn."""

    def test_forbidden_host_rejected(self, monkeypatch):
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://u:p@198.51.100.51:5432/db")
        monkeypatch.setenv("M3_PG_FORBIDDEN_HOSTS", "198.51.100.51")
        from memory.backends import postgres_backend as pb

        with pytest.raises(RuntimeError) as ei:
            pb._resolve_dsn()
        assert "forbidden host" in str(ei.value).lower()

    def test_forbidden_host_allows_other_hosts(self, monkeypatch):
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://u:p@127.0.0.1:5433/dev")
        monkeypatch.setenv("M3_PG_FORBIDDEN_HOSTS", "198.51.100.51")
        from memory.backends import postgres_backend as pb

        assert pb._resolve_dsn() == "postgresql://u:p@127.0.0.1:5433/dev"

    def test_forbidden_host_no_false_positive_on_prefix(self, monkeypatch):
        # A forbidden 198.51.100.5 must NOT reject the distinct host 198.51.100.51
        # (parsed-host exact match, not substring).
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://u:p@198.51.100.51:5432/db")
        monkeypatch.setenv("M3_PG_FORBIDDEN_HOSTS", "198.51.100.5")
        from memory.backends import postgres_backend as pb

        assert pb._resolve_dsn() == "postgresql://u:p@198.51.100.51:5432/db"

    def test_forbidden_host_not_matched_inside_password(self, monkeypatch):
        # The forbidden host string appearing only in the password must not trigger.
        monkeypatch.setenv(
            "M3_PRIMARY_PG_URL", "postgresql://u:198.51.100.51pw@safehost:5432/db"
        )
        monkeypatch.setenv("M3_PG_FORBIDDEN_HOSTS", "198.51.100.51")
        from memory.backends import postgres_backend as pb

        assert pb._resolve_dsn() == "postgresql://u:198.51.100.51pw@safehost:5432/db"

    def test_same_as_warehouse_rejected(self, monkeypatch):
        same = "postgresql://u:p@127.0.0.1:5432/shared"
        monkeypatch.setenv("M3_PRIMARY_PG_URL", same)
        monkeypatch.setenv("M3_CDW_PG_URL", same)
        from memory.backends import postgres_backend as pb

        with pytest.raises(RuntimeError) as ei:
            pb._resolve_dsn()
        assert "warehouse" in str(ei.value).lower()

    def test_same_host_different_db_allowed(self, monkeypatch):
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://u:p@127.0.0.1:5432/primary")
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://u:p@127.0.0.1:5432/warehouse")
        from memory.backends import postgres_backend as pb

        assert pb._resolve_dsn() == "postgresql://u:p@127.0.0.1:5432/primary"

    def test_same_db_rejected_despite_benign_dsn_differences(self, monkeypatch):
        # Same database, but not byte-identical: explicit vs implicit default port,
        # trailing slash, different credentials. Normalized identity must still match.
        monkeypatch.setenv("M3_PRIMARY_PG_URL", "postgresql://alice:pw1@db.host/shared")
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://bob:pw2@db.host:5432/shared/")
        from memory.backends import postgres_backend as pb

        with pytest.raises(RuntimeError) as ei:
            pb._resolve_dsn()
        assert "same database" in str(ei.value).lower()

    def test_same_db_via_vault_warehouse_rejected(self, monkeypatch):
        # The warehouse DSN lives in the vault under PG_URL (standard keyring
        # setup), NOT env. The same-DSN guard must still see it via the vault.
        same = "postgresql://u:p@127.0.0.1:5432/shared"
        monkeypatch.setenv("M3_PRIMARY_PG_URL", same)
        # no M3_CDW_PG_URL / PG_URL in env — warehouse comes from the vault
        import m3_core.context as _ctx
        from memory.backends import postgres_backend as pb

        monkeypatch.setattr(
            _ctx.M3Context, "get_secret",
            lambda self, key: same if key == "PG_URL" else None,
        )
        with pytest.raises(RuntimeError) as ei:
            pb._resolve_dsn()
        assert "same database" in str(ei.value).lower()

    def test_no_dsn_error_names_primary_var(self, monkeypatch):
        # Stub the vault so the test is deterministic regardless of the machine's
        # keyring (no M3_PRIMARY_PG_URL secret present).
        import m3_core.context as _ctx
        from memory.backends import postgres_backend as pb

        monkeypatch.setattr(_ctx.M3Context, "get_secret", lambda self, key: None)
        with pytest.raises(RuntimeError) as ei:
            pb._resolve_dsn()
        msg = str(ei.value)
        assert "M3_PRIMARY_PG_URL" in msg
        assert "does not read PG_URL" in msg  # the anti-footgun note
