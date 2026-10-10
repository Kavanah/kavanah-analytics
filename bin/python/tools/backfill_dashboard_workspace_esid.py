"""One-shot backfill: tag every existing dashboard with workspace_esid.

Pre-fix dashboards in ``de_asset_echarts_dashboards`` had no
``workspace_esid`` field. ``DashboardService.get_visible_echarts_dashboards``
let every ``visibility_mode='show_all'`` dashboard through regardless of
which workspace owned it, so a workspace-B user saw the kavanah-media
dashboards in their sidebar. The post-fix path requires every dashboard
to carry a ``workspace_esid`` so the visibility check can intersect with
the user's allowed-workspace set.

Beta-state assignment rule mirrors backfill: every existing
dashboard -> the Kavanah singleton workspace. The actual owner can be
re-stamped later via a workspace migration tool when (if) the data is
re-attributed.

The script is dry-run by default. Use ``--apply`` to write.

Idempotency: docs that already have ``workspace_esid`` set are skipped
(the script does not overwrite). Two consecutive ``--apply`` runs
produce ``applied=0 skipped=N`` on the second pass.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_dashboard_workspace_esid

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_dashboard_workspace_esid --apply

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_dashboard_workspace_esid --apply \\
        --workspace-slug my-workspace
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "kavanah-media"
WORKSPACE_INDEX = "de_asset_workspace"
DASHBOARDS_INDEX = "de_asset_echarts_dashboards"


def fetch_workspace_esid(es, *, workspace_slug: str) -> str | None:
    """Resolve a workspace slug to its ES _id. Returns None if missing.

    Mirrors the same helper in backfill_asset_workspace_esid.py — kept
    inline rather than factored to keep each backfill self-contained
    and runnable from a partially-broken cluster (a missing import in
    one backfill shouldn't stop another).
    """
    try:
        resp = es.get(index=WORKSPACE_INDEX, id=workspace_slug)
    except Exception as e:
        if "not_found" in str(e).lower() or "404" in str(e):
            return None
        raise
    return resp.get("_id")


def fetch_dashboards_missing_workspace(es) -> list[str]:
    """Return dashboard _ids that lack a workspace_esid value.

    Empty-string workspace_esid counts as missing — the save_dashboard
    path stamps an empty string when the creator resolution fails, so
    the backfill needs to catch that case too.
    """
    try:
        resp = es.search(
            index=DASHBOARDS_INDEX,
            body={
                "query": {
                    "bool": {
                        "should": [
                            {"bool": {"must_not": {"exists": {"field": "workspace_esid"}}}},
                            # workspace_esid reindexed to keyword, so
                            # the bare field gives the exact-empty-string match
                            #.
                            {"term": {"workspace_esid": ""}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "size": 10000,
                "_source": False,
            },
            ignore_unavailable=True,
        )
    except Exception as e:
        logger.error("Could not search %s: %s", DASHBOARDS_INDEX, e)
        return []

    return [hit["_id"] for hit in resp.get("hits", {}).get("hits", [])]


def run(*, apply: bool, workspace_slug: str) -> int:
    """Plan and (optionally) apply the backfill."""
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    target_workspace_esid = fetch_workspace_esid(es, workspace_slug=workspace_slug)
    if target_workspace_esid is None:
        logger.error(
            "Workspace slug %r not found in %s. Re-run tools.bootstrap_workspaces --apply first.",
            workspace_slug,
            WORKSPACE_INDEX,
        )
        return 2

    missing = fetch_dashboards_missing_workspace(es)
    planned = len(missing)
    if planned == 0:
        logger.info("All dashboards already have workspace_esid; nothing to do.")
        return 0

    if not apply:
        logger.info(
            "DRY-RUN: %d dashboard(s) would gain workspace_esid=%s (slug=%s). Pass --apply.",
            planned,
            target_workspace_esid,
            workspace_slug,
        )
        for dashboard_id in missing:
            logger.info("  - %s", dashboard_id)
        return 0

    applied = 0
    for dashboard_id in missing:
        try:
            es.update(
                index=DASHBOARDS_INDEX,
                id=dashboard_id,
                doc={"workspace_esid": target_workspace_esid},
                refresh="wait_for",
            )
            applied += 1
        except Exception as e:
            logger.error("FAILED %s/%s: %s", DASHBOARDS_INDEX, dashboard_id, e)
    logger.info(
        "APPLY: applied=%d / planned=%d -> workspace_esid=%s",
        applied,
        planned,
        target_workspace_esid,
    )

    # Verification pass: every dashboard MUST now have a workspace_esid.
    leftover = fetch_dashboards_missing_workspace(es)
    if leftover:
        logger.error(
            "Verification failed: %d dashboard(s) still lack workspace_esid:",
            len(leftover),
        )
        for dashboard_id in leftover:
            logger.error("  - %s", dashboard_id)
        return 1
    logger.info("Verification: every dashboard has workspace_esid. OK.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    parser.add_argument(
        "--workspace-slug",
        default=DEFAULT_WORKSPACE_SLUG,
        help=f"Target workspace slug (default: {DEFAULT_WORKSPACE_SLUG}).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable INFO logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logger.setLevel(logging.INFO)

    return run(apply=args.apply, workspace_slug=args.workspace_slug)


if __name__ == "__main__":
    sys.exit(main())
