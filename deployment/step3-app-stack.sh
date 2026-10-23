#!/usr/bin/env bash
# =============================================================================
# Step 3: DEAM Application Stack Bootstrap
# Target: Ubuntu 24.04 LTS
# Run as: bash step3-app-stack.sh
# =============================================================================
# What this script does:
#   1. Installs prerequisites (yq, vm.max_map_count for Elasticsearch)
#   2. Generates .env.test with test-only credentials (no production secrets)
#   3. Creates master-config.yaml seeded from .env.test
#   4. Creates the elastic-network Docker bridge
#   5. ES first-boot bootstrap (ONE-TIME per host): launches es01 via docker
#      run so ES auto-config can write the auto-generated certs / yml /
#      keystore, then docker-cps those out to the host so the canonical
#      compose file (deployment/docker-compose.yml) can bind-mount them
#      back in on every subsequent launch. Skipped on re-run if the host
#      already has the persisted security state.
#   6. Writes kibana.yml (Kibana's runtime config, mounted by compose)
#   7. Builds the de-imports:latest image (compose references it as :latest)
#   8. Ensures DOCS_DIR / MARKETING_DIR / .env.aws exist so compose bind
#      mounts always succeed (empty placeholders are fine — nginx serves
#      404 from empty content dirs)
#   9. Writes deployment/.env with all the variables the canonical compose
#      file substitutes from
#  10. Hands off: instructs the operator to run deployment/02_deploy_stack.sh
#      to start the stack via docker compose up -d
#
# What this script no longer does (2026-05-09 migration, see DECISIONS.md):
#   - Launch kib01 / imports_container / nginx-reverse-proxy via docker run
#   - Generate a docker-compose.yml from a heredoc (the canonical compose
#     file lives at deployment/docker-compose.yml in git)
#   - Run health checks (02_deploy_stack.sh does this with proper polling)
#
# SECURITY NOTES:
#   - .env.test is generated with random test credentials only
#   - .env.test must NEVER contain production OAuth tokens or API keys
#   - deployment/.env is gitignored — it contains substituted secrets
#     read from master-config.yaml at deploy time
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
ES_VERSION="9.3.2"
KIBANA_VERSION="9.3.2"
DEAM_IMAGE="de-imports:latest"
# Default to ~/kavanah-analytics (production layout). Staging clones into
# ~/DE-Reporting; export REPO_DIR=~/DE-Reporting before running this script
# on staging to keep the imports_container bind mounts pointing at the same
# directory you `git pull` into.
REPO_DIR="${REPO_DIR:-$HOME/kavanah-analytics}"
CONFIG_DIR="$REPO_DIR/config"
ELK_DIR="$REPO_DIR/elk-01"
IMPORT_DATA_DIR="$REPO_DIR/import-data"
SNAPSHOT_DIR="$REPO_DIR/es-snapshots"
NGINX_DIR="$REPO_DIR/nginx-reverse-proxy"
# DOCS_DIR / MARKETING_DIR mirror the values step6 + step7 use. We touch
# the directories here so compose bind mounts succeed even on a minimal
# install where docs haven't been built and marketing content hasn't
# been deployed.
DOCS_DIR="$REPO_DIR/docs/documentation/_build/html"
MARKETING_DIR="/var/www/marketing"
MASTER_CONFIG="$CONFIG_DIR/master-config.yaml"
ENV_TEST="$REPO_DIR/.env.test"
# deployment/.env is the compose-time variable substitution source. Keys
# match ${VAR} placeholders in deployment/docker-compose.yml. Gitignored.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_ENV="$DEPLOY_DIR/.env"
COMPOSE_AWS_ENV="$DEPLOY_DIR/.env.aws"

# Shared secret validator. Sourced relative to this
# script so step3 can be invoked from any cwd.
# shellcheck source=lib/validate-secret.sh
source "${DEPLOY_DIR}/lib/validate-secret.sh"

# Per-service memory limits. ES is intentionally un-limited so the container
# can use up to the host's available RAM, and ES's built-in heap auto-sizing
# picks a reasonable -Xmx based on container memory (roughly 50% of RAM up to
# ~31 GB). The old 1.3 GB limit + hardcoded 512 MB JVM heap severely
# under-tuned ES on any real workload.
KIB_MEM_LIMIT="1g"
FLASK_MEM_LIMIT="1g"
ES_CONTAINER="es01"
DOCKER_NETWORK="elastic-network"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# stamp a file we just wrote with the checkout's ownership.
# Files written here are later rewritten in place by tooling that swaps the
# inode (yq -i, sed -i, and the mktemp+mv in start-secrets-container.sh), so the
# owner tracks whoever ran the writer rather than whoever owned the file. A
# root-owned 600 deployment/.env is unreadable by the deploy user and forces
# sudo on every later compose/wrapper call — the blast radius. Taking
# the owner from $REPO_DIR (never $USER/$SUDO_USER, which under sudo depend on
# env_reset/env_keep) keeps every generated file matching the checkout. The
# chown is non-fatal: a bootstrap must not abort because of it.
stamp_owner() {
    local target="$1" owner
    owner="$(stat -c '%U:%G' "$REPO_DIR" 2>/dev/null || stat -f '%Su:%Sg' "$REPO_DIR" 2>/dev/null || true)"
    if [[ -z "$owner" ]]; then
        warn "could not read the owner of $REPO_DIR — leaving $target owned by the invoking user"
        return 0
    fi
    chown "$owner" "$target" 2>/dev/null \
        || warn "could not chown $target to $owner — confirm the deploy user can still read it"
    return 0
}

