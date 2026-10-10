"""rebuild de_asset_tiktok_accounts from de_connection.

    python3 /app/python/tools/rebuild_tiktok_account_projection.py            # report only
    python3 /app/python/tools/rebuild_tiktok_account_projection.py --apply    # write

The Asset Explorer's TikTok accounts are a projection of the TikTok
de_connection rows (services/tiktok_account_projection.py). Every code path that
changes a connection re-syncs its doc, so this tool is for backfill (connections
made before the projection existed) and repair (a sync that failed, or a
connection deleted outside those paths — e.g. the orphan sweep). For each
TikTok connection it syncs the doc, taking the workspace from the first team that
links it; projection docs whose connection is gone are deleted.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

logger = logging.getLogger(__name__)


def plan(es) -> dict:
    """What a rebuild would do: ``{"sync": {esid: workspace}, "delete": [esid]}``."""
    from asset_management.web.services.connection_service import PENDING_ACCOUNT_ID_PREFIX, PROVIDER_TIKTOK
    from asset_management.web.services.tiktok_account_projection import INDEX
    from lib.es_helpers import get_all_documents_paginated
    from lib.tiktok_constants import TEAM_CONNECTIONS_FIELD

    connections = {
        h["_id"]
        for h in get_all_documents_paginated("de_connection", {"term": {"provider": PROVIDER_TIKTOK}})
        if not str((h.get("_source") or {}).get("provider_account_id") or "").startswith(PENDING_ACCOUNT_ID_PREFIX)
    }
    workspace: dict[str, str] = {}
    for team in get_all_documents_paginated("de_asset_teams", {"exists": {"field": TEAM_CONNECTIONS_FIELD}}):
        src = team.get("_source") or {}
        for esid in src.get(TEAM_CONNECTIONS_FIELD) or []:
            if esid in connections and esid not in workspace and src.get("workspace_esid"):
                workspace[esid] = str(src["workspace_esid"])
    projected = set()
    if es.indices.exists(index=INDEX):
        projected = {h["_id"] for h in get_all_documents_paginated(INDEX, {"match_all": {}})}
    return {
        "sync": {esid: workspace.get(esid, "") for esid in sorted(connections)},
        "delete": sorted(projected - connections),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: report only)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from asset_management.web.services import tiktok_account_projection
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logger.error("no Elasticsearch client")
        return 1
    todo = plan(es)
    for esid, ws in todo["sync"].items():
        logger.info("sync   %s  workspace=%s", esid, ws or "(none linked yet)")
    for esid in todo["delete"]:
        logger.info("delete %s  (connection gone)", esid)
    if not args.apply:
        logger.info("report only: %d to sync, %d to delete — rerun with --apply", len(todo["sync"]), len(todo["delete"]))
        return 0

    failed = 0
    for esid, ws in todo["sync"].items():
        if not tiktok_account_projection.sync(esid, workspace_esid=ws or None):
            failed += 1
    for esid in todo["delete"]:
        es.delete(index=tiktok_account_projection.INDEX, id=esid, refresh="wait_for")
    logger.info("applied: %d synced, %d failed, %d deleted", len(todo["sync"]) - failed, failed, len(todo["delete"]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
