#!/usr/bin/env bash
# =============================================================================
# | 06_rollback.sh — Roll the stack back to a prior image version
# -----------------------------------------------------------------------------
# Rolls the de-imports image back to a specified version and restarts the
# stack. Target is a git SHA (short or full) or a git tag that was published
# as an image tag by 01_build_images.sh.
#
# The nginx reverse-proxy image is intentionally out of scope here — it is
# built per-VM by deployment/step6-nginx-setup.sh, never pushed to ECR.
# See the header of 01_build_images.sh for why.
#
# Usage:
#   06_rollback.sh <target> [--local]
#
# Modes:
#   ECR (default) — pull $ECR_REGISTRY/<imports_repo>:<target>, re-tag as
#                   :latest locally, restart stack.
#   --local       — git checkout <target> in $REPO_DIR, run 01_build_images.sh
#                   --local to rebuild, restart stack. Requires a clean tree.
#
# Idempotent:
#   If the running de-imports container already matches the target image,
#   no pull / rebuild / restart is performed (still runs health check).
#
# After restart, invokes 05_health_check.sh automatically.
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
MASTER_CONFIG="${MASTER_CONFIG:-$CONFIG_DIR/master-config.yaml}"
COMPOSE_FILE="${COMPOSE_FILE:-$CONFIG_DIR/docker-compose.yml}"
HEALTH_CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/05_health_check.sh"
BUILD_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/01_build_images.sh"

FLASK_CONTAINER="${FLASK_CONTAINER:-imports_container}"
IMPORTS_IMAGE_LOCAL="de-imports"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
TARGET=""
LOCAL_MODE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)   LOCAL_MODE=true; shift ;;
        -h|--help) usage 0 ;;
        -*)        error "Unknown flag: $1" ;;
        *)         [[ -z "$TARGET" ]] || error "Only one target may be given"
                   TARGET="$1"; shift ;;
    esac
done
[[ -n "$TARGET" ]] || { echo "Usage: $0 <target> [--local]" >&2; exit 2; }

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed."
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin not available."
[[ -f "$COMPOSE_FILE" ]] || error "Compose file missing: $COMPOSE_FILE"
[[ -x "$HEALTH_CHECK" || -f "$HEALTH_CHECK" ]] || error "Missing $HEALTH_CHECK"

info "| 06_rollback.sh"
info "Target: $TARGET"
info "Mode:   $([[ "$LOCAL_MODE" == true ]] && echo 'local (git checkout + rebuild)' || echo 'ECR pull')"

# ── Detect currently-running image ────────────────────────────────────────────
# Returns the image ID the container was launched from, or empty if no such
# container exists.
running_image_id() {
    local name="$1"
    docker inspect --format '{{.Image}}' "$name" 2>/dev/null || true
}

# Returns the image ID for a local tag (empty if not found)
local_image_id() {
    local ref="$1"
    docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || true
}

CURRENT_IMPORTS_ID=$(running_image_id "$FLASK_CONTAINER")

# ── Local mode: git checkout + rebuild ────────────────────────────────────────
if [[ "$LOCAL_MODE" == true ]]; then
    command -v git >/dev/null 2>&1 || error "git is not installed."
    cd "$REPO_DIR"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        error "Working tree has uncommitted changes. Commit, stash, or discard first."
    fi
    # Early no-op: if the repo is already at the target commit and the running
    # container's image still exists locally, there is nothing to rebuild.
    # Rebuilding always produces a new image ID, so checking ID equivalence
    # after rebuild would never detect the no-op case.
    CURRENT_GIT_SHA=$(git rev-parse HEAD)
    RESOLVED_TARGET=$(git rev-parse "$TARGET" 2>/dev/null || echo "")
    if [[ -n "$RESOLVED_TARGET" && "$CURRENT_GIT_SHA" == "$RESOLVED_TARGET" && -n "$CURRENT_IMPORTS_ID" ]]; then
        success "Repo already at $TARGET and running container uses a matching image. No-op rollback."
        info "Running health check for verification..."
        bash "$HEALTH_CHECK"
        exit $?
    fi
    info "Checking out $TARGET in $REPO_DIR..."
    git checkout "$TARGET"
    info "Rebuilding images via 01_build_images.sh --local..."
    bash "$BUILD_SCRIPT" --local
    TARGET_IMPORTS_ID=$(local_image_id "$IMPORTS_IMAGE_LOCAL:latest")