# ── Preflight ─────────────────────────────────────────────────────────────────
info "Starting Step 3 — DEAM bootstrap on $(hostname)"
info "Date: $(date)"

[[ $EUID -eq 0 ]] && error "Do not run as root. Script uses sudo internally."
[[ ! -d "$REPO_DIR" ]] && error "Repo not found at $REPO_DIR. Run Step 2 first."
command -v docker &>/dev/null || error "Docker not installed. Run Step 2 first."
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin missing. Install docker-compose-plugin."

# ── 1. Install yq ─────────────────────────────────────────────────────────────
if command -v yq &>/dev/null; then
    success "yq already installed: $(yq --version)"
else
    info "Installing yq via snap..."
    sudo snap install yq
    success "yq installed: $(yq --version)"
fi

# ── 2. Increase vm.max_map_count (required for Elasticsearch) ─────────────────
CURRENT_MAP=$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)
if [[ "$CURRENT_MAP" -ge 262144 ]]; then
    success "vm.max_map_count already set to $CURRENT_MAP"
else
    info "Setting vm.max_map_count=262144 (required for Elasticsearch)..."
    sudo sysctl -w vm.max_map_count=262144
    if grep -q "^vm.max_map_count" /etc/sysctl.conf 2>/dev/null; then
        sudo sed -i 's/^vm.max_map_count.*/vm.max_map_count=262144/' /etc/sysctl.conf
    else
        echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
    fi
    success "vm.max_map_count set to 262144"
fi

# ── 3. Generate .env.test ─────────────────────────────────────────────────────
if [[ -f "$ENV_TEST" ]]; then
    warn ".env.test already exists at $ENV_TEST. Skipping generation."
    warn "Delete it and re-run to regenerate test credentials."
else
    info "Generating .env.test with test credentials..."

    TEST_ENC_KEY=$(openssl rand -base64 32)
    TEST_SECRET_KEY=$(openssl rand -hex 32)

    cat > "$ENV_TEST" << EOF
# =============================================================================
# Test Environment Configuration
# Generated: $(date)
# WARNING: Test credentials only — NEVER add production secrets here
# WARNING: .env.test is gitignored and must not be committed
# =============================================================================

# Encryption key for Python Fernet (base64-encoded 32 bytes)
ENC_KEY=${TEST_ENC_KEY}

# Flask secret key
FLASK_SECRET_KEY=${TEST_SECRET_KEY}

# Elasticsearch connection (populated after ES container starts)
ES01_INTERNAL_IP=
ES01_PASSWORD=
KIBANA_TOKEN=

# =============================================================================
# INTENTIONALLY OMITTED (add only for sandboxed OAuth testing, NEVER production):
# FB_APP_ID=
# FB_APP_SECRET=
# FB_ACCESS_TOKEN=
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=
# ECHO_API_KEY=
# =============================================================================
EOF
    stamp_owner "$ENV_TEST"
    success ".env.test created at $ENV_TEST"
fi

# Source .env.test into current shell
set -a; source "$ENV_TEST"; set +a

# ── 4. Set up master-config.yaml ──────────────────────────────────────────────
info "Preparing config directory and master-config.yaml..."
mkdir -p "$CONFIG_DIR"

# Read a master-config value, normalizing yq's "null" (missing key) to "" so it
# composes cleanly with env-first defaults (${ENV:-$(_yq_or_empty ...)}) and the
# seed guards below. A literal "null" fallback would otherwise
# look like a real value and slip past the env-first precedence.
_yq_or_empty() { local v; v=$(yq e "$1" "$MASTER_CONFIG" 2>/dev/null); [[ "$v" == "null" ]] && v=""; printf '%s' "$v"; }

if [[ ! -f "$MASTER_CONFIG" ]]; then
    info "Creating master-config.yaml..."
    cat > "$MASTER_CONFIG" << EOF
server:
  ip_address: "192.168.56.102"
  admin_email: "test@localhost"
reporting:
  hostname: "localhost"
fboauth:
  hostname: "localhost"
enc_key: "${ENC_KEY}"
EOF
    stamp_owner "$MASTER_CONFIG"
    success "master-config.yaml created"
else
    warn "master-config.yaml already exists. Skipping creation."
fi

# Ensure enc_key is set — but NOT on a Phase-3 stripped host. The seed
# only fires when the secret is absent from BOTH master-config AND the env. On an
# SSM-sourced host the env var is present, so step3 treats the (intentionally
# stripped) YAML value as "already set" and does NOT regenerate it. Regenerating
# would (a) un-strip master-config and (b) install a WRONG enc_key that orphans
# every connector token — the seed path must never run against a live system.
EXISTING_ENC=$(_yq_or_empty '.enc_key')
if [[ -z "$EXISTING_ENC" && -z "${ENC_KEY:-}" ]]; then
    yq e -i ".enc_key = \"${ENC_KEY}\"" "$MASTER_CONFIG"
