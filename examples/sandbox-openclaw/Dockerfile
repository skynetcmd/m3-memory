# Node 24 is REQUIRED by the OpenClaw pin below, not a gratuitous bump:
# openclaw@2026.9.4 declares engines.node ">=24.16.0 <25 || >=26.1.0" (verified
# against the upstream tag). node:24-slim currently resolves to 24.21.0, inside
# that range. Running OpenClaw on an under-version Node does NOT fail cleanly --
# upstream's 2026.9.3 notes report SQLite text truncation, i.e. silent data
# corruption. So this line and the `npm install -g openclaw@...` line are ONE
# constraint: never move one without the other.
FROM node:24-slim

# Install git, C++ build tools (required for OpenClaw's native node-llama-cpp),
# and runtime utilities requested by the agent.
# hadolint ignore=DL3008: this is an example sandbox image; pinning ~15 apt
# package versions to exact Debian releases would break the build whenever the
# upstream repo rotates them. Unpinned is the intentional choice for an example.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential python3 cmake \
    curl wget iputils-ping jq ffmpeg git-lfs unzip \
    zip sqlite3 dnsutils python3-pip file tree imagemagick cron \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Security: Run as an unprivileged jailable user
RUN groupadd -r clawgroup && useradd -r -g clawgroup clawuser

# Install OpenClaw globally.
# 2026.9.4 (was 2026.2.26, which predates native MCP entirely). The floor for
# the `mcp` section in config/openclaw.json is 2026.3.22 -- the release that
# added the native MCP client. Pinned rather than @latest so the image is
# reproducible; if you bump it, re-check engines.node against the FROM line above.
RUN npm install -g openclaw@2026.9.4

# Create necessary directories and enforce ownership
RUN mkdir -p /home/clawuser/.openclaw /shared && chown -R clawuser:clawgroup /home/clawuser /shared

# Drop root privileges immediately
# DL3066 warns a non-numeric user-id 'may not be resolvable by the host
# system', which applies to images that INHERIT a user they did not create.
# This image creates clawuser/clawgroup itself, so the name always resolves,
# and a name is far more auditable than a bare uid when reading what this
# container runs as.
# hadolint ignore=DL3066
USER clawuser
WORKDIR /shared

# Expose the gateway on all interfaces so the macOS host can connect
# hadolint ignore=DL3025 -- the SHELL form is required, not a lapse. DL3025
# asks for JSON-array notation, which execs the command directly and therefore
# cannot express the `|| exit 1` fallback this check depends on: the array form
# has no shell to evaluate `||`, so a curl failure would surface curl's own
# exit code and Docker would report unhealthy for the wrong reason.
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000/healthz || exit 1

ENTRYPOINT ["openclaw", "gateway", "run", "--bind", "lan"]
