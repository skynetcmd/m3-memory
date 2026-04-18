import os
import sys
import platform
import re

def is_valid_ip(ip):
    """Validate IP address format."""
    if not ip:
        return True
    return re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip)

def is_valid_url(url):
    """Validate URL format."""
    if not url:
        return True
    return re.match(r"^https?:\/\/.+", url)

def is_postgres_url(url):
    """Validate PostgreSQL connection string format."""
    if not url:
        return True
    return re.match(r"^postgresql:\/\/.+", url)

def is_absolute_path(path):
    """Validate if a path is absolute."""
    if not path:
        return True
    return os.path.isabs(path)

ENV_VARS = [
    {"name": "M3_MEMORY_ROOT", "required": True, "validator": is_absolute_path, "format": "Absolute path"},
    {"name": "SYNC_TARGET_IP", "required": False, "validator": is_valid_ip, "format": "IP address (e.g., 192.168.1.100)"},
    {"name": "CHROMA_BASE_URL", "required": False, "validator": is_valid_url, "format": "URL (e.g., http://localhost:8000)"},
    {"name": "PG_URL", "required": True, "validator": is_postgres_url, "format": "PostgreSQL connection string (e.g., postgresql://USERNAME:REPLACE_WITH_YOUR_PASSWORD@host/db)"},
    {"name": "AGENT_OS_MASTER_KEY", "required": True, "validator": None, "format": None},
    {"name": "LM_API_TOKEN", "required": True, "validator": None, "format": None},
    {"name": "PERPLEXITY_API_KEY", "required": False, "validator": None, "format": None},
    {"name": "XAI_API_KEY", "required": False, "validator": None, "format": None},
    {"name": "ANTHROPIC_API_KEY", "required": False, "validator": None, "format": None},
    {"name": "GEMINI_API_KEY", "required": False, "validator": None, "format": None},
    {"name": "OPENCLAW_GATEWAY_TOKEN", "required": False, "validator": None, "format": None},
]

def get_platform_instructions(var_name):
    """Get instructions for setting environment variables permanently on all platforms."""
    return {
        "Windows (Command Prompt)": f'setx {var_name} "YOUR_VALUE"',
        "Windows (PowerShell)": f'[Environment]::SetEnvironmentVariable("{var_name}", "YOUR_VALUE", "User")',
        "macOS (Zsh)": f'echo "export {var_name}=\\"YOUR_VALUE\\"" >> ~/.zshrc',
        "Linux (Bash)": f'echo "export {var_name}=\\"YOUR_VALUE\\"" >> ~/.bashrc'
    }

import argparse

def list_secrets():
    """List all tracked environment variables and their current values."""
    print("--- Current Environment Variable Values ---")
    max_len = max(len(var["name"]) for var in ENV_VARS)
    for var in ENV_VARS:
        var_name = var["name"]
        value = os.getenv(var_name, "(Not Set)")
        print(f"{var_name:<{max_len}} : {value}")

def main():
    parser = argparse.ArgumentParser(description="Validate or list environment variables.")
    parser.add_argument("-l", "--list", help="List values of environment variables (e.g., -l secrets)", metavar="MODE")
    
    args, unknown = parser.parse_known_args()

    if args.list == "secrets":
        list_secrets()
        sys.exit(0)
    elif args.list:
        print(f"Unknown list mode: {args.list}. Did you mean '-l secrets'?")
        sys.exit(1)

    errors_found = 0
    print("--- Validating Environment Variables ---")

    for var in ENV_VARS:
        var_name = var["name"]
        value = os.getenv(var_name)

        if not value:
            if var["required"]:
                print(f"\n❌ ERROR: Required environment variable '{var_name}' is not set.")
                errors_found += 1
            else:
                print(f"\n⚠️  Warning: Optional environment variable '{var_name}' is not set.")
            
            print(f"   To set it permanently, use the command for your OS:")
            instructions = get_platform_instructions(var_name)
            for platform_name, command in instructions.items():
                print(f"   - {platform_name}: {command}")
        else:
            if var["validator"] and not var["validator"](value):
                print(f"\n❌ ERROR: Environment variable '{var_name}' is not in the correct format.")
                print(f"   Expected format: {var['format']}")
                print(f"   Current value: {value}")
                errors_found += 1
            else:
                print(f"✅ {var_name} is set.")

    if errors_found > 0:
        print(f"\nFound {errors_found} error(s). Please fix them and re-run the validation.")
        print("Note: If you just set an environment variable permanently, you may need to restart your terminal for it to take effect.")
        sys.exit(1)
    else:
        print("\nAll required environment variables are set and have the correct format.")
        print("Note: If some variables appear missing but you've set them, try restarting your terminal.")
        sys.exit(0)

if __name__ == "__main__":
    main()
