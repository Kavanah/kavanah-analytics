#!/bin/bash
# Step 6: nginx reverse proxy setup (HTTP + HTTPS with Let's Encrypt)
# Requires: Let's Encrypt certs already provisioned at /etc/letsencrypt/live/<domain>/
# Logic adapted from bin/scripts/install_nginx_reverse_proxy.sh,
# install_fb_auth_endpoints.sh, and install_asset_web.sh.
set -euo pipefail

DERS_HOME="$HOME/kavanah-analytics"
NGINX_DIR="$DERS_HOME/nginx-reverse-proxy"
CONFIG_FILE="$DERS_HOME/config/master-config.yaml"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ── Read credentials from master-config.yaml ─────────────────────────────────
ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$CONFIG_FILE")
[ -z "$ELASTIC_PASS" ] && die "Could not read elastic password from master-config.yaml"

REPORTING_HOSTNAME=$(yq e '.reporting.hostname' "$CONFIG_FILE")
[ -z "$REPORTING_HOSTNAME" ] && die "Could not read reporting.hostname from master-config.yaml"

FB_HOSTNAME=$(yq e '.fboauth.hostname' "$CONFIG_FILE")
if [[ -z "$FB_HOSTNAME" || "$FB_HOSTNAME" == "null" ]]; then
    die "Could not read fboauth.hostname from master-config.yaml"
fi

TEMPLATE_FILE="$DERS_HOME/deployment/nginx/conf.d/default.conf.template"
[ -f "$TEMPLATE_FILE" ] || die "nginx template not found at $TEMPLATE_FILE"

# Verify Let's Encrypt certs exist. /etc/letsencrypt/live is typically mode 700
# (root-only) on hardened installs, so `[[ -f ... ]]` from a non-root user would
# falsely report the files missing. Use `sudo test -f` to check through the
# permission barrier without weakening directory permissions.
CERT_DIR="/etc/letsencrypt/live/${REPORTING_HOSTNAME}"
if ! sudo test -f "${CERT_DIR}/fullchain.pem" || ! sudo test -f "${CERT_DIR}/privkey.pem"; then
    die "Let's Encrypt certs not found at ${CERT_DIR}. Run certbot first: certbot certonly --standalone -d ${REPORTING_HOSTNAME}"
fi
ok "Found Let's Encrypt certs for ${REPORTING_HOSTNAME}"

# ── Cleanup old install if present ───────────────────────────────────────────
echo "Cleaning up old nginx install if present..."
docker rm -f nginx-reverse-proxy 2>/dev/null && ok "Removed old container" || true
docker rmi -f nginx-reverse-proxy 2>/dev/null && ok "Removed old image" || true
rm -rf "$NGINX_DIR"

# ── Prepare directories ───────────────────────────────────────────────────────
echo "Preparing directories..."
mkdir -p "$NGINX_DIR/conf.d" "$NGINX_DIR/ssl"
ok "Directories ready"

# ── Generate .htpasswd ────────────────────────────────────────────────────────
echo "Generating .htpasswd..."
if ! command -v htpasswd &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq apache2-utils
fi
htpasswd -cb "$NGINX_DIR/.htpasswd" elastic "$ELASTIC_PASS"
ok "Generated .htpasswd"

