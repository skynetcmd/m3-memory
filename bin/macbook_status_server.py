#!/usr/bin/env python3
"""
MacBook network & LM Studio status server for Homepage dashboard.
Listens on port 9876. Returns JSON at /status with interface and LM Studio info.
"""

import http.server
import json
import os
import subprocess
import urllib.request
import urllib.error

PORT = 9876
LM_STUDIO_URL = "http://localhost:1234"
EMBEDDING_KEYWORDS = ("embed", "nomic", "jina", "bge", "minilm", "e5")

# Interface map: ordered by preference (10GbE first, WiFi fallback)
INTERFACES = [
    ("en8", "10GbE"),   # Thunderbolt Ethernet
    ("en0", "WiFi"),    # Wi-Fi
]


def get_iface_ip(iface: str):
    """Return IPv4 address for a given interface, or None if not up."""
    try:
        out = subprocess.run(
            ["ifconfig", iface],
            capture_output=True, text=True, timeout=2
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                return line.split()[1]
    except Exception:
        pass
    return None


def get_lm_token() -> str:
    """Resolve LM Studio API token from env or keychain."""
    token = os.environ.get("LM_API_TOKEN") or os.environ.get("LM_STUDIO_API_KEY")
    if not token:
        try:
            token = subprocess.run(
                ["security", "find-generic-password", "-s", "LM_STUDIO_API_KEY", "-w"],
                capture_output=True, text=True, timeout=3
            ).stdout.strip()
        except Exception:
            pass
    return token or ""


def check_lm_studio():
    """
    Check LM Studio. Returns (status, model).
    status: "up" | "down"
    model:  model ID string or reason string
    """
    token = get_lm_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        req = urllib.request.Request(
            f"{LM_STUDIO_URL}/v1/models", headers=headers
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = data.get("data", [])
            inference = [
                m["id"] for m in models
                if not any(k in m["id"].lower() for k in EMBEDDING_KEYWORDS)
            ]
            if inference:
                return "up", inference[0]
            if models:
                return "up", models[0]["id"]
            return "up", "no models loaded"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "up", "auth required"
        return "down", f"HTTP {exc.code}"
    except OSError:
        return "down", "unreachable"
    except Exception as exc:
        return "down", type(exc).__name__


def get_status() -> dict:
    ip_map = {label: get_iface_ip(dev) for dev, label in INTERFACES}

    # Pick preferred active interface
    active_iface = "none"
    active_ip = "offline"
    for _dev, label in INTERFACES:
        ip = ip_map[label]
        if ip:
            active_iface = label
            active_ip = ip
            break

    lm_status, lm_model = check_lm_studio()

    return {
        "interface": active_iface,
        "ip": active_ip,
        "10gbe": ip_map["10GbE"] or "down",
        "wifi": ip_map["WiFi"] or "down",
        "lmstudio": lm_status,
        "model": lm_model,
    }


class StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/status"):
            body = json.dumps(get_status(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), StatusHandler)
    print(f"MacBook status server listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
