import os
import json
import tempfile
import pytest
from unittest import mock

from bin.audit_trail import write_audit_entry, verify_audit_trail, get_audit_trail_path

def test_cryptographic_audit_trail():
    """Verify that writing to the audit trail forms a correct hash chain and detecting tampering works."""
    # Use a temporary directory for the audit trail
    with tempfile.TemporaryDirectory() as tmpdir:
        # Mock get_m3_root to return our temp directory so we don't mess with real user logs
        with mock.patch("bin.audit_trail.get_m3_root", return_value=tmpdir):
            audit_path = get_audit_trail_path()
            assert not os.path.exists(audit_path)
            
            # 1. Verification of empty log (should return True)
            assert verify_audit_trail() is True
            
            # 2. Log first entry (Genesis)
            h1 = write_audit_entry(
                action="memory_delete",
                target_id="uuid-1",
                metadata={"hard": True}
            )
            assert os.path.exists(audit_path)
            
            # Read and verify first entry
            with open(audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 1
            e1 = json.loads(lines[0])
            assert e1["action"] == "memory_delete"
            assert e1["target_id"] == "uuid-1"
            assert e1["prev_hash"] == "0" * 64
            assert e1["hash"] == h1
            
            # Verify the log has valid signature
            assert verify_audit_trail() is True
            
            # 3. Log second entry (should link to h1)
            h2 = write_audit_entry(
                action="gdpr_forget",
                target_id="user-123",
                metadata={"items_affected": 5}
            )
            
            with open(audit_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            assert len(lines) == 2
            e2 = json.loads(lines[1])
            assert e2["action"] == "gdpr_forget"
            assert e2["target_id"] == "user-123"
            assert e2["prev_hash"] == h1
            assert e2["hash"] == h2
            
            # Verify chain consistency
            assert verify_audit_trail() is True
            
            # 4. Tamper with the log to ensure verify_audit_trail catches it
            # Change the metadata of the first entry
            e1["metadata"]["hard"] = False
            
            with open(audit_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(e1) + "\n")
                f.write(json.dumps(e2) + "\n")
                
            # Verification should now FAIL due to altered content violating the hash
            assert verify_audit_trail() is False
            
            # Reset first entry back, but alter the prev_hash of second entry
            e1["metadata"]["hard"] = True
            e2["prev_hash"] = "altered_hash_value"
            
            with open(audit_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(e1) + "\n")
                f.write(json.dumps(e2) + "\n")
                
            # Verification should still FAIL due to broken chain pointer
            assert verify_audit_trail() is False
