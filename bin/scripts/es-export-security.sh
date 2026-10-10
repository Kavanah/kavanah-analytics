#!/bin/bash
# Export Elasticsearch custom roles and users from prod.
# Passwords are NOT exportable — users will need new passwords on the test server.
# Run this on the PROD server, then transfer the output directory to the test server.
#
# Usage:
#   bash es-export-security.sh [output-directory]
#
# Example:
#   bash es-export-security.sh ~/es-dump

set -euo pipefail

DUMP_DIR="${1:-$HOME/es-dump-$(date +%Y%m%d-%H%M%S)}"
ES_HOST="https://localhost:9200"

read -p "ES username [elastic]: " ES_USER
ES_USER="${ES_USER:-elastic}"
read -s -p "ES password: " ES_PASS
echo

mkdir -p "$DUMP_DIR"

# Verify ES is reachable
if ! curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cluster/health" > /dev/null; then
    echo "ERROR: Cannot reach Elasticsearch. Check credentials and that ES is running."
    exit 1
fi

echo ""

# ── Export custom roles (exclude built-in reserved roles) ─────────────────────
echo "Exporting custom roles..."
curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_security/role" | \
    python3 -c "
import json, sys
roles = json.load(sys.stdin)
custom = {name: data for name, data in roles.items() if not data.get('metadata', {}).get('_reserved', False)}
print(json.dumps(custom, indent=2))
" > "$DUMP_DIR/security_roles.json"

ROLE_COUNT=$(python3 -c "import json; data=json.load(open('$DUMP_DIR/security_roles.json')); print(len(data))")
echo "  ✓ Exported $ROLE_COUNT custom roles"

# ── Export users (exclude built-in reserved users) ────────────────────────────
echo "Exporting users..."
curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_security/user" | \
    python3 -c "
import json, sys
users = json.load(sys.stdin)
custom = {name: data for name, data in users.items() if not data.get('metadata', {}).get('_reserved', False)}
print(json.dumps(custom, indent=2))
" > "$DUMP_DIR/security_users.json"

USER_COUNT=$(python3 -c "import json; data=json.load(open('$DUMP_DIR/security_users.json')); print(len(data))")
echo "  ✓ Exported $USER_COUNT custom users (without passwords)"

echo ""
echo "========================================"
echo "Security export complete!"
echo "Dump directory: $DUMP_DIR"
echo ""
echo "NOTE: Passwords are not exportable. You will be prompted to set"
echo "      a temporary default password for all users during import."
echo "========================================"
