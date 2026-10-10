"""
Bitly Link Clicks Fetcher

Fetches daily click counts for every bitlink each ACTIVE Bitly connection can
see, attributes each link to a team via its Bitly TAGS, and saves the results
as JSON for later Elasticsearch indexing.

Configuration (Phase 3): the spike config /app/reporting_configs/bitly.yaml is
RETIRED. Credentials and team scope now come from ``de_connection``: the Data
Connectors wizard stores one encrypted API token per team (provider=bitly)
plus a tag filter on the paired ``bitly_account`` connection_asset
(``metadata.tags``). See import_scripts/bitly/connections.py.

Attribution is tag-driven and strict, evaluated across ALL active connections:

  * a link whose tags match exactly ONE team's filter → attributed to it;
  * a link matching ZERO teams → SKIPPED with a logged warning (counted);
  * a link matching MORE THAN ONE team → SKIPPED with a logged error. Never
    guess attribution (hard project rule — decline and report, don't guess).

Usage:
    python3 /app/python/import.py bitly get_link_clicks [--days=N]
"""

import json
import logging
import os
import urllib.parse
from datetime import datetime

import requests

import lib.logging as custom_logging

logger = logging.getLogger(__name__)

BITLY_API_BASE = "https://api-ssl.bitly.com/v4"
OUTPUT_DIR = "/app/data/bitly/link_clicks/"

DEFAULT_DAYS = 3
MAX_DAYS = 30
PAGE_SIZE = 50
REQUEST_TIMEOUT = 30  # seconds


def match_link_to_teams(link_tags, configs):
    """
    Match a link's Bitly tags against every connection's tag filter.

    Pure function (unit-testable). ``configs`` is a list of
    ``BitlyConnectionConfig``-shaped objects (``team_esid`` + ``tags``).
    Returns the list of DISTINCT team_esids whose tag filter intersects the
    link's tags, preserving configs order for deterministic reporting. The
    caller enforces the exactly-one rule: zero matches → skip with warning;
    more than one team → skip with error (never guess attribution).
    """
    link_tag_set = set(link_tags or [])
    matched: list[str] = []
    for config in configs or []:
        if config.team_esid in matched:
            continue
        if link_tag_set & set(config.tags or ()):
            matched.append(config.team_esid)
    return matched


def sanitize_link_id(link_id):
    """Return a filesystem/ES-doc-id safe form of a bitlink id.

    Bitlink ids look like ``bit.ly/3abcDEF`` — the slash must not reach a
    deterministic ES _id or a filename.
    """
    return str(link_id or "").replace("/", "_")


