#!/usr/bin/env bash
# =============================================================================
# Container watchdog
# =============================================================================
# Checks that the four expected DEAM containers are running. Recovers any
# that are stopped, in a restart loop, unhealthy — or,, deliberately
# removed but still defined in the compose file.
#
# Recovery modes (in order of escalation):
#   1. docker start  — for exited / paused / dead / created containers.
#   2. docker compose up -d <service>  — for entirely-missing containers that
#      the compose file at deployment/docker-compose.yml still defines.
# Added after the prod deploy outage where step6
#      destroyed nginx-reverse-proxy and the watchdog couldn't re-provision
#      it. The watchdog used to error out on missing containers; now it
#      escalates to `compose up` IF the compose file defines the service.
#
# Why not always `docker compose up`: down-and-up destroys ephemeral state.
# The watchdog still prefers `docker start` for existing-but-stopped
# containers; `compose up` is the fallback for the missing-entirely case
# that `docker start` literally cannot fix.
#
# Designed to run from a systemd timer every 2 minutes. Logs to stdout/stderr;
# systemd handles journald routing.
#
# Exit codes:
#   0  All containers healthy or auto-recovered.
#   1  One or more containers in a state the watchdog cannot fix.
#      (eg container missing AND not defined in compose, or repeated
#      start/compose failures.) systemd marks the unit failed; alert hooks
#      fire from there. The next timer firing tries again from scratch —
#      transient failures (image-pull rate limit, port in TIME_WAIT) self-heal
#      on the next tick.
# =============================================================================
set -uo pipefail

# Deliberately not using `set -e` — we want to keep checking the rest of the
# containers even if one is in a bad state.

# ── Config ────────────────────────────────────────────────────────────────────
# Compose file used by the missing-container recovery path. Derived
# from this script's own location so it works on both staging
# (~/DE-Reporting/deployment/...) and prod (~/kavanah-analytics/deployment/...)
# without per-host edits.
COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-compose.yml"

EXPECTED_CONTAINERS=(es01 imports_container kib01 nginx-reverse-proxy)

# Expected Docker networks per container. Used by reattach_missing_networks
# to heal the network-orphan failure mode where a transient port-bind error
# during restart clears a container's NetworkSettings.Networks even though
# its compose labels remain. docker start
# does not re-evaluate the compose spec, so once orphaned the container
# crashloops until something runs `docker compose up` or `docker network
# connect`. All four DEAM containers live on elastic-network.
declare -A EXPECTED_NETWORKS=(
    [es01]="elastic-network"
    [imports_container]="elastic-network"
    [kib01]="elastic-network"
    [nginx-reverse-proxy]="elastic-network"
)

# Boot-order dependency map — when starting from cold, start dependencies
# first so dependent containers find their upstreams ready.
START_ORDER=(es01 imports_container kib01 nginx-reverse-proxy)

# Max restart attempts per run before giving up on a container. The watchdog
# fires every 2 min; this caps in-run thrash, not lifetime attempts.
MAX_START_ATTEMPTS=2

# Seconds to wait after a `docker start` before considering it stable.
STABILITY_WAIT=8

# Restart-loop detection. Docker's RestartCount keeps climbing across a
# crashloop; if a container has restarted more than this in the current
# uptime window, the watchdog treats it as crashlooping and intervenes.
CRASHLOOP_RESTART_THRESHOLD=3

# RestartCount never resets for the lifetime of a container, so a
# restart burst from a long-ago cutover (e.g. es01's burst) trips the
# high-restartCount signal weeks later. Only treat the count as meaningful
# when the container's current run STARTED within this window: a recent
# startedAt means the restarts are recent too (each restart resets StartedAt).
RESTART_RECENCY_WINDOW_HOURS=24

# Crashloop intervention: stop the container so dockerd's restart policy
# stops grabbing resources, then start it cleanly. This breaks IP-race
# style loops where the container keeps losing its IP to a neighbor.
# (See dev-reporting outage 2026-05-05 RCA.)