fi

# Ensure asset_web.flask_secret_key is set. Without this, yq returns
# the literal string "null" for a missing key and Flask boots with a forgeable
# session key — root cause of the 2026-04-23 prod incident. Same env-aware seed
# guard as enc_key: skip the regenerate when FLASK_SECRET_KEY is in the
# env (Phase-3 stripped + SSM-sourced) so step3 doesn't write a fresh secret back
# into a stripped master-config.
EXISTING_FLASK_SECRET=$(_yq_or_empty '.asset_web.flask_secret_key')
if [[ -z "$EXISTING_FLASK_SECRET" && -z "${FLASK_SECRET_KEY:-}" ]]; then
    info "Generating asset_web.flask_secret_key (32 bytes hex)..."
    NEW_FLASK_SECRET=$(openssl rand -hex 32)
    yq e -i ".asset_web.flask_secret_key = \"${NEW_FLASK_SECRET}\"" "$MASTER_CONFIG"
    success "asset_web.flask_secret_key seeded in master-config.yaml"
fi

# Seed s3_backup defaults. Dev points at elastic-stack-dev/. Prod
# overrides this block to elastic-stack-prod via its own master-config — the
# prefix km-infra's backup-elk-*.sh write to.
EXISTING_S3_BUCKET=$(yq e '.s3_backup.bucket' "$MASTER_CONFIG")
if [[ -z "$EXISTING_S3_BUCKET" || "$EXISTING_S3_BUCKET" == "null" ]]; then
    yq e -i '.s3_backup.bucket = "km-wp-backups"' "$MASTER_CONFIG"
fi
EXISTING_S3_PREFIX=$(yq e '.s3_backup.prefix' "$MASTER_CONFIG")
if [[ -z "$EXISTING_S3_PREFIX" || "$EXISTING_S3_PREFIX" == "null" ]]; then
    yq e -i '.s3_backup.prefix = "elastic-stack-dev"' "$MASTER_CONFIG"
fi

# ── 5. Create elastic Docker network ──────────────────────────────────────────
# Network is `external: true` in the canonical compose file — compose
# attaches but doesn't recreate. step3 owns the network's lifecycle so the
# 172.18.0.0/16 subnet survives `compose down` (which would otherwise
# rotate IPs and break the fixed-IP assumptions in master-config.yaml).
if docker network ls | grep -qw "$DOCKER_NETWORK"; then
    success "Docker network '$DOCKER_NETWORK' already exists"
else
    info "Creating Docker network: $DOCKER_NETWORK..."
    docker network create --subnet=172.18.0.0/16 --gateway=172.18.0.1 "$DOCKER_NETWORK"
    success "Docker network '$DOCKER_NETWORK' created (subnet 172.18.0.0/16)"
fi

# ── 6. Elasticsearch first-boot bootstrap (one-time per host) ────────────────
# ES 9.x with discovery.type=single-node + ELASTIC_PASSWORD auto-configures
# TLS and security on first launch — but only when the data dir is empty.
# We launch via `docker run` here so we can `docker cp` the auto-generated
# certs / elasticsearch.yml / keystore out to the host before compose ever
# sees the container. Compose on subsequent runs mounts those host files
# back in, so ES never re-runs auto-config.
#
# Detection: if the persisted security state (http_ca.crt) already exists
# on the host, the bootstrap has already run and we skip this block.
# Compose will own lifecycle from here on.
wait_for_es() {
    local pass="$1"
    local retries=60
    info "Polling Elasticsearch API (up to 5 min)..."
    for i in $(seq 1 $retries); do
        STATUS=$(curl -sk -u "elastic:${pass}" \
            "https://localhost:9200/_cluster/health" 2>/dev/null | \
            grep -o '"status":"[^"]*"' | cut -d'"' -f4 || true)
        if [[ "$STATUS" == "green" || "$STATUS" == "yellow" ]]; then
            success "Elasticsearch is up (status: $STATUS)"
            return 0
        fi
        echo "  waiting... attempt ${i}/${retries}"
        sleep 5
    done
    error "Elasticsearch did not become healthy in time. Check: docker logs $ES_CONTAINER"
}

ES_HOST_CERT="$ELK_DIR/es01/config/certs/http_ca.crt"
if [[ -f "$ES_HOST_CERT" ]]; then
    success "ES host security state already extracted (skipping first-boot bootstrap)"
elif docker ps -a | grep -qw "$ES_CONTAINER"; then
    # Container exists but we haven't extracted security state yet — unusual.
    # Most likely the operator started es01 some other way. Bail rather than
    # racing.
    error "$ES_CONTAINER exists but $ES_HOST_CERT does not. Manual intervention needed: extract certs from the running container or remove it and re-run step3."
