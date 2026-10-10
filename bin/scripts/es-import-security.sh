#!/bin/bash
# Import Elasticsearch custom roles and users into the test server.
# All imported users will be assigned a temporary default password you specify.
# Run this on the TEST server after transferring the dump from prod.
#
# Usage:
#   bash es-import-security.sh [dump-directory]
#
# Example:
#   bash es-import-security.sh ~/es-dump

set -euo pipefail

DUMP_DIR="${1:-}"

if [ -z "$DUMP_DIR" ]; then
    read -p "Path to dump directory: " DUMP_DIR
fi

if [ ! -d "$DUMP_DIR" ]; then
    echo "ERROR: Directory not found: $DUMP_DIR"
    exit 1
fi

if [ ! -f "$DUMP_DIR/security_roles.json" ] || [ ! -f "$DUMP_DIR/security_users.json" ]; then
    echo "ERROR: security_roles.json or security_users.json not found in $DUMP_DIR"
    echo "Run es-export-security.sh on the prod server first."
    exit 1
fi

ES_HOST="https://localhost:9200"

read -p "ES username [elastic]: " ES_USER
ES_USER="${ES_USER:-elastic}"
read -s -p "ES password: " ES_PASS
echo

read -s -p "Temporary password for all imported users: " DEFAULT_PASS
echo
read -s -p "Confirm temporary password: " DEFAULT_PASS_CONFIRM
echo

if [ "$DEFAULT_PASS" != "$DEFAULT_PASS_CONFIRM" ]; then
    echo "ERROR: Passwords do not match."
    exit 1
fi

# Verify ES is reachable
if ! curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cluster/health" > /dev/null; then
    echo "ERROR: Cannot reach Elasticsearch. Check credentials and that ES is running."
    exit 1
fi

echo ""

# ── Import roles ──────────────────────────────────────────────────────────────
echo "Importing roles..."
ROLES=$(python3 -c "import json; data=json.load(open('$DUMP_DIR/security_roles.json')); print('\n'.join(data.keys()))")

if [ -z "$ROLES" ]; then
    echo "  No custom roles found in export."
else
    while IFS= read -r ROLE; do
        ROLE_BODY=$(python3 -c "import json; data=json.load(open('$DUMP_DIR/security_roles.json')); print(json.dumps(data['$ROLE']))")
        HTTP_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" -u "$ES_USER:$ES_PASS" \
            -X PUT "$ES_HOST/_security/role/$ROLE" \
            -H "Content-Type: application/json" \
            -d "$ROLE_BODY")
        if [ "$HTTP_STATUS" = "200" ]; then
            echo "  ✓ Role: $ROLE"
        else
            echo "  ✗ Failed to import role: $ROLE (HTTP $HTTP_STATUS)"
        fi
    done <<< "$ROLES"
fi

echo ""

# ── Import users ──────────────────────────────────────────────────────────────
echo "Importing users (with temporary password)..."
USERS=$(python3 -c "import json; data=json.load(open('$DUMP_DIR/security_users.json')); print('\n'.join(data.keys()))")

if [ -z "$USERS" ]; then
    echo "  No custom users found in export."
else
    while IFS= read -r USER; do
        USER_DATA=$(python3 -c "
import json
data = json.load(open('$DUMP_DIR/security_users.json'))
user = data['$USER']
# Build import payload — inject temporary password, drop non-writable fields
payload = {
    'password': '$DEFAULT_PASS',
    'roles': user.get('roles', []),
    'full_name': user.get('full_name', ''),
    'email': user.get('email', ''),
    'metadata': {k: v for k, v in user.get('metadata', {}).items() if not k.startswith('_')},
    'enabled': user.get('enabled', True)
}
print(json.dumps(payload))
")
        HTTP_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" -u "$ES_USER:$ES_PASS" \
            -X PUT "$ES_HOST/_security/user/$USER" \
            -H "Content-Type: application/json" \
            -d "$USER_DATA")
        if [ "$HTTP_STATUS" = "200" ]; then
            echo "  ✓ User: $USER"
        else
            echo "  ✗ Failed to import user: $USER (HTTP $HTTP_STATUS)"
        fi
    done <<< "$USERS"
fi

echo ""
echo "========================================"
echo "Security import complete!"
echo ""
echo "All imported users have the temporary password you set."
echo "Remind users to update their passwords after first login."
echo "========================================"
