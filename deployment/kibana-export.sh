#!/usr/bin/env bash
# =============================================================================
# Kibana Saved Objects Export
# Exports all Kibana spaces + saved objects (data views, dashboards, etc.)
# from a source Kibana instance over HTTPS.
#
# Usage:
#   bash kibana-export.sh <kibana-url> [output-dir]
#
# Examples:
#   bash kibana-export.sh https://<staging-host>
#   bash kibana-export.sh https://<staging-host> ~/kibana-export
#
# What it exports per space:
#   index-pattern, dashboard, visualization, lens,
#   canvas-workpad, search, tag, map
# =============================================================================

set -euo pipefail

KIBANA_URL="${1:-}"
EXPORT_DIR="${2:-$HOME/kibana-export-$(date +%Y%m%d-%H%M%S)}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

[ -z "$KIBANA_URL" ] && die "Usage: bash kibana-export.sh <kibana-url> [output-dir]"

# Strip trailing slash
KIBANA_URL="${KIBANA_URL%/}"

# Prompt for password (not passed as argument to avoid shell history exposure)
read -s -p "Elastic password for ${KIBANA_URL}: " ELASTIC_PASS
echo

mkdir -p "$EXPORT_DIR"
info "Export directory: $EXPORT_DIR"

# ── Test connectivity ──────────────────────────────────────────────────────────
info "Testing Kibana connectivity..."
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" \
    -u "elastic:${ELASTIC_PASS}" \
    "${KIBANA_URL}/api/status")
[[ "$STATUS" == "200" ]] || die "Cannot reach ${KIBANA_URL}/api/status (HTTP ${STATUS}). Check URL and password."
ok "Connected to Kibana at ${KIBANA_URL}"

# ── Export spaces ─────────────────────────────────────────────────────────────
info "Fetching Kibana spaces..."
SPACES_JSON=$(curl -sk \
    -u "elastic:${ELASTIC_PASS}" \
    -H "kbn-xsrf: true" \
    "${KIBANA_URL}/api/spaces/space")

echo "$SPACES_JSON" > "${EXPORT_DIR}/spaces.json"

# Extract space IDs
SPACE_IDS=$(echo "$SPACES_JSON" | python3 -c "
import json, sys
spaces = json.load(sys.stdin)
for s in spaces:
    print(s['id'])
" 2>/dev/null)

SPACE_COUNT=$(echo "$SPACE_IDS" | wc -l | tr -d ' ')
ok "Found ${SPACE_COUNT} space(s): $(echo "$SPACE_IDS" | tr '\n' ' ')"

# ── Export saved objects per space ────────────────────────────────────────────
OBJECT_TYPES='["index-pattern","dashboard","visualization","lens","canvas-workpad","search","tag","map"]'

for SPACE_ID in $SPACE_IDS; do
    info "Exporting saved objects from space: ${SPACE_ID}..."

    if [[ "$SPACE_ID" == "default" ]]; then
        EXPORT_URL="${KIBANA_URL}/api/saved_objects/_export"
    else
        EXPORT_URL="${KIBANA_URL}/s/${SPACE_ID}/api/saved_objects/_export"
    fi

    OUTPUT_FILE="${EXPORT_DIR}/saved_objects_${SPACE_ID}.ndjson"

    # Write body and HTTP code to separate files so we never confuse ndjson
    # lines with the status code.
    HTTP_CODE=$(curl -sk \
        -u "elastic:${ELASTIC_PASS}" \
        -H "kbn-xsrf: true" \
        -H "Content-Type: application/json" \
        -X POST "${EXPORT_URL}" \
        -d "{\"type\": ${OBJECT_TYPES}, \"excludeExportDetails\": true, \"includeReferencesDeep\": true}" \
        -o "$OUTPUT_FILE" \
        -w "%{http_code}")

    if [[ "$HTTP_CODE" == "200" ]]; then
        OBJ_COUNT=$(wc -l < "$OUTPUT_FILE" | tr -d ' ')
        ok "  space '${SPACE_ID}': ${OBJ_COUNT} objects → $(basename "$OUTPUT_FILE")"
    elif [[ "$HTTP_CODE" == "404" ]]; then
        warn "  space '${SPACE_ID}': no objects found (HTTP 404) — skipping"
        rm -f "$OUTPUT_FILE"
    else
        warn "  space '${SPACE_ID}': export returned HTTP ${HTTP_CODE} — check $(basename "$OUTPUT_FILE") for details"
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Kibana Export Complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "  Source:     ${KIBANA_URL}"
echo -e "  Output dir: ${EXPORT_DIR}"
echo -e "  Files:"
ls -lh "$EXPORT_DIR"
echo ""
echo -e "${YELLOW}Transfer to the test VM with:${NC}"
echo -e "  scp -r ${EXPORT_DIR} joshua@192.168.56.102:~/kibana-export"
echo -e "${YELLOW}Then on the test VM run:${NC}"
echo -e "  bash deployment/kibana-import.sh ~/kibana-export"
echo -e "${GREEN}============================================${NC}"