else
    # First-boot path. Generate fresh credentials, launch ES via docker run,
    # wait for healthy, copy security state out, then stop the container.
    # Compose takes over from here.
    ELASTIC_PASS=$(openssl rand -hex 16)
    KIBANA_SYS_PASS=$(openssl rand -hex 16)

    info "Pulling Elasticsearch $ES_VERSION image (this may take a few minutes)..."
    docker pull "docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}"

    mkdir -p "$ELK_DIR/es01/data" "$ELK_DIR/es01/config" "$SNAPSHOT_DIR"
    # ES container runs as uid/gid 1000. Snapshot dir must be writable by
    # that uid so _snapshot/repo registration and backup writes work.
    sudo chown -R 1000:1000 "$SNAPSHOT_DIR" 2>/dev/null || chown -R 1000:1000 "$SNAPSHOT_DIR"

    info "Starting Elasticsearch $ES_VERSION (one-time bootstrap, fixed IP 172.18.0.2)..."
    # the bootstrap container is loopback-published — wait_for_es and
    # the kibana_system password call below curl localhost:9200 from this host,
    # and nothing else needs it before compose replaces this container.
    docker run --name "$ES_CONTAINER" \
        --net "$DOCKER_NETWORK" \
        --ip "172.18.0.2" \
        -p 127.0.0.1:9200:9200 \
        -e "discovery.type=single-node" \
        -e "ELASTIC_PASSWORD=${ELASTIC_PASS}" \
        -e "path.repo=/usr/share/elasticsearch/snapshots" \
        -v "$ELK_DIR/es01/data:/usr/share/elasticsearch/data" \
        -v "$SNAPSHOT_DIR:/usr/share/elasticsearch/snapshots" \
        -d "docker.elastic.co/elasticsearch/elasticsearch:${ES_VERSION}"

    wait_for_es "$ELASTIC_PASS"

    info "Copying entire ES config dir to host (whole-dir copy needed because compose mounts the parent dir, not individual files — see docker-compose.yml)..."
    mkdir -p "$ELK_DIR/es01/config"
    # `:/path/.` (trailing /.) copies the directory contents into the host
    # destination, not the directory itself. We need everything: certs/,
    # elasticsearch.yml, elasticsearch.keystore, AND all the image's default
    # files (jvm.options, log4j2.properties, roles.yml, role_mapping.yml,
    # users, users_roles, service_tokens, log4j2.file.properties). Without
    # those defaults in the host dir, the bind mount would shadow them with
    # nothing and ES would fail to start with missing-config errors.
    docker cp "$ES_CONTAINER:/usr/share/elasticsearch/config/." \
        "$ELK_DIR/es01/config/"

    # Persist path.repo in elasticsearch.yml so compose-recreated containers
    # keep snapshot support. Env vars set in `docker run` above don't survive
    # a compose recreate — the mounted yml is the source of truth.
    if ! grep -q "^path.repo:" "$ELK_DIR/es01/config/elasticsearch.yml"; then
        info "Adding path.repo to elasticsearch.yml for snapshot support..."
        echo "" | sudo tee -a "$ELK_DIR/es01/config/elasticsearch.yml" >/dev/null
        echo "# Snapshot repository path (mounted from host \$SNAPSHOT_DIR)" | sudo tee -a "$ELK_DIR/es01/config/elasticsearch.yml" >/dev/null
        echo "path.repo: [\"/usr/share/elasticsearch/snapshots\"]" | sudo tee -a "$ELK_DIR/es01/config/elasticsearch.yml" >/dev/null
    fi

    # Set kibana_system password via ES API
    info "Setting kibana_system password..."
    curl -sk -u "elastic:${ELASTIC_PASS}" \
        -X POST "https://localhost:9200/_security/user/kibana_system/_password" \
        -H "Content-Type: application/json" \
        -d "{\"password\":\"${KIBANA_SYS_PASS}\"}" | grep -q '"acknowledged":\|^{}' || \
    warn "kibana_system password may not have been set — check ES API manually"

    # Stop the bootstrap container. 02_deploy_stack.sh will recreate it
    # under compose with the proper labels / restart policy / healthcheck.
    info "Stopping bootstrap es01 — compose will recreate it on first deploy..."
    docker stop "$ES_CONTAINER" >/dev/null
    docker rm "$ES_CONTAINER" >/dev/null

    # Save credentials and metadata to master-config.yaml.
    yq e -i ".elk-01.es01.admin_user = \"elastic\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.es01.admin_pass = \"${ELASTIC_PASS}\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.es01.kibana_sys_pass = \"${KIBANA_SYS_PASS}\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.es01.internal_ip = \"172.18.0.2\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.es01.elastic_version = \"${ES_VERSION}\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.es01.container_memory_limit = \"auto\"" "$MASTER_CONFIG"

    sed -i "s|^ES01_INTERNAL_IP=.*|ES01_INTERNAL_IP=172.18.0.2|" "$ENV_TEST"
    sed -i "s|^ES01_PASSWORD=.*|ES01_PASSWORD=${ELASTIC_PASS}|" "$ENV_TEST"

    success "Elasticsearch bootstrap complete (security state persisted to $ELK_DIR/es01/config/)"
fi

