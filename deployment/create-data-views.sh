#!/usr/bin/env bash
# =============================================================================
# Create Kibana Data Views
# Creates data views for all migrated ES index patterns on the test server.
# Run this after es-import.sh has populated the indices.
#
# Usage:
#   bash deployment/create-data-views.sh
# =============================================================================

set -euo pipefail

# Support both repo directory names (dev vs production server)
if [ -d "$HOME/DE-Reporting" ]; then
    REPO_DIR="$HOME/DE-Reporting"
elif [ -d "$HOME/kavanah-analytics" ]; then
    REPO_DIR="$HOME/kavanah-analytics"
else
    echo -e "${RED}[FAIL]${NC} Repository not found at ~/DE-Reporting or ~/kavanah-analytics" >&2
    exit 1
fi
MASTER_CONFIG="$REPO_DIR/config/master-config.yaml"

# Detect Kibana basePath from config (e.g. /kibana) and append to URL.
# step3-app-stack.sh writes kibana.yml to $REPO_DIR/kibana.yml (mounted
# read-only into the kib01 container), so look there.
KIBANA_URL="http://localhost:5601"
KIB_CONFIG="$REPO_DIR/kibana.yml"
if [ -f "$KIB_CONFIG" ]; then
    BASE_PATH=$(grep '^server.basePath:' "$KIB_CONFIG" | sed 's/server.basePath:[[:space:]]*//' | tr -d '"' | tr -d "'")
    [ -n "$BASE_PATH" ] && KIBANA_URL="${KIBANA_URL}${BASE_PATH}"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

# Phase 3 stripped secret VALUES from master-config.yaml on prod, so
# read the elastic password env-first (the same pattern as step3-app-stack.sh):
# source deployment/lib/source-ssm-secrets.sh exports ES_ADMIN_PASS. Fall back
# to master-config for envs that still carry the value (staging/dev).
#
#   SSM_ENV=prod SSM_PROFILE="" SSM_SUDO=1 \
#     source deployment/lib/source-ssm-secrets.sh \
#     && bash deployment/create-data-views.sh
ELASTIC_PASS="${ES_ADMIN_PASS:-}"
if [ -z "$ELASTIC_PASS" ]; then
    [ -f "$MASTER_CONFIG" ] || die "master-config.yaml not found at $MASTER_CONFIG"
    ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG")
fi
if [ -z "$ELASTIC_PASS" ] || [ "$ELASTIC_PASS" = "null" ]; then
    die "No elastic password: export ES_ADMIN_PASS (source deployment/lib/source-ssm-secrets.sh) or populate master-config.yaml"
fi

# ── Test connectivity ──────────────────────────────────────────────────────────
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" -u "elastic:${ELASTIC_PASS}" "${KIBANA_URL}/api/status")
[[ "$STATUS" == "200" ]] || die "Cannot reach Kibana at ${KIBANA_URL} (HTTP ${STATUS})"
ok "Connected to Kibana"