# ── Assemble the build context from the canonical deployment/nginx/ ───────────
# there is ONE source of truth for the nginx image —
# deployment/nginx/ (Dockerfile + entrypoint.sh + nginx.conf + conf.d/
# default.conf.template + km-pw-reset.js). step6 used to write its OWN inline
# Dockerfile + nginx.conf via heredocs, which DRIFTED: the inline Dockerfile
# had no ENTRYPOINT and never installed gettext/certbot, so the built image
# did NOT re-render default.conf from the template on start. That silently
# broke the "force-recreate re-renders" guarantee on prod (a template
# change like didn't ship via --force-recreate; it had to be
# hand-rendered). We now copy the canonical context verbatim and add only the
# install-time secret (.htpasswd), so step6 can never drift from it again.
echo "Assembling nginx build context from $DERS_HOME/deployment/nginx/ ..."
NGINX_SRC="$DERS_HOME/deployment/nginx"
[ -f "$NGINX_SRC/Dockerfile" ] || die "canonical nginx Dockerfile not found at $NGINX_SRC/Dockerfile"
[ -f "$NGINX_SRC/entrypoint.sh" ] || die "canonical nginx entrypoint not found at $NGINX_SRC/entrypoint.sh"
cp "$NGINX_SRC/Dockerfile" "$NGINX_DIR/Dockerfile"
cp "$NGINX_SRC/nginx.conf" "$NGINX_DIR/nginx.conf"
cp "$NGINX_SRC/entrypoint.sh" "$NGINX_DIR/entrypoint.sh"
cp "$NGINX_SRC/km-pw-reset.js" "$NGINX_DIR/km-pw-reset.js"
cp "$NGINX_SRC/conf.d/default.conf.template" "$NGINX_DIR/conf.d/default.conf.template"
# ES allow-list: per-caller `allow <IP>;` files, kept out of the public
# template. Seed from the private submodule when present; always make the
# directory so the template's include glob has a home.
mkdir -p "$NGINX_DIR/conf.d/es-allow"
ES_ALLOW_SRC="$DERS_HOME/internal/deployment/nginx/es-allow"
if compgen -G "$ES_ALLOW_SRC/*.conf" > /dev/null; then
    cp "$ES_ALLOW_SRC"/*.conf "$NGINX_DIR/conf.d/es-allow/"
    ok "ES allow-list seeded from $ES_ALLOW_SRC"
else
    warn "No ES allow-list at $ES_ALLOW_SRC — /elasticsearch/ denies every caller until conf.d/es-allow/*.conf exists"
fi
ok "Build context assembled from $NGINX_SRC (single source of truth)"

# The container entrypoint renders default.conf from the template at START
# (envsubst SERVER_NAME/FB_SERVER_NAME/ES_HOST, sourced from deployment/.env by
# compose), so there is no build-time render here anymore — that is what makes
# `docker compose up -d --force-recreate nginx-reverse-proxy` pick up a template
# change. We still seed a rendered default.conf into the bind-mounted
# conf.d so the dir is non-empty before the first container start; the entrypoint
# overwrites it on boot.
echo "Seeding initial default.conf (entrypoint re-renders on every start)..."
if ! command -v envsubst &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq gettext-base
fi
SERVER_NAME="$REPORTING_HOSTNAME" \
FB_SERVER_NAME="$FB_HOSTNAME" \
ES_HOST=es01 \
envsubst '${SERVER_NAME} ${FB_SERVER_NAME} ${ES_HOST}' \
    < "$TEMPLATE_FILE" \
    > "$NGINX_DIR/conf.d/default.conf"
ok "default.conf seeded from $TEMPLATE_FILE"

# ── Build image ───────────────────────────────────────────────────────────────
# Tag matches the canonical compose file (image: nginx-reverse-proxy:local).
# `--no-cache` so a Dockerfile/entrypoint change or a template edit always
# produces a fresh image. Build is fast (~10s) so the cache miss is cheap
# insurance against stale layers.
echo "Building nginx-reverse-proxy:local image..."
docker build --no-cache -t nginx-reverse-proxy:local "$NGINX_DIR"
ok "Image built (tag: nginx-reverse-proxy:local)"

# ── Ensure compose bind-mount targets exist ──────────────────────────────────
# Docs (built by step7-build-docs.sh) and marketing site (rsync'd from the
# private the private docs submodule) are mounted by the canonical compose
# file. Compose fails to start a container when a bind-mount source is
# missing, so we touch the directories here. Empty dirs are fine — nginx
# serves 404 for routes whose content hasn't been deployed yet.
DOCS_DIR="$DERS_HOME/docs/documentation/_build/html"
MARKETING_DIR="/var/www/marketing"
mkdir -p "$DOCS_DIR"
sudo mkdir -p "$MARKETING_DIR"
sudo chown -R "$USER:$USER" "$MARKETING_DIR" 2>/dev/null || true

# Record the canonical nginx IP in master-config.yaml so other scripts that
# look it up (none currently — this is forward-compat with potential ops
# tooling) get the value compose will assign.
yq e -i ".elk-01.nginx_reverse_proxy.internal_ip = \"172.18.0.5\"" "$CONFIG_FILE"
ok "nginx IP (172.18.0.5) recorded in master-config.yaml"

# ── Hand-off ─────────────────────────────────────────────────────────────────
# step6 no longer launches the container or mutates docker-compose.yml.
# The canonical compose file (deployment/docker-compose.yml) declares
# nginx-reverse-proxy with the correct image tag, fixed IP, depends_on
# graph, and bind mounts. (2026-05-09): see DECISIONS.md
# for why the lifecycle moved out of step6.
echo ""
ok "nginx reverse proxy assets prepared"
echo "    Image:      nginx-reverse-proxy:local"
echo "    Config dir: $NGINX_DIR  (mounted into the container by compose)"
echo "    SSL cert:   /etc/letsencrypt/live/${REPORTING_HOSTNAME}/"
echo ""
echo "Public-facing routes (defined in $TEMPLATE_FILE):"
echo "    /                 → marketing site (static, /var/www/marketing)"
echo "    /pricing          → marketing site"
echo "    /features/<slug>  → marketing site"
echo "    /docs/            → Sphinx docs (/var/www/deam-docs)"
echo "    /dashboard        → Asset Management :8084"
echo "    /assets/          → Asset Management :8084"
echo "    /dashboards/      → Asset Management :8084"
echo "    /fboauth/         → FB OAuth :8083"
echo "    /elasticsearch/   → Elasticsearch :9200"
echo ""
echo -e "${YELLOW}Next step — start (or restart) the stack:${NC}"
echo "    bash deployment/02_deploy_stack.sh"
echo ""
if [[ -z "$(ls -A "$DOCS_DIR" 2>/dev/null)" ]]; then
    warn "Docs dir is empty — run 'bash deployment/step7-build-docs.sh' to build and populate /docs/"
fi
if [[ -z "$(ls -A "$MARKETING_DIR" 2>/dev/null)" ]]; then
    warn "Marketing dir is empty — / redirects to /assets/ and /pricing etc. 404 until marketing content is deployed (optional; private the private docs)"
fi
