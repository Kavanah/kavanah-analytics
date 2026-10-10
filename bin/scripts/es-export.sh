#!/bin/bash
# Export all Elasticsearch indices to JSON dump files.
# Run this on the PROD server, then transfer the dump directory to the test server.
#
# Usage:
#   bash es-export.sh [output-directory]
#
# Example:
#   bash es-export.sh ~/es-dump

set -euo pipefail

DUMP_DIR="${1:-$HOME/es-dump-$(date +%Y%m%d-%H%M%S)}"
ES_HOST="https://localhost:9200"

read -p "ES username [elastic]: " ES_USER
ES_USER="${ES_USER:-elastic}"
read -s -p "ES password: " ES_PASS
echo

mkdir -p "$DUMP_DIR"
echo "Dump directory: $DUMP_DIR"
echo ""

# Get all non-system indices (exclude indices starting with .)
INDICES=$(curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cat/indices?h=index" | grep -v '^\.' | sort)

if [ -z "$INDICES" ]; then
    echo "ERROR: No indices found. Check credentials and that ES is running."
    exit 1
fi

echo "Indices to export:"
echo "$INDICES"
echo ""

ES_URL="https://${ES_USER}:${ES_PASS}@localhost:9200"

for INDEX in $INDICES; do
    echo "── Exporting: $INDEX ──"

    # Export mapping
    sudo docker run --rm --network host \
        -e NODE_TLS_REJECT_UNAUTHORIZED=0 \
        -v "$DUMP_DIR:/dump" \
        elasticdump/elasticsearch-dump \
        --input="${ES_URL}/${INDEX}" \
        --output="/dump/${INDEX}_mapping.json" \
        --type=mapping

    # Export data
    sudo docker run --rm --network host \
        -e NODE_TLS_REJECT_UNAUTHORIZED=0 \
        -v "$DUMP_DIR:/dump" \
        elasticdump/elasticsearch-dump \
        --input="${ES_URL}/${INDEX}" \
        --output="/dump/${INDEX}_data.json" \
        --type=data

    echo "  ✓ Done: $INDEX"
    echo ""
done

echo "========================================"
echo "Export complete!"
echo "Dump directory: $DUMP_DIR"
echo ""
echo "Transfer to your local machine with:"
echo "  scp -r -i <your-key> ubuntu@<server-ip>:$DUMP_DIR ~/es-dump"
echo ""
echo "Then transfer to the test server with:"
echo "  scp -r ~/es-dump <user>@<test-server>:~/es-dump"
echo "========================================"
