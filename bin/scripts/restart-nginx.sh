#!/bin/bash
# Restart the nginx-reverse-proxy container with fresh environment variables from master-config.yaml
# Handles missing SSL certs by running certbot automatically.
# Usage: bash /home/ubuntu/DE-Reporting/bin/scripts/restart-nginx.sh
# Note: Do NOT run with "sudo bash" - the script uses sudo only for docker commands.
#       Running the whole script as root breaks snap yq's file access confinement.

set -e

# Determine project root (parent of bin/)
ders_home=$(dirname "$(dirname "$(realpath "$0")")")
ders_home=$(dirname "$ders_home")
master_config="$ders_home/config/master-config.yaml"
conf_d="$ders_home/nginx-reverse-proxy/conf.d"
# SSL certs live at the OS-standard /etc/letsencrypt/ path (managed by the
# host's certbot or by km-infra on prod). We bind-mount that path into the
# container read-only so nginx can read whatever's there without needing its
# own copy.
host_ssl_dir="/etc/letsencrypt"

echo "=== Nginx Reverse Proxy Restart ==="
echo "Project root: $ders_home"
echo ""

# Check master-config.yaml exists
if [ ! -f "$master_config" ]; then
    echo "ERROR: master-config.yaml not found at $master_config"
    exit 1
fi

# Read configuration values (pipe via stdin to avoid snap yq file access issues)
nginx_api_key=$(cat "$master_config" | yq e '.nginx_es_api_key' - 2>/dev/null || echo "")
es_ip=$(cat "$master_config" | yq e '.elk-01.es01.internal_ip' - 2>/dev/null || echo "172.18.0.2")
reporting_hostname=$(cat "$master_config" | yq e '.reporting.hostname' - 2>/dev/null || echo "localhost")
fb_hostname=$(cat "$master_config" | yq e '.fboauth.hostname' - 2>/dev/null || echo "")
admin_email=$(cat "$master_config" | yq e '.server.admin_email' - 2>/dev/null || echo "")

# Treat yq "null" as empty
[ "$nginx_api_key" = "null" ] && nginx_api_key=""
[ "$es_ip" = "null" ] && es_ip="172.18.0.2"
[ "$reporting_hostname" = "null" ] && reporting_hostname="localhost"
[ "$fb_hostname" = "null" ] && fb_hostname=""
[ "$admin_email" = "null" ] && admin_email=""

# Fallback: if fboauth.hostname isn't set, derive it as fb.<reporting_hostname>
if [ -z "$fb_hostname" ]; then
    fb_hostname="fb.$reporting_hostname"
    echo "WARNING: fboauth.hostname not set in master-config.yaml; falling back to $fb_hostname"
fi

# Display configuration summary (mask secrets)
echo "Configuration from master-config.yaml:"
echo "  SERVER_NAME:            $reporting_hostname"
echo "  FB_SERVER_NAME:         $fb_hostname"
echo "  ES_HOST:                es01"
echo "  ES01_INTERNAL_IP:       $es_ip"
echo "  ADMIN_EMAIL:            ${admin_email:-<empty>}"
echo "  NGINX_ES_API_KEY:       ${nginx_api_key:+***set***}${nginx_api_key:-<empty>}"
echo ""

# Check if SSL certs exist. /etc/letsencrypt/live is typically mode 700
# (root-only), so use sudo test to check without weakening permissions.
ssl_cert="$host_ssl_dir/live/$reporting_hostname/fullchain.pem"
need_certbot=false
if ! sudo test -f "$ssl_cert"; then
    echo "SSL certs not found at: $ssl_cert"
    echo "Will start with HTTP-only config and run certbot to obtain new certs."
    echo ""
    need_certbot=true
fi

# If certs are missing, temporarily swap in an HTTP-only config so nginx can start
if [ "$need_certbot" = true ]; then
    echo "Backing up current default.conf..."
    cp "$conf_d/default.conf" "$conf_d/default.conf.ssl-backup"

    # Get kibana IP for the temporary config
    kibana_ip=$(sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kib01 2>/dev/null || echo "172.18.0.3")

    echo "Writing temporary HTTP-only config..."
    cat > "$conf_d/default.conf" <<HTTPCONF
