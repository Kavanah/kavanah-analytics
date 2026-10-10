"""One-shot backfill: tag every existing data-source asset with workspace_esid.

 spike specified workspace_esid only on de_asset_organization
(/ F5 / tools.backfill_org_workspace_esid). extends
the same pattern to the 13 data-source asset indices plus de_asset_teams
so the Asset Explorer's per-tenant isolation can scope by a single
keyword field instead of walking team -> org -> workspace per query.

Beta state assignment rule: every existing asset doc -> the Kavanah
singleton workspace (same target as org backfill). Multi-
workspace ownership is not supported in beta; cross-workspace asset
sharing is deferred to a future share-grant index if it ever
materializes.

The script is dry-run by default. Use --apply to write.

Idempotency: each asset doc gets a partial update via es.update writing
workspace_esid only. Docs that already have workspace_esid set are
skipped (the script does not overwrite). Two consecutive --apply runs
produce updated=0 skipped=N on the second pass.

Per-index verification runs after --apply: for each target index, count
docs missing workspace_esid. Acceptance criterion: every count is zero.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_asset_workspace_esid

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_asset_workspace_esid --apply

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_asset_workspace_esid --apply --workspace-slug my-workspace
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Same three-candidate resolver as backfill_org_workspace_esid.py.
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

# every data-source asset index gets workspace_esid backfilled.
# Teams join the list too so the filter has a uniform field across the
# whole asset tree. Organizations are intentionally excluded —
# backfill already covered them and also writes organization_slug, which
# this script does not.
#
# de_asset_ga4_accounts added — the GA4 index didn't exist when
# was written, so existing GA4 account docs were never stamped and
# were invisible to workspace-scoped operators in Asset Explorer.
TARGET_INDICES = (
    "de_asset_teams",
    "de_asset_chatwoot_accounts",
    "de_asset_echo_tenants",
    "de_asset_fb_accounts",
    "de_asset_fb_apps",
    "de_asset_fb_ad_accounts",
    "de_asset_fb_app_users",
    "de_asset_fb_pages",
    "de_asset_ga_accounts",
    "de_asset_ga4_accounts",
    "de_asset_ga_properties",
    "de_asset_google_ads_customers",
    "de_asset_ig_accounts",
    "de_asset_youtube_accounts",
    "de_asset_youtube_channels",
)


def fetch_workspace_esid(es, *, workspace_slug: str) -> str | None:
    """Resolve a workspace slug to its ES _id. Returns None if missing."""
    try:
        resp = es.get(index=WORKSPACE_INDEX, id=workspace_slug)
    except Exception as e:
        if "not_found" in str(e).lower() or "404" in str(e):
            return None
        raise
    return resp.get("_id")


def fetch_docs_missing_workspace(es, index: str) -> list[str]:
    """Return ESIDs in ``index`` that lack a workspace_esid value.

    Beta state has small doc counts per index (low double digits at the
    high end). A single 10000-cap match_not_exists query covers it; if
    that ever changes, pagination is a later cleanup.
    """
    try:
        resp = es.search(
            index=index,
            body={
                "query": {"bool": {"must_not": {"exists": {"field": "workspace_esid"}}}},
                "size": 10000,
                "_source": False,
            },
            ignore_unavailable=True,
        )
    except Exception as e:
        logger.error("Could not search %s: %s", index, e)
        return []

    return [hit["_id"] for hit in resp.get("hits", {}).get("hits", [])]


def apply_to_index(
    es,
    index: str,
    *,
    target_workspace_esid: str,
    apply: bool,
) -> tuple[int, int]:
    """Plan and (optionally) execute the backfill for one index.

    Returns ``(planned, applied)``. ``planned`` is the count of docs
    that lacked workspace_esid before the run; ``applied`` is the count
    of successful es.update calls (zero in dry-run mode).
    """
    missing = fetch_docs_missing_workspace(es, index)
    planned = len(missing)
    if planned == 0:
        logger.info("  %s: 0 docs need backfill (idempotent skip).", index)
        return 0, 0

    if not apply:
        logger.info(
            "  %s: %d docs would gain workspace_esid=%s (dry-run, no writes).",
            index,
            planned,
            target_workspace_esid,
        )
        return planned, 0

    applied = 0
    for esid in missing:
        try:
            es.update(
                index=index,
                id=esid,
                doc={"workspace_esid": target_workspace_esid},
                refresh="wait_for",
            )
            applied += 1
        except Exception as e:
            logger.error("    FAILED %s/%s: %s", index, esid, e)
    logger.info(
        "  %s: applied=%d / planned=%d -> workspace_esid=%s",
        index,
        applied,
        planned,
        target_workspace_esid,
    )
    return planned, applied


def run(*, apply: bool, workspace_slug: str) -> int:
    """Plan and (optionally) apply the backfill across every target index."""
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

    logger.info(
        "Backfill plan (workspace_esid=%s, %d indices):",
        target_workspace_esid,
        len(TARGET_INDICES),
    )
    total_planned = 0
    total_applied = 0
    for index in TARGET_INDICES:
        planned, applied = apply_to_index(
            es,
            index,
            target_workspace_esid=target_workspace_esid,
            apply=apply,
        )
        total_planned += planned
        total_applied += applied

    logger.info("")
    if not apply:
        logger.info(
            "DRY-RUN totals: %d docs across %d indices would gain workspace_esid. Pass --apply.",
            total_planned,
            len(TARGET_INDICES),
        )
        return 0

    logger.info(
        "APPLY totals: applied=%d / planned=%d across %d indices.",
        total_applied,
        total_planned,
        len(TARGET_INDICES),
    )

    # Per-index verification: every target index must have zero docs
    # missing workspace_esid post-apply.
    logger.info("")
    logger.info("Verification:")
    remaining_total = 0
    for index in TARGET_INDICES:
        try:
            resp = es.count(
                index=index,
                body={"query": {"bool": {"must_not": {"exists": {"field": "workspace_esid"}}}}},
                ignore_unavailable=True,
            )
            remaining = resp.get("count", 0)
            logger.info("  %s: %d docs still missing workspace_esid", index, remaining)
            remaining_total += remaining
        except Exception as e:
            logger.warning("  %s: verification query failed: %s", index, e)

    if remaining_total > 0:
        logger.error(
            "Verification FAILED: %d docs across all indices still missing workspace_esid.",
            remaining_total,
        )
        return 1
    if total_applied < total_planned:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the asset updates. Default is dry-run.",
    )
    parser.add_argument(
        "--workspace-slug",
        default=DEFAULT_WORKSPACE_SLUG,
        help=f"Target workspace slug. Default: {DEFAULT_WORKSPACE_SLUG!r}.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply, workspace_slug=args.workspace_slug)


if __name__ == "__main__":
    raise SystemExit(main())
