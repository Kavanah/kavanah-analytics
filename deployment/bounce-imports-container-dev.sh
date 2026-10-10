#!/bin/bash
#
# bounce-imports-container-dev.sh — bounce imports_container on dev/staging
# via docker compose.
#
# WHY a separate wrapper:
#   step3-app-stack.sh does the full host bootstrap (image build, ES
#   first-boot certs, kibana.yml, .env writing) — too heavy for "just
#   bounce imports_container" use case. start-imports-container.sh is
#   the prod variant and pulls AWS credentials from SSM, which
#   dev/staging neither has nor needs.
#
# WHAT it does:
#   Runs `docker compose up -d --force-recreate imports_container` against
# the existing deployment/.env. Recreate is fast (~5s);
#   SIGTERM trap means the old container drains cleanly within Docker's
#   10s grace.
#
# WHEN to run:
#   - Any time imports_container needs a recreate on dev/staging without
#     re-running step3-app-stack.sh.
#
# NOT for prod. The prod variant (start-imports-container.sh) injects
# AWS credentials. If imports_container's current env has
# AWS_ACCESS_KEY_ID set, this script refuses to overwrite it.
#
# History:
# (2026-05-09): moved from a bespoke `docker run` to
#   `docker compose up -d --force-recreate` so the compose file is the
#   single declarative source of truth for env / mounts / network /
#   restart policy.
# (2026-05-28): removed the USE_NEW_PERMISSIONS / _SHADOW env
#   exports + container-side verification. Both flags were retired with
#   the cutover; the wrapper no longer needs to pass anything through.

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# Try staging path first, fall back to prod-style. Same pattern step3 uses.
REPO_DIR="${REPO_DIR:-$HOME/DE-Reporting}"
[[ -d "$REPO_DIR" ]] || REPO_DIR="$HOME/kavanah-analytics"
DEPLOY_DIR="$REPO_DIR/deployment"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
COMPOSE_ENV="$DEPLOY_DIR/.env"
FLASK_CONTAINER="imports_container"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo "[INFO]    $*"; }
success() { echo "[SUCCESS] $*"; }
error()   { echo "[ERROR]   $*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "docker not found"
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin missing."

[[ -d "$REPO_DIR" ]] || error "Repo directory not found: $REPO_DIR"
[[ -f "$COMPOSE_FILE" ]] || error "Compose file not found at $COMPOSE_FILE — run step3-app-stack.sh first."
[[ -f "$COMPOSE_ENV" ]] || error "Compose env file not found at $COMPOSE_ENV — run step3-app-stack.sh first."

# Refuse to overwrite a prod-style container (one with AWS creds in env).
if docker ps -a --format '{{.Names}}' | grep -qw "$FLASK_CONTAINER"; then
    if docker inspect "$FLASK_CONTAINER" --format '{{json .Config.Env}}' | grep -q AWS_ACCESS_KEY_ID; then
        error "imports_container has AWS_ACCESS_KEY_ID set — looks like the prod variant. Use start-imports-container.sh instead, not this dev wrapper."
    fi
fi

# ── Recreate via compose ─────────────────────────────────────────────────────
# `--force-recreate` so the container picks up any env changes even if
# the compose file itself hasn't changed.
info "Recreating $FLASK_CONTAINER..."
docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV" \
    up -d --force-recreate "$FLASK_CONTAINER"

success "$FLASK_CONTAINER recreated."
