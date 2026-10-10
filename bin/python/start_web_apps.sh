#!/bin/bash
echo "Starting Flask applications..."

# tests/conftest.py and a few CLI tools resolve the project root from
# their own location and look for ``bin/python`` underneath. The
# container mounts the host's bin/python at /app/python (see
# deployment/step3-app-stack.sh), so /app/bin/python doesn't exist
# unless we create it. Link it once at startup so pytest run from
# the testing page (KAN-XXX) can import asset_management cleanly.
mkdir -p /app/bin
ln -sfn /app/python /app/bin/python

# Default KIBANA_URL to container-to-container address if not set.
# The external URL (https://<public-host>/kibana) fails from inside
# the container — always use the Docker network name for internal API calls.
# Must include /kibana basePath — Kibana rewrites all requests to require it.
export KIBANA_URL="${KIBANA_URL:-http://kib01:5601/kibana}"
echo "KIBANA_URL=${KIBANA_URL}"

# Dependencies should be baked into the Docker image.
# If running on a base image without them, fall back to pip install.
python3 -c "import flask" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "WARNING: Dependencies not found in image. Installing from requirements.txt..."
    echo "Consider rebuilding the image: docker build -t de-imports:latest -f bin/python/Dockerfile bin/python/"
    pip install -q --require-hashes -r /app/python/requirements.txt 2>&1 | tail -1  # hashed lockfile
fi

# graceful shutdown.
# This script is PID 1 in imports_container, so it owns SIGTERM. Without a
# trap, `docker stop` (and the auto-SIGTERM Docker sends during host reboot)
# would hit the 10s grace, get SIGKILL'd, and exit 137 — which Docker then
# treats as a "clean stop" and skips `restart: always` on the next daemon
# start. That cost prod ~10h of downtime on 2026-05-08.
child_pids=()
forward_term() {
    echo "Received SIGTERM — forwarding to gunicorn masters: ${child_pids[*]}"
    # Send SIGTERM to each gunicorn master. Each master will SIGTERM its own
    # workers and drain in-flight requests before exiting (gunicorn's
    # default graceful_timeout is 30s, but Docker only waits 10s — workers
    # that don't drain in time get SIGKILL'd by their masters during exit).
    for pid in "${child_pids[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
}
trap forward_term TERM INT

# logs live under /app/data/logs, NOT /tmp. /app/data is the
# host-mounted import-data directory (host: ~/kavanah-analytics/import-data),
# which is where the importers already write theirs — so app logs now SURVIVE a
# container recreate. Previously they sat in the container's /tmp and were wiped
# on every recreate; the open-signup investigation could only see back
# to the last container rebuild, and would have had no evidence at all a few
# days later.
#
# mkdir -p is load-bearing: gunicorn exits immediately if it cannot open its
# --error-logfile, so a missing directory would fail the whole container start
# on a fresh box.
#
# NOTE: retention comes from the host logrotate rule (km-infra,
# /etc/logrotate.d/deam-app-logs). Persisting logs without rotation would just
# trade a self-clearing file for an unbounded one.
mkdir -p /app/data/logs

# -capture-output on every gunicorn below. Without it gunicorn
# writes only its OWN records to --error-logfile; the Flask apps' stdout/stderr
# (logging.* via the default stderr handler, print, tracebacks outside the
# logging module) go nowhere — the error logs sit frozen at container start
# while the apps log correct diagnoses nobody can read (cost ~1h on the
# SSO staging rollout; the only workaround was `docker attach` while
# reproducing live).

# Start Facebook OAuth server on port 8083
echo "Starting Facebook OAuth server..."
cd /app/python
gunicorn -b 0.0.0.0:8083 fb_oauth_server:app \
    --workers 2 \
    --timeout 120 \
    --access-logfile /app/data/logs/fb_oauth_access.log \
    --error-logfile /app/data/logs/fb_oauth_error.log \
    --capture-output \
    --log-level info &
child_pids+=($!)

# Start Asset Management web app on port 8084
echo "Starting Asset Management app..."
# TRUST_NGINX_HEADERS is no longer force-enabled here. The old
# unconditional export assumed "nginx is the only path to Flask",
# but docker-compose published :8084 to the host, so the assumption did not
# hold and any client could forge X-Remote-User/X-Remote-Roles. The config
# default is false; a deployment that genuinely fronts Flask with a
# header-asserting proxy must set TRUST_NGINX_HEADERS=true *plus*
# TRUSTED_PROXY_IPS and PROXY_AUTH_SECRET in its environment — verify_auth
# now refuses the headers without the proven proxy hop. (The live SSO is
# Entra OAuth; nothing currently produces these headers.)
cd /app/python/asset_management/web
# -threads switches gunicorn to the gthread worker (2x8 = 16
# concurrent request slots). The workload is ES-I/O-bound, so sync workers
# capped the whole app at 2 in-flight requests — every dashboard fires one
# /api/query per chart in parallel, and all users share the same 2 slots.
gunicorn -b 0.0.0.0:8084 app:app \
    --workers 2 \
    --threads 8 \
    --timeout 120 \
    --access-logfile /app/data/logs/asset_web_access.log \
    --error-logfile /app/data/logs/asset_web_error.log \
    --capture-output \
    --log-level info &
child_pids+=($!)

# Start Password Reset app on port 8085
echo "Starting Password Reset app..."
export ES_PASS=$ES01_PASSWORD
cd /app/python/password_reset
gunicorn -b 0.0.0.0:8085 app:app \
    --workers 2 \
    --timeout 120 \
    --access-logfile /app/data/logs/pw_reset_access.log \
    --error-logfile /app/data/logs/pw_reset_error.log \
    --capture-output \
    --log-level info &
child_pids+=($!)

# Wait on the gunicorn masters instead of `tail -f /dev/null`. `wait` is
# bash's only built-in that processes traps while blocked — `tail` would
# swallow SIGTERM until it returned (which it never does), defeating the
# trap above. The first `wait -n` returns when ANY child exits; we then
# `wait` for the rest so the script doesn't return while gunicorn is
# still draining.
echo "Web apps started. Waiting on gunicorn masters: ${child_pids[*]}"
wait -n
wait