# Read credentials env-first, falling back to master config.
# Once Phase 3 strips secret VALUES from master-config (keeping the keys), the
# YAML reads return "" and these resolve from the SSM-injected environment
# instead. Pre-source the secrets before running step3 on an SSM-sourced host:
#   SSM_ENV=<env> SSM_PROFILE=<...> SSM_SUDO=<...> \
#     source deployment/lib/source-ssm-secrets.sh && bash deployment/step3-app-stack.sh
# (or export ENC_KEY/FLASK_SECRET_KEY/ES_ADMIN_PASS/KIBANA_SYS_PASS/GITHUB_TOKEN
#  by hand). The validate_secret guards below turn a forgotten source into a
# loud abort, never a blank secret silently baked into .env / kibana.yml.
# (_yq_or_empty is defined once near section 4 — it's used by the seed guards too.)
ELASTIC_PASS="${ES_ADMIN_PASS:-$(_yq_or_empty '.elk-01.es01.admin_pass')}"
KIBANA_SYS_PASS="${KIBANA_SYS_PASS:-$(_yq_or_empty '.elk-01.es01.kibana_sys_pass')}"

# ── 7. Write kibana.yml ──────────────────────────────────────────────────────
# kibana.yml is mounted by compose; it's the canonical Kibana config surface.
# Generated here (not in compose) because it depends on master-config values
# (publicBaseUrl from reporting.hostname, kibana_system credentials).
KIBANA_YML="$REPO_DIR/kibana.yml"
if [[ -f "$KIBANA_YML" ]]; then
    success "kibana.yml already exists at $KIBANA_YML (will not overwrite)"
else
    info "Pulling Kibana $KIBANA_VERSION image (cached on subsequent runs)..."
    docker pull "docker.elastic.co/kibana/kibana:${KIBANA_VERSION}" >/dev/null

    info "Writing kibana.yml..."
    # Phase 3: kibana_system password is baked LITERALLY into kibana.yml
    # (Kibana does NOT do ${VAR} substitution on its config file, and we don't use
    # the keystore). So the value is always a literal here — but its SOURCE is now
    # env-first (KIBANA_SYS_PASS resolved above), so a master-config with the value
    # stripped still produces a working kibana.yml when the SSM env is sourced.
    # Guard: refuse to bake a blank/short password (a forgotten source, or a
    # stripped YAML with no env) rather than write `password: ""` and silently
    # break Kibana ES auth — the failure mode the staging incident hit.
    validate_secret "$KIBANA_SYS_PASS" "elk-01.es01.kibana_sys_pass (env KIBANA_SYS_PASS)" || \
        error "Refusing to write kibana.yml with an invalid kibana_system password. On an SSM-sourced host, source the secrets first (see the env-first note above section 7)."
    SECURITY_KEY=$(openssl rand -base64 32)
    SAVED_OBJECTS_KEY=$(openssl rand -base64 32)
    REPORTING_KEY=$(openssl rand -base64 32)

    REPORTING_HOSTNAME=$(yq e '.reporting.hostname' "$MASTER_CONFIG" 2>/dev/null || echo "")
    if [[ -n "$REPORTING_HOSTNAME" && "$REPORTING_HOSTNAME" != "null" ]]; then
        KIBANA_PUBLIC_URL="https://${REPORTING_HOSTNAME}"
    else
        KIBANA_PUBLIC_URL="http://localhost:5601"
        warn "reporting.hostname not set — using localhost for Kibana publicBaseUrl."
        warn "After setting reporting.hostname, delete $KIBANA_YML and re-run step3."
    fi

    cat > "$KIBANA_YML" << EOF
server.name: kib01
server.host: "0.0.0.0"
server.basePath: "/kibana"
server.rewriteBasePath: true
server.publicBaseUrl: "${KIBANA_PUBLIC_URL}/kibana"
elasticsearch.hosts: ["https://172.18.0.2:9200"]
elasticsearch.username: "kibana_system"
elasticsearch.password: "\${KIBANA_SYS_PASS}"
elasticsearch.ssl.verificationMode: certificate
elasticsearch.ssl.certificateAuthorities: ["/usr/share/kibana/config/certs/http_ca.crt"]
xpack.security.encryptionKey: "${SECURITY_KEY}"
xpack.encryptedSavedObjects.encryptionKey: "${SAVED_OBJECTS_KEY}"
xpack.reporting.encryptionKey: "${REPORTING_KEY}"
xpack.security.session.idleTimeout: "8h"
xpack.security.session.lifespan: "30d"
xpack.security.session.cleanupInterval: "1h"
xpack.security.authc.providers:
  basic.basic1:
    order: 0
EOF
    stamp_owner "$KIBANA_YML"

    yq e -i ".elk-01.kib01.internal_ip = \"172.18.0.3\"" "$MASTER_CONFIG"
    yq e -i ".elk-01.kib01.kibana_version = \"${KIBANA_VERSION}\"" "$MASTER_CONFIG"
    success "kibana.yml written to $KIBANA_YML"
fi

# ── 8. Build the de-imports image ─────────────────────────────────────────────
# Compose references this image by tag (de-imports:latest). We build here
# rather than letting compose's `build:` block do it because step3 is the
# documented "I want a working host" entry point and the image build is a
# heavy operation we don't want to re-trigger on every `compose up -d`.
info "Building $DEAM_IMAGE from Dockerfile..."
docker build -t "$DEAM_IMAGE" -f "$REPO_DIR/bin/python/Dockerfile" "$REPO_DIR/bin/python/" >/dev/null
success "$DEAM_IMAGE built"

