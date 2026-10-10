#!/usr/bin/env bash
# =============================================================================
# Step 1-2: Base System Setup
# Target: Ubuntu 24.04 LTS (joshua-VirtualBox)
# Run as: bash base-setup.sh
# =============================================================================
# ENVIRONMENT PARITY DEVIATIONS (accepted, document):
#   - OS:  This VM runs Ubuntu 24.04 (noble). Spec + production = Ubuntu 22.04
#          (jammy). Docker repo codename is resolved dynamically so installs
#          correctly, but package versions may differ from production.
#   - RAM: 8 GB is the minimum for the full stack. Under 8 GB, Elasticsearch
#          may OOM or fail to start.
#   - Python: VM has 3.12.3. Ubuntu 22.04 production likely uses 3.10.x.
#          Confirm the exact version from the production Dockerfile FROM line
#          and pin PYTHON_VERSION below if needed.
#
# Docker version pin: set DOCKER_VERSION if production specifies one.
#   e.g. DOCKER_VERSION="5:27.3.1-1~ubuntu.24.04~noble"
#   Leave blank to install latest stable.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCKER_VERSION=""          # Leave blank for latest, e.g. "5:27.3.1-1~ubuntu.24.04~noble"

# Repository to clone when no existing clone is detected. Override by exporting
# REPO_SSH before running (e.g. for forks):
#   REPO_SSH=git@github.com:myorg/myfork.git bash deployment/step1-and-2-base-setup.sh
REPO_SSH="${REPO_SSH:-git@github.com:Kavanah/kavanah-analytics.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

# Target clone directory. If the script is run from inside a git repo, that
# repo is reused instead (see ── 6. Clone repository ── below).
CLONE_DIR="${CLONE_DIR:-$HOME/kavanah-analytics}"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
info "Starting base system setup on $(hostname)"
info "Date: $(date)"

[[ $EUID -eq 0 ]] && error "Do not run as root. Script uses sudo internally."

# ── RAM check ─────────────────────────────────────────────────────────────────
TOTAL_RAM_GB=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
if (( TOTAL_RAM_GB < 8 )); then
    warn "RAM: ${TOTAL_RAM_GB}GB detected. 8GB is the minimum for the full stack."
    warn "Elasticsearch may fail to start or OOM. Increase RAM before running"
    warn "the full stack. Continuing anyway for base setup..."
fi

# ── 1. Update all packages ────────────────────────────────────────────────────
info "Updating package index..."
sudo apt-get update -y

info "Upgrading installed packages..."
sudo apt-get upgrade -y

info "Removing unused packages..."
sudo apt-get autoremove -y
success "System packages up to date."

# ── 2. Install Docker Engine ──────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    warn "Docker already installed: $(docker --version). Skipping."
else
    info "Installing Docker Engine prerequisites..."
    sudo apt-get install -y ca-certificates curl gnupg lsb-release

    info "Adding Docker GPG key..."
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    info "Adding Docker apt repository..."
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update -y

    if [[ -n "$DOCKER_VERSION" ]]; then
        info "Installing Docker Engine version: $DOCKER_VERSION"
        sudo apt-get install -y \
            docker-ce="$DOCKER_VERSION" \
            docker-ce-cli="$DOCKER_VERSION" \
            containerd.io \
            docker-buildx-plugin \
            docker-compose-plugin
    else
        info "Installing latest stable Docker Engine..."
        sudo apt-get install -y \
            docker-ce \
            docker-ce-cli \
            containerd.io \
            docker-buildx-plugin \
            docker-compose-plugin
    fi

    info "Adding $USER to docker group (re-login required to take effect)..."
    sudo usermod -aG docker "$USER"

    sudo systemctl enable docker
    sudo systemctl start docker
    success "Docker installed: $(docker --version)"
fi

# ── 3. Verify Docker Compose ──────────────────────────────────────────────────
if docker compose version &>/dev/null; then
    success "Docker Compose available: $(docker compose version)"
else
    error "Docker Compose plugin not found. Check Docker installation."
fi

# ── 4. Python 3 and pip ───────────────────────────────────────────────────────
info "Checking Python 3..."
if ! command -v python3 &>/dev/null; then
    warn "Python 3 not found. Installing python3..."
    sudo apt-get install -y python3
fi
success "Python: $(python3 --version)"

