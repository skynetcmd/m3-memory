import os
import sys
import subprocess
import venv
import platform
import getpass

# Define paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(BASE_DIR, ".venv")
DB_DIR = os.path.join(BASE_DIR, "memory")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
REQ_FILE = os.path.join(BASE_DIR, "requirements.txt")

def run_cmd(cmd, env=None):
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"\n[ERROR] Command failed: {' '.join(cmd)}", file=sys.stderr)
        sys.exit(result.returncode)

def setup_master_key(python_exe):
    """Prompts the user to configure the AGENT_OS_MASTER_KEY using the python keyring."""
    print("\n" + "="*50)
    print("🔐 ZERO-KNOWLEDGE ENCRYPTED VAULT SETUP")
    print("="*50)
    print("To sync API keys securely across devices, you must provide the AGENT_OS_MASTER_KEY.")
    print("This key will be securely stored in your native OS keyring (macOS Keychain,")
    print("Windows Credential Manager, or Linux Secret Service) and NEVER synced.")
    
    while True:
        master_key = getpass.getpass("\nEnter the AGENT_OS_MASTER_KEY (or press Enter to skip for now): ").strip()
        if not master_key:
            print("Skipping master key setup. You will need to configure it later to use synced API keys.")
            break
            
        confirm_key = getpass.getpass("Confirm AGENT_OS_MASTER_KEY: ").strip()
        if master_key == confirm_key:
            # We use the venv python to set the keyring so we know the module is available
            script = """
import os
import keyring
master_key = os.environ.get('AGENT_OS_MASTER_KEY')
if not master_key:
    print('❌ Master key not found in environment.')
    exit(1)
try:
    keyring.set_password('system', 'AGENT_OS_MASTER_KEY', master_key)
    print('✅ Successfully saved AGENT_OS_MASTER_KEY to native OS keyring!')
except Exception as e:
    print(f'❌ Failed to save to keyring: {e}')
"""
            env = os.environ.copy()
            env["AGENT_OS_MASTER_KEY"] = master_key
            run_cmd([python_exe, "-c", script], env=env)
            break
        else:
            print("❌ Passwords do not match. Please try again.")

