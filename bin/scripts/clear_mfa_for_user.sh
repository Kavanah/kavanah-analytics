#!/bin/bash
# Break-glass: clear the MFA record for a single user.
#
# Use this when the normal "Bypass 2FA" button in User Management is not
# reachable — e.g. the only remaining superuser locked themselves out.
#
# Effect: deletes the user's document from de_asset_user_mfa. Next login,
# the user will be treated as "no record yet" and — if enforce_mfa is on —
# routed through the enrollment picker. Existing roles/password are untouched.
#
# Usage:
#   bash clear_mfa_for_user.sh <username>
#
# Must be run on a host with network access to the Elasticsearch node.

set -euo pipefail

if [ $# -ne 1 ] || [ -z "${1:-}" ]; then
    echo "Usage: $0 <username>" >&2
    exit 1
fi

TARGET_USER="$1"
INDEX="de_asset_user_mfa"
ES_HOST="${ES_HOST:-https://localhost:9200}"

read -p "ES admin username [elastic]: " ES_USER
ES_USER="${ES_USER:-elastic}"
read -s -p "ES password: " ES_PASS
echo

echo
echo "About to delete the MFA record for '${TARGET_USER}' from ${INDEX} on ${ES_HOST}."
echo "This is a break-glass action and will be audit-logged in Elasticsearch."
read -p "Type the username again to confirm: " CONFIRM
if [ "$CONFIRM" != "$TARGET_USER" ]; then
    echo "Confirmation did not match. Aborting." >&2
    exit 1
fi

HTTP_CODE=$(curl -sk -o /tmp/clear_mfa_response.$$ -w "%{http_code}" \
    -u "${ES_USER}:${ES_PASS}" \
    -X DELETE \
    "${ES_HOST}/${INDEX}/_doc/${TARGET_USER}?refresh=wait_for")

BODY=$(cat /tmp/clear_mfa_response.$$)
rm -f /tmp/clear_mfa_response.$$

case "$HTTP_CODE" in
    200)
        echo "Done. MFA record deleted for '${TARGET_USER}'."
        echo "They will be prompted to set up 2FA again on next login."
        ;;
    404)
        echo "No MFA record existed for '${TARGET_USER}' — nothing to clear."
        echo "If the user still cannot log in, the problem is elsewhere."
        ;;
    401|403)
        echo "ERROR: authentication/authorization failed (HTTP ${HTTP_CODE})." >&2
        echo "$BODY" >&2
        exit 1
        ;;
    *)
        echo "ERROR: unexpected response (HTTP ${HTTP_CODE})." >&2
        echo "$BODY" >&2
        exit 1
        ;;
esac
