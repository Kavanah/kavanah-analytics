"""backfill workspace_esid on Google Ads / Echo / Chatwoot assets,
resolving each asset's workspace FROM THE TEAM THAT LINKS IT.

Why a new tool (not ``backfill_asset_workspace_esid``): that older tool
stamps every orphan with the single ``kavanah-media`` beta singleton. That was
correct in beta but is now WRONG — staging/prod are multi-workspace,
so an orphaned asset whose team lives in another workspace would be mis-stamped
into the wrong tenant (worse than null: it would surface in the wrong tenant's
Asset Explorer and scope the importer wrong). This tool resolves the *correct*
workspace per asset by reading the linking team's ``workspace_esid`` — the same
source the live fix (``connector_wizard_service._stamp_asset_workspace``) uses.

Scope: the three connectors the workspace-stamp sweeps missed
(``configure_google_ads_customer`` / ``configure_echo`` / ``configure_chatwoot``
created their assets without a stamp, so any added via the wizard since they
shipped is orphaned and hidden in Asset Explorer). GA4 / YouTube / Facebook
already stamp on create and are not touched here.

Resolution: each connector links exactly ONE asset per team via a single
``*_esid`` field on the team doc. For each orphaned asset we find the team whose
link field == the asset's ``_id`` and stamp the asset with that team's
``workspace_esid``. An orphan that NO team links (a true orphan — e.g. a
pre-dedup duplicate) is left untouched and reported; a stamp without a real
owner would only re-create the mis-tenant problem.

Dry-run by default. ``--apply`` writes. Idempotent: a row that already has
``workspace_esid`` is never queried (we only fetch missing-field rows) and never
overwritten.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_connector_workspace_from_team

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_connector_workspace_from_team --apply
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

TEAMS_INDEX = "de_asset_teams"

# (asset index, the single team field that points at this asset's _id).
CONNECTORS = (
    ("de_asset_google_ads_customers", "google_ads_customer_esid"),
    ("de_asset_echo_tenants", "echo_tenant_esid"),
    ("de_asset_chatwoot_accounts", "chatwoot_account_esid"),
)


def _ids_missing_workspace(es, index: str) -> list[str]:
    """ESIDs in ``index`` that lack a workspace_esid value."""
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


def _team_workspace_for_asset(es, link_field: str, asset_esid: str) -> str | None:
    """Find the team whose ``link_field`` == ``asset_esid`` and return that
    team's workspace_esid. Matches on the ``.keyword`` subfield — the bare text
    field silently returns 0 after analysis (the mapping trap).
    Returns None when no team links the asset or the team has no workspace_esid.
    """
    try:
        resp = es.search(
            index=TEAMS_INDEX,
            body={
                "query": {"term": {f"{link_field}.keyword": asset_esid}},
                "size": 1,
                "_source": ["workspace_esid"],
            },
            ignore_unavailable=True,
        )
    except Exception as e:
        logger.error("    team lookup failed (%s=%s): %s", link_field, asset_esid, e)
        return None
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return None
    return ((hits[0].get("_source") or {}).get("workspace_esid") or "").strip() or None


def _backfill_connector(es, index: str, link_field: str, *, apply: bool) -> tuple[int, int, int]:
    """Returns (planned, applied, unowned) for one connector index.

    ``planned``  — orphaned assets that resolved to a team workspace.
    ``applied``  — successful writes (0 in dry-run).
    ``unowned``  — orphaned assets that NO team links (left untouched).
    """
    missing = _ids_missing_workspace(es, index)
    if not missing:
        logger.info("  %s: 0 orphaned docs.", index)
        return 0, 0, 0

    planned = applied = unowned = 0
    for esid in missing:
        ws = _team_workspace_for_asset(es, link_field, esid)
        if not ws:
            unowned += 1
            logger.warning("    %s/%s: no linking team (or team has no workspace) — SKIPPED.", index, esid)
            continue
        planned += 1
        if not apply:
            logger.info("    %s/%s would gain workspace_esid=%s (dry-run).", index, esid, ws)
            continue
        try:
            es.update(index=index, id=esid, doc={"workspace_esid": ws}, refresh="wait_for")
            applied += 1
            logger.info("    %s/%s -> workspace_esid=%s", index, esid, ws)
        except Exception as e:
            logger.error("    FAILED %s/%s: %s", index, esid, e)
    logger.info(
        "  %s: planned=%d applied=%d unowned=%d (of %d orphaned)",
        index,
        planned,
        applied,
        unowned,
        len(missing),
    )
    return planned, applied, unowned


def run(*, apply: bool) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    logger.info("connector workspace backfill (resolve from linking team):")
    total_planned = total_applied = total_unowned = 0
    for index, link_field in CONNECTORS:
        p, a, u = _backfill_connector(es, index, link_field, apply=apply)
        total_planned += p
        total_applied += a
        total_unowned += u

    logger.info("")
    if not apply:
        logger.info(
            "DRY-RUN: %d orphan(s) would be stamped from their team; %d unowned orphan(s) skipped. Pass --apply.",
            total_planned,
            total_unowned,
        )
        return 0

    logger.info(
        "APPLY: applied=%d / planned=%d; %d unowned orphan(s) left untouched.",
        total_applied,
        total_planned,
        total_unowned,
    )
    if total_unowned:
        logger.info(
            "NOTE: %d orphan(s) had no linking team — likely pre-dedup duplicates. "
            "Inspect/delete them manually rather than stamping a guessed workspace.",
            total_unowned,
        )
    return 0 if total_applied == total_planned else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Write the updates. Default is dry-run.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
