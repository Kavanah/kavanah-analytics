"""pull DT posts from each configured site to raw JSON.

Action: ``python3 import.py disciple_tools fetch [--days_to_pull=N] [--team_ids=a,b]``

For every active ``dt_site`` connection, authenticates via Site-to-Site Link and
writes one staging file per (team, post_type) to ``/app/data/disciple_tools/raw/``.
The send step indexes those files. Splitting fetch from index mirrors the
FB/Echo importer shape (network vs. ES failures stay isolated).

the staging file holds the MAPPED documents (``docs``), never the DT
REST payload: each post goes through ``mapping.map_post`` (the
minimisation, with the street reduced to a coarse place by ``lib.geocoding``)
BEFORE anything is written, so a convert's name, street, phone, age, gender and
faith status never reach disk. ``send_json_to_elastic`` only adds ``team_title``
and indexes.

``--days_to_pull=N`` limits to posts modified in the last N days (incremental);
omitted = full sync. Upsert-by-ID downstream makes either safe to re-run.

First pull per site is ALWAYS full: the window
cuts on ``last_modified``, so a windowed bootstrap would permanently miss any
record not edited within the window — the dashboards' all-time counts would
undercount until someone happened to touch the old records. When a team's
``{team_esid}__disciple_tools`` index is missing or empty, the window is
ignored for that site; later runs honor it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import lib.logging
from import_scripts.disciple_tools import mapping
from import_scripts.disciple_tools.client import DTAuthError, DTSiteClient
from import_scripts.disciple_tools.connections import list_dt_site_configs
from lib.es_helpers import build_data_index_name, get_elasticsearch_client
from lib.geocoding import coarse_location_for_address
from lib.import_scope import scope_resolution_error

RAW_DIR = "/app/data/disciple_tools/raw"
POST_TYPES = ("contacts", "groups")
SOURCE_SLUG = "disciple_tools"


def _modified_since(params) -> int | None:
    raw = params.get("days_to_pull")
    if not raw:
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logging.warning("DT fetch: ignoring non-int days_to_pull=%r", raw)
        return None
    if days <= 0:
        return None
    return int(datetime.now(timezone.utc).timestamp()) - days * 86400


def _team_filter(params) -> set[str] | None:
    raw = params.get("team_ids")
    if not raw or raw is True:
        return None
    return {t.strip() for t in str(raw).split(",") if t.strip()}


def _effective_since(es, team_esid: str, since_ts: int | None) -> int | None:
    """Return the modified-since cutoff to use for one site, or None for full.

    First pull per site is always full: when the team's DT index is missing or
    empty, a window on ``last_modified`` would permanently skip records not
    edited within it. Fail-safe on any ES error: keep the requested window
    (the pre-existing behavior) rather than silently turning an incremental
    cron run into N full pulls.
    """
    if since_ts is None or es is None:
        return since_ts
    index_name = build_data_index_name(team_esid, SOURCE_SLUG)
    try:
        if not es.indices.exists(index=index_name):
            return None
        if int((es.count(index=index_name) or {}).get("count") or 0) == 0:
            return None
    except Exception as e:  # noqa: BLE001 — fail-safe: keep the window
        logging.warning("DT fetch: first-pull check for %s failed (%s) — keeping window", team_esid, e)
    return since_ts


def minimise_posts(posts: list[dict], team_esid: str) -> list[dict]:
    """map DT REST posts to the stored document shape before any disk write.

    ``mapping.map_post`` is the minimisation (no contact name, age,
    gender, faith status or street; the street becomes ``meta.contact_place`` /
    ``meta.contact_country`` via ``coarse_location_for_address``, which never
    sends the street off the box). ``team_title`` is filled in by the send step.
    Unsupported post types map to None and are dropped, as the send step did.
    """
    docs = []
    for post in posts:
        doc = mapping.map_post(post, team_esid, geocode=coarse_location_for_address)
        if doc is not None:
            docs.append(doc)
    return docs


def main(params):
    lib.logging.define("dt-fetch")
    os.makedirs(RAW_DIR, exist_ok=True)

    since_ts = _modified_since(params)
    # hoist the scope BEFORE resolving configs. Previously this was
    # inlined into the call below and its result discarded, so an empty `configs`
    # could not distinguish "unscoped run, fleet has no DT sites" (legitimate)
    # from "--team_ids=X resolved to zero sites" (a misconfiguration) — both
    # logged one INFO line and exited rc 0, which the Report Scheduler recorded
    # as a SUCCESS. A revoked DT token empties configs the same way.
    team_filter = _team_filter(params)
    configs = list_dt_site_configs(team_filter=team_filter)
    if not configs:
        if team_filter is not None:
            raise scope_resolution_error(
                scope_param="team_ids",
                scope_value=params.get("team_ids"),
                connector_id="disciple_tools",
            )
        logging.info("DT fetch: no configured sites — nothing to do")
        return

    es = get_elasticsearch_client() if since_ts is not None else None
    lib.logging.define("dt-fetch")  # es_helpers may have reset the prefix

    mode = "full" if since_ts is None else f"modified since {datetime.fromtimestamp(since_ts, timezone.utc):%Y-%m-%d}"
    logging.info("DT fetch: %d site(s), %s", len(configs), mode)

    total_written = 0
    for cfg in configs:
        client = DTSiteClient(cfg.dt_domain, cfg.our_domain, cfg.token)
        site_since = _effective_since(es, cfg.team_esid, since_ts)
        if since_ts is not None and site_since is None:
            logging.info("DT fetch: first pull for team %s — full sync (window ignored)", cfg.team_esid)
        for post_type in POST_TYPES:
            try:
                posts = list(client.fetch_posts(post_type, modified_since_ts=site_since))
            except DTAuthError as e:
                logging.error("DT fetch: %s @ %s — %s", post_type, cfg.dt_domain, e)
                continue
            except Exception as e:
                logging.error("DT fetch: %s @ %s failed: %s", post_type, cfg.dt_domain, e)
                continue

            # minimise BEFORE the write; the REST payload never reaches disk.
            docs = minimise_posts(posts, cfg.team_esid)
            out_path = os.path.join(RAW_DIR, f"{cfg.team_esid}__{post_type}.json")
            payload = {
                "team_esid": cfg.team_esid,
                "dt_domain": cfg.dt_domain,
                "post_type": post_type,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "docs": docs,
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            total_written += len(docs)
            logging.info("DT fetch: wrote %d %s for team %s", len(docs), post_type, cfg.team_esid)

    logging.info("DT fetch: complete — %d post(s) across %d site(s)", total_written, len(configs))
