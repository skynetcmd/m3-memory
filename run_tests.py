import os
import sys
import subprocess
import platform
import glob
from pathlib import Path

def main():
    if platform.system() == "Windows":
        sys.stdout.reconfigure(encoding='utf-8')
    
    m3_memory_root = Path(__file__).parent.resolve()
    
    if platform.system() == "Windows":
        venv_python = m3_memory_root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = m3_memory_root / ".venv" / "bin" / "python3"

    if not venv_python.exists():
        print(f"Error: Python virtual environment not found at {venv_python}")
        print("Please run the setup instructions.")
        sys.exit(1)

    logs_dir = m3_memory_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    test_files = glob.glob(str(m3_memory_root / "bin" / "test_*.py"))
    
    # Metadata for tests: {filename: (expected_time_str, timeout_seconds)}
    TEST_METADATA = {
        "test_knowledge.py": ("5-10s", 30),
        "test_mcp_proxy.py": ("15s (runs only when needed)", 15),
        "test_unified_router.py": ("60s+", 120),
        "test_memory_bridge.py": ("5-10s", 30),
        "test_debug_agent.py": ("2-5s", 30),
        "test_keychain.py": ("1-2s", 30),
        "test_mission_control.py": ("2-5s", 30),
        "test_embedding_logic.py": ("5-10s", 30),
    }

    for f in test_files:
        f_path = Path(f)
        test_name = f_path.name

        if test_name == "test_unified_router.py":
            print(f"--- Skipping {test_name} (run manually, needs 60s+) ---")
            continue

        metadata = TEST_METADATA.get(test_name, ("unknown", 30))
        expected_time, timeout_seconds = metadata

        log_file = logs_dir / f"{test_name}.log"

        print(f"--- Running {test_name} (Expected: {expected_time}, Timeout: {timeout_seconds}s) ---")
        
        try:
            result = subprocess.run(
                [str(venv_python), str(f_path)],
                timeout=timeout_seconds,
                capture_output=True,
                encoding='utf-8',
                errors='replace'
            )
            
            with open(log_file, "w", encoding='utf-8') as log:
                if result.stdout:
                    log.write(result.stdout)
                if result.stderr:
                    log.write(result.stderr)

            if result.returncode == 0:
                print(f"✅ {test_name}: PASSED")
            else:
                print(f"❌ {test_name}: FAILED (exit {result.returncode} — see {log_file})")

        except subprocess.TimeoutExpired as e:
            print(f"❌ {test_name}: TIMEOUT (>{timeout_seconds}s)")
            with open(log_file, "w", encoding='utf-8') as log:
                if e.stdout:
                    log.write(e.stdout)
                if e.stderr:
                    log.write(e.stderr)

if __name__ == "__main__":
    main()