def _bitly_get(api_token, path, params=None):
    """GET {BITLY_API_BASE}{path} with bearer auth; returns parsed JSON.

    Raises requests.HTTPError on a non-2xx response (the caller decides how to
    degrade — per-link errors are skipped, account-level errors propagate).
    """
    response = requests.get(
        f"{BITLY_API_BASE}{path}",
        headers={"Authorization": f"Bearer {api_token}"},
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def get_groups(api_token):
    """Return the list of group dicts the token can see (GET /groups)."""
    payload = _bitly_get(api_token, "/groups")
    groups = payload.get("groups", []) or []
    logging.info("Found %d Bitly group(s)", len(groups))
    return groups


def get_group_bitlinks(api_token, group_guid):
    """Return every bitlink in a group (GET /groups/{guid}/bitlinks, paged).

    Pages with size=50 until a page comes back short/empty (Bitly's
    ``pagination.next`` is also honored as the continuation signal).
    """
    links = []
    page = 1
    while True:
        payload = _bitly_get(
            api_token,
            f"/groups/{urllib.parse.quote(group_guid, safe='')}/bitlinks",
            params={"size": PAGE_SIZE, "page": page},
        )
        page_links = payload.get("links", []) or []
        links.extend(page_links)
        pagination = payload.get("pagination", {}) or {}
        if len(page_links) < PAGE_SIZE or not pagination.get("next"):
            break
        page += 1
    logging.info("Group %s: %d bitlink(s)", group_guid, len(links))
    return links


def get_link_clicks(api_token, link_id, days):
    """Return the daily click points for a bitlink.

    GET /bitlinks/{id}/clicks?unit=day&units=N → list of
    ``{"date": "...T00:00:00+0000", "clicks": n}`` dicts (may be empty).
    """
    quoted = urllib.parse.quote(str(link_id), safe="")
    payload = _bitly_get(api_token, f"/bitlinks/{quoted}/clicks", params={"unit": "day", "units": days})
    return payload.get("link_clicks", []) or []


def build_click_docs(link, click_points, team):
    """
    Transform one bitlink + its daily click points into ES-ready docs.

    One doc per (link, day) — all returned daily points are written, including
    zero-click days; the deterministic _id (see send_json_to_elastic) makes
    re-runs safe upserts.

    Args:
        link: Bitlink dict from the API (id, title, long_url, tags).
        click_points: List of {"date": ..., "clicks": ...} dicts.
        team: {"team_title": ..., "team_esid": ...} for the attributed team.

    Returns:
        list: One dict per daily point.
    """
    docs = []
    for point in click_points or []:
        # Bitly returns ISO datetimes ("2026-08-04T00:00:00+0000"); the ES
        # `date` field is yyyy-MM-dd so keep the date part only.
        day = str(point.get("date", ""))[:10]
        if not day:
            continue
        docs.append(
            {
                "@timestamp": day,
                "kibana_date": day,
                "date": day,
                "record_type": "link_clicks",
                "link_id": link.get("id", ""),
                "link_title": link.get("title", ""),
                "long_url": link.get("long_url", ""),
                "tags": list(link.get("tags", []) or []),
                "clicks": int(point.get("clicks", 0) or 0),
                "team_title": team.get("team_title", "Unknown"),
                "team_esid": team.get("team_esid", ""),
            }
        )
    return docs


def resolve_days(raw_days):
    """Clamp the --days argument to [1, MAX_DAYS]; default DEFAULT_DAYS.

    A standalone ``--days`` flag arrives as True from import.py's parser —
    treated as absent. A non-numeric value raises ValueError (fail loudly, per
    the contract, rather than silently importing a default window).
    """
    if raw_days is None or raw_days is True:
        return DEFAULT_DAYS
    days = int(raw_days)
    if days < 1:
        return 1
    if days > MAX_DAYS:
        logging.warning("--days=%d exceeds the Bitly window cap; clamping to %d", days, MAX_DAYS)
        return MAX_DAYS
    return days


def save_click_docs(docs, output_dir=OUTPUT_DIR):
    """Write one JSON file for this run containing all fetched docs."""
    os.makedirs(output_dir, exist_ok=True)
    filename = f"bitly_clicks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w") as f:
        json.dump(docs, f)
    logging.info("Saved %d doc(s) to %s", len(docs), filepath)
    return filepath


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict of command-line parameters
                - days: Trailing window of daily click points to pull
                        (default 3, max 30)
    """
    custom_logging.define("bitly-get-link-clicks")

    days = resolve_days(params.get("days"))

    # Scope errors propagate (no active connections must
    # fail the run, not exit 0 as a successful import of zero rows). The
    # orchestrator (run_daily_imports) pre-checks this and refuses to run,
    # so raising here is the defence for a direct invocation.
    from import_scripts.bitly.connections import list_bitly_connection_configs

    configs = list_bitly_connection_configs()
    if not configs:
        raise ValueError(
            "no active Bitly connections in de_connection — no team scope is "
            "configured, so nothing can be attributed. Connect Bitly from the "
            "Data Connectors page. Nothing was imported."
        )

    logging.info(
        "Starting Bitly link-clicks fetch (last %d day(s), %d connection(s))",
        days,
        len(configs),
    )

    docs = []
    matched_links = 0
    skipped_no_team = 0
    skipped_multi_team = 0
    # A link may be visible to more than one connection (two teams sharing a
    # Bitly account/token). Track handled link ids so a link is fetched and
    # counted once, under the first connection that can see it.
    seen_link_ids: set = set()

    for config in configs:
        for group in get_groups(config.token):
            group_guid = group.get("guid")
            if not group_guid:
                continue
            for link in get_group_bitlinks(config.token, group_guid):
                link_id = link.get("id", "")
                if link_id in seen_link_ids:
                    continue
                # Attribution is evaluated across ALL connections' tag
                # filters so a cross-team tag collision is caught even when
                # only one connection can see the link.
                matches = match_link_to_teams(link.get("tags"), configs)

                if len(matches) == 0:
                    seen_link_ids.add(link_id)
                    skipped_no_team += 1
                    logging.warning(
                        "bitly: skipping link %s — its tags %s match no connected team's tag filter",
                        link_id,
                        list(link.get("tags", []) or []),
                    )
                    continue
                if len(matches) > 1:
                    seen_link_ids.add(link_id)
                    skipped_multi_team += 1
                    logging.error(
                        "bitly: skipping link %s — its tags match MORE THAN ONE connected "
                        "team (%s); attribution is ambiguous and is never guessed. Fix the "
                        "link's tags in Bitly or the teams' tag filters.",
                        link_id,
                        matches,
                    )
                    continue

                if matches[0] != config.team_esid:
                    # The link belongs to a DIFFERENT team's filter. Leave it
                    # un-seen so that team's own connection (which may hold a
                    # different token) fetches it with its own credential.
                    continue

                seen_link_ids.add(link_id)
                team = {"team_title": config.team_title, "team_esid": config.team_esid}
                try:
                    click_points = get_link_clicks(config.token, link_id, days)
                except requests.RequestException as e:
                    logging.error("bitly: failed to fetch clicks for link %s: %s", link_id, e)
                    continue
                docs.extend(build_click_docs(link, click_points, team))
                matched_links += 1

    if docs:
        save_click_docs(docs)

    logging.info(
        "Bitly fetch complete: %d link(s) attributed (%d doc(s)); "
        "%d link(s) skipped with no matching team; %d link(s) skipped as ambiguous (multi-team)",
        matched_links,
        len(docs),
        skipped_no_team,
        skipped_multi_team,
    )


if __name__ == "__main__":
    # For standalone testing
    main({})
