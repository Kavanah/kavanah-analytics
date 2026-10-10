#!/usr/bin/env bash
# =============================================================================
# Install / refresh the project-wide default index template.
#
# What this does:
#   1. Calls lib.es_index_template_service.ensure_project_default_template
#      inside imports_container — same code path app.py runs at startup.
#   2. Logs success / failure.
#
# When to run:
# Once on each existing environment when first ships, so
#     environments running an unrestarted Flask process pick up the
#     wide override without waiting for the next app start.
#   - Any time the wide template was deleted / overwritten (rare).
#
# Cleanup of data streams (separate concern):
#   Existing indices created BEFORE the wide override is in place are
#   already in their wrong shape (data streams). The wide override only
#   governs FUTURE creates. To recover an existing bad data stream,
#   delete it explicitly, then trigger a fresh write — the wide
#   override will create it as a regular index this time.
#
#   Example for de_import_progress on dev-reporting:
#     docker exec imports_container python3 -c "
#       import sys; sys.path.insert(0, '/app/python')
#       from lib.es_helpers import get_elasticsearch_client
#       es = get_elasticsearch_client()
#       es.indices.delete_data_stream(name='de_import_progress')
#       print('deleted')
#     "
#   The next write to de_import_progress will then create a regular index.
# =============================================================================

set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-imports_container}"

echo "Installing  project-wide default index template..."
echo "  Container: ${CONTAINER_NAME}"
echo

docker exec "${CONTAINER_NAME}" python3 -c "
import sys
sys.path.insert(0, '/app/python')
from lib.es_index_template_service import (
    ensure_project_default_template,
    ensure_source_mapping_templates,
    TEMPLATE_NAME,
    PRIORITY,
    SOURCE_MAPPING_PRIORITY,
)

ok = ensure_project_default_template()
if ok:
    print(f'OK: installed/refreshed template {TEMPLATE_NAME!r} at priority {PRIORITY}')
else:
    print('FAIL: see container logs for the underlying ES error')
    sys.exit(1)

# per-source mapping templates (correct field types on auto-create).
results = ensure_source_mapping_templates()
bad = [s for s, v in results.items() if not v]
print(f'OK: installed/refreshed {sum(results.values())}/{len(results)} per-source '
      f'mapping templates at priority {SOURCE_MAPPING_PRIORITY}')
if bad:
    print(f'FAIL: per-source template(s) failed: {bad}')
    sys.exit(1)
"

echo
echo "Done."
echo
echo "To verify against ES:"
echo "  docker exec ${CONTAINER_NAME} python3 -c \"import sys; sys.path.insert(0, '/app/python'); from lib.es_helpers import get_elasticsearch_client; print(get_elasticsearch_client().indices.get_index_template(name='project_default_no_data_stream'))\""
