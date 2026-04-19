from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from bs4 import BeautifulSoup
import ujson
import subprocess
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
import os
import sys

# Import auth_utils from the parent bin directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bin")))
try:
    from auth_utils import get_api_key
except ImportError:
    def get_api_key(service): return None

app = FastAPI(title="Homelab Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

POSTGRES_SERVER = os.environ.get("POSTGRES_SERVER", "localhost")
HOMEPAGE_URL = f"http://{POSTGRES_SERVER}:3000/"

def get_lm_headers():
    key = get_api_key("LM_STUDIO_API_KEY") or get_api_key("LM_API_TOKEN") or ""
    return {"Authorization": f"Bearer {key}"} if key else {}

import base64
import re
from pathlib import Path

ICON_CACHE_DIR = Path(__file__).parent / "cache" / "icons"
ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
ICON_CACHE_DIR = ICON_CACHE_DIR.resolve()

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.svg$")

def get_safe_filename(name):
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=") + ".svg"

@app.get("/api/icon")
async def get_icon(name: str):
    safe_name = get_safe_filename(name)
    if not _SAFE_NAME_RE.match(safe_name):
        raise HTTPException(status_code=400, detail="Invalid icon name")
    cache_path = ICON_CACHE_DIR / safe_name

    if cache_path.exists():
        return FileResponse(str(cache_path), media_type="image/svg+xml")

    async with httpx.AsyncClient() as client:
        try:
            content = None
            media_type = "image/svg+xml"
            if name.startswith("/"):
                url = f"{HOMEPAGE_URL.rstrip('/')}{name}"
                res = await client.get(url, timeout=5.0)
                if res.status_code == 200:
                    content = res.content
                    media_type = res.headers.get("content-type", "image/svg+xml")

            elif name.startswith("si-"):
                # SimpleIcons
                icon_name = name[3:]
                res = await client.get(f"https://cdn.jsdelivr.net/npm/simple-icons@v13/icons/{icon_name}.svg", timeout=5.0)
                if res.status_code == 200:
                    content = res.content

            elif name.startswith("mdi-"):
                # Material Design Icons
                icon_name = name[4:]
                res = await client.get(f"https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg/{icon_name}.svg", timeout=5.0)
                if res.status_code == 200:
                    content = res.content

            if content:
                with open(cache_path, "wb") as f:
                    f.write(content)
                return Response(content=content, media_type=media_type)

            raise HTTPException(status_code=404, detail="Icon not found")
        except Exception:
            raise HTTPException(status_code=404, detail="Icon not found")

@app.get("/api/homepage")
async def get_homepage_data():
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(HOMEPAGE_URL, timeout=10.0)
            response.raise_for_status()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        
        soup = BeautifulSoup(response.text, "html.parser")
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if not next_data_script:
            raise HTTPException(status_code=500, detail="NEXT_DATA not found")
        
        try:
            data = ujson.loads(next_data_script.string)
            props = data.get("props", {}).get("pageProps", {})
            return {"status": "success", "data": props}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to parse JSON: {e}")

import os
import sys

# Import M3Context from the parent bin directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bin")))
try:
    from m3_sdk import M3Context
except ImportError:
    M3Context = None

_SWIFT_THERMAL = (
    "import Foundation\n"
    "let t = ProcessInfo.processInfo.thermalState\n"
    "switch t {\n"
    "case .nominal: print(\"Nominal\")\n"
    "case .fair: print(\"Fair\")\n"
    "case .serious: print(\"Serious\")\n"
    "case .critical: print(\"Critical\")\n"
    "@unknown default: print(\"Unknown\")\n"
    "}\n"
)

# ... API endpoints ...

@app.get("/api/os/health")
async def get_os_health():
    """Reports health telemetry for M3 Memory."""
    if not M3Context:
        return {"status": "error", "error": "M3 SDK not found. Ensure backend is running within project root."}
    
    ctx = M3Context()
    health = {
        "status": "healthy",
        "local_memory": "offline",
        "data_warehouse": "offline",
        "db_path": ctx.db_path
    }

    # Check Local SQLite
    try:
        with ctx.get_sqlite_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM memory_items")
            count = cur.fetchone()[0]
            health["local_memory"] = "online"
            health["local_memory_items"] = count
    except Exception as e:
        health["status"] = "degraded"
        health["local_memory_error"] = str(e)

    # Check PG Data Warehouse
    try:
        with ctx.pg_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1")
        health["data_warehouse"] = "online"
    except Exception as e:
        health["status"] = "degraded"
        health["data_warehouse_error"] = str(e)

    return health

@app.get("/api/thermal")
async def get_thermal_load():
    try:
        status = subprocess.check_output(
            ["swift", "-"],
            input=_SWIFT_THERMAL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        return {"status": "success", "thermal_load": status}
    except Exception:
        return {"status": "error", "thermal_load": "Unknown"}

@app.get("/api/llm")
async def get_llm_state():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:1234/v1/models", headers=get_lm_headers(), timeout=5.0)
            models = resp.json().get("data", [])
            # Filter out embedding models
            models = [m for m in models if "embed" not in m["id"].lower() and "nomic" not in m["id"].lower()]
            return {"status": "success", "models": models}
    except Exception:
         return {"status": "error", "models": []}

@app.post("/api/analyze")
async def analyze_infrastructure():
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Get homepage data
        try:
            hp_resp = await client.get(HOMEPAGE_URL, timeout=10.0)
            hp_resp.raise_for_status()
            soup = BeautifulSoup(hp_resp.text, "html.parser")
            next_data_script = soup.find("script", id="__NEXT_DATA__")
            if not next_data_script:
                raise Exception("NEXT_DATA not found")
            data = ujson.loads(next_data_script.string)
            services = data.get("props", {}).get("pageProps", {}).get("fallback", {}).get("/api/services", [])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to get infrastructure data: {e}")

        # 2. Get available models
        try:
            model_resp = await client.get("http://localhost:1234/v1/models", headers=get_lm_headers(), timeout=5.0)
            models = model_resp.json().get("data", [])
            # Filter out embedding models
            models = [m for m in models if "embed" not in m["id"].lower() and "nomic" not in m["id"].lower()]
            if not models:
                raise Exception("No text models loaded in LM Studio")
            # Just pick the first available model for now, ideally the largest loaded model
            model_id = models[0]["id"]
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"LM Studio error: {e}")

        # 3. Analyze with LLM
        prompt = f"Analyze the following homelab infrastructure state for anomalies, offline services, or structural issues. Keep it concise (1-2 paragraphs). State:\n{ujson.dumps(services)[:2000]}..."
        try:
            chat_payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": "You are an expert homelab DevOps AI. Your job is to monitor infrastructure state and report anomalies."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.2,
                "max_tokens": 500
            }
            chat_resp = await client.post("http://localhost:1234/v1/chat/completions", headers=get_lm_headers(), json=chat_payload)
            chat_resp.raise_for_status()
            analysis = chat_resp.json()["choices"][0]["message"]["content"]
            return {"status": "success", "model": model_id, "analysis": analysis}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"AI Analysis failed: {e}")

frontend_dist_path = (Path(__file__).parent / ".." / "frontend" / "dist").resolve()

if frontend_dist_path.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist_path / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        index_path = frontend_dist_path / "index.html"
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")

        if not full_path:
            return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

        if ".." in full_path.split("/") or full_path.startswith("/") or "\\" in full_path:
            return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        candidate = (frontend_dist_path / full_path).resolve()
        try:
            candidate.relative_to(frontend_dist_path)
        except ValueError:
            return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index_path), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

if __name__ == "__main__":
    import uvicorn
    # Example homelab dashboard is intentionally exposed on the LAN so other
    # devices on the trusted home network can view host status. Deployers who
    # want stricter binding can override via their process manager.
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)  # nosec B104