FROM node:22-slim

# Install git, C++ build tools (required for OpenClaw's native node-llama-cpp),
# and runtime utilities requested by the agent
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential python3 cmake \
    curl wget iputils-ping jq ffmpeg git-lfs unzip \
    zip sqlite3 dnsutils python3-pip file tree imagemagick cron \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Security: Run as an unprivileged jailable user
RUN groupadd -r clawgroup && useradd -r -g clawgroup clawuser

# Install OpenClaw globally
RUN npm install -g openclaw@2026.2.26

# Create necessary directories and enforce ownership
RUN mkdir -p /home/clawuser/.openclaw /shared && chown -R clawuser:clawgroup /home/clawuser /shared

# Drop root privileges immediately
USER clawuser
WORKDIR /shared

# Expose the gateway on all interfaces so the macOS host can connect
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3000/healthz || exit 1

ENTRYPOINT ["openclaw", "gateway", "run", "--bind", "lan"]