info "Installing pip..."
if ! command -v pip3 &>/dev/null; then
    sudo apt-get install -y python3-pip
fi

# python3-venv is required by step7-build-docs.sh (Sphinx venv) and by any
# other script that needs an isolated Python env under PEP 668.
info "Ensuring python3-venv is installed..."
sudo apt-get install -y python3-venv

# Ensure pip itself is up to date
# --break-system-packages: Ubuntu 24.04 / Python 3.12 ship with PEP 668 lockdown.
# We're bootstrapping a VM, not a shared Python env — system-site upgrade is fine.
python3 -m pip install --upgrade pip --quiet --break-system-packages
success "pip: $(pip3 --version)"

# ── 5. Git ────────────────────────────────────────────────────────────────────
if command -v git &>/dev/null; then
    warn "Git already installed: $(git --version). Skipping."
else
    info "Installing Git..."
    sudo apt-get install -y git
    success "Git installed: $(git --version)"
fi

# ── 6. Clone repository ───────────────────────────────────────────────────────
# Three cases:
#   (a) $CLONE_DIR/.git exists            → reuse it, pull latest
#   (b) script is running from inside a   → reuse that repo (supports forks,
#       git repo                            private mirrors, alternate layouts)
#   (c) neither                           → fresh clone from $REPO_SSH
SCRIPT_REPO_ROOT=""
if command -v git &>/dev/null; then
    SCRIPT_REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)
fi

if [[ -d "$CLONE_DIR/.git" ]]; then
    warn "Repo already cloned at $CLONE_DIR. Pulling latest..."
    git -C "$CLONE_DIR" fetch --all
    git -C "$CLONE_DIR" checkout "$REPO_BRANCH"
    git -C "$CLONE_DIR" pull origin "$REPO_BRANCH"
    success "Repo updated at $CLONE_DIR"
elif [[ -n "$SCRIPT_REPO_ROOT" && -d "$SCRIPT_REPO_ROOT/.git" ]]; then
    info "Script is running from inside a git repo: $SCRIPT_REPO_ROOT"
    info "Reusing this repo instead of cloning $REPO_SSH."
    CLONE_DIR="$SCRIPT_REPO_ROOT"
    git -C "$CLONE_DIR" fetch --all || warn "git fetch failed (continuing with current state)"
    success "Repo in use: $CLONE_DIR (branch: $(git -C "$CLONE_DIR" rev-parse --abbrev-ref HEAD))"
else
    info "Checking for SSH key for GitHub access..."
    if [[ ! -f "$HOME/.ssh/id_ed25519" && ! -f "$HOME/.ssh/id_rsa" ]]; then
        warn "No SSH key found at ~/.ssh/id_ed25519 or ~/.ssh/id_rsa."
        warn "You will need to add an SSH key authorised on GitHub before cloning."
        warn "Generate one with: ssh-keygen -t ed25519 -C 'server-deploy'"
        warn "Then add the public key to your GitHub repo as a deploy key."
        error "Cannot clone without an SSH key. Aborting."
    fi

    info "Cloning $REPO_SSH into $CLONE_DIR..."
    # Accept GitHub host key automatically (first-time clone)
    ssh -o StrictHostKeyChecking=accept-new git@github.com 2>/dev/null || true
    git clone "$REPO_SSH" "$CLONE_DIR"
    success "Repo cloned."

    info "Checking out branch: $REPO_BRANCH"
    git -C "$CLONE_DIR" checkout "$REPO_BRANCH"
    success "On branch: $(git -C "$CLONE_DIR" rev-parse --abbrev-ref HEAD)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Base Setup Complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "  OS:             $(lsb_release -ds)"
echo -e "  Docker:         $(docker --version)"
echo -e "  Docker Compose: $(docker compose version)"
echo -e "  Python:         $(python3 --version)"
echo -e "  pip:            $(pip3 --version | awk '{print $1, $2}')"
echo -e "  Git:            $(git --version)"
echo -e "  Repo:           $CLONE_DIR"
echo -e "  Branch:         $(git -C "$CLONE_DIR" rev-parse --abbrev-ref HEAD)"
echo ""
echo -e "${YELLOW}NOTE: Log out and back in (or run 'newgrp docker') for${NC}"
echo -e "${YELLOW}      docker group membership to take effect.${NC}"
echo -e "${GREEN}============================================${NC}"
