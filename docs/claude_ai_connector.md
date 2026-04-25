# Use m3-memory from Claude.ai (web / desktop) and the Anthropic API

Claude.ai web/desktop and the Anthropic API's MCP Connector talk to MCP
servers over HTTP, not stdio. m3-memory ships with a built-in HTTP transport
(`mcp-memory serve`) plus instructions to expose it safely.

## TL;DR

```bash
mcp-memory serve --host 127.0.0.1 --port 8080
```

This starts the same 66-tool bridge you use locally, on
`http://127.0.0.1:8080/mcp` (Streamable HTTP transport, the spec Claude
expects).

You then need to make `127.0.0.1:8080` reachable by Claude's servers.
Pick **one** of the tunnel options below.

## Tunnel options (pick one)

### Cloudflare Tunnel (free, no public DNS needed)

```bash
# Install once:
brew install cloudflared          # macOS
# or: see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/

# Quick tunnel — gives you a one-time *.trycloudflare.com URL:
cloudflared tunnel --url http://127.0.0.1:8080
```

Cloudflared prints a URL like `https://abc123.trycloudflare.com`. Append
`/mcp` and you have your endpoint: `https://abc123.trycloudflare.com/mcp`.

For a stable URL, set up a named tunnel with your own domain (the
cloudflared docs walk through it).

### Tailscale Funnel (HTTPS, no public DNS needed, requires Tailscale)

```bash
# After Tailscale is set up on your machine:
tailscale serve --bg --https=8443 http://127.0.0.1:8080
tailscale funnel 8443 on
```

Funnel gives you a `https://<host>.<tailnet>.ts.net:8443` URL. Append
`/mcp`.

### ngrok

```bash
ngrok http 8080
```

Ngrok prints a `https://*.ngrok-free.app` URL. Same `/mcp` suffix.

### Self-hosted reverse proxy

If you already run nginx / Caddy / Traefik with a public TLS cert,
forward `/mcp` (or any path) to `127.0.0.1:8080`. Lock it down with
mTLS, an auth-header filter, or a IP-allowlist — see the security
section below.

## Adding the connector to Claude.ai

1. Open Claude.ai → Settings → Connectors → Add custom connector.
2. **URL**: paste the tunnel URL with `/mcp` suffix.
3. **Auth**: configure if your tunnel requires it (Cloudflare Access /
   ngrok-auth / mTLS — claude.ai supports OAuth and bearer-token).
4. Save. Claude.ai will probe the endpoint and list the 66 tools.

## Anthropic API (MCP Connector beta)

Same endpoint works programmatically:

```python
from anthropic import Anthropic

client = Anthropic()
msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=4096,
    extra_headers={"anthropic-beta": "mcp-client-2025-11-20"},
    tools=[{
        "type": "mcp",
        "name": "m3-memory",
        "url": "https://your-tunnel-host/mcp",
    }],
    messages=[{"role": "user", "content": "Search my memory for..."}],
)
```

The connector path is **tool-only** — MCP resources / prompts are not
exposed. All 66 m3-memory tools are tools, so this is fine for our case.

## Running serve as a service

### systemd (Linux)

`/etc/systemd/system/mcp-memory.service`:

```ini
[Unit]
Description=m3-memory HTTP MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
ExecStart=/home/youruser/.local/bin/mcp-memory serve --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

# m3-memory needs HOME for ~/.m3-memory; set it explicitly under a service unit.
Environment=HOME=/home/youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-memory
journalctl -u mcp-memory -f
```

### launchd (macOS)

`~/Library/LaunchAgents/dev.m3-memory.serve.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>dev.m3-memory.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/youruser/.local/bin/mcp-memory</string>
        <string>serve</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>8080</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.m3-memory.serve.plist
```

## Security

**Default bind is `127.0.0.1`.** The HTTP server is only reachable from
the local box until you put a tunnel or reverse proxy in front of it.

**Do not bind `0.0.0.0` directly to the public internet.** The MCP
endpoint exposes write operations including `memory_delete` and
`gdpr_forget` — if it's reachable without auth, anyone can clobber
your memory store. Always front it with one of:

- Cloudflare Access / Tailscale Funnel — auth at the tunnel layer.
- A reverse proxy with `Authorization` header check.
- mTLS.

**Logs.** `mcp-memory serve` logs incoming requests to stderr at INFO
level. Tail the journalctl/launchctl output if anything misbehaves.

**Resource limits.** The bridge uses one SQLite connection pool (5
connections by default) and processes requests sequentially. For
single-user / Claude.ai use, that's plenty. If you front a team-sized
deployment, you may want to run multiple instances behind a load
balancer with shared storage.

## Troubleshooting

- **Claude.ai connector probe times out**: most often the tunnel isn't
  forwarding `/mcp` correctly. Curl `https://your-tunnel/mcp` from
  another box — you should see an MCP protocol error (not a 404).
- **`mcp-memory serve` crashes on startup with `cannot import name 'streamable_http_path'`**:
  your installed `mcp` Python package is older than fastmcp 3.x's HTTP
  support. `pipx upgrade m3-memory` will pull a recent enough version.
- **Tools work but writes don't persist**: check `mcp-memory doctor` —
  the server may be writing to a different `~/.m3-memory/` than your
  local CLI uses (e.g. when running under a different `HOME` in
  systemd). Set `HOME=` explicitly in the unit file.
