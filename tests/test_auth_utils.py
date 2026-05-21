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
