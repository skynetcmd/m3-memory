#!/usr/bin/env bash
# m3-memory installer — Linux + macOS.
#
# Detects the OS, installs the OS-level prerequisites (pipx, git, sqlite3),
# then runs `pipx install m3-memory && m3 setup` as the calling user.
# Idempotent: safe to re-run.
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
#   --no-setup                                   stop after pipx install (skip the wizard)
#   --install-gpu-embedder                       also build the in-process GPU embedder (CUDA/Vulkan/Metal)

set -euo pipefail

CAPTURE_MODE="both"
ENDPOINT=""
SKIP_PREREQS=0
RUN_SETUP=1
INSTALL_GPU_EMBEDDER=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --capture-mode)         CAPTURE_MODE="$2"; shift 2 ;;
        --endpoint)             ENDPOINT="$2"; shift 2 ;;
        --skip-prereqs)         SKIP_PREREQS=1; shift ;;
        --no-setup)             RUN_SETUP=0; shift ;;
        --install-gpu-embedder) INSTALL_GPU_EMBEDDER=1; shift ;;
        -h|--help)
            # Self-contained heredoc, not `sed "$0"`: when the script is run
            # via `curl ... | bash`, $0 is "bash" and there is no file to read.
            cat <<'USAGE'
m3-memory installer — Linux + macOS.

Detects the OS, installs the OS-level prerequisites (pipx, git, sqlite3),
then runs `pipx install m3-memory && m3 setup` as the calling user.
Idempotent: safe to re-run.

Usage:
  curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash

Cautious version:
  curl -fsSL .../install.sh -o install.sh && less install.sh && bash install.sh

Flags:
  --capture-mode {both|stop|precompact|none}   default: both
  --endpoint URL                               pin LLM_ENDPOINTS_CSV
  --skip-prereqs                               assume pipx/git/sqlite3 already present
  --no-setup                                   stop after pipx install (skip the wizard)
  --install-gpu-embedder                       also build the in-process GPU embedder (CUDA/Vulkan/Metal)
USAGE
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

# Returns 0 if the user can sudo without a password (NOPASSWD or already
# authenticated in this session). Uses -n so it never prompts.
can_sudo_nopasswd() { sudo -n true 2>/dev/null; }

# Returns 0 if the user is in the sudo or wheel group (can sudo, but may need
# a password).
in_privileged_group() {
    local user="${USER:-$(id -un)}"
    local group
    for group in sudo wheel; do
        if getent group "$group" 2>/dev/null | grep -qE "(^|,)${user}(,|$)"; then
            return 0
        fi
    done
    return 1
}

