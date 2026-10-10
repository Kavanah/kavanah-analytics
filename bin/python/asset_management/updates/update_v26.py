"""Phase 1 — effective-dated team ad-account links.

Adds the ``fb_ad_account_links`` nested field to ``de_asset_teams`` and
seeds ONE open link per account-wide team from the existing scalar pair
(``fb_ad_account_esid`` + ``connection_esid``), so the link list becomes
the durable record of which ad account(s) a team runs ads from and when.

Design: the private docs The scalar
fields stay authoritative for the importer until Phase 2; this update
only makes the history representable and puts existing wiring on it.

Seed semantics:
* Only teams with ``fb_import_mode == "account_wide"`` AND a non-empty
  ``fb_ad_account_esid`` are seeded (page-filtered/legacy teams have no
  scalar ad-account wiring to describe).
* ``start_date`` is intentionally ABSENT on seeded links — the true
  adoption date is unknown; the reconciliation pass (Phase 5, gated on
  the ministry team's CSV) fills real windows and historical links.
* Idempotent: a team that already has ``fb_ad_account_links`` is
  skipped, so re-running is a no-op.
* ``fb_ad_account_id`` is resolved from the pointed-at
  ``de_asset_fb_ad_accounts`` doc; an unresolvable pointer (deleted /
  twin-cleanup casualty) is seeded WITHOUT the id and logged — the link
  still records the esid the team pointed at.
"""

import logging
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v26] %(message)s")

TEAMS_INDEX = "de_asset_teams"
AD_ACCOUNTS_INDEX = "de_asset_fb_ad_accounts"

LINKS_MAPPING = {
    "fb_ad_account_links": {
        "type": "nested",
        "properties": {
            "ad_account_esid": {"type": "keyword"},
            "fb_ad_account_id": {"type": "keyword"},
            "connection_esid": {"type": "keyword"},
            "start_date": {"type": "date"},
            "end_date": {"type": "date"},
            "source": {"type": "keyword"},
            "note": {"type": "keyword", "ignore_above": 8191},
            "updated_by": {"type": "keyword"},
            "updated_at": {"type": "date"},
        },
    }
}


def update_de_asset_teams_mapping(es) -> str:
    """Add the fb_ad_account_links nested field to de_asset_teams."""
    try:
        current_mapping = es.indices.get_mapping(index=TEAMS_INDEX)
        current_properties = current_mapping[TEAMS_INDEX]["mappings"]["properties"]
    except Exception as e:
        logging.error(f"Error fetching current mapping for {TEAMS_INDEX}: {e}")
        return "abort"

    if "fb_ad_account_links" in current_properties:
        logging.info(f"No mapping update needed for {TEAMS_INDEX}; fb_ad_account_links already exists.")
        return "success"

    try:
        es.indices.put_mapping(index=TEAMS_INDEX, body={"properties": LINKS_MAPPING})
        logging.info(f"Updated mapping for {TEAMS_INDEX}: added fb_ad_account_links (nested).")
    except Exception as e:
        logging.error(f"Error updating mapping for {TEAMS_INDEX}: {e}")
        return "abort"
    return "success"


def build_seed_link(team_source: dict, ad_account_doc_source: dict | None, now_iso: str) -> dict:
    """Build the single open seed link for an account-wide team.

    Pure so the release-plan tests can exercise the seed shape without ES.
    ``start_date`` is deliberately omitted (unknown adoption date) and
    ``end_date`` is omitted (open link = the team's current account).
    """
    link = {
        "ad_account_esid": team_source.get("fb_ad_account_esid"),
        "connection_esid": team_source.get("connection_esid"),
        "source": "update_v26-seed",
        "updated_by": "update_v26",
        "updated_at": now_iso,
    }
    fb_id = (ad_account_doc_source or {}).get("fb_ad_account_id")
    if fb_id:
        link["fb_ad_account_id"] = fb_id
    return link


def seed_links_from_scalars(es) -> str:
    """Seed fb_ad_account_links on every account-wide team that lacks it."""
    query = {
        "bool": {
            "must": [
                {"term": {"fb_import_mode": "account_wide"}},
                {"exists": {"field": "fb_ad_account_esid"}},
            ],
            "must_not": [
                {"nested": {"path": "fb_ad_account_links", "query": {"match_all": {}}}},
            ],
        }
    }
    try:
        resp = es.search(index=TEAMS_INDEX, body={"query": query, "size": 500})
    except Exception as e:
        logging.error(f"Error querying account-wide teams: {e}")
        return "abort"

    hits = resp.get("hits", {}).get("hits", []) or []
    logging.info(f"Found {len(hits)} account-wide team(s) without fb_ad_account_links to seed.")

    now_iso = datetime.now(timezone.utc).isoformat()
    seeded = 0
    for hit in hits:
        team_esid = hit["_id"]
        source = hit.get("_source") or {}
        ad_account_esid = source.get("fb_ad_account_esid")

        ad_account_source = None
        try:
            ad_account_source = es.get(index=AD_ACCOUNTS_INDEX, id=ad_account_esid).get("_source")
        except Exception as e:
            # Stale pointer (deleted/twin-cleaned doc) — seed the link anyway so
            # the pointer's history is recorded; Phase 5 reconciliation fixes it.
            logging.warning(
                f"Team {team_esid}: fb_ad_account_esid={ad_account_esid} does not "
                f"resolve to an ad-account doc ({e}); seeding link without fb_ad_account_id."
            )

        link = build_seed_link(source, ad_account_source, now_iso)
        try:
            es.update(index=TEAMS_INDEX, id=team_esid, doc={"fb_ad_account_links": [link]})
            seeded += 1
            logging.info(
                f"Seeded fb_ad_account_links on team {team_esid}: "
                f"ad_account_esid={ad_account_esid} fb_ad_account_id={link.get('fb_ad_account_id')}"
            )
        except Exception as e:
            logging.error(f"Error seeding fb_ad_account_links on team {team_esid}: {e}")
            return "abort"

    logging.info(f"Seeded {seeded} team(s).")
    return "success"


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v26:  fb_ad_account_links mapping + seed")

    if update_de_asset_teams_mapping(es) == "abort":
        logging.error("Update aborted (mapping)")
        return "abort"

    if seed_links_from_scalars(es) == "abort":
        logging.error("Update aborted (seed)")
        return "abort"

    logging.info("Update_v26 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
