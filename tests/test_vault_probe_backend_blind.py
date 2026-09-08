"""The vault-presence probe must follow the store, not assume a SQLite file.

`synchronized_secrets` lives in the MAIN store, whose path comes from
`resolve_db_path()`. The probe used to be:

    if not os.path.exists(vault_path): return False
    conn = sqlite3.connect(vault_path, timeout=2)

Both halves assert a local SQLite file. On a PostgreSQL install `vault_path` is
a stale-or-absent SQLite path, so `_vault_has_secrets()` returned **False** for
an install that demonstrably HAS secrets.

That return value is not cosmetic. `_get_device_salt()` uses it to tell a
genuine fresh install (safe to mint a new salt) from an existing install whose
salt went missing (must NOT regenerate) -- and a fresh salt orphans every
encrypted secret in the vault, permanently. `SaltMissingError` exists precisely
to stop that, added 2026-07-03 after the Homecoming root relocation lost a salt
and a silent regen destroyed a live vault. A False here walks straight past that
guard.

So this is a correctness/data-loss bug on the second supported backend, not a
tidiness conversion. These tests pin the behaviour, not the implementation.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock as mock
from contextlib import contextmanager
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import auth_utils  # noqa: E402


class _FakeConn:
    """Minimal DB-API surface the probe touches."""

    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, *a):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return self._row


def _backend_yielding(row, *, seen=None):
    """A stand-in backend whose open_readonly yields a connection returning `row`."""

    class _B:
        @staticmethod
        @contextmanager
        def open_readonly(db_path):
            if seen is not None:
                seen.append(db_path)
            yield _FakeConn(row)

    return _B()


class TestVaultProbeIsBackendBlind(unittest.TestCase):

    def test_probe_goes_through_the_seam(self):
        """It must ask the backend, not sqlite3 -- that is what makes it work
        on PostgreSQL, where there is no vault FILE at all."""
        seen: list = []
        with mock.patch.object(auth_utils, "_backend",
                               return_value=_backend_yielding((1,), seen=seen)):
            self.assertTrue(auth_utils._vault_has_secrets())
        self.assertEqual(len(seen), 1, "open_readonly was not called exactly once")

    def test_secrets_present_on_a_pathless_backend(self):
        """THE REGRESSION. On PG the db_path names no local file. The old
        os.path.exists() gate returned False here -- the answer that makes a
        caller mint a new salt and orphan the vault."""
        with mock.patch.object(auth_utils, "_vault_db_path",
                               return_value="/nonexistent/not-a-real-file.db"), \
             mock.patch.object(auth_utils, "_backend",
                               return_value=_backend_yielding((1,))):
            self.assertIs(
                auth_utils._vault_has_secrets(), True,
                "vault WITH secrets reported empty because the path is not a "
                "local file -- this is the salt-orphaning bug",
            )

    def test_empty_vault_still_reports_false(self):
        """The fix must not invert the answer: no rows still means no vault."""
        with mock.patch.object(auth_utils, "_backend",
                               return_value=_backend_yielding(None)):
            self.assertIs(auth_utils._vault_has_secrets(), False)

    def test_backend_error_is_treated_as_empty(self):
        """Documented contract: any error -> False (let a fresh salt mint).
        A locked/missing table must not raise out of a probe."""

        class _Boom:
            @staticmethod
            def open_readonly(db_path):
                raise RuntimeError("backend unavailable")

        with mock.patch.object(auth_utils, "_backend", return_value=_Boom()):
            self.assertIs(auth_utils._vault_has_secrets(), False)

    def test_bootstrap_shim_exists_when_seam_is_unimportable(self):
        """auth_utils runs during installer bootstrap, before the seam is
        importable. It must still work rather than crash the install."""
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def _no_backends(name, *a, **k):
            if name == "memory.backends":
                raise ImportError("seam not on sys.path yet (bootstrap)")
            return real_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=_no_backends):
            b = auth_utils._backend()
        self.assertTrue(hasattr(b, "open_readonly"),
                        "bootstrap fallback must still offer open_readonly")

    def test_no_raw_sqlite_connect_left_in_the_probe(self):
        """Pin the fix at the source: the probe body must not reach for
        sqlite3 directly again.

        CODE only, not prose about code: the function carries a comment naming
        `sqlite3.connect` to explain the bug it fixed, and a naive substring
        check flags that comment -- which would make the guard fail for
        documenting itself. (The raw-connection drift guard skips comment lines
        for exactly this reason; same trap, same fix.)
        """
        import inspect

        body = "\n".join(
            ln for ln in inspect.getsource(auth_utils._vault_has_secrets).splitlines()
            if not ln.strip().startswith("#")
        )
        # Drop the docstring too -- it also legitimately discusses the old path.
        self.assertNotIn("sqlite3.connect", body)
        self.assertNotIn("os.path.exists", body)
        self.assertIn("open_readonly", body)


class TestSetupSecretIsBackendBlind(unittest.TestCase):
    """setup_secret.py reaches the SAME synchronized_secrets table as
    auth_utils, so it carried the identical file-shaped assumptions: an
    os.path.exists() gate that reports "vault empty"/"not found" on PostgreSQL,
    and literal `?` placeholders that PostgreSQL does not accept.

    The delete path is the one that matters most: it is a WRITE, and the
    os.path.exists() gate would _fail("vault database not found") for a store
    that demonstrably holds the row.
    """

    def _mod(self):
        import setup_secret
        return setup_secret

    def test_no_raw_connects_remain(self):
        import inspect
        src = "\n".join(
            ln for ln in inspect.getsource(self._mod()).splitlines()
            if not ln.strip().startswith("#")
        )
        self.assertNotIn("sqlite3.connect", src)

    def test_reads_go_through_open_readonly(self):
        seen: list = []
        m = self._mod()
        with mock.patch.object(m, "_backend",
                               return_value=_backend_yielding((3, "2026-01-01"),
                                                              seen=seen)):
            self.assertEqual(m._existing_info("SVC"), (3, "2026-01-01"))
        self.assertEqual(len(seen), 1)

    def test_existing_info_absent_returns_none(self):
        m = self._mod()
        with mock.patch.object(m, "_backend",
                               return_value=_backend_yielding(None)):
            self.assertIsNone(m._existing_info("NOPE"))

    def test_placeholders_come_from_the_dialect(self):
        """A literal `?` is a portability bug (§10a) — PG uses %s."""
        import inspect
        for fn in (self._mod()._existing_info, self._mod()._delete_service):
            src = inspect.getsource(fn)
            self.assertIn("d.param()", src,
                          f"{fn.__name__} must ask the dialect for placeholders")

    def test_delete_uses_the_pooled_write_connection(self):
        """Writes go through backend.connection() (pooled, commit/rollback
        discipline), never a private handle."""
        import inspect
        # CODE only: the function's comment explains the os.path.exists gate it
        # REMOVED, and a naive substring check would flag that explanation.
        src = "\n".join(
            ln for ln in inspect.getsource(self._mod()._delete_service).splitlines()
            if not ln.strip().startswith("#")
        )
        self.assertIn("connection()", src)
        self.assertNotIn("os.path.exists", src)


if __name__ == "__main__":
    unittest.main()
