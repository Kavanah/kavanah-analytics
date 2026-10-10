#!/usr/bin/env bash
# =============================================================================
# DEAM Server Setup
# Master script — runs all deployment steps in sequence.
#
# Prerequisites (must be in place BEFORE running this script):
# Full details: the private docs (private) → Prerequisites section.
#
#   1. Ubuntu 24.04 LTS with SSH access.
#
#   2. Passwordless sudo configured for your user:
#        echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/$USER-nopasswd
#        sudo chmod 440 /etc/sudoers.d/$USER-nopasswd
#
#   3. GitHub deploy key configured on this server (~/.ssh/id_ed25519):
#        ssh-keygen -t ed25519 -C "server-deploy" -f ~/.ssh/id_ed25519 -N ""
#        # Add the public key to your repo → Settings → Deploy keys
#        # (read-only is sufficient; leave "Allow write access" unchecked)
#
#   4. Domain name pointed to this server (DNS A record live and propagated):
#        # Verify: dig +short <your-domain> should return this server's IP
#
#   5. Let's Encrypt SSL certificate provisioned for your domain:
#        sudo apt-get install -y certbot
#        sudo certbot certonly --standalone -d <your-domain>
#        # For internal/Tailscale-only servers, use DNS-01 instead of standalone.
#
#   6. Hostname set in config/master-config.yaml (step3 creates this file):
#        After step3 runs, set the hostname before step6:
#          yq e -i '.reporting.hostname = "your-domain.com"' \
#            ~/kavanah-analytics/config/master-config.yaml
#
#   7. GitHub Actions runner registration token (manual — after step5 runs):
#        Get the token from your repo:
#          Settings → Actions → Runners → New self-hosted runner
#        Register after this script completes:
#          cd ~/actions-runner
#          ./config.sh --url <your-repo-url> --token <TOKEN>
#          sudo ./svc.sh install && sudo ./svc.sh start
#
# Usage:
#   bash deployment/server-setup.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MASTER_CONFIG="$HOME/kavanah-analytics/config/master-config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }
header()  {
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════${NC}"
    echo -e "${GREEN}  $*${NC}"
    echo -e "${GREEN}══════════════════════════════════════════${NC}"
}

[[ $EUID -eq 0 ]] && die "Do not run as root. Script uses sudo internally."

# ── Pre-flight checks ─────────────────────────────────────────────────────────
header "DEAM Server Setup — Pre-flight Checks"

# GitHub SSH key
if [[ ! -f "$HOME/.ssh/id_ed25519" && ! -f "$HOME/.ssh/id_rsa" ]]; then
    die "No GitHub SSH key found at ~/.ssh/id_ed25519 or ~/.ssh/id_rsa.\nGenerate one and add it to GitHub before running:\n  ssh-keygen -t ed25519 -C 'server-deploy'"
fi
success "GitHub SSH key found"

# Let's Encrypt certs — verify early if master-config already exists
if [[ -f "$MASTER_CONFIG" ]]; then
    HOSTNAME_VAL=$(yq e '.reporting.hostname' "$MASTER_CONFIG" 2>/dev/null || true)
    if [[ -n "$HOSTNAME_VAL" && "$HOSTNAME_VAL" != "null" ]]; then
        CERT_DIR="/etc/letsencrypt/live/${HOSTNAME_VAL}"
        if [[ ! -f "${CERT_DIR}/fullchain.pem" ]]; then
            die "Let's Encrypt cert not found for ${HOSTNAME_VAL}.\nRun: sudo certbot certonly --standalone -d ${HOSTNAME_VAL}"
        fi
        success "SSL cert found for ${HOSTNAME_VAL}"
    else
        warn "reporting.hostname not set in master-config.yaml — step6 will fail without it."
        warn "After step3 completes, set it with:"
        warn "  yq e -i '.reporting.hostname = \"your-domain.com\"' $MASTER_CONFIG"
        warn "Then re-run: bash deployment/step6-nginx-setup.sh"
    fi
