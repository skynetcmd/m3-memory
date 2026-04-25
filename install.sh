#!/usr/bin/env bash
# m3-memory installer — Linux + macOS.
#
# Detects the OS, installs the OS-level prerequisites (pipx, git, sqlite3),
# then runs `pipx install m3-memory && mcp-memory install-m3` as the calling
# user. Idempotent: safe to re-run.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
#
# Cautious version:
#   curl -fsSL .../install.sh -o install.sh && less install.sh && bash install.sh
#
# Flags:
#   --capture-mode {both|stop|precompact|none}   default: both
#   --endpoint URL                               pin LLM_ENDPOINTS_CSV
#   --skip-prereqs                               assume pipx/git/sqlite3 already present
#   --no-install-m3                              stop after pipx install (don't fetch payload)

set -euo pipefail

CAPTURE_MODE="both"
ENDPOINT=""
SKIP_PREREQS=0
RUN_INSTALL_M3=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --capture-mode) CAPTURE_MODE="$2"; shift 2 ;;
        --endpoint)     ENDPOINT="$2"; shift 2 ;;
        --skip-prereqs) SKIP_PREREQS=1; shift ;;
        --no-install-m3) RUN_INSTALL_M3=0; shift ;;
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

# ── helpers ───────────────────────────────────────────────────────────────────

color_ok()    { printf '\033[32m%s\033[0m\n' "$*"; }
color_warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
color_err()   { printf '\033[31m%s\033[0m\n' "$*"; }
say()         { printf '\033[36m==>\033[0m %s\n' "$*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# Refuse to run as root via curl|bash. Root would put pipx files under /root and
# leave the actual user without an install. Sudo prompts inside the script
# install OS packages cleanly without this risk.
if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    color_err "Refusing to run as root — pipx installs into the running user's home."
    color_err "Re-run as your normal user. The script will sudo for apt/dnf when needed."
    exit 1
fi

# ── OS detection ──────────────────────────────────────────────────────────────

OS=""
DISTRO=""

case "$(uname -s)" in
    Linux)
        OS=linux
        if [[ -r /etc/os-release ]]; then
            # shellcheck disable=SC1091
            . /etc/os-release
            DISTRO="${ID:-unknown}"
        fi
        ;;
    Darwin)
        OS=macos
        ;;
    *)
        color_err "Unsupported OS: $(uname -s). See docs/install_windows.md for Windows."
        exit 1
        ;;
esac

say "Detected: $OS${DISTRO:+ ($DISTRO)}"

# ── prereq install ────────────────────────────────────────────────────────────

install_prereqs_linux() {
    local missing=()
    need_cmd pipx    || missing+=(pipx)
    need_cmd git     || missing+=(git)
    need_cmd sqlite3 || missing+=(sqlite3)
    need_cmd curl    || missing+=(curl)

    if [[ ${#missing[@]} -eq 0 ]]; then
        say "All prerequisites already installed."
        return
    fi

    say "Need: ${missing[*]}"

    case "$DISTRO" in
        debian|ubuntu|linuxmint|pop|raspbian)
            # python3-venv is a runtime dependency of pipx on Debian/Ubuntu.
            local pkgs=("${missing[@]}")
            need_cmd pipx || pkgs+=(python3-venv)
            sudo apt-get update
            sudo apt-get install -y "${pkgs[@]}"
            ;;
        fedora|rhel|centos|rocky|almalinux)
            # Fedora calls the package python3-virtualenv (pipx >= 38 ships
            # with system venv on RHEL clones; treat it like Debian).
            local pkgs=("${missing[@]}")
            need_cmd pipx || pkgs+=(python3-virtualenv)
            sudo dnf install -y "${pkgs[@]}"
            ;;
        arch|manjaro|endeavouros)
            local map=()
            for p in "${missing[@]}"; do
                case "$p" in
                    pipx) map+=(python-pipx) ;;
                    *)    map+=("$p") ;;
                esac
            done
            sudo pacman -S --needed --noconfirm "${map[@]}"
            ;;
        opensuse*|suse|sles)
            sudo zypper install -y "${missing[@]}"
            ;;
        alpine)
            sudo apk add --no-cache "${missing[@]}"
            ;;
        *)
            color_err "Unknown Linux distro: ${DISTRO:-unset}"
            color_err "Install manually: ${missing[*]}"
            color_err "Then re-run with --skip-prereqs."
            exit 1
            ;;
    esac
}

install_prereqs_macos() {
    if ! need_cmd brew; then
        color_err "Homebrew not found. Install from https://brew.sh and re-run."
        exit 1
    fi
    local missing=()
    need_cmd pipx    || missing+=(pipx)
    need_cmd git     || missing+=(git)
    need_cmd sqlite3 || missing+=(sqlite)
    if [[ ${#missing[@]} -eq 0 ]]; then
        say "All prerequisites already installed."
        return
    fi
    say "Need: ${missing[*]}"
    brew install "${missing[@]}"
}

if [[ $SKIP_PREREQS -eq 0 ]]; then
    case "$OS" in
        linux) install_prereqs_linux ;;
        macos) install_prereqs_macos ;;
    esac
else
    say "Skipping prereq install (--skip-prereqs)."
fi

# ── pipx ensurepath ───────────────────────────────────────────────────────────

# pipx adds ~/.local/bin (Linux) or /opt/homebrew/bin shims (macOS, already on
# PATH via brew) to the user's PATH. Run ensurepath either way; it's idempotent
# and prints what (if anything) it changed.
pipx ensurepath >/dev/null 2>&1 || true

# Make ~/.local/bin available IN THIS process so the install-m3 step below
# resolves mcp-memory without requiring `exec $SHELL -l`.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac

# ── pipx install m3-memory ────────────────────────────────────────────────────

if pipx list 2>/dev/null | grep -q '^   package m3-memory '; then
    say "m3-memory already installed via pipx — upgrading."
    pipx upgrade m3-memory || pipx install --force m3-memory
else
    say "Installing m3-memory via pipx."
    pipx install m3-memory
fi

# ── mcp-memory install-m3 ─────────────────────────────────────────────────────

if [[ $RUN_INSTALL_M3 -eq 0 ]]; then
    color_ok "Installed m3-memory. Skipping install-m3 (--no-install-m3)."
    exit 0
fi

INSTALL_M3_ARGS=(--non-interactive --capture-mode "$CAPTURE_MODE")
if [[ -n "$ENDPOINT" ]]; then
    INSTALL_M3_ARGS+=(--endpoint "$ENDPOINT")
fi

# install-m3 is idempotent only with --force when a previous install exists.
# Detect and force, since this script is the "fresh install" entrypoint.
if [[ -d "$HOME/.m3-memory/repo" ]]; then
    INSTALL_M3_ARGS+=(--force)
fi

say "Running: mcp-memory install-m3 ${INSTALL_M3_ARGS[*]}"
mcp-memory install-m3 "${INSTALL_M3_ARGS[@]}"

# ── doctor summary ────────────────────────────────────────────────────────────

echo
say "Verifying with mcp-memory doctor:"
mcp-memory doctor || true

echo
color_ok "Done. m3-memory is installed."
echo
cat <<EOF
Next steps:
  1. Open a new terminal (or run: exec \$SHELL -l) so PATH picks up ~/.local/bin.
  2. Wire your AI agent to call mcp-memory:
       Claude Code: claude mcp add memory mcp-memory
       Gemini CLI:  already configured by install-m3 if gemini was on PATH.
  3. Re-run this script anytime to upgrade and re-verify.
EOF