def install_node_manager():
    """
    Handles Node.js version management setup.
    Windows: Prefers nvm-windows (CoreyButler.NVMforWindows).
    Unix/macOS: Prefers nvm or fnm.
    """
    system = platform.system()
    print(f"\n[*] Setting up Node.js Manager for {system}...")

    if system == "Windows":
        # 1. Check if nvm is already installed
        try:
            res = subprocess.run(["nvm", "version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  -> Found existing nvm: {res.stdout.strip()}")
                return
        except FileNotFoundError:
            pass

        # 2. Check for existing standalone Node.js (vulnerable to conflicts)
        try:
            res = subprocess.run(["node", "--version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  ⚠️ Warning: Found standalone Node.js ({res.stdout.strip()}).")
                print("     nvm-windows works best if standalone Node.js is uninstalled first.")
        except FileNotFoundError:
            pass

        # 3. Try to install nvm-windows via winget
        print("  -> Attempting to install nvm-windows via winget...")
        try:
            subprocess.run(["winget", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            res = subprocess.run(["winget", "install", "-e", "--id", "CoreyButler.NVMforWindows", "--silent", "--accept-package-agreements", "--accept-source-agreements"])
            if res.returncode == 0:
                print("  ✅ nvm-windows installed successfully!")
                print("     RESTART YOUR TERMINAL to use it.")
                return
            else:
                print("  -> winget install failed. You may need to install it manually.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("  -> winget not found. Please install nvm-windows manually: https://github.com/coreybutler/nvm-windows/releases")
            
    else:
        # Unix/macOS: Prefer fnm or nvm
        print("  -> Setting up Node.js manager for Unix...")
        try:
            subprocess.run(["fnm", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("  -> fnm already installed.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            run_cmd(["bash", "-c", "curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell"])
            print("  -> fnm installed. Please follow the terminal instructions to add it to your shell.")

def main():
    print("\n" + "="*50)
    print("🚀 M3 MAX AGENTIC OS: UNIVERSAL INSTALLER")
    print("="*50)
    
    # 0. Setup Node.js Management
    install_node_manager()

    # 1. Ensure auxiliary directories exist
    print("\n[1/6] Creating auxiliary directories...")
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    print(f"  -> {DB_DIR}")
    print(f"  -> {LOGS_DIR}")

    # 2. Create Virtual Environment
    print("\n[2/6] Setting up isolated Python environment...")
    if not os.path.exists(VENV_DIR):
        venv.create(VENV_DIR, with_pip=True)
        print("  -> Created new virtual environment.")
    else:
        print("  -> Virtual environment already exists.")

    # Determine paths for pip and python inside venv based on platform
    if sys.platform == "win32":
        pip_exe = os.path.join(VENV_DIR, "Scripts", "pip.exe")
        python_exe = os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        pip_exe = os.path.join(VENV_DIR, "bin", "pip")
        python_exe = os.path.join(VENV_DIR, "bin", "python")

    # 3. Install dependencies
    print("\n[3/6] Installing cross-platform dependencies...")
    run_cmd([pip_exe, "install", "--upgrade", "pip"])
    if os.path.exists(REQ_FILE):
        run_cmd([pip_exe, "install", "-r", REQ_FILE])
    else:
        print(f"  -> Warning: {REQ_FILE} not found. Installing defaults...")
        run_cmd([pip_exe, "install", "fastmcp", "httpx", "numpy", "keyring", "cryptography", "psycopg2-binary"])

    # 4. Initialize local SQLite Database Schema
    print("\n[4/6] Initializing local Agent Memory schema...")
    migrate_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
    if os.path.exists(migrate_script):
        run_cmd([python_exe, migrate_script])
    else:
        print(f"  -> Warning: Migration script {migrate_script} not found.")

    # 5. Secure Auth Setup
    print("\n[5/6] Initializing Security Layer...")
    setup_master_key(python_exe)

    # 6. Initial Data Warehouse Sync
    print("\n[6/6] Connecting to PostgreSQL data warehouse...")
    pg_sync_script = os.path.join(BASE_DIR, "bin", "pg_sync.py")
    if os.path.exists(pg_sync_script):
        print("Executing initial bi-directional synchronization...")
        try:
            # Don't fail the whole install if the homelab is unreachable
            subprocess.run([python_exe, pg_sync_script], check=True)
            print("✅ Initial sync successful!")
        except subprocess.CalledProcessError:
            print("⚠️ Initial sync failed. You may not be on the target private network.")
            print("   The sync will run automatically later when the network is reachable.")
    else:
        print("  -> Warning: Sync script not found.")

    # 7. Generate MCP configs (.mcp.json, claude-settings.json, gemini-settings.json)
    print("\n[7/7] Generating MCP configuration files...")
    gen_config_script = os.path.join(BASE_DIR, "bin", "generate_configs.py")
    if os.path.exists(gen_config_script):
        run_cmd([python_exe, gen_config_script])
    else:
        print(f"  -> Warning: {gen_config_script} not found. Run it manually after install.")

    print("\n" + "="*50)
    print("🎉 INSTALLATION COMPLETE!")
    print("="*50)
    print(f"Your Agentic OS is now fully initialized for {platform.system()}.")
    print("\nMCP bridges are configured in .mcp.json — Claude Code will load them automatically.")
    print("\nNext Steps for scheduling automated background syncs:")

    if sys.platform == "win32":
        print("  Use Windows Task Scheduler to run:")
        print(f"    {python_exe} {pg_sync_script} hourly.")
    else:
        print("  Add the following to your crontab (crontab -e):")
        print(f"    0 * * * * {os.path.join(BASE_DIR, 'bin', 'pg_sync.sh')} >> {os.path.join(LOGS_DIR, 'cron.log')} 2>&1")

    print("="*50 + "\n")

if __name__ == "__main__":
    main()
