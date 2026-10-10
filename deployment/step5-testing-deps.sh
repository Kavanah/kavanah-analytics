#!/usr/bin/env bash
# =============================================================================
# Step 5: Testing Dependencies
# Target: Ubuntu 24.04 LTS (joshua-VirtualBox)
# Run as: bash step5-testing-deps.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# Derive GitHub repo URL for runner registration messages. Prefer the actual
# remote of the repo this script lives in (works for forks, mirrors, private
# clones). Falls back to the upstream public repo if detection fails.
detect_repo_url() {
    local remote
    if command -v git &>/dev/null; then
        remote=$(git -C "$SCRIPT_DIR" config --get remote.origin.url 2>/dev/null || true)
        if [[ -n "$remote" ]]; then
            # Convert git@github.com:org/repo.git → https://github.com/org/repo
            remote="${remote#git@github.com:}"
            remote="${remote#https://github.com/}"
            remote="${remote%.git}"
            echo "https://github.com/${remote}"
            return
        fi
    fi
    echo "https://github.com/Kavanah/kavanah-analytics"
}
REPO_URL=$(detect_repo_url)

[[ $EUID -eq 0 ]] && error "Do not run as root. Script uses sudo internally."

# ── 1. pytest, pytest-flask, pytest-cov ──────────────────────────────────────
info "Installing pytest, pytest-flask, pytest-cov..."
pip3 install --quiet --break-system-packages \
    pytest \
    pytest-flask \
    pytest-cov \
    pytest-asyncio \
    requests
success "pytest: $(python3 -m pytest --version)"
success "pytest-flask, pytest-cov, pytest-asyncio installed."

# ── 2. Playwright ────────────────────────────────────────────────────────────
info "Installing Playwright Python package..."
pip3 install --quiet --break-system-packages playwright
success "Playwright Python package installed."

info "Installing Playwright browsers (Chromium) and system deps..."
python3 -m playwright install --with-deps chromium
success "Playwright Chromium browser installed."

# ── 3. Locust ────────────────────────────────────────────────────────────────
info "Installing Locust..."
pip3 install --quiet --break-system-packages locust
success "Locust: $(locust --version)"

# ── 4. OWASP ZAP (Docker image) ──────────────────────────────────────────────
info "Pulling OWASP ZAP Docker image (ghcr.io/zaproxy/zaproxy:stable)..."
docker pull ghcr.io/zaproxy/zaproxy:stable
success "OWASP ZAP image pulled: $(docker images ghcr.io/zaproxy/zaproxy:stable --format '{{.Repository}}:{{.Tag}} ({{.Size}})')"

# ── 5. Trivy ─────────────────────────────────────────────────────────────────
if command -v trivy &>/dev/null; then
    warn "Trivy already installed: $(trivy --version | head -1). Skipping."
else
    info "Adding Trivy apt repository..."
    sudo apt-get install -y wget apt-transport-https gnupg lsb-release

    wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key \
        | gpg --dearmor \
        | sudo tee /usr/share/keyrings/trivy.gpg > /dev/null

    echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] \
https://aquasecurity.github.io/trivy-repo/deb \
$(lsb_release -sc) main" \
        | sudo tee /etc/apt/sources.list.d/trivy.list > /dev/null

    sudo apt-get update -y
    sudo apt-get install -y trivy
    success "Trivy: $(trivy --version | head -1)"
fi

# ── 6. Node.js 24 + Newman ───────────────────────────────────────────────────
if node --version 2>/dev/null | grep -q "^v24"; then
    warn "Node.js 24 already installed: $(node --version). Skipping."
else
    info "Adding NodeSource repository for Node.js 24..."
    curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
    sudo apt-get install -y nodejs
    success "Node.js: $(node --version), npm: $(npm --version)"
fi

if command -v newman &>/dev/null; then
    warn "Newman already installed: $(newman --version). Skipping."
else
    info "Installing Newman..."
    sudo npm install -g newman
    success "Newman: $(newman --version)"
fi

# ── 7. Semgrep ───────────────────────────────────────────────────────────────
if command -v semgrep &>/dev/null || [[ -f "$HOME/.local/bin/semgrep" ]]; then
    warn "Semgrep already installed. Skipping."
else
    info "Installing Semgrep..."
    pip3 install --quiet --break-system-packages semgrep
    success "Semgrep installed."
fi

# ── 8. Gitleaks ──────────────────────────────────────────────────────────────
GITLEAKS_VERSION="8.30.1"
if command -v gitleaks &>/dev/null; then
    warn "Gitleaks already installed: $(gitleaks version). Skipping."
else
    info "Installing Gitleaks v${GITLEAKS_VERSION}..."
    GITLEAKS_PKG="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${GITLEAKS_PKG}" \
        -o "/tmp/${GITLEAKS_PKG}"
    sudo tar -xzf "/tmp/${GITLEAKS_PKG}" -C /usr/local/bin gitleaks
    rm -f "/tmp/${GITLEAKS_PKG}"
    success "Gitleaks: $(gitleaks version)"
fi

# ── 9. Nikto ─────────────────────────────────────────────────────────────────
if command -v nikto &>/dev/null; then
    warn "Nikto already installed. Skipping."
