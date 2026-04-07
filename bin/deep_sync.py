import subprocess
import os
import logging
from datetime import datetime

# Structured Logging for macOS Unified Logging
logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("deep_sync")

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = BASE_DIR
BACKUP_MSG = f"Deep-Sync: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

def run_git(args):
    try:
        subprocess.check_call(["git", "-C", WORKSPACE] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        logger.error(f"Git Error: Command {' '.join(args)} failed with exit code {e.returncode}")
        raise e

def sync():
    logger.info("🚀 Starting Deep-Sync & Housekeeping for M3 Max...")
    
    # 0. Perform Housekeeping (Space optimization)
    try:
        cleanup_script = os.path.join(WORKSPACE, "bin", "cleanup_logs.sh")
        if os.path.exists(cleanup_script):
            subprocess.run([cleanup_script], check=True, stdout=subprocess.DEVNULL)
            logger.info("✅ Housekeeping pass complete.")
    except Exception as e:
        logger.warning(f"Housekeeping pass skipped: {e}")

    try:
        # 1. Add all changes (scripts, reports, etc.) respecting .gitignore
        logger.info("Adding workspace changes...")
        run_git(["add", "."])
        
        # 2. Commit (only if there are changes)
        try:
            run_git(["commit", "-m", BACKUP_MSG])
            logger.info("Changes committed successfully.")
        except subprocess.CalledProcessError:
            logger.info("No changes to sync today.")
            return

        # 3. Push to your private GitHub repo
        logger.info("📤 Pushing to private GitHub repository...")
        run_git(["push", "origin", "main"])
        logger.info("✅ Deep-Sync Complete.")
    except Exception as e:
        logger.error(f"Deep-Sync failed: {e}")

if __name__ == "__main__":
    sync()
