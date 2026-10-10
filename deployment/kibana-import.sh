#!/usr/bin/env bash
# =============================================================================
# Kibana Saved Objects Import
# Imports Kibana spaces + saved objects (data views, dashboards, etc.)
# into the local test server from a kibana-export directory.
#
# Usage:
#   bash kibana-import.sh [export-dir]
#
# Example:
#   bash kibana-import.sh ~/kibana-export
#
# Reads credentials from config/master-config.yaml.
# Kibana must be running on http://localhost:5601 before running this.
# =============================================================================

set -euo pipefail

EXPORT_DIR="${1:-}"
# Support both repo directory names (dev vs production server)
if [ -d "$HOME/DE-Reporting" ]; then
    REPO_DIR="$HOME/DE-Reporting"
else
    REPO_DIR="$HOME/kavanah-analytics"
fi
MASTER_CONFIG="$REPO_DIR/config/master-config.yaml"
KIBANA_URL="http://localhost:5601"

# Detect Kibana basePath from kibana.yml and append to URL. On production,
# Kibana runs under /kibana so the host-side curls must include it.
KIB_YML="$REPO_DIR/kibana.yml"
if [ -f "$KIB_YML" ]; then
    BASE_PATH=$(grep '^server.basePath:' "$KIB_YML" | sed 's/server.basePath:[[:space:]]*//' | tr -d '"' | tr -d "'")
    [ -n "$BASE_PATH" ] && KIBANA_URL="${KIBANA_URL}${BASE_PATH}"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

# ── Resolve export dir ────────────────────────────────────────────────────────
if [ -z "$EXPORT_DIR" ]; then
    # Find most recent export dir
    EXPORT_DIR=$(ls -td ~/kibana-export* 2>/dev/null | head -1 || true)
    [ -z "$EXPORT_DIR" ] && die "No export directory found. Pass the path as an argument: bash kibana-import.sh ~/kibana-export"
    info "Using most recent export: $EXPORT_DIR"
fi

[ -d "$EXPORT_DIR" ] || die "Directory not found: $EXPORT_DIR"
[ -f "${EXPORT_DIR}/spaces.json" ] || die "No spaces.json found in ${EXPORT_DIR}. Run kibana-export.sh first."

# ── Read credentials ──────────────────────────────────────────────────────────
[ -f "$MASTER_CONFIG" ] || die "master-config.yaml not found at $MASTER_CONFIG"
ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG")
[ -z "$ELASTIC_PASS" ] && die "Could not read elastic password from master-config.yaml"

# ── Test Kibana connectivity ──────────────────────────────────────────────────
info "Testing Kibana connectivity at ${KIBANA_URL}..."
for i in $(seq 1 12); do
    STATUS=$(curl -sk -o /dev/null -w "%{http_code}" \
        -u "elastic:${ELASTIC_PASS}" "${KIBANA_URL}/api/status" 2>/dev/null || echo "000")
    if [[ "$STATUS" == "200" ]]; then break; fi
    echo "  waiting for Kibana... attempt ${i}/12 (HTTP ${STATUS})"
    sleep 10
done
[[ "$STATUS" == "200" ]] || die "Cannot reach ${KIBANA_URL} (HTTP ${STATUS}). Is Kibana running?"
ok "Connected to Kibana"

# ── Create Kibana spaces ──────────────────────────────────────────────────────
info "Creating Kibana spaces from spaces.json..."

python3 - <<PYEOF
import json, subprocess, sys

with open("${EXPORT_DIR}/spaces.json") as f:
    spaces = json.load(f)

kibana_url = "${KIBANA_URL}"
elastic_pass = "${ELASTIC_PASS}"

