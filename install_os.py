import getpass
import os
import subprocess
import sys
import venv


def _os_name() -> str:
    """WMI-safe OS name. Replaces platform.system(), which routes through a WMI
    query that can hang on Py3.14/Windows. This is a standalone pre-install
    script (stdlib only, runs before m3 is importable), so the helper is inlined
    rather than shared. os.name/sys.platform are constants — no WMI."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"

# On Windows the default console code page is cp1252, which can't encode
# characters outside that 8-bit range (emoji, arrows, box-drawing). A stray
# non-ASCII char in a print() (e.g. the banner's rocket emoji) crashes the
# whole installer with UnicodeEncodeError. Force stdio onto UTF-8 so output is
# safe regardless of console code page. Guard with hasattr: .reconfigure()
# exists on the real TextIOWrapper but not on substitutes (StringIO under tests).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_m3_root() -> str:
    """Returns the M3 root directory for user state (~/.m3-memory)."""
    root = os.getenv("M3_MEMORY_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    return os.path.join(os.path.expanduser("~"), ".m3-memory")

M3_ROOT = get_m3_root()
VENV_DIR = os.path.join(BASE_DIR, ".venv")
DB_DIR = os.path.join(M3_ROOT, "memory")
LOGS_DIR = os.path.join(M3_ROOT, "logs")
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
    system = _os_name()
    print(f"\n[*] Setting up Node.js Manager for {system}...")

    if system == "Windows":
        # 1. Check if nvm is already installed
        try:
            res = subprocess.run(["nvm", "version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  -> Found existing nvm: {res.stdout.strip()}")
                return
        except (FileNotFoundError, PermissionError):
            pass

        # 2. Check for existing standalone Node.js (vulnerable to conflicts)
        try:
            res = subprocess.run(["node", "--version"], capture_output=True, text=True)
            if res.returncode == 0:
                print(f"  ⚠️ Warning: Found standalone Node.js ({res.stdout.strip()}).")
                print("     nvm-windows works best if standalone Node.js is uninstalled first.")
        except (FileNotFoundError, PermissionError):
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
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError):
            print("  -> winget not found. Please install nvm-windows manually: https://github.com/coreybutler/nvm-windows/releases")

    else:
        # Unix/macOS: Prefer fnm or nvm
        print("  -> Setting up Node.js manager for Unix...")
        try:
            subprocess.run(["fnm", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("  -> fnm already installed.")
        except (subprocess.CalledProcessError, FileNotFoundError, PermissionError):
            # PermissionError: fnm binary exists on the system but is not
            # executable by this user (e.g. installed system-wide for another
            # user). Treat as not installed and install into the user's home.
            run_cmd(["bash", "-c", "curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell"])
            print("  -> fnm installed. Please follow the terminal instructions to add it to your shell.")

def setup_oxidation():
    """Prompts the user to install Project Oxidation (Rust compute core).

    Delegates to m3_memory.rust_core_install, which:
      - Auto-detects the right backend (Metal on macOS, CUDA/Vulkan/CPU on
        Linux+Windows) and picks the matching prebuilt PyPI wheel
        (m3-core-rs-<os>-<backend>).
      - Falls back to a source build with the correct Cargo features
        (--features embedded-metal / embedded-cuda / embedded-vulkan) if no
        prebuilt wheel matches this platform/Python.
      - Installs into the *current* Python (sys.executable — the pipx venv
        where mcp-memory itself runs), NOT a sibling repo/.venv that
        mcp-memory can't see.

    The non-interactive code path (env M3_INSTALL_OXIDATION=1/0) lets
    install.sh / install.ps1 drive this unattended.
    """
    print("\n" + "="*50)
    print("🦀 PROJECT OXIDATION: HIGH-PERFORMANCE RUST CORE")
    print("="*50)
    print("Project Oxidation replaces critical Python hot-paths with")
    print("optimized Rust primitives. Per-operation speedups (Rust vs Python,")
    print("FFI-inclusive; see docs/OXIDATION_BENCHMARKS.md):")
    print("  - Packed MMR rerank: up to ~720x at a realistic candidate pool.")
    print("  - Packed batch-cosine: ~90-180x.  Chatlog redaction: 11-15x.")
    print("  - Trivially small C-backed ops stay on Python where it's faster.")
    print("\n[NOTE] Prebuilt wheels are tried first (no toolchain needed).")
    print("Source fallback requires Rust + a C/C++ compiler.")

    # Non-interactive override for install.sh / install.ps1.
    env_choice = os.environ.get("M3_INSTALL_OXIDATION", "").strip().lower()
    if env_choice in ("0", "false", "no", "n"):
        choice = "n"
    elif env_choice in ("1", "true", "yes", "y"):
        choice = "y"
    else:
        try:
            choice = input(
                "\nWould you like to install Project Oxidation now? [y/N]: "
            ).strip().lower()
        except EOFError:
            # Piped stdin exhausted (e.g. `printf '\n' | install-m3`).
            # Default to skip — caller can run `m3 embedder install-gpu` later.
            choice = "n"

    if choice in ("y", "yes"):
        if _os_name() == "Windows":
            print("\n[*] Checking for Windows C++ Build Tools...")
            script = os.path.join(BASE_DIR, "install_oxidation_buildtools.ps1")
            if os.path.exists(script):
                print("  -> If the source-fallback build fails, run this script in an")
                print("     ADMINISTRATOR PowerShell first:")
                print(f"       powershell -ExecutionPolicy Bypass -File {script}")

        print("\n[*] Installing m3-core-rs (prebuilt wheel preferred)...")
        try:
            # Local import: m3_memory is importable here because install_os.py
            # is launched under the same interpreter that runs mcp-memory (the
            # pipx venv). This avoids the historical bug where setup_oxidation
            # installed into repo/.venv/bin/pip, leaving the pipx-installed
            # mcp-memory unable to import m3_core_rs.
            from m3_memory.rust_core_install import install_rust_core
        except ImportError as e:
            print(f"  ⚠️  m3_memory.rust_core_install not importable ({e}).")
            print("     Skipping Oxidation. Install later via: m3 embedder install-gpu")
            return

        # allow_source_fallback=False: the curl-install.sh flow should not
        # silently launch a multi-minute Rust+cmake build that surprises the
        # user. If both prebuilt paths miss, install_rust_core prints a
        # multi-line recommendation (prereqs + manual command). The user can
        # then opt in deliberately via `m3 embedder install-gpu`.
        rc = install_rust_core(allow_source_fallback=False)
        if rc == 0:
            print("✅ Project Oxidation installed successfully!")
        else:
            # install_rust_core already printed the recommendation. Keep
            # this terse — repeating it would just push the helpful info
            # off the user's screen.
            print(f"\n⚠️  Project Oxidation prebuilt unavailable (exit {rc}).")
            print("    Embeddings still work via the tier-2 HTTP fallback.")
            print("    See above for source-build steps if you want native speed.")
    else:
        print(
            "Skipping Project Oxidation. Install it later via:\n"
            "  m3 embedder install-gpu"
        )

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

    # 4. Project Oxidation (Rust Core)
    # No pip_exe arg — setup_oxidation installs into the current interpreter
    # (the pipx venv where mcp-memory runs), not into repo/.venv.
    setup_oxidation()

    # 5. Initialize local SQLite Database Schema
    print("\n[5/6] Initializing local Agent Memory schema...")
    migrate_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
    if os.path.exists(migrate_script):
        run_cmd([python_exe, migrate_script])
    else:
        print(f"  -> Warning: Migration script {migrate_script} not found.")

    # 6. Secure Auth Setup
    print("\n[6/6] Initializing Security Layer...")
    setup_master_key(python_exe)

    # 7. Initial Data Warehouse Sync
    print("\n[7/7] Connecting to PostgreSQL data warehouse...")
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

    # 8. Generate MCP configs (.mcp.json, claude-settings.json, gemini-settings.json)
    print("\n[8/8] Generating MCP configuration files...")
    gen_config_script = os.path.join(BASE_DIR, "bin", "generate_configs.py")
    if os.path.exists(gen_config_script):
        run_cmd([python_exe, gen_config_script])
    else:
        print(f"  -> Warning: {gen_config_script} not found. Run it manually after install.")

    print("\n" + "="*50)
    print("🎉 INSTALLATION COMPLETE!")
    print("="*50)
    print(f"M3 Memory is now fully initialized for {_os_name()}.")
    print("\nMCP bridges are configured in .mcp.json — Claude Code will load them automatically.")
    print("\nNext Steps:")
    print("  1. (Recommended) Install a self-contained local embedder for Hybrid Search:")
    print(f"     {python_exe} -m m3_memory.cli install-embedder")
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
