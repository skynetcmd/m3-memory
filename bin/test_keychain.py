import sys
import os

# Ensure bin is in path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from auth_utils import get_api_key

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

def test_key(service):
    print(f"Testing {service}...")
    try:
        res = get_api_key(service)
        if res:
            print(f"✅ Success! Length: {len(res)}")
        else:
            print(f"❌ Failed: Secret not found in any store.")
    except Exception as e:
        print(f"❌ Failed: {e}")

if __name__ == "__main__":
    test_key("PERPLEXITY_API_KEY")
    test_key("LM_STUDIO_API_KEY")
    test_key("AGENT_OS_MASTER_KEY")