# Run a command that needs root. Tries sudo first; if unavailable, prints the
# command the user should run in a root shell and exits with an error.
#
# Usage: run_as_root <cmd> [args...]
run_as_root() {
    if can_sudo_nopasswd; then
        sudo "$@"
    elif in_privileged_group; then
        # In the group but sudo needs a password — try interactively.
        # If there's no tty (curl|bash), sudo will fail; catch that and advise.
        if ! sudo "$@" 2>/dev/null; then
            color_err "sudo requires a password but no terminal is available."
            color_err "Open another shell as root and run:"
            color_err "    $*"
            color_err "Then re-run this installer."
            exit 1
        fi
    else
        color_err "This step requires root but you are not in the sudo or wheel group."
        color_err "Ask a system administrator to run the following in a root shell:"
        color_err "    $*"
        color_err "Then re-run this installer with --skip-prereqs."
        exit 1
    fi
}

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
        debian|ubuntu|linuxmint|pop|raspbian|neon|kali|zorin|elementary|mint)
            # python3-venv is a runtime dependency of pipx on Debian/Ubuntu.
            local pkgs=("${missing[@]}")
            need_cmd pipx || pkgs+=(python3-venv)
            run_as_root apt-get update -qq
            run_as_root apt-get install -y "${pkgs[@]}"
            ;;
        fedora|rhel|centos|rocky|almalinux)
            # Fedora calls the package python3-virtualenv (pipx >= 38 ships
            # with system venv on RHEL clones; treat it like Debian).
            local pkgs=("${missing[@]}")
            need_cmd pipx || pkgs+=(python3-virtualenv)
            run_as_root dnf install -y "${pkgs[@]}"
            ;;
        arch|manjaro|endeavouros)
            local map=()
            for p in "${missing[@]}"; do
                case "$p" in
                    pipx) map+=(python-pipx) ;;
                    *)    map+=("$p") ;;
                esac
            done
            run_as_root pacman -S --needed --noconfirm "${map[@]}"
            ;;
        opensuse*|suse|sles)
            run_as_root zypper install -y "${missing[@]}"
            ;;
        alpine)
            run_as_root apk add --no-cache "${missing[@]}"
            ;;
        *)
            # Unknown distro — try apt-get (covers many Debian derivatives
            # that don't set a recognised ID in /etc/os-release).
            if need_cmd apt-get; then
                color_warn "Unknown distro '${DISTRO:-unset}'; trying apt-get (Debian-family fallback)."
                local pkgs=("${missing[@]}")
                need_cmd pipx || pkgs+=(python3-venv)
                run_as_root apt-get update -qq
                run_as_root apt-get install -y "${pkgs[@]}"
            elif need_cmd dnf; then
                color_warn "Unknown distro '${DISTRO:-unset}'; trying dnf (RHEL-family fallback)."
                local pkgs=("${missing[@]}")
                need_cmd pipx || pkgs+=(python3-virtualenv)
                run_as_root dnf install -y "${pkgs[@]}"
            else
                color_err "Unknown Linux distro: ${DISTRO:-unset} — no known package manager found."
                color_err "Install manually: ${missing[*]}"
                color_err "Then re-run with --skip-prereqs."
                exit 1
            fi
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

# Detect an existing m3-memory pipx install without depending on the exact
# layout of pipx's human-readable `list` output (the old check hardcoded a
# 3-space indent + "package " prefix and broke on any pipx format change).
# Prefer `--short` (machine-readable: "<pkg> <version>" per line); fall back to
# a loose, indent-independent, case-insensitive match on plain `list`.
m3_installed() {
    local out
    if out=$(pipx list --short 2>/dev/null) && [[ -n "$out" ]]; then
        printf '%s\n' "$out" | grep -qiE '(^|/)m3-memory[[:space:]]'
    else
        pipx list 2>/dev/null | grep -qiE '(^|[^[:alnum:]_-])m3-memory([^[:alnum:]_-]|$)'
    fi
}

if m3_installed; then
    say "m3-memory already installed via pipx — upgrading."
    pipx upgrade m3-memory || pipx install --force m3-memory
else
    say "Installing m3-memory via pipx."
    pipx install m3-memory
fi

# ── m3 setup (one-command wizard, non-interactive) ────────────────────────────

if [[ $RUN_SETUP -eq 0 ]]; then
    color_ok "Installed m3-memory. Skipping setup (--no-setup)."
    exit 0
fi

SETUP_ARGS=(--non-interactive --capture-mode "$CAPTURE_MODE")
if [[ -n "$ENDPOINT" ]]; then
    SETUP_ARGS+=(--endpoint "$ENDPOINT")
fi
if [[ $INSTALL_GPU_EMBEDDER -eq 1 ]]; then
    SETUP_ARGS+=(--install-gpu-embedder)
fi

say "Running: m3 setup ${SETUP_ARGS[*]}"
m3 setup "${SETUP_ARGS[@]}"

echo
color_ok "Done. m3-memory is installed."
echo
cat <<EOF
Next steps:
  1. Open a new terminal (or run: exec \$SHELL -l) so PATH picks up ~/.local/bin.
  2. Restart any agent (Claude Code, Gemini CLI, OpenCode) so it picks up the m3 MCP server.
  3. (Optional) Add GPU acceleration to the embedder: m3 embedder install-gpu
  4. Re-run this script anytime to upgrade and re-verify.
EOF
