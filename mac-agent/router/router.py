import os
import sys
import httpx
from datetime import datetime
from fastapi import APIRouter, HTTPException

# Add project root to path for bin imports
# Dynamically resolve project root relative to this file's location
# mac-agent/router/router.py -> parent(router) -> parent(mac-agent) -> parent(m3-memory)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(BASE_DIR)
from bin.agent_protocol import AgentProtocol

router = APIRouter()

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions")
CLIENT = httpx.AsyncClient(timeout=60)
PROTOCOL = AgentProtocol()


async def _post(url: str, json: dict, headers: dict | None = None):
    resp = await CLIENT.post(url, json=json, headers=headers)
    if resp.status_code >= 400:
        print(f"ROUTER ERROR [{resp.status_code}] {url}: {resp.text}")
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@router.post("/v1/chat/completions")
async def route(request: dict):
    model = request.get("model", "")
    messages = request.get("messages", [])

    # Local LM Studio (MLX models)
    if model.startswith("local-") or model == "":
        return await _post(LM_STUDIO_URL, request)

    # Claude (Anthropic)
    if model.startswith("claude-"):
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": os.getenv("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
        anthropic_payload = PROTOCOL.openai_to_anthropic(messages, model)
        resp = await _post(url, anthropic_payload, headers)
        return PROTOCOL.translate_response(resp, "anthropic")

    # Gemini
    if model.startswith("gemini-"):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={os.getenv('GEMINI_API_KEY')}"
        )
        gemini_payload = PROTOCOL.openai_to_gemini(messages, model)
        resp = await _post(url, gemini_payload)
        return PROTOCOL.translate_response(resp, "gemini")

    # Grok
    if model.startswith("grok-"):
        url = "https://api.x.ai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {os.getenv('GROK_API_KEY')}"}
        return await _post(url, request, headers)

    # Perplexity
    if model.startswith("pplx-"):
        url = "https://api.perplexity.ai/chat/completions"
        headers = {"Authorization": f"Bearer {os.getenv('PPLX_API_KEY')}"}
        return await _post(url, request, headers)

    # Home GPU / remote router (optional)
    if model.startswith("home-"):
        home_url = os.getenv("HOME_ROUTER_URL")
        if home_url:
            return await _post(f"{home_url}/v1/chat/completions", request)

    # Fallback to local LM Studio
    return await _post(LM_STUDIO_URL, request)

