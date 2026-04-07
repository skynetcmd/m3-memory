import os
import requests

# Constants
ROUTER_URL = "http://127.0.0.1:1234/v1/chat/completions" # Default router/LM Studio port
API_TOKEN = os.getenv("LM_API_TOKEN") or "no-token-found"

def test_payload(model, description):
    print(f"\n--- Testing: {description} (Model: {model}) ---")
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful OS assistant."},
            {"role": "user", "content": "Hello! Confirm you received this OpenAI-style payload."}
        ]
    }
    
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        # Increased timeout to 60s for reasoning models
        response = requests.post(ROUTER_URL, headers=headers, json=payload, timeout=60)
        if response.status_code == 200:
            data = response.json()
            # Standard OpenAI response format
            content = data['choices'][0]['message']['content']
            print("✅ Success! Response received.")
            print(f"Response Preview: {content[:100]}...")
        else:
            print(f"❌ Failed with status code: {response.status_code}")
            print(f"Error: {response.text}")
    except Exception as e:
        print(f"❌ Connection Error: {str(e)}")
        print("Note: Ensure your router or LM Studio is running on port 1234.")

if __name__ == "__main__":
    # Test local routing (DeepSeek/LM Studio)
    test_payload("deepseek-r1-distill-llama-70b-mlx", "Local LM Studio / DeepSeek")
    
    # If keys are present, test cloud routing
    if os.getenv("ANTHROPIC_API_KEY"):
        test_payload("claude-3-5-sonnet", "Anthropic Claude Translation")
    
    if os.getenv("GEMINI_API_KEY"):
        test_payload("gemini-1.5-pro", "Google Gemini Translation")
