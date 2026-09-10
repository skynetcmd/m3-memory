# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Use m3-memory from Claude.ai (web / desktop) and the Anthropic API

Claude.ai web/desktop and the Anthropic API's MCP Connector talk to MCP
servers over HTTP, not stdio. m3-memory ships with a built-in HTTP transport
(`m3 serve`) plus instructions to expose it safely.

> Prerequisite: install m3 first via `pip install m3-memory && m3 setup`
> (or the [one-line installer](../install.sh)). The wizard sets up the
> embedder and bridge before you expose the HTTP transport.

## Read this before you start

Three things decide whether this will work for you at all. They are here rather
than at the bottom because each one is cheaper to check now than to discover
after you have built a tunnel.

**1. Your server must be reachable from the public internet.** Claude connects to
a remote MCP server *from Anthropic's cloud infrastructure, not from your
device*, on every client including the mobile apps. A server on a private
network, behind a VPN, or on a Tailscale/WireGuard tailnet **will not connect** —
not even from the Claude app on a device that is itself on that tailnet.
([Anthropic's docs](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp))

**2. The endpoint must be HTTPS.** The connector dialog requires an `https://`
URL. `m3 serve` speaks plain HTTP, so TLS comes from whatever you put in front
of it — every tunnel option below terminates TLS for you.

**3. Static-token auth may not be available on your account.** Sending an
`Authorization` header is configured under **Request headers** in the connector
dialog, which Anthropic documents as *beta, available to a limited set of
organizations*. If you do not see that section, your options are OAuth (which m3
does not implement) or not using a connector. Check the dialog before doing any
of the setup below.

> **The bridge requires a bearer token and refuses to start without one.** That
> is deliberate: this endpoint exposes `memory_delete` and `gdpr_forget`, and the
> deployment shape above means it is on the public internet. There is no
> "trusted network" configuration to fall back on.

If any of those rule you out and you only wanted mobile access to your memory,
the [web dashboard](#alternative-the-dashboard-over-a-vpn) over a VPN is a
better fit — it runs in the browser on your own device, so the reachability
constraint above does not apply.

---

## TL;DR

```bash
m3 serve --generate-token          # once: mint a token, printed one time only
m3 serve --host 127.0.0.1 --port 8080 --public-host <your-tunnel-hostname>
```

This starts the same 100+ tool bridge you use locally on
`http://127.0.0.1:8080/mcp` (Streamable HTTP transport, the spec Claude
expects). Domain-gated by default — only ~6 essentials register at session
start, rest expand on demand via `tools_load_domain` (see the
[lazy-loading note](../README.md#-domain-gating-the-full-catalog-without-the-context-cost)).

`--public-host` is **required when you use a tunnel**: the transport validates the
`Host` header, and a tunnel forwards its own public hostname. Without it every
tunnelled request is rejected with `421 Misdirected Request` *before*
authentication runs — which looks like a broken tunnel rather than a rejected
host. Pass the flag once per hostname (repeatable).

You then need to make `127.0.0.1:8080` reachable by Claude's servers.
Pick **one** of the tunnel options below.

---

## Tunnel options (pick one)

> Each of these publishes your bridge **to the internet**. Do not run any of them
> until `m3 serve --show-token` reports a configured token — the server enforces
> this by refusing to start, but a tunnel pointed at a *different* local port can
> still expose something else.

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

---

## Adding the connector to Claude.ai

1. Open Claude.ai → Settings → Connectors → Add custom connector.
   (On mobile: **Customize → Connectors**. Installing connectors on mobile is
   in beta; desktop and web are the primary path.)
2. **URL**: paste the tunnel URL with the `/mcp` suffix. It must be `https://`.
3. **Authentication**: choose **None** — that means "no OAuth sign-in", not "no
   credential". The bearer token goes in the next field.
4. **Request headers** → add one:

   | Field | Value |
   |---|---|
   | Header | `Authorization` |
   | Value  | `Bearer ` + your token |

   ⚠ **Include the literal `Bearer ` prefix, with the trailing space.** Claude
   sends the value *exactly* as entered and adds no scheme of its own, so a bare
   token is rejected with `401`. `m3 serve --generate-token` prints the complete
   line to paste.

   If you do not see a **Request headers** section, your organization does not
   have that beta — see point 3 at the top of this page.
5. Save. Claude.ai will probe the endpoint and list the active tool set
   — by default ~6 essentials plus the two `tools_*` meta-tools (the
   agent expands more domains as needed).

**If the connector fails to probe**, check in this order — the failure modes look
alike from the dialog:

| Symptom | Likely cause |
|---|---|
| `401` | Token missing the `Bearer ` prefix, or a stale token; `m3 serve --show-token` |
| `421` | `--public-host` not set to the tunnel's hostname |
| Connection refused / timeout | Tunnel down, or the bridge is not running |
| Server never started | No token configured — the bridge refuses; check its log |

---

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
exposed. Every m3-memory capability is a tool, so this is fine for our case.

---

## Running serve as a service

### systemd (Linux)

`/etc/systemd/system/m3-memory.service`:

```ini
[Unit]
Description=m3-memory HTTP MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<user>
ExecStart=/home/<user>/.local/bin/m3 serve --host 127.0.0.1 --port 8080 \
          --public-host mcp.example.com
Restart=on-failure
RestartSec=5

# m3-memory needs HOME to resolve ~/.m3/engine and ~/.m3/config; set it
# explicitly under a service unit.
Environment=HOME=/home/<user>

# The bearer token. A systemd unit does NOT inherit your login session's keyring,
# so a token stored only in the OS keyring is unreachable here and the service
# will refuse to start. Either point it at a file the unit can read:
#   EnvironmentFile=/etc/m3-memory/serve.env      # M3_SERVE_TOKEN=...  (chmod 600)
# or rely on the encrypted vault, which is headless-safe and needs no session.

[Install]
WantedBy=multi-user.target
```

> If the service exits immediately, read its log before anything else: the bridge
> prints exactly which condition it refused on (no token / undecryptable / too
> short), and each names a different fix.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now m3-memory
journalctl -u m3-memory -f
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
        <string>/Users/<user>/.local/bin/m3</string>
        <string>serve</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>8080</string>
        <string>--public-host</string><string>mcp.example.com</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
```

A LaunchAgent (per-user, loaded into your GUI session) can normally reach the
login Keychain, so a vault- or Keychain-stored token resolves without extra
configuration. A **LaunchDaemon** (system-wide) cannot — give that one an
explicit `M3_SERVE_TOKEN` via `EnvironmentVariables`, or keep the token in the
encrypted vault, which needs no session.

```bash
launchctl load ~/Library/LaunchAgents/dev.m3-memory.serve.plist
```

---

## Security

**A bearer token is mandatory and enforced by the server.** `m3 serve` refuses to
start without one and exits non-zero — including on `127.0.0.1`, because the
tunnels this feature exists to support bind loopback and publish it, so a
loopback bind is not evidence of a private deployment. Every request is checked
with a constant-time comparison; a missing token and a wrong token produce
byte-identical `401`s, so the endpoint is not an oracle.

**Default bind is still `127.0.0.1`.** Keep it there and let the tunnel reach in;
binding `0.0.0.0` adds exposure without adding capability.

**Token handling.**

```bash
m3 serve --generate-token          # prints ONCE; stores in the m3 vault
m3 serve --show-token              # reports configured / missing — never prints it
m3 serve --generate-token --force  # rotate (invalidates every existing connector)
```

The token resolves through the standard cascade (env → OS keyring → encrypted
vault), so it works under systemd/launchd with no interactive prompt. ⚠ The
environment wins over the vault: if `M3_SERVE_TOKEN` is set in the shell, that is
the value the server expects, not the one `--generate-token` just printed. The
command warns when it detects this.

**Defence in depth is still worth it.** The token is one credential on a public
endpoint. Where you can, also front it with Cloudflare Access, an IP allowlist,
or mTLS — the bridge's own auth is the floor, not the ceiling.

**Logs.** `m3 serve` logs incoming requests to stderr at INFO level. Tail
the journalctl/launchctl output if anything misbehaves. Tokens are ≥32 chars, so
m3's own chatlog redaction rule for `Bearer <20+ chars>` scrubs them if capture
is enabled — but your *tunnel's* logs are outside m3's control.

**Resource limits.** The bridge uses one SQLite connection pool (5
connections by default) and processes requests sequentially. For
single-user / Claude.ai use, that's plenty. If you front a team-sized
deployment, you may want to run multiple instances behind a load
balancer with shared storage.

---

## Alternative: the dashboard over a VPN

If you wanted this mainly to reach your memory from a phone or another machine,
the web dashboard is a better fit and avoids the public-exposure requirement
entirely. The constraint at the top of this page applies to *Claude connecting to
your server*; a browser on your own device has no such restriction, so a VPN or
tailnet works.

```bash
m3 serve --generate-token         # same token as the bridge, if you have not already
m3 dashboard                      # loopback by default
python bin/dashboard_server.py --show-url
```

`--show-url` prints a one-time sign-in URL containing the token. Open it once on
the target device: the token is exchanged for an `HttpOnly` cookie and the URL is
rewritten without it, so the secret does not linger in history. Bind the
dashboard to an address your VPN can route to, and it is reachable from your
other devices with no public endpoint at all.

What you give up: it is a browsing and curation UI, not a conversation — Claude
itself is not in the loop. What you gain: nothing is published to the internet.

> A loopback dashboard with no token configured keeps its historical
> no-auth behaviour, unchanged. Configure a token, or bind a non-loopback
> address, and the gate engages (a non-loopback bind without a token refuses to
> start).

---

## Troubleshooting

- **Claude.ai connector probe times out**: most often the tunnel isn't
  forwarding `/mcp` correctly. Curl `https://your-tunnel/mcp` from
  another box — you should see an MCP protocol error (not a 404).
- **`m3 serve` crashes on startup with `cannot import name 'streamable_http_path'`**:
  your installed `mcp` Python package is older than fastmcp 3.x's HTTP
  support. `pipx upgrade m3-memory` will pull a recent enough version.
- **Tools work but writes don't persist**: check `m3 doctor` — the server
  may be writing to a different `~/.m3-memory/` than your local CLI uses
  (e.g. when running under a different `HOME` in systemd). Set `HOME=`
  explicitly in the unit file.