# ── Helper: create a data view in a given space ───────────────────────────────
create_data_view() {
    local title="$1"
    local name="$2"
    local time_field="${3:-}"
    local space="${4:-default}"

    if [[ "$space" == "default" ]]; then
        api_url="${KIBANA_URL}/api/data_views/data_view"
        check_url="${KIBANA_URL}/api/data_views"
    else
        api_url="${KIBANA_URL}/s/${space}/api/data_views/data_view"
        check_url="${KIBANA_URL}/s/${space}/api/data_views"
    fi

    # Check if already exists in this space — returns "id|existing_name" or empty
    existing_info=$(curl -sk -u "elastic:${ELASTIC_PASS}" "$check_url" | \
        python3 -c "
import json,sys
views=json.load(sys.stdin).get('data_view',[])
match=next((v for v in views if v['title']=='${title}'),None)
if match: print(match['id']+'|'+(match.get('name') or ''))
" 2>/dev/null || true)

    if [ -n "$existing_info" ]; then
        existing_id="${existing_info%%|*}"
        existing_name="${existing_info#*|}"
        if [ -n "$existing_name" ]; then
            ok "  [${space}] '${name}' already exists — skipping"
            return
        fi
        # Name field missing — update the existing data view
        if [[ "$space" == "default" ]]; then
            update_url="${KIBANA_URL}/api/data_views/data_view/${existing_id}"
        else
            update_url="${KIBANA_URL}/s/${space}/api/data_views/data_view/${existing_id}"
        fi
        curl -sk -X POST -u "elastic:${ELASTIC_PASS}" "$update_url" \
            -H "kbn-xsrf: true" -H "Content-Type: application/json" \
            -d "{\"data_view\":{\"name\":\"${name}\"}}" > /dev/null 2>&1
        ok "  [${space}] Updated '${name}' — added missing display name"
        return
    fi

    payload="{\"data_view\":{\"title\":\"${title}\",\"name\":\"${name}\""
    if [ -n "$time_field" ]; then
        payload="${payload},\"timeFieldName\":\"${time_field}\""
    fi
    payload="${payload}}}"

    response=$(curl -sk -X POST -u "elastic:${ELASTIC_PASS}" "$api_url" \
        -H "kbn-xsrf: true" \
        -H "Content-Type: application/json" \
        -d "$payload")

    id=$(echo "$response" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data_view',{}).get('id',''))" 2>/dev/null || true)
    if [ -n "$id" ]; then
        ok "  [${space}] Created '${name}' (${title})"
    else
        warn "  [${space}] Failed to create '${name}': $(echo "$response" | head -c 200)"
    fi
}

# ── Create data views in the default (Kavanah Analytics) space ────────────────
# The default space is the only Kibana space the app uses.
#
# Format: "index_pattern|Display Name|time_field"
# Empty time_field means no time field (e.g. asset indices).

DATA_VIEWS=(
    "*_echo_conversations|Echo Conversations|@timestamp"
    "*_fb_insights|Facebook Ad Insights|@timestamp"
    # the weekly view (pattern *_fb_insights_weekly, name "Facebook
    # Ad Insights Weekly") is retired with the weekly importer (superseded by
    # the window pipeline) — do not recreate.
    # the FB dedup "window" view is retired from the builder picker.
    # With DEDUP_ROUTING_ENABLED on, dashboards build on the canonical
    # *_fb_insights view and routing serves deduplicated reach from the window
    # INDEX transparently (the index + importer stay; only the user-facing data
    # view goes). Do not recreate it. (The live Kibana view object, if present,
    # is dropped by hand during the cutover — see the staging runbook.)
    # "*_fb_insights_window|Facebook Ad Insights (deduplicated window)|@timestamp"
    "*_fb_page_insights|Facebook Page Insights|@timestamp"
    "*_fb_page_posts|Facebook Page Posts|post_created_time"
    "*_ga_metrics|Google Analytics|@timestamp"
    "*_ga_metrics_window|Google Analytics (deduplicated window)|@timestamp"
    "*_ig_insights|Instagram Insights|@timestamp"
    "*_google_ads|Google Ads|@timestamp"
    "*_youtube_analytics|YouTube Analytics|@timestamp"
    "*_chatwoot_conversations|Chatwoot Conversations|@timestamp"
    "*_chatwoot_insights|Chatwoot Insights|@timestamp"
    # Bitly link-clicks importer ({team_esid}__bitly_clicks).
    "*_bitly_clicks|Bitly Clicks|@timestamp"
    # TikTok organic snapshots ({team_esid}__tiktok_organic).
    "*_tiktok_organic|TikTok Organic|@timestamp"
    # per-country delivery breakdown of FB ad insights.
    "*_fb_insights_country|Facebook Ads by Country (daily)|@timestamp"
    # monthly per-team conversation rollups written by the retention
    # sweep before raw conversations older than 24 months are deleted.
    "*_conversation_rollups|Conversation Rollups (monthly)|@timestamp"
    # the legacy "disciple-tools|Disciple Tools|date_modified" view is
    # retired with the cross-tenant alias — do not recreate.
    "*_disciple_tools|Disciple Tools (by team)|date_created"
    # follow-up: the de_dashboard_templates config index is NOT an
    # analytics data source (no team_title) and should not be a selectable data
    # view in the builder. Removed so every listed data view carries team_title.
)

SPACE="default"
info "Creating data views in '${SPACE}' space..."
for entry in "${DATA_VIEWS[@]}"; do
    title="${entry%%|*}"
    rest="${entry#*|}"
    name="${rest%%|*}"
    time_field="${rest##*|}"
    create_data_view "$title" "$name" "$time_field" "$SPACE"
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
for SPACE in default; do
    check_url="${KIBANA_URL}/api/data_views" 
    info "Data views in '${SPACE}' space:"
    curl -sk -u "elastic:${ELASTIC_PASS}" "$check_url" | \
        python3 -c "
import json, sys
views = json.load(sys.stdin).get('data_view', [])
print(f'  Total: {len(views)}')
for v in sorted(views, key=lambda x: (x.get('name') or x['title']).lower()):
    name = v.get('name') or v['title']
    print(f'    - {name} ({v[\"title\"]})')
"
done

echo ""
ok "Done. View data views in Kibana at ${KIBANA_URL}/app/management/kibana/dataViews"
