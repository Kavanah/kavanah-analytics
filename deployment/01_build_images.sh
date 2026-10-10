#!/usr/bin/env bash
# =============================================================================
# | 01_build_images.sh — Build Docker image(s) and push to ECR
# -----------------------------------------------------------------------------
# Builds the de-imports image, tags it with the git SHA + `latest`, and
# (unless --local is given) pushes it to AWS ECR.
#
# Images built:
#   - de-imports   — Flask imports + asset management container
#                    (bin/python/Dockerfile)
#
# The reverse-proxy image (nginx-reverse-proxy) is intentionally NOT built
# here: its Dockerfile bakes per-VM credentials (.htpasswd from the elastic
# password in master-config.yaml) into the image at build time, so the image
# is not portable and cannot be pushed to a shared ECR. The nginx image is
# built and managed by deployment/step6-nginx-setup.sh on each VM. A future
# refactor to runtime-mount .htpasswd would let this script build it too.
#
# ECR registry is read from master-config.yaml (deploy.aws.*) or env vars.
# Env overrides take precedence:
#   ECR_REGISTRY   — e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com
#   AWS_REGION     — e.g. us-east-1
#   IMPORTS_REPO   — ECR repo name for de-imports (default: de-imports)
#
# Flags:
#   --local        — build the image only; skip ECR login and push
#   -h, --help     — show usage
#
# Idempotent: re-running rebuilds (Docker layer cache applies) and re-pushes.
# Pushing the same tag overwrites it in ECR — no duplicate tags.
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MASTER_CONFIG="${MASTER_CONFIG:-$REPO_DIR/config/master-config.yaml}"

IMPORTS_CONTEXT="$REPO_DIR/bin/python"
IMPORTS_DOCKERFILE="$IMPORTS_CONTEXT/Dockerfile"
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
LOCAL_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --local) LOCAL_ONLY=true ;;
        -h|--help) usage 0 ;;
        *) error "Unknown argument: $arg" ;;
    esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed."
command -v git    >/dev/null 2>&1 || error "git is not installed."
[[ -f "$IMPORTS_DOCKERFILE" ]] || error "Missing $IMPORTS_DOCKERFILE"

GIT_SHA=$(cd "$REPO_DIR" && git rev-parse --short=12 HEAD 2>/dev/null || echo "")
[[ -n "$GIT_SHA" ]] || error "Could not read git SHA from $REPO_DIR"

# ── Resolve ECR config (env overrides master-config.yaml) ─────────────────────
ECR_REGISTRY="${ECR_REGISTRY:-}"
AWS_REGION="${AWS_REGION:-}"
IMPORTS_REPO="${IMPORTS_REPO:-de-imports}"

if [[ -z "$ECR_REGISTRY" || -z "$AWS_REGION" ]] && [[ -f "$MASTER_CONFIG" ]] && command -v yq >/dev/null 2>&1; then
    [[ -z "$ECR_REGISTRY" ]] && ECR_REGISTRY=$(yq e '.deploy.aws.ecr_registry // ""' "$MASTER_CONFIG")
    [[ -z "$AWS_REGION"   ]] && AWS_REGION=$(yq e '.deploy.aws.region // ""' "$MASTER_CONFIG")
    IMPORTS_REPO=$(yq e ".deploy.images.imports_repo // \"$IMPORTS_REPO\"" "$MASTER_CONFIG")
fi

# ── Build ─────────────────────────────────────────────────────────────────────
info "| 01_build_images.sh"
info "Repo:    $REPO_DIR"
info "Git SHA: $GIT_SHA"
info "Mode:    $([[ "$LOCAL_ONLY" == true ]] && echo 'local (no ECR push)' || echo 'ECR push')"

build_image() {
    local name="$1" context="$2" dockerfile="$3"
    info "Building $name:$GIT_SHA (also tagged :latest)..."
    docker build \
        --tag "$name:$GIT_SHA" \
        --tag "$name:latest" \
        --file "$dockerfile" \
        "$context"
    # Docker BuildKit can exit 0 even when the build fails (e.g. missing
    # COPY source file surfaces as an error but non-zero exit is swallowed).
    # Verify the tagged image actually exists before declaring success.
    if ! docker image inspect "$name:$GIT_SHA" >/dev/null 2>&1; then
        error "Build of $name:$GIT_SHA failed — image not in local image store."
    fi
    success "Built $name:$GIT_SHA"
}

build_image "$IMPORTS_IMAGE_LOCAL" "$IMPORTS_CONTEXT" "$IMPORTS_DOCKERFILE"

if [[ "$LOCAL_ONLY" == true ]]; then
    success "Local-only build complete. Skipping ECR push."
    echo
    info "Local tags available:"
    echo "  $IMPORTS_IMAGE_LOCAL:$GIT_SHA"
    echo "  $IMPORTS_IMAGE_LOCAL:latest"
    exit 0
fi

# ── ECR push ──────────────────────────────────────────────────────────────────
[[ -n "$ECR_REGISTRY" ]] || error "ECR_REGISTRY not set (env var or deploy.aws.ecr_registry in master-config.yaml)"
[[ -n "$AWS_REGION"   ]] || error "AWS_REGION not set (env var or deploy.aws.region in master-config.yaml)"
command -v aws >/dev/null 2>&1 || error "AWS CLI not installed (required unless --local)."

info "ECR registry: $ECR_REGISTRY"
info "AWS region:   $AWS_REGION"
info "Repo:         $IMPORTS_REPO"

info "Logging in to ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null
success "ECR login OK"

push_to_ecr() {
    local local_name="$1" repo_name="$2"
    local remote="$ECR_REGISTRY/$repo_name"
    info "Tagging + pushing $local_name → $remote:{$GIT_SHA,latest}"
    docker tag "$local_name:$GIT_SHA" "$remote:$GIT_SHA"
    docker tag "$local_name:latest"   "$remote:latest"
    docker push "$remote:$GIT_SHA"
    docker push "$remote:latest"
    success "Pushed $remote:$GIT_SHA and :latest"
}

push_to_ecr "$IMPORTS_IMAGE_LOCAL" "$IMPORTS_REPO"

echo
success "Image built and pushed."
echo "  $ECR_REGISTRY/$IMPORTS_REPO:$GIT_SHA"