else
    warn "master-config.yaml not found yet — will be created by step3."
    warn "After step3 completes, set reporting.hostname then re-run step6 if needed."
fi

success "Pre-flight checks complete. Starting deployment..."

# ── Step 1 & 2: Base System Setup ─────────────────────────────────────────────
header "Step 1 & 2: Base System Setup"
bash "$SCRIPT_DIR/step1-and-2-base-setup.sh"

# Docker group membership takes effect on next login; check access before continuing
if ! docker info &>/dev/null 2>&1; then
    echo ""
    warn "Docker is not accessible with your current session permissions."
    warn "Run the following, then re-run this script to continue from step3:"
    echo -e "${CYAN}  newgrp docker${NC}"
    exit 0
fi

# ── Step 3: DEAM Application Stack ────────────────────────────────────────────
header "Step 3: DEAM Application Stack"
bash "$SCRIPT_DIR/step3-app-stack.sh"

# ── Step 4: Seed Test Data ────────────────────────────────────────────────────
header "Step 4: Seed Test Data"
bash "$SCRIPT_DIR/step4-seed-test-data.sh"

# ── Step 5: Testing Dependencies ──────────────────────────────────────────────
header "Step 5: Testing Dependencies"
bash "$SCRIPT_DIR/step5-testing-deps.sh"

# ── Step 6: nginx Reverse Proxy (SSL) ─────────────────────────────────────────
header "Step 6: nginx Reverse Proxy (SSL)"

# Verify hostname is set in master-config before step6
if [[ -f "$MASTER_CONFIG" ]]; then
    HOSTNAME_VAL=$(yq e '.reporting.hostname' "$MASTER_CONFIG" 2>/dev/null || true)
    if [[ -z "$HOSTNAME_VAL" || "$HOSTNAME_VAL" == "null" ]]; then
        echo ""
        die "reporting.hostname is not set in master-config.yaml.\nSet it with:\n  yq e -i '.reporting.hostname = \"your-domain.com\"' $MASTER_CONFIG\nThen run: bash deployment/step6-nginx-setup.sh"
    fi
else
    die "master-config.yaml not found. Run step3-app-stack.sh first."
fi

bash "$SCRIPT_DIR/step6-nginx-setup.sh"

# ── Summary ───────────────────────────────────────────────────────────────────
HOSTNAME_VAL=$(yq e '.reporting.hostname' "$MASTER_CONFIG" 2>/dev/null || echo "<your-domain>")

echo ""
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo -e "${GREEN}  DEAM Server Setup Complete${NC}"
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo -e ""
echo -e "  Kibana:           https://${HOSTNAME_VAL}"
echo -e "  Asset Management: https://${HOSTNAME_VAL}/assets/"
echo -e "  FB OAuth:         https://${HOSTNAME_VAL}/fboauth/"
echo -e "  Elasticsearch:    https://${HOSTNAME_VAL}/elasticsearch/"
echo -e ""
echo -e "  Credentials:      ~/kavanah-analytics/config/master-config.yaml"
echo -e ""
echo -e "${YELLOW}══ MANUAL STEPS REMAINING ══${NC}"
echo -e ""
echo -e "${YELLOW}1. Register GitHub Actions self-hosted runner:${NC}"
echo -e "   Get token from your repo: Settings → Actions → Runners → New self-hosted runner"
echo -e "${CYAN}   cd ~/actions-runner${NC}"
echo -e "${CYAN}   ./config.sh --url <your-repo-url> --token <TOKEN>${NC}"
echo -e "${CYAN}   sudo ./svc.sh install && sudo ./svc.sh start${NC}"
echo -e ""
echo -e "${YELLOW}2. Import Kibana saved objects from staging (data views, dashboards):${NC}"
echo -e "${CYAN}   bash deployment/kibana-export.sh https://<staging-host> ~/kibana-export${NC}"
echo -e "${CYAN}   bash deployment/kibana-import.sh ~/kibana-export${NC}"
echo -e ""
echo -e "${GREEN}════════════════════════════════════════════${NC}"
