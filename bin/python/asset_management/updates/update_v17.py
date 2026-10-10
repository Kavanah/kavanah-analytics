"""
update_v17.py — migrate team_city_esid -> team_city (text) and delete
de_asset_cities.

Cities are no longer a managed asset; they become a free-text
field on the Team. Existing teams may carry a team_city_esid pointing at a
de_asset_cities doc — this script reads that doc's city_title and folds it
back into the team's team_city field as plain text, drops the esid field,
then deletes the de_asset_cities index.

Idempotent:
  - If de_asset_cities is already gone, the script exits cleanly with a
    no-op log (the team scan still runs because Phase D may leave the
    index alive on environments that haven't run this script yet).
  - If no team has a non-empty team_city_esid, the team scan is a no-op
    and the index delete (if the index exists and is empty / unreferenced)
    proceeds.
  - Re-running on an already-migrated environment finds zero teams to
    update and zero index to delete.

Orphan handling:
  If a team_city_esid points at a city doc that doesn't exist (user deleted
  the city upstream of this migration), the team's team_city is left empty
  and the dead esid is dropped with a warning. We do not abort the entire
  run on a single orphan.
"""

import logging
import os
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v17] %(message)s")

TEAMS_INDEX = "de_asset_teams"
CITIES_INDEX = "de_asset_cities"


def build_city_lookup(es) -> dict[str, str]:
    """Return {city_esid: city_title} for every doc in de_asset_cities."""
    if not es.indices.exists(index=CITIES_INDEX):
        return {}
    resp = es.search(index=CITIES_INDEX, body={"query": {"match_all": {}}, "size": 1000})
    out: dict[str, str] = {}
    for hit in resp["hits"]["hits"]:
        title = hit["_source"].get("city_title", "")
        out[hit["_id"]] = title
    return out


def migrate_team_city(team_source: dict[str, Any], city_lookup: dict[str, str]) -> tuple[dict[str, Any], str]:
    """Return (updated_team_source, action) where action is one of:
        - "noop"     — no team_city_esid present, nothing to do.
        - "migrated" — esid resolved, team_city populated, esid dropped.
        - "orphan"   — esid present but no matching city doc; esid dropped, team_city left untouched.

    Pure function — does not touch ES. The caller is responsible for writing the
    result back. team_city_esid is removed from the returned dict in both
    "migrated" and "orphan" cases so a follow-up run sees a noop.
    """
    esid = team_source.get("team_city_esid")
    if not esid:
        return team_source, "noop"

    new_source = dict(team_source)
    new_source.pop("team_city_esid", None)

    title = city_lookup.get(esid)
    if title:
        new_source["team_city"] = title
        return new_source, "migrated"

    return new_source, "orphan"


def run_update() -> str:
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v17: migrate team_city_esid -> team_city and delete de_asset_cities")

    city_lookup = build_city_lookup(es)
    logging.info(f"Loaded {len(city_lookup)} city title(s) from {CITIES_INDEX}")

    if not es.indices.exists(index=TEAMS_INDEX):
        logging.info(f"Index {TEAMS_INDEX} not found; nothing to migrate")
    else:
        resp = es.search(index=TEAMS_INDEX, body={"query": {"match_all": {}}, "size": 1000})
        teams = resp["hits"]["hits"]
        logging.info(f"Scanning {len(teams)} team(s) for team_city_esid")

        migrated = 0
        orphans = 0
        unchanged = 0

        for hit in teams:
            team_id = hit["_id"]
            source = hit["_source"]
            team_title = source.get("team_title", team_id)

            new_source, action = migrate_team_city(source, city_lookup)
            if action == "noop":
                unchanged += 1
                continue

            es.index(index=TEAMS_INDEX, id=team_id, body=new_source)
            if action == "migrated":
                logging.info(f"  {team_title}: team_city populated from city_title")
                migrated += 1
            else:
                logging.warning(f"  {team_title}: orphan team_city_esid dropped (no matching city doc)")
                orphans += 1

        logging.info(f"Team scan complete: {migrated} migrated, {orphans} orphans, {unchanged} unchanged")

    if es.indices.exists(index=CITIES_INDEX):
        logging.info(f"Deleting {CITIES_INDEX}")
        es.indices.delete(index=CITIES_INDEX)
        logging.info(f"{CITIES_INDEX} deleted")
    else:
        logging.info(f"{CITIES_INDEX} not present; skipping delete")

    summary = "update_v17 complete"
    logging.info(summary)
    print(summary)
    return "success"


if __name__ == "__main__":
    run_update()
