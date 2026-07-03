import os
import sys
from unittest.mock import patch

# Add bin to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import auth_utils


def test_get_master_key_env():
    """Test retrieving master key from environment variable."""
    with patch.dict(os.environ, {"AGENT_OS_MASTER_KEY": "test-env-key"}):
        assert auth_utils.get_master_key() == "test-env-key"

def test_fernet_encryption_decryption():
    """Test that _get_fernet produces a valid encryption object."""
    master_key = "test-secret-key-123"
    f = auth_utils._get_fernet(master_key)

    original_text = "sensitive data"
    encrypted = f.encrypt(original_text.encode())
    decrypted = f.decrypt(encrypted).decode()

    assert decrypted == original_text
    assert encrypted != original_text.encode()

def test_device_salt_persistence(tmp_path):
    """Test that device salt is persistent across calls.

    _get_device_salt() persists the salt under get_m3_root()/.agent_os_salt.
    Pin get_m3_root() to a writable tmp dir so the test exercises the
    persistence logic instead of depending on the runner's filesystem
    (CI runners may resolve m3_root to a non-writable path, which makes
    each call fall back to a fresh os.urandom and the salts diverge).
    """
    with patch("m3_sdk.get_m3_root", return_value=str(tmp_path)):
        salt1 = auth_utils._get_device_salt()
        salt2 = auth_utils._get_device_salt()
    assert salt1 == salt2
    assert len(salt1) == 16


def _empty_config(tmp_path):
    """A config/root dir with no .agent_os_salt file present."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    return cfg


def test_salt_mints_on_fresh_install(tmp_path):
    """No salt file + empty/absent vault => mint a new salt (fresh install)."""
    cfg = _empty_config(tmp_path)
    with patch("m3_core.paths.get_m3_config_root", return_value=str(cfg)), \
         patch("m3_core.paths.get_m3_root", return_value=str(tmp_path)), \
         patch.object(auth_utils, "_vault_has_secrets", return_value=False), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("M3_AGENT_OS_SALT_HEX", None)
        salt = auth_utils._get_device_salt()
    assert len(salt) == 16


def test_salt_missing_with_secrets_raises(tmp_path):
    """No salt file BUT the vault already has secrets => fail loud, do NOT
    silently regenerate (regressions the 2026-07-03 vault-orphaning footgun)."""
    cfg = _empty_config(tmp_path)
    with patch("m3_core.paths.get_m3_config_root", return_value=str(cfg)), \
         patch("m3_core.paths.get_m3_root", return_value=str(tmp_path)), \
         patch.object(auth_utils, "_vault_has_secrets", return_value=True):
        os.environ.pop("M3_AGENT_OS_SALT_HEX", None)
        try:
            auth_utils._get_device_salt()
            assert False, "expected SaltMissingError, none raised"
        except auth_utils.SaltMissingError:
            pass


def test_salt_env_override_wins(tmp_path):
    """M3_AGENT_OS_SALT_HEX (32 hex chars) overrides file/vault logic entirely —
    the recovery escape hatch for a lost salt."""
    cfg = _empty_config(tmp_path)
    hexval = "00112233445566778899aabbccddeeff"
    with patch("m3_core.paths.get_m3_config_root", return_value=str(cfg)), \
         patch("m3_core.paths.get_m3_root", return_value=str(tmp_path)), \
         patch.object(auth_utils, "_vault_has_secrets", return_value=True), \
         patch.dict(os.environ, {"M3_AGENT_OS_SALT_HEX": hexval}):
        salt = auth_utils._get_device_salt()
    assert salt == bytes.fromhex(hexval)
    assert len(salt) == 16