else
    info "Installing Nikto..."
    sudo apt-get install -y nikto
    success "Nikto installed."
fi

# ── 10. SQLMap ────────────────────────────────────────────────────────────────
if command -v sqlmap &>/dev/null; then
    warn "SQLMap already installed. Skipping."
else
    info "Installing SQLMap..."
    sudo apt-get install -y sqlmap
    success "SQLMap: $(sqlmap --version 2>&1 | head -1)"
fi

# ── 11. Prowler ───────────────────────────────────────────────────────────────
if [[ -f "$HOME/.local/bin/prowler" ]]; then
    warn "Prowler already installed. Skipping."
else
    info "Installing Prowler..."
    pip3 install --quiet --break-system-packages prowler
    success "Prowler installed."
    warn "Known conflict: Prowler may downgrade jsonschema below Semgrep's requirement (~4.25.1 vs 4.23.0). Both tools work fine from the CLI."
fi

# ── 12. ~/.local/bin in PATH ──────────────────────────────────────────────────
if grep -q 'LOCAL_BIN_PATH_SET' "$HOME/.bashrc" 2>/dev/null; then
    warn "~/.local/bin already in PATH (marker found). Skipping."
else
    info "Adding ~/.local/bin to PATH in ~/.bashrc..."
    {
        echo ''
        echo '# Added by step5-testing-deps.sh (LOCAL_BIN_PATH_SET)'
        echo 'export PATH="$PATH:$HOME/.local/bin"'
    } >> "$HOME/.bashrc"
    success "~/.local/bin added to PATH. Run 'source ~/.bashrc' or re-login to apply."
fi

# ── 13. GitHub Actions self-hosted runner ─────────────────────────────────────
RUNNER_DIR="$HOME/actions-runner"
RUNNER_VERSION="2.321.0"
RUNNER_ARCH="linux-x64"
RUNNER_PKG="actions-runner-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_PKG}"

if [[ -f "$RUNNER_DIR/config.sh" ]]; then
    warn "GitHub Actions runner already extracted at $RUNNER_DIR. Skipping download."
elif [[ -f "$RUNNER_DIR/run.sh" ]]; then
    warn "GitHub Actions runner already configured at $RUNNER_DIR."
else
    info "Creating runner directory at $RUNNER_DIR..."
    mkdir -p "$RUNNER_DIR"

    info "Downloading GitHub Actions runner v${RUNNER_VERSION}..."
    curl -fsSL "$RUNNER_URL" -o "/tmp/${RUNNER_PKG}"

    info "Extracting runner..."
    tar -xzf "/tmp/${RUNNER_PKG}" -C "$RUNNER_DIR"
    rm -f "/tmp/${RUNNER_PKG}"

    success "GitHub Actions runner v${RUNNER_VERSION} extracted to $RUNNER_DIR."
    echo ""
    echo -e "${YELLOW}=== MANUAL STEP REQUIRED ===${NC}"
    echo -e "${YELLOW}To register the runner with your GitHub repo, run:${NC}"
    echo -e "${CYAN}  cd ~/actions-runner${NC}"
    echo -e "${CYAN}  ./config.sh --url ${REPO_URL} --token <REGISTRATION_TOKEN>${NC}"
    echo ""
    echo -e "${YELLOW}Get the registration token from:${NC}"
    echo -e "${CYAN}  ${REPO_URL}/settings/actions/runners/new${NC}"
    echo ""
    echo -e "${YELLOW}After configuring, start the runner with:${NC}"
    echo -e "${CYAN}  cd ~/actions-runner && ./run.sh${NC}"
    echo -e "${YELLOW}Or install as a systemd service:${NC}"
    echo -e "${CYAN}  cd ~/actions-runner && sudo ./svc.sh install && sudo ./svc.sh start${NC}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Step 5: Testing Deps Complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "  pytest:      $(python3 -m pytest --version)"
echo -e "  playwright:  $(python3 -m playwright --version)"
echo -e "  locust:      $(locust --version)"
echo -e "  zap image:   $(docker images ghcr.io/zaproxy/zaproxy:stable --format '{{.Tag}}' 2>/dev/null || echo 'not found')"
echo -e "  trivy:       $(trivy --version 2>/dev/null | head -1 || echo 'not found')"
echo -e "  node:        $(node --version 2>/dev/null || echo 'not found')"
echo -e "  newman:      $(newman --version 2>/dev/null || echo 'not found')"
echo -e "  semgrep:     $(PATH=$PATH:$HOME/.local/bin semgrep --version 2>/dev/null || echo 'not found')"
echo -e "  gitleaks:    $(gitleaks version 2>/dev/null || echo 'not found')"
echo -e "  nikto:       $(nikto -Version 2>&1 | head -1 || echo 'not found')"
echo -e "  sqlmap:      $(sqlmap --version 2>&1 | head -1 || echo 'not found')"
echo -e "  prowler:     $(PATH=$PATH:$HOME/.local/bin prowler --version 2>/dev/null || echo 'not found')"
echo -e "  runner dir:  $RUNNER_DIR"
echo -e "${GREEN}============================================${NC}"