for space in spaces:
    space_id = space.get("id")
    if space_id == "default":
        print(f"  [SKIP] default space (always exists)")
        continue

    # Check if space already exists
    result = subprocess.run(
        ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
         "-u", f"elastic:{elastic_pass}",
         f"{kibana_url}/api/spaces/space/{space_id}"],
        capture_output=True, text=True
    )
    if result.stdout.strip() == "200":
        print(f"  [SKIP] space '{space_id}' already exists")
        continue

    # Create the space
    payload = {k: v for k, v in space.items() if k in
               ["id", "name", "description", "color", "initials", "imageUrl", "disabledFeatures"]}

    result = subprocess.run(
        ["curl", "-sk", "-X", "POST",
         "-u", f"elastic:{elastic_pass}",
         "-H", "kbn-xsrf: true",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload),
         f"{kibana_url}/api/spaces/space"],
        capture_output=True, text=True
    )
    try:
        resp = json.loads(result.stdout)
        if "id" in resp:
            print(f"  [OK]   Created space: {space_id}")
        else:
            print(f"  [WARN] Space '{space_id}': {result.stdout[:200]}")
    except Exception:
        print(f"  [WARN] Space '{space_id}': unexpected response: {result.stdout[:200]}")
PYEOF

# ── Import saved objects per space ────────────────────────────────────────────
info "Importing saved objects..."

for NDJSON_FILE in "${EXPORT_DIR}"/saved_objects_*.ndjson; do
    [ -f "$NDJSON_FILE" ] || continue

    # Extract space ID from filename: saved_objects_<space_id>.ndjson
    SPACE_ID=$(basename "$NDJSON_FILE" .ndjson | sed 's/^saved_objects_//')

    if [[ "$SPACE_ID" == "default" ]]; then
        IMPORT_URL="${KIBANA_URL}/api/saved_objects/_import?overwrite=true"
    else
        IMPORT_URL="${KIBANA_URL}/s/${SPACE_ID}/api/saved_objects/_import?overwrite=true"
    fi

    LINE_COUNT=$(wc -l < "$NDJSON_FILE" | tr -d ' ')
    info "  Importing ${LINE_COUNT} objects into space '${SPACE_ID}'..."

    RESPONSE=$(curl -sk \
        -u "elastic:${ELASTIC_PASS}" \
        -X POST "${IMPORT_URL}" \
        -H "kbn-xsrf: true" \
        --form "file=@${NDJSON_FILE};type=application/ndjson")

    SUCCESS=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('success','?'))" 2>/dev/null || echo "?")
    SUCCESS_COUNT=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('successCount', 0))" 2>/dev/null || echo "?")
    ERRORS=$(echo "$RESPONSE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
errs = d.get('errors', [])
if errs:
    for e in errs[:5]:
        print(f\"    - {e.get('type','?')} '{e.get('meta',{}).get('title','?')}': {e.get('error',{}).get('type','?')}\")
" 2>/dev/null || true)

    if [[ "$SUCCESS" == "True" ]]; then
        ok "  space '${SPACE_ID}': ${SUCCESS_COUNT} objects imported successfully"
    else
        warn "  space '${SPACE_ID}': success=${SUCCESS}, ${SUCCESS_COUNT} imported"
        if [ -n "$ERRORS" ]; then
            echo "$ERRORS"
        fi
    fi
done

# ── Verify data views ─────────────────────────────────────────────────────────
echo ""
info "Verifying data views on test server..."
curl -sk -u "elastic:${ELASTIC_PASS}" \
    "${KIBANA_URL}/api/data_views" | \
    python3 -c "
import json, sys
d = json.load(sys.stdin)
views = d.get('data_view', [])
print(f'  Found {len(views)} data view(s):')
for v in views:
    print(f'    - {v.get(\"title\",\"?\")!r} (id: {v.get(\"id\",\"?\")[:8]}...)')
"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Kibana Import Complete${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "  Kibana: ${KIBANA_URL}"
echo -e "  Source: ${EXPORT_DIR}"
echo ""
echo -e "${YELLOW}Next: verify in Kibana UI at http://192.168.56.102:5601${NC}"
echo -e "${GREEN}============================================${NC}"