# ── Logging ───────────────────────────────────────────────────────────────────
log()  { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }
warn() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] WARN: $*" >&2; }
err()  { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ERROR: $*" >&2; }

# ── Inspect helpers ───────────────────────────────────────────────────────────
container_exists() {
    docker inspect "$1" >/dev/null 2>&1
}

container_status() {
    docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "missing"
}

container_health() {
    # Returns the health string, or "none" if no healthcheck is defined.
    # `Health` is absent on containers without a HEALTHCHECK directive, so
    # the default branch handles both "no healthcheck" and "starting".
    docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo "missing"
}

container_restart_count() {
    docker inspect -f '{{.RestartCount}}' "$1" 2>/dev/null || echo 0
}

# true when the container's current run began within
# RESTART_RECENCY_WINDOW_HOURS. Unparseable/missing startedAt counts as
# recent (fail toward warning, never toward silence).
container_started_recently() {
    local started_at started_epoch now_epoch window_secs
    started_at=$(docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null) || return 0
    started_epoch=$(date -d "$started_at" +%s 2>/dev/null) || return 0
    now_epoch=$(date +%s)
    window_secs=$(( RESTART_RECENCY_WINDOW_HOURS * 3600 ))
    (( now_epoch - started_epoch < window_secs ))
}

# ── Recovery actions ──────────────────────────────────────────────────────────
# Returns 0 if the container is running and (if it has a healthcheck) not
# unhealthy after STABILITY_WAIT. Returns 1 otherwise.
verify_running() {
    local name="$1"
    sleep "$STABILITY_WAIT"
    local status health
    status=$(container_status "$name")
    health=$(container_health "$name")
    if [[ "$status" != "running" ]]; then
        return 1
    fi
    # "starting" is OK on first probe — we'll catch unhealthy on the next run.
    # "none" means no healthcheck defined; treat as healthy.
    if [[ "$health" == "unhealthy" ]]; then
        return 1
    fi
    return 0
}

# Reattach any declared networks that are missing from .NetworkSettings.Networks.
# Healing for the a failed start (eg port :80 race with
# certbot's standalone authenticator) can clear the persisted network spec
# while leaving compose labels intact. Subsequent docker start calls then
# bring the container up with no networks, and DNS resolution of sibling
# containers fails. docker network connect restores the attachment without
# recreating the container.
reattach_missing_networks() {
    local name="$1"
    local expected="${EXPECTED_NETWORKS[$name]:-}"
    [[ -z "$expected" ]] && return 0
    local net
    for net in $expected; do
        local has
        has=$(docker inspect -f "{{if index .NetworkSettings.Networks \"$net\"}}yes{{end}}" "$name" 2>/dev/null)
        if [[ "$has" != "yes" ]]; then
            warn "$name missing network $net — reattaching with docker network connect."
            if ! docker network connect "$net" "$name" >/dev/null 2>&1; then
                err "docker network connect $net $name failed."
                return 1
            fi
        fi
    done
    return 0
}

start_container() {
    local name="$1"
    local attempt
    for ((attempt = 1; attempt <= MAX_START_ATTEMPTS; attempt++)); do
        log "Starting $name (attempt $attempt/$MAX_START_ATTEMPTS)..."
        reattach_missing_networks "$name" || warn "$name network reattach failed; proceeding with start anyway."
        if docker start "$name" >/dev/null 2>&1; then
            if verify_running "$name"; then
                log "$name is running."
                return 0
            fi
            warn "$name started but did not stabilize (status=$(container_status "$name") health=$(container_health "$name"))."
        else
            warn "docker start $name failed."
        fi
    done
    err "$name failed to start after $MAX_START_ATTEMPTS attempts."
    return 1
}

# Re-provision a container that's missing entirely.
#
# Called only from the `missing` branch — when `docker inspect` shows no
# such container at all. Distinct from start_container, which only handles
# existing-but-stopped containers.
#
# Prerequisites the function itself enforces:
#   1. COMPOSE_FILE exists at the expected path. If not, the operator deployed
#      to a host where this script's directory layout differs from staging/prod
#      — page a human rather than guessing.
#   2. The compose file actually defines a service matching the container's
#      name. If a future compose-file rename drops the service, we'd
#      otherwise silently invoke a no-op `compose up` and exit 0. The grep
#      guards against that.
#
# Retry policy: bounded by MAX_START_ATTEMPTS, same as start_container —
# transient failures (image pull rate-limited, port still in TIME_WAIT)
# self-heal on the next timer tick (2 min later); persistent failures spam
# journald until the operator fixes the compose file. Deliberately no
# cross-run state file: oneshot systemd units are stateless by design, and
# adding /var/lib/container-watchdog/ would couple the watchdog to a
# writable filesystem (we currently run with ProtectSystem=strict).
compose_up_service() {
    local name="$1"
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        err "Compose file not found at $COMPOSE_FILE — cannot re-provision $name."
        return 1
    fi
    # Service-name match: the compose service for each DEAM container shares
    # its container name (see deployment/docker-compose.yml). We grep for the
    # service header (`  <name>:` at exactly two spaces, line-start) rather
    # than a bare `name:` to avoid matching keys nested deeper in the YAML.
    if ! grep -qE "^  ${name}:" "$COMPOSE_FILE"; then
        err "Service '$name' not defined in $COMPOSE_FILE — cannot re-provision."
        return 1
    fi
    local attempt
    for ((attempt = 1; attempt <= MAX_START_ATTEMPTS; attempt++)); do
        log "Re-provisioning $name via docker compose (attempt $attempt/$MAX_START_ATTEMPTS)..."
        if docker compose -f "$COMPOSE_FILE" up -d "$name" >/dev/null 2>&1; then
            if verify_running "$name"; then
                log "$name re-provisioned and running."
                return 0
            fi
            warn "$name re-provisioned but did not stabilize (status=$(container_status "$name") health=$(container_health "$name"))."
        else
            warn "docker compose up -d $name failed."
        fi
    done
    err "$name failed to re-provision after $MAX_START_ATTEMPTS attempts."
    return 1
}

# Crashloop break: stop, wait, start. Used when the container is "restarting"
# or has a high RestartCount. Stopping first frees any held resources (network
# IP, port, lock) that the next start needs.
break_crashloop() {
    local name="$1"
    log "Breaking crashloop for $name (current restartCount=$(container_restart_count "$name"))..."
    docker stop "$name" >/dev/null 2>&1 || warn "docker stop $name returned non-zero (may already be stopped)."
    sleep 2
    start_container "$name"
}

# ── Main check loop ───────────────────────────────────────────────────────────
overall_rc=0

# First pass: gather state for all containers.
declare -A state
declare -A health
declare -A restarts
for name in "${EXPECTED_CONTAINERS[@]}"; do
    if ! container_exists "$name"; then
        state[$name]="missing"
        health[$name]="missing"
        restarts[$name]=0
    else
        state[$name]=$(container_status "$name")
        health[$name]=$(container_health "$name")
        restarts[$name]=$(container_restart_count "$name")
    fi
done

# Print one-line summary on every run so journald has a heartbeat.
summary=""
for name in "${EXPECTED_CONTAINERS[@]}"; do
    summary+=" $name=${state[$name]}"
done
log "State:$summary"

# Second pass: recover, in dependency order.
for name in "${START_ORDER[@]}"; do
    s="${state[$name]}"
    h="${health[$name]}"
    rc="${restarts[$name]}"

    case "$s" in
        missing)
            # Container is gone entirely. if the compose file at
            # COMPOSE_FILE still defines the service, escalate to
            # `docker compose up -d <name>` to re-provision it. That covers
            # the prod failure mode where step6-nginx-setup.sh
            # deliberately removed the nginx-reverse-proxy container and the
            # follow-up runbook step didn't recreate it — the watchdog
            # logged the missing container every 2 min for 3.5 minutes but
            # couldn't act on it.
            #
            # If the compose file is unreachable or the service isn't
            # defined, compose_up_service errors out and we still page a
            # human (overall_rc=1).
            log "$name does not exist. Attempting to re-provision from $COMPOSE_FILE."
            if ! compose_up_service "$name"; then
                overall_rc=1
            fi
            ;;
        running)
            if [[ "$h" == "unhealthy" ]]; then
                warn "$name is running but unhealthy (restartCount=$rc). Cycling."
                if ! break_crashloop "$name"; then
                    overall_rc=1
                fi
            elif (( rc > CRASHLOOP_RESTART_THRESHOLD )) && container_started_recently "$name"; then
                # Running now, but has been flapping RECENTLY (a
                # high count with an old startedAt is a stale burst — say
                # nothing; the per-run state summary already covers it).
                # Don't intervene unless it's actually unhealthy — warn so
                # we can spot patterns.
                warn "$name running but has high restartCount=$rc within the last ${RESTART_RECENCY_WINDOW_HOURS}h. Monitoring."
            fi
            ;;
        restarting)
            warn "$name is in a restart loop (restartCount=$rc)."
            if ! break_crashloop "$name"; then
                overall_rc=1
            fi
            ;;
        exited|created|paused|dead)
            log "$name is $s. Starting."
            if ! start_container "$name"; then
                overall_rc=1
            fi
            ;;
        *)
            warn "$name in unexpected state '$s'. Skipping."
            overall_rc=1
            ;;
    esac
done

if (( overall_rc == 0 )); then
    log "All containers healthy."
else
    err "Watchdog finished with unrecoverable issues. See log above."
fi

exit "$overall_rc"