# ── ECR mode: pull tagged images + re-tag as :latest ─────────────────────────
else
    command -v yq >/dev/null 2>&1 || error "yq is required (snap install yq)."
    ECR_REGISTRY="${ECR_REGISTRY:-$(yq e '.deploy.aws.ecr_registry // ""' "$MASTER_CONFIG" 2>/dev/null)}"
    AWS_REGION="${AWS_REGION:-$(yq e '.deploy.aws.region // ""' "$MASTER_CONFIG" 2>/dev/null)}"
    IMPORTS_REPO="${IMPORTS_REPO:-$(yq e '.deploy.images.imports_repo // "de-imports"' "$MASTER_CONFIG" 2>/dev/null)}"

    [[ -n "$ECR_REGISTRY" ]] || error "ECR_REGISTRY not set (env or deploy.aws.ecr_registry). Use --local for local rollback."
    [[ -n "$AWS_REGION"   ]] || error "AWS_REGION not set (env or deploy.aws.region)."
    command -v aws >/dev/null 2>&1 || error "AWS CLI not installed (required for ECR mode)."

    REMOTE_IMPORTS="$ECR_REGISTRY/$IMPORTS_REPO:$TARGET"

    # Skip the pull entirely if the running container already uses this image ID.
    TARGET_LOCAL_ID=$(local_image_id "$REMOTE_IMPORTS")
    if [[ -n "$TARGET_LOCAL_ID" && "$TARGET_LOCAL_ID" == "$CURRENT_IMPORTS_ID" ]]; then
        success "Target $TARGET is already the running version. No-op rollback."
        info "Running health check for verification..."
        bash "$HEALTH_CHECK"
        exit $?
    fi

    info "Logging in to ECR..."
    aws ecr get-login-password --region "$AWS_REGION" \
        | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null

    info "Pulling $REMOTE_IMPORTS..."
    docker pull "$REMOTE_IMPORTS"

    info "Re-tagging as :latest for compose..."
    docker tag "$REMOTE_IMPORTS" "$IMPORTS_IMAGE_LOCAL:latest"
    docker tag "$REMOTE_IMPORTS" "$IMPORTS_IMAGE_LOCAL:$TARGET"
    TARGET_IMPORTS_ID=$(local_image_id "$IMPORTS_IMAGE_LOCAL:latest")
fi

# ── Final no-op check (works for both modes) ──────────────────────────────────
if [[ -n "$TARGET_IMPORTS_ID" && "$TARGET_IMPORTS_ID" == "$CURRENT_IMPORTS_ID" ]]; then
    success "Running container already matches target image ID. No restart needed."
    info "Running health check for verification..."
    bash "$HEALTH_CHECK"
    exit $?
fi

# ── Restart the stack + wait for health ───────────────────────────────────────
# Force-recreate so compose picks up the re-tagged :latest image, then
# delegate the wait-for-health loop to 02_deploy_stack.sh so we don't
# duplicate its per-service polling logic.
info "Force-recreating containers to pick up new :latest tag..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate >/dev/null
DEPLOY_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/02_deploy_stack.sh"
info "Waiting for stack to become healthy via 02_deploy_stack.sh..."
bash "$DEPLOY_SCRIPT"
success "Stack restarted and healthy."

# ── Post-restart health check ─────────────────────────────────────────────────
echo
info "Running 05_health_check.sh for final verification..."
if bash "$HEALTH_CHECK"; then
    success "Rollback to $TARGET complete and healthy."
else
    error "Rollback to $TARGET completed but health check failed. Investigate before leaving."
fi