# Belt-and-braces secret validation. Even though we just seeded
# the secret, validate it before writing it into deployment/.env where
# compose will pick it up. Env-first: once master-config has
# the VALUE stripped, these resolve from the SSM-injected env; validate_secret
# then guards whatever resolved, so a stripped YAML with no sourced env aborts
# loudly instead of writing a blank/"null" secret into .env.
LATEST_FLASK_SECRET="${FLASK_SECRET_KEY:-$(_yq_or_empty '.asset_web.flask_secret_key')}"
LATEST_ES_PASS="$ELASTIC_PASS"   # resolved env-first in section 7
validate_secret "$LATEST_FLASK_SECRET" "asset_web.flask_secret_key (env FLASK_SECRET_KEY)" || \
    error "Refusing to write deployment/.env with invalid SECRET_KEY. Set it in master-config.yaml, or source the SSM secrets first (env-first, see section 7)."
validate_secret "$LATEST_ES_PASS" "elk-01.es01.admin_pass (env ES_ADMIN_PASS)" || \
    error "Refusing to write deployment/.env with invalid ES password. Set it in master-config.yaml, or source the SSM secrets first (env-first, see section 7)."

# ── 9. Ensure compose bind-mount targets exist ───────────────────────────────
# Compose fails to start a container if a bind-mount source path doesn't
# exist on the host. We touch the optional content dirs here so a minimal
# install (no docs built, no marketing content deployed) doesn't break the
# stack — nginx serves 404 from empty dirs, which matches the pre-compose
# behavior of "skip the mount if dir absent".
mkdir -p "$DOCS_DIR"
sudo mkdir -p "$MARKETING_DIR"
sudo chown -R "$USER:$USER" "$MARKETING_DIR" 2>/dev/null || true

# imports_container runs as uid/gid 1000 (not root), so the
# writable mounts must belong to that uid, and the logo-uploads dir must exist
# before compose (a missing bind source is created root-owned, which the app
# user then cannot write).
mkdir -p "$IMPORT_DATA_DIR/uploads/logos" "$IMPORT_DATA_DIR/password_reset_config"
sudo chown -R 1000:1000 "$IMPORT_DATA_DIR" "$CONFIG_DIR" 2>/dev/null \
    || warn "could not chown $IMPORT_DATA_DIR / $CONFIG_DIR to 1000:1000 — imports_container cannot write them"

# .env.aws is read by compose for imports_container's env_file: directive.
# On prod, deployment/start-imports-container.sh writes real values here
# from SSM on every bounce. On dev / fresh installs, an empty placeholder
# is fine — Flask falls back gracefully when AWS_ACCESS_KEY_ID is unset.
if [[ ! -f "$COMPOSE_AWS_ENV" ]]; then
    cat > "$COMPOSE_AWS_ENV" << 'EOF'
# Placeholder for AWS credentials — populated by
# deployment/start-imports-container.sh on prod, empty on dev/staging.
# Compose's env_file: requires this file to exist; empty is fine.
EOF
    stamp_owner "$COMPOSE_AWS_ENV"
fi

# ── 10. Write deployment/.env for compose substitution ───────────────────────
# This file is the variable source for deployment/docker-compose.yml. Every
# ${VAR} placeholder in compose resolves from here. earlier silent
# overwrites are no longer a concern: this file is a reduction of master-
# config.yaml + step3-derived paths, regenerated deterministically.
# Env-first. enc_key is the master secret; if it resolves
# blank here (stripped YAML + no sourced env) the wrapper would later inject it,
# but a blank enc_key in .env between step3 and the wrapper run would orphan
# every connector token — so guard it like the other secrets. github token is
# optional (Security-tab CI panel only), so it stays best-effort (no guard).
COMP_ENC="${ENC_KEY:-$(_yq_or_empty '.enc_key')}"
COMP_GITHUB_TOKEN="${GITHUB_TOKEN:-$(_yq_or_empty '.github.read_only_token')}"
validate_secret "$COMP_ENC" "enc_key (env ENC_KEY)" || \
    error "Refusing to write deployment/.env with an invalid enc_key — it decrypts every connector token. Set it in master-config.yaml, or source the SSM secrets first (env-first, see section 7)."

# nginx render vars. The nginx-reverse-proxy entrypoint re-renders
# default.conf from the template via `envsubst '${SERVER_NAME} ${FB_SERVER_NAME}
# ${ES_HOST}'` on EVERY container start. Before this, the compose service passed
# none of them, so a `compose up -d --force-recreate nginx-reverse-proxy`
# re-rendered BLANK server_names and downed the site. Emit them here (same source
# as step6-nginx-setup.sh) so compose can pass them and --force-recreate becomes
# the safe, blessed way to pick up template changes.
COMP_SERVER_NAME=$(yq e '.reporting.hostname' "$MASTER_CONFIG" 2>/dev/null || echo "")
COMP_FB_SERVER_NAME=$(yq e '.fboauth.hostname' "$MASTER_CONFIG" 2>/dev/null || echo "")
# fboauth.hostname is optional; fall back to fb.<reporting-hostname> like step6.
if [[ -z "$COMP_FB_SERVER_NAME" || "$COMP_FB_SERVER_NAME" == "null" ]]; then
    COMP_FB_SERVER_NAME="fb.${COMP_SERVER_NAME}"
