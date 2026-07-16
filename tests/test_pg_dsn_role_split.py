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


class TestInstallerMultiLocationScan:
    """The installer's scanner must find PG_URL in EVERY common location and
    report them all at once — not stop at the first hit."""

    def test_finds_env_and_config_and_shell_profile_together(self, monkeypatch, tmp_path):
        from m3_memory import installer as I

        # 1. live env
        monkeypatch.setenv("PG_URL", "postgresql://env/legacy")

        # 2. a client settings.json with PG_URL in an mcpServers env block
        settings = tmp_path / "settings.json"
        settings.write_text(
            '{"mcpServers": {"memory": {"command": "py", "env": '
            '{"PG_URL": "postgresql://cfg/legacy"}}}}',
            encoding="utf-8",
        )
        monkeypatch.setattr(I, "_client_config_sources",
                            lambda: {"Claude Code": [settings]})
        # no cwd .env
        monkeypatch.chdir(tmp_path)

        # 3. a shell profile that exports PG_URL — point HOME at tmp_path
        home = tmp_path / "home"
        home.mkdir()
        (home / ".zshenv").write_text(
            "# shell rc\nexport PG_URL=postgresql://shell/legacy\n", encoding="utf-8"
        )
        monkeypatch.setattr(I.Path, "home", staticmethod(lambda: home))
        # Isolate from the host's real Windows registry so the count is deterministic.
        monkeypatch.setattr(I.sys, "platform", "linux")

        locs = I._find_deprecated_pg_url_locations()
        # ALL THREE surfaced — not just the first found.
        assert any("environment" in x for x in locs)
        assert str(settings) in locs
        assert str(home / ".zshenv") in locs
        assert len(locs) == 3

        with pytest.raises(RuntimeError) as ei:
            I._assert_no_deprecated_pg_url_anywhere()
        msg = str(ei.value)
        assert "3 location" in msg or "location(s)" in msg
        assert str(settings) in msg and str(home / ".zshenv") in msg

    def test_clean_when_nowhere_set(self, monkeypatch, tmp_path):
        from m3_memory import installer as I

        monkeypatch.delenv("PG_URL", raising=False)
        monkeypatch.setattr(I, "_client_config_sources", lambda: {})
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(I.Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(tmp_path)
        # Neutralize the Windows registry branch so this "clean" case doesn't pick
        # up a real HKCU PG_URL on the test machine (isolate the unit under test).
        monkeypatch.setattr(I.sys, "platform", "linux")

        assert I._find_deprecated_pg_url_locations() == []
        I._assert_no_deprecated_pg_url_anywhere()  # must not raise

    def test_windows_registry_user_scope_detected(self, monkeypatch, tmp_path):
        """A persistent Windows User-scope PG_URL (registry) is surfaced, not just
        the inherited process copy — unsetting the process alone wouldn't stick."""
        from m3_memory import installer as I

        monkeypatch.delenv("PG_URL", raising=False)
        monkeypatch.setattr(I, "_client_config_sources", lambda: {})
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(I.Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(I.sys, "platform", "win32")

        # Fake winreg: PG_URL present at HKCU\Environment, absent at HKLM.
        store = {("HKCU", r"Environment"): {"PG_URL": ("postgresql://reg/legacy", 1)}}
        monkeypatch.setitem(__import__("sys").modules, "winreg",
                            _make_fake_winreg(store))

        locs = I._find_deprecated_pg_url_locations()
        assert any("HKEY_CURRENT_USER" in x for x in locs), locs
        assert not any("HKEY_LOCAL_MACHINE" in x for x in locs)  # absent at machine scope

    def test_shell_profile_regex_ignores_m3_cdw_and_comments(self, monkeypatch, tmp_path):
        """M3_CDW_PG_URL= and a commented PG_URL must NOT be flagged."""
        from m3_memory import installer as I

        monkeypatch.delenv("PG_URL", raising=False)
        monkeypatch.setattr(I, "_client_config_sources", lambda: {})
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text(
            "export M3_CDW_PG_URL=postgresql://ok/new\n"
            "# export PG_URL=postgresql://old/commented\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(I.Path, "home", staticmethod(lambda: home))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(I.sys, "platform", "linux")  # isolate from host registry

        assert I._find_deprecated_pg_url_locations() == []


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


# ── Windows registry env migration (doctor --fix auto-writes User scope) ──────
def _make_fake_winreg(store):
    """Build a fake `winreg` module backed by `store` = {(hive, subkey): {name: (val, type)}}.
    Simulates OpenKey/QueryValueEx/SetValueEx/DeleteValue with mutation."""
    import types

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.HKEY_LOCAL_MACHINE = "HKLM"
    fake.KEY_READ = 0x20019
    fake.KEY_SET_VALUE = 0x0002
    fake.REG_SZ = 1

    class _Key:
        def __init__(self, hive, subkey):
            self.k = (hive, subkey)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def OpenKey(hive, subkey, reserved=0, access=0):
        if (hive, subkey) not in store:
            raise FileNotFoundError(subkey)
        return _Key(hive, subkey)

    def QueryValueEx(key, name):
        d = store[key.k]
        if name not in d:
            raise FileNotFoundError(name)
        return d[name]

    def SetValueEx(key, name, reserved, type_, value):
        store[key.k][name] = (value, type_)

    def DeleteValue(key, name):
        store[key.k].pop(name, None)

    fake.OpenKey = OpenKey
    fake.QueryValueEx = QueryValueEx
    fake.SetValueEx = SetValueEx
    fake.DeleteValue = DeleteValue
    return fake


class TestRegistryEnvMigration:
    def _wire(self, monkeypatch, store):
        from m3_memory import installer as I

        monkeypatch.setattr(I.sys, "platform", "win32")
        monkeypatch.setitem(__import__("sys").modules, "winreg",
                            _make_fake_winreg(store))
        # avoid a real WM_SETTINGCHANGE broadcast in the test
        monkeypatch.setattr(I, "_broadcast_env_change", lambda: None)
        return I

    def test_scan_all_names_not_just_pg_url(self, monkeypatch):
        store = {("HKCU", r"Environment"): {
            "PG_URL": ("postgresql://legacy", 1),
            "SYNC_TARGET_IP": ("198.51.100.9", 1),  # RFC 5737 doc IP, not real infra
            "M3_CHROMA_BASE_URL": ("http://ok", 1),  # already-new, must NOT flag
        }}
        I = self._wire(monkeypatch, store)
        hits = I._scan_registry_env_deprecations(
            {"PG_URL": "M3_CDW_PG_URL", "SYNC_TARGET_IP": "M3_SYNC_TARGET_IP",
             "CHROMA_BASE_URL": "M3_CHROMA_BASE_URL"}
        )
        olds = {h["old"] for h in hits}
        assert olds == {"PG_URL", "SYNC_TARGET_IP"}  # not the already-migrated one

    def test_apply_renames_user_scope_and_carries_value(self, monkeypatch):
        store = {("HKCU", r"Environment"): {"PG_URL": ("postgresql://secret@h/db", 1)}}
        I = self._wire(monkeypatch, store)
        actions = I._migrate_registry_env_names(apply=True)
        env = store[("HKCU", r"Environment")]
        assert "PG_URL" not in env
        assert env["M3_CDW_PG_URL"] == ("postgresql://secret@h/db", 1)  # value carried
        assert any("renamed PG_URL -> M3_CDW_PG_URL" in a for a in actions)

    def test_actions_never_print_the_secret_value(self, monkeypatch):
        # Obvious dummy credential (RFC 5737 doc host) — the test asserts these
        # tokens do NOT appear in the action log (redaction), so they must be
        # unmistakably fake and never a real secret.
        secret_pw = "DUMMYPWDONOTLOG"
        secret_host = "198.51.100.200"
        store = {("HKCU", r"Environment"):
                 {"PG_URL": (f"postgresql://user:{secret_pw}@{secret_host}/db", 1)}}
        I = self._wire(monkeypatch, store)
        blob = "\n".join(I._migrate_registry_env_names(apply=False))
        assert secret_pw not in blob and secret_host not in blob

    def test_conflict_drops_old_keeps_new(self, monkeypatch):
        store = {("HKCU", r"Environment"): {
            "PG_URL": ("postgresql://old", 1),
            "M3_CDW_PG_URL": ("postgresql://new", 1),
        }}
        I = self._wire(monkeypatch, store)
        actions = I._migrate_registry_env_names(apply=True)
        env = store[("HKCU", r"Environment")]
        assert "PG_URL" not in env
        assert env["M3_CDW_PG_URL"] == ("postgresql://new", 1)  # new untouched
        assert any("dropped superseded PG_URL" in a for a in actions)

    def test_hklm_machine_scope_reported_not_written(self, monkeypatch):
        store = {
            ("HKCU", r"Environment"): {},
            ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"):
                {"PG_URL": ("postgresql://machine", 1)},
        }
        I = self._wire(monkeypatch, store)
        actions = I._migrate_registry_env_names(apply=True)
        hklm = store[("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]
        assert "PG_URL" in hklm  # NOT written — machine scope needs admin
        assert any("admin" in a.lower() and "PG_URL" in a for a in actions)

    def test_noop_on_non_windows(self, monkeypatch):
        from m3_memory import installer as I
        monkeypatch.setattr(I.sys, "platform", "linux")
        assert I._migrate_registry_env_names(apply=True) == []
        assert I._scan_registry_env_deprecations({"PG_URL": "M3_CDW_PG_URL"}) == []
