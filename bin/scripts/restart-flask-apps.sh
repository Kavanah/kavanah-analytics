#!/bin/bash
# Restart Flask web applications after code changes
# Usage: sudo docker exec imports_container /app/scripts/restart-flask-apps.sh
#
# Note: `set -e` is intentionally NOT used. `pkill` returns non-zero when no
# matching processes exist (normal on a fresh container), and other commands
# (ss | grep) are expected to non-match in the failure-reporting block. The
# secret-validation block below uses explicit `|| { ... exit 1; }` instead.

echo "Restarting Flask applications..."

# Kill existing processes
echo "Stopping existing Flask processes..."
pkill -f "gunicorn.*fb_oauth_server:app" 2>/dev/null
pkill -f "gunicorn.*app:app" 2>/dev/null
pkill -f "python3 app.py" 2>/dev/null
sleep 2

# Start Facebook OAuth server on port 8083
echo "Starting Facebook OAuth server..."
cd /app/python
gunicorn -b 0.0.0.0:8083 fb_oauth_server:app \
    --workers 2 \
    --timeout 120 \
    --daemon \
    --pid /tmp/fb_oauth.pid \
    --access-logfile /tmp/fb_oauth_access.log \
    --error-logfile /tmp/fb_oauth_error.log \
    --log-level info

# Start Asset Management web app on port 8084
# refuse to start with a missing/weak SECRET_KEY rather than
# warning. Validation lives in asset_management.web.secret_validation so
# the rules stay in sync with app.py's import-time check.
echo "Starting Asset Management app..."
SECRET_KEY=$(python3 - <<'PY'
import sys, yaml
sys.path.insert(0, "/app/python")
from asset_management.web.secret_validation import (
    InvalidSecretError,
    validate_secret_key,
)

with open("/app/reporting_configs/master-config.yaml") as f:
    cfg = yaml.safe_load(f) or {}

value = (cfg.get("asset_web") or {}).get("flask_secret_key")
try:
    validate_secret_key(value, name="asset_web.flask_secret_key")
except InvalidSecretError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

print(value)
PY
) || {
    echo "ERROR: SECRET_KEY validation failed — refusing to start Asset Management app." >&2
    exit 1
}
export SECRET_KEY

# ES01_PASSWORD is exported by the container env (set by step3 / docker-compose).
# Validate it before starting any app that depends on ES auth.
python3 - <<PY || { echo "ERROR: ES01_PASSWORD validation failed." >&2; exit 1; }
import os, sys
sys.path.insert(0, "/app/python")
from asset_management.web.secret_validation import (
    InvalidSecretError,
    validate_secret_key,
)
try:
    validate_secret_key(os.environ.get("ES01_PASSWORD"), name="ES01_PASSWORD")
except InvalidSecretError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PY

cd /app/python/asset_management/web
gunicorn -b 0.0.0.0:8084 app:app \
    --workers 2 \
    --timeout 120 \
    --daemon \
    --pid /tmp/asset_web.pid \
    --access-logfile /tmp/asset_web_access.log \
    --error-logfile /tmp/asset_web_error.log \
    --log-level info

# Start Password Reset app on port 8085
echo "Starting Password Reset app..."
export ES_PASS=$ES01_PASSWORD
cd /app/python/password_reset
gunicorn -b 0.0.0.0:8085 app:app \
    --workers 2 \
    --timeout 120 \
    --daemon \
    --pid /tmp/pw_reset.pid \
    --access-logfile /tmp/pw_reset_access.log \
    --error-logfile /tmp/pw_reset_error.log \
    --log-level info

# Wait for services to start
sleep 3

# Verify services are running
echo ""
echo "Status check:"
if ss -tlnp | grep -q 8083; then
    echo "✓ Facebook OAuth server running on port 8083"
else
    echo "✗ Facebook OAuth server failed to start"
    echo "Check logs: cat /tmp/fb_oauth_error.log"
fi

if ss -tlnp | grep -q 8084; then
    echo "✓ Asset Management app running on port 8084"
else
    echo "✗ Asset Management app failed to start"
    echo "Check logs: cat /tmp/asset_web_error.log"
fi

if ss -tlnp | grep -q 8085; then
    echo "✓ Password Reset app running on port 8085"
else
    echo "✗ Password Reset app failed to start"
    echo "Check logs: cat /tmp/pw_reset_error.log"
fi

echo ""
echo "Done! Services restarted."