server {
    listen 80;
    server_name $reporting_hostname;

    # Certbot webroot challenge directory
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://$kibana_ip:5601;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
HTTPCONF
fi

# Stop and remove existing container
echo "Stopping nginx-reverse-proxy container..."
sudo docker stop nginx-reverse-proxy 2>/dev/null || echo "  Container was not running"
echo "Removing nginx-reverse-proxy container..."
sudo docker rm nginx-reverse-proxy 2>/dev/null || echo "  Container did not exist"
echo ""

# Optional content mounts — match step6-nginx-setup.sh so static content
# stays reachable across restarts. Missing dirs -> the corresponding
# routes 404; present dirs -> mounted read-only.
docs_dir="$ders_home/docs/documentation/_build/html"
marketing_dir="/var/www/marketing"
docs_mount_args=()
marketing_mount_args=()
if [ -d "$docs_dir" ]; then
    docs_mount_args=(-v "$docs_dir:/var/www/deam-docs:ro")
    echo "Mounting docs from $docs_dir"
else
    echo "WARNING: docs dir $docs_dir not found — /docs/ will 404 until built"
fi
if [ -d "$marketing_dir" ]; then
    marketing_mount_args=(-v "$marketing_dir:/var/www/marketing:ro")
    echo "Mounting marketing from $marketing_dir"
else
    echo "WARNING: marketing dir $marketing_dir not found — / and /pricing will 404 until deployed"
fi

# Start new container with fresh env vars.
# SERVER_NAME/FB_SERVER_NAME/ES_HOST are consumed by the entrypoint's
# envsubst over default.conf.template. The legacy HOSTNAME/ES01_INTERNAL_IP/
# NGINX_ES_API_KEY vars are retained for any downstream tooling that may
# still read them.
echo "Starting new nginx-reverse-proxy container..."
sudo docker run --name nginx-reverse-proxy --net elastic-network -p 80:80 -p 443:443 \
    -e SERVER_NAME="$reporting_hostname" \
    -e FB_SERVER_NAME="$fb_hostname" \
    -e ES_HOST="es01" \
    -e NGINX_ES_API_KEY="$nginx_api_key" \
    -e ES01_INTERNAL_IP="$es_ip" \
    -e HOSTNAME="$reporting_hostname" \
    -v "$ders_home/nginx-reverse-proxy/nginx.conf:/usr/local/openresty/nginx/conf/nginx.conf:ro" \
    -v "$ders_home/nginx-reverse-proxy/conf.d:/etc/nginx/conf.d" \
    -v "$host_ssl_dir:/etc/letsencrypt:ro" \
    "${docs_mount_args[@]}" \
    "${marketing_mount_args[@]}" \
    -d nginx-reverse-proxy

sleep 5

# If certs were missing, run certbot then restore the SSL config and restart
if [ "$need_certbot" = true ]; then
    echo ""
    echo "=== Obtaining SSL Certificate ==="
    if [ -z "$admin_email" ]; then
        echo "ERROR: server.admin_email not set in master-config.yaml. Cannot run certbot."
        echo "       Restoring SSL config backup..."
        cp "$conf_d/default.conf.ssl-backup" "$conf_d/default.conf"
        exit 1
    fi

    # Use certbot webroot mode to avoid certbot trying to modify read-only nginx config.
    # The temporary HTTP config above serves /.well-known/acme-challenge/ from /var/www/certbot.
    sudo docker exec nginx-reverse-proxy mkdir -p /var/www/certbot

    sudo docker exec nginx-reverse-proxy certbot certonly --webroot -w /var/www/certbot \
        -d "$reporting_hostname" --non-interactive --agree-tos -m "$admin_email"

    echo ""
    echo "Restoring SSL config and restarting nginx..."
    cp "$conf_d/default.conf.ssl-backup" "$conf_d/default.conf"
    rm "$conf_d/default.conf.ssl-backup"
    sudo docker restart nginx-reverse-proxy
    sleep 5
fi

# Update container IP in master config
container_ip=$(sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' nginx-reverse-proxy)
cat "$master_config" | yq e ".elk-01.nginx_reverse_proxy.internal_ip = \"$container_ip\"" - > /tmp/master-config.yaml.tmp
sudo mv /tmp/master-config.yaml.tmp "$master_config"
sudo chown ubuntu:ubuntu "$master_config"

# Verify
echo ""
echo "=== Verification ==="
if sudo docker ps --filter "name=nginx-reverse-proxy" --format "{{.Status}}" | grep -q "Up"; then
    echo "Container is running (IP: $container_ip)"
else
    echo "ERROR: Container failed to start. Check logs:"
    echo "  sudo docker logs nginx-reverse-proxy --tail 50"
    exit 1
fi

echo ""
echo "Environment variables in container:"
sudo docker exec nginx-reverse-proxy env | grep -E "SERVER_NAME|FB_SERVER_NAME|ES_HOST|NGINX_ES_API_KEY|ES01_INTERNAL_IP|^HOSTNAME" | sed 's/\(KEY\)=.*/\1=***/'

echo ""
echo "Done!"
echo "  sudo docker logs nginx-reverse-proxy --tail 20"
echo "  curl -I https://$reporting_hostname/assets/"