fi
if [[ -z "$COMP_SERVER_NAME" || "$COMP_SERVER_NAME" == "null" ]]; then
    # No hostname yet (fresh/dev bootstrap): leave blank rather than guess. The
    # persisted default.conf from a prior step6 still serves; a --force-recreate
    # before reporting.hostname is set is not a supported prod path.
    COMP_SERVER_NAME=""
    warn "reporting.hostname not set — SERVER_NAME left blank in .env (set it + re-run step3 before relying on --force-recreate for nginx)."
fi

# TEST_AUTH_SECRET enables the X-Test-Persona header-auth path used by
# ProofShot verification (see bin/python/asset_management/web/auth.py). It is
# generated fresh on every step3 run (rotates the secret; re-establishing a
# persona session is cheap) but ONLY on an explicitly-recognized non-prod host.
#
# ALLOW-LIST, not deny-list (fail closed). The secret grants an MFA-bypass
# session, so the default MUST be EMPTY (path inert) for anything we don't
# positively recognize as non-prod: an unknown or mistyped hostname, a new prod
# domain, or the empty-hostname bootstrap case above must NOT get a live backdoor
# secret. Generate it only when DEPLOY_ENV explicitly says non-prod, OR (when
# DEPLOY_ENV is unset) COMP_SERVER_NAME matches a known non-prod host (anchored,
# no wildcards that a prod domain could satisfy). Same fail-closed posture as
# generate-attack-surface-env.sh refusing a prod base URL.
DEPLOY_ENV="${DEPLOY_ENV:-}"
COMP_TEST_AUTH_SECRET=""
if [[ "$DEPLOY_ENV" == "prod" || "$DEPLOY_ENV" == "production" ]]; then
    info "DEPLOY_ENV=$DEPLOY_ENV — TEST_AUTH_SECRET left EMPTY (header-auth path inert)."
elif [[ "$DEPLOY_ENV" == "dev" || "$DEPLOY_ENV" == "staging" || "$DEPLOY_ENV" == "test" ]]; then
    COMP_TEST_AUTH_SECRET=$(openssl rand -hex 32)
    info "DEPLOY_ENV=$DEPLOY_ENV — generated a fresh TEST_AUTH_SECRET for the header-auth verification path."
elif [[ -z "$DEPLOY_ENV" ]]; then
    # No explicit env marker — fall back to an ANCHORED non-prod hostname
    # allow-list. Anything not on it (prod, unknown, or empty) stays EMPTY.
    # Named staging hosts are deliberately NOT listed (no org hostnames
    # in the tree) — set DEPLOY_ENV=staging on those boxes.
    case "$COMP_SERVER_NAME" in
        *.ts.net|localhost|127.0.0.1)
            COMP_TEST_AUTH_SECRET=$(openssl rand -hex 32)
            info "Recognized non-prod host ($COMP_SERVER_NAME) — generated a fresh TEST_AUTH_SECRET."
            ;;
        "")
            warn "SERVER_NAME empty and DEPLOY_ENV unset — TEST_AUTH_SECRET left EMPTY (fail closed). Set DEPLOY_ENV=staging to enable the header-auth path."
            ;;
        *)
            info "Host '$COMP_SERVER_NAME' not on the non-prod allow-list and DEPLOY_ENV unset — TEST_AUTH_SECRET left EMPTY (fail closed)."
            ;;
    esac
else
    error "Unknown DEPLOY_ENV='$DEPLOY_ENV' — refusing to generate TEST_AUTH_SECRET. Use dev|staging|test|prod."
fi

info "Writing $COMPOSE_ENV..."
cat > "$COMPOSE_ENV" << EOF
# =============================================================================
# Compose variable substitution source
# Generated by step3-app-stack.sh on $(date)
# Do not edit by hand — re-run step3 after changing master-config.yaml.
# Gitignored. Contains substituted secrets from master-config.yaml.
# =============================================================================

# Container image versions
ES_VERSION=${ES_VERSION}
KIBANA_VERSION=${KIBANA_VERSION}
DEAM_IMAGE=${DEAM_IMAGE}

# Memory limits (per service)
KIB_MEM_LIMIT=${KIB_MEM_LIMIT}
FLASK_MEM_LIMIT=${FLASK_MEM_LIMIT}

# Host paths (everything bind-mounted into containers)
REPO_DIR=${REPO_DIR}
ELK_DIR=${ELK_DIR}
SNAPSHOT_DIR=${SNAPSHOT_DIR}
IMPORT_DATA_DIR=${IMPORT_DATA_DIR}
CONFIG_DIR=${CONFIG_DIR}
NGINX_DIR=${NGINX_DIR}
DOCS_DIR=${DOCS_DIR}
MARKETING_DIR=${MARKETING_DIR}

# nginx render vars — the nginx-reverse-proxy entrypoint envsubst's
# these into default.conf on every start. Passed via compose so --force-recreate
# re-renders correctly instead of blanking server_name.
SERVER_NAME=${COMP_SERVER_NAME}
FB_SERVER_NAME=${COMP_FB_SERVER_NAME}
ES_HOST=es01

