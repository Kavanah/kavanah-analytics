#!/bin/bash
# Import Elasticsearch indices from a JSON dump directory into the test server.
# Run this on the TEST server after transferring the dump from prod.
#
# Usage:
#   bash es-import.sh [dump-directory]
#
# Example:
#   bash es-import.sh ~/es-dump

set -euo pipefail

DUMP_DIR="${1:-}"

if [ -z "$DUMP_DIR" ]; then
    read -p "Path to dump directory: " DUMP_DIR
fi

if [ ! -d "$DUMP_DIR" ]; then
    echo "ERROR: Directory not found: $DUMP_DIR"
    exit 1
fi

ES_HOST="https://localhost:9200"

read -p "ES username [elastic]: " ES_USER
ES_USER="${ES_USER:-elastic}"
read -s -p "ES password: " ES_PASS
echo

ES_URL="https://${ES_USER}:${ES_PASS}@localhost:9200"

# Verify ES is reachable
if ! curl -sk -u "$ES_USER:$ES_PASS" "$ES_HOST/_cluster/health" > /dev/null; then
    echo "ERROR: Cannot reach Elasticsearch at $ES_HOST. Check credentials and that ES is running."
    exit 1
fi

# Get unique index names from mapping files
INDICES=$(ls "$DUMP_DIR"/*_mapping.json 2>/dev/null | sed 's|.*/||;s/_mapping\.json$//' | sort)

if [ -z "$INDICES" ]; then
    echo "ERROR: No mapping files found in $DUMP_DIR"
    exit 1
fi

echo "Indices to import:"
echo "$INDICES"
echo ""

for INDEX in $INDICES; do
    echo "── Importing: $INDEX ──"

    # Delete existing index if present
    curl -sk -u "$ES_USER:$ES_PASS" -X DELETE "$ES_HOST/$INDEX" > /dev/null 2>&1 || true

    # Import mapping
    sudo docker run --rm --network host \
        -e NODE_TLS_REJECT_UNAUTHORIZED=0 \
        -v "$DUMP_DIR:/dump" \
        elasticdump/elasticsearch-dump \
        --input="/dump/${INDEX}_mapping.json" \
        --output="${ES_URL}/${INDEX}" \
        --type=mapping

    # Import data
    sudo docker run --rm --network host \
        -e NODE_TLS_REJECT_UNAUTHORIZED=0 \
        -v "$DUMP_DIR:/dump" \
        elasticdump/elasticsearch-dump \
        --input="/dump/${INDEX}_data.json" \
        --output="${ES_URL}/${INDEX}" \
        --type=data

    echo "  ✓ Done: $INDEX"
    echo ""
done

echo "========================================"
echo "Import complete!"
echo "========================================"
