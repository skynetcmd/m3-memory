#!/usr/bin/env python3
import logging
import os
import secrets
import string
import sys
from datetime import datetime, timezone

# Add the bin directory to path so we can import SDK modules if called from elsewhere
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from auth_utils import set_api_key
from m3_sdk import M3Context

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("SecretRotator")

# Target keys that should be rotated. (Do NOT rotate external keys like OpenAI/Anthropic/Gemini)
# We only rotate internal OS keys that we control, like PostgreSQL or internal service tokens.
ROTATION_TARGETS = [
    "AGENT_OS_INTERNAL_SERVICE_TOKEN"
]

def generate_secure_token(length: int = 64) -> str:
    """Generates a cryptographically secure random string."""
    alphabet = string.ascii_letters + string.digits + string.punctuation.replace('"', '').replace("'", "")
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def rotate_secrets(dry_run: bool = False):
    logger.info(f"Starting automated secret rotation {'(DRY RUN)' if dry_run else ''}...")
    ctx = M3Context()

    for key_name in ROTATION_TARGETS:
        try:
            logger.info(f"Rotating secret for: {key_name}")

            # 1. Fetch current secret for backup
            old_secret = ctx.get_secret(key_name)
            if old_secret and not dry_run:
                # Store backup in a dedicated table (ensure schema exists)
                with ctx.get_sqlite_conn() as conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS secret_backups (service_name TEXT, encrypted_value TEXT, rotated_at TEXT)")
                    # Get the encrypted blob directly from the source table
                    row = conn.execute("SELECT encrypted_value FROM synchronized_secrets WHERE service_name = ?", (key_name,)).fetchone()
                    if row:
                        conn.execute("INSERT INTO secret_backups VALUES (?, ?, ?)", (key_name, row[0], datetime.now(timezone.utc).isoformat()))
                        conn.commit()
                        logger.info(f"Backup created for {key_name}.")

            # 2. Generate new cryptographically secure key
            new_secret = generate_secure_token()

            if dry_run:
                logger.info(f"[DRY RUN] Would rotate {key_name} to new {len(new_secret)} char token.")
                continue

            # 3. Store securely in the local SQLite vault
            set_api_key(key_name, new_secret)

            # 4. Log the rotation event
            ctx.log_event(
                category="security",
                detail_a="Automated Secret Rotation",
                detail_b=f"Successfully rotated {key_name} and encrypted to vault."
            )
            logger.info(f"Successfully rotated and vaulted {key_name}.")

        except Exception as e:
            logger.error(f"Failed to rotate {key_name}: {e}")
            if not dry_run:
                ctx.log_event(
                    category="security_error",
                    detail_a="Automated Secret Rotation Failed",
                    detail_b=f"Failed to rotate {key_name}: {e}"
                )

    logger.info(f"Secret rotation complete {'(DRY RUN)' if dry_run else ''}.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rotate_secrets(dry_run=args.dry_run)