# Header-auth test persona domain. Personas are real ES users named
# test-<slug>@<domain>; web/auth.py and the seed tools both read this. Blank = the
# neutral default (example.com). Set it to the domain your personas were seeded under.
TEST_PERSONA_DOMAIN=${TEST_PERSONA_DOMAIN:-}

# Extra production hostnames, comma-separated. lib/prod_hosts.py adds
# them to the built-in product domain for every refuse-prod guard (seed tools,
# import load tests, live security suite). Blank = built-in default only.
PROD_HOSTNAMES=${PROD_HOSTNAMES:-}

# Public hostname of this deployment — only used by
# bin/python/scripts/check_oauth_tokens.py to print full token-refresh URLs.
# Defaults to reporting.hostname; blank means the script prints relative paths.
APP_PUBLIC_HOSTNAME=${APP_PUBLIC_HOSTNAME:-$COMP_SERVER_NAME}

# Explicit deploy marker. Carried into imports_container so the app's
# refuse-only startup gate can fail a prod container that has TEST_AUTH_SECRET
# set. Same DEPLOY_ENV that decided COMP_TEST_AUTH_SECRET above; blank is fine.
DEPLOY_ENV=${DEPLOY_ENV}

# Cold self-signup switch. false = only
# invited visitors can create accounts. Flip to true only per the
# reopening decision; compose passes it straight into imports_container.
SELF_SIGNUP_ENABLED=${SELF_SIGNUP_ENABLED:-false}

# TikTok connector switch. Blank = off; set true to turn the connector
# on. Compose passes it into imports_container, and 03_provision_cron_jobs.sh
# reads it from this file to decide whether to install the TikTok nightly line.
TIKTOK_ENABLED=${TIKTOK_ENABLED:-}

# Global import queue. IMPORT_GLOBAL_CAP = max importer processes at
# once; blank or 0 = no cap (off). ~4 per 2 vCPU (prod 4 vCPU = 8).
# Over-cap runs wait FIFO for up to IMPORT_QUEUE_MAX_WAIT_SECONDS (blank = 12 h),
# re-checking every IMPORT_QUEUE_POLL_SECONDS (blank = 5).
IMPORT_GLOBAL_CAP=${IMPORT_GLOBAL_CAP:-}
IMPORT_QUEUE_MAX_WAIT_SECONDS=${IMPORT_QUEUE_MAX_WAIT_SECONDS:-}
IMPORT_QUEUE_POLL_SECONDS=${IMPORT_QUEUE_POLL_SECONDS:-}

# Application secrets (substituted into imports_container env)
ES_ADMIN_PASS=${LATEST_ES_PASS}
ENC_KEY=${COMP_ENC}
# retiring Fernet key(s) during a rotation window; blank otherwise.
# Populated only by start-secrets-container.sh from SSM enc-key-previous.
ENC_KEY_PREVIOUS=${ENC_KEY_PREVIOUS:-}
FLASK_SECRET_KEY=${LATEST_FLASK_SECRET}

# Kibana system password — substituted into the kib01 container env, which
# Kibana then expands into kibana.yml's elasticsearch.password (Phase 3,
# so the password is not a plaintext literal in kibana.yml). Resolved env-first
# in section 7 (KIBANA_SYS_PASS).
KIBANA_SYS_PASS=${KIBANA_SYS_PASS}

# USE_NEW_PERMISSIONS + USE_NEW_PERMISSIONS_SHADOW retired
# (2026-05-28). The new-authz read path is unconditional; this script
# no longer writes either variable into deployment/.env.

# Optional: enables the Security tab's CI Pipeline Status panel.
# Set github.read_only_token in master-config.yaml to populate.
GITHUB_TOKEN=${COMP_GITHUB_TOKEN}

# AWS region for the Restore-from-Backup feature. Credentials live in
# deployment/.env.aws (written by start-imports-container.sh on prod).
AWS_REGION=us-west-2

# X-Test-Persona header-auth secret. Random on non-prod, EMPTY on
# prod (path inert). Regenerated each step3 run. See auth.py verify_auth.
TEST_AUTH_SECRET=${COMP_TEST_AUTH_SECRET}
EOF
chmod 600 "$COMPOSE_ENV"
stamp_owner "$COMPOSE_ENV"
success "$COMPOSE_ENV written (mode 600)"

# ── 11. Hand-off ─────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Step 3 — DEAM Bootstrap Complete${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "  Repo:          $REPO_DIR"
echo -e "  Master config: $MASTER_CONFIG"
echo -e "  Compose env:   $COMPOSE_ENV"
echo -e "  ES certs:      $ELK_DIR/es01/config/certs/"
echo -e "  Kibana config: $KIBANA_YML"
echo -e "  Snapshots:     $SNAPSHOT_DIR  (mounted at /usr/share/elasticsearch/snapshots inside es01)"
echo ""
echo -e "${YELLOW}Next step — start the stack:${NC}"
echo -e "  bash deployment/02_deploy_stack.sh"
echo ""
echo -e "${YELLOW}If you also want the public-facing nginx reverse proxy:${NC}"
echo -e "  bash deployment/step6-nginx-setup.sh   (build nginx image + render config)"
echo -e "  bash deployment/02_deploy_stack.sh     (re-run to bring nginx up)"
echo -e "${GREEN}============================================================${NC}"
