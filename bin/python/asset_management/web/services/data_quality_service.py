"""Data Quality Service —

Surfaces the same checks the CLI audit scripts run (find_unknown_geo_teams,
audit_country_centroids), but as a live service so the Status Reports
page can show them without anyone having to SSH into the server.

Read-only. Five-minute in-process cache so a chatty page render doesn't
pound ES — staleness is fine here because the report is for trend
spotting, not real-time monitoring (operators run the CLI scripts
during active cleanup).
"""

from __future__ import annotations

import datetime
import logging
import re
import sys
import time
from typing import Any

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from lib import job_health as jh  # noqa: E402
from lib.geocoding import _COUNTRY_CENTROIDS  # noqa: E402


# ── Cache ──────────────────────────────────────────────────────────────
# 5-minute TTL is a deliberate tradeoff: the audit hits ES with several
# aggregations (~6 indices × 1-2 queries each ≈ 1-2s wall time), and the
# data it shows is for trend spotting, not real-time monitoring. The
# operator who needs fresh results always has the CLI script.
_CACHE_TTL_SECONDS = 300
_cache: dict[str, Any] = {"timestamp": 0.0, "result": None}


_DATA_INDICES: tuple[str, ...] = (
    "*fb_insights",
    "*fb_page_insights",
    "*ig_insights",
    "*youtube_analytics",
    "*ga_metrics",
    "*echo_conversations",
)


def _resolvable_country(team: dict[str, Any]) -> str | None:
    """Mirror lib/geocoding._resolve_team_country fallback chain."""
    country = team.get("team_country")
    if isinstance(country, list):
        country = country[0] if country else None
    country = (country or "").strip()
    if country:
        return country
    text = team.get("team_country_text")
    if isinstance(text, list):
        text = text[0] if text else None
    text = (text or "").strip()
    if not text:
        return None
    codes = [c.strip() for c in text.split(",") if c.strip()]
    return codes[0] if codes else None


def _teams_without_country(es) -> list[dict[str, Any]]:
    """Return team docs whose country can't be resolved."""
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"match_all": {}},
            "_source": [
                "team_title",
                "team_country",
                "team_country_text",
                "organization_title",
            ],
        },
    )
    out = []
    for h in resp["hits"]["hits"]:
        src = h["_source"]
        if _resolvable_country(src):
            continue
        org = src.get("organization_title")
        if isinstance(org, list):
            org = org[0] if org else ""
        out.append(
            {
                "team_title": src.get("team_title", ""),
                "organization_title": org or "",
            }
        )
    return out


def _unknown_team_country_per_index(es) -> list[dict[str, Any]]:
    """For each importer index, count docs with team_country='unknown'."""
    rows = []
    for index in _DATA_INDICES:
        try:
            resp = es.search(
                index=index,
                body={
                    "size": 0,
                    "query": {"term": {"team_country": "unknown"}},
                    "aggs": {"by_team": {"terms": {"field": "team_title", "size": 30}}},
                },
            )
        except Exception:  # noqa: BLE001 — one index failing must not sink the report
            logging.exception("_unknown_team_country_per_index: search failed for %s", index)
            rows.append(
                {
                    "index": index,
                    "total": 0,
                    "error": "Query failed for this index",
                    "by_team": [],
                }
            )
            continue
        total = resp["hits"]["total"]["value"]
        if total == 0:
            continue
        rows.append(
            {
                "index": index,
                "total": total,
                "by_team": [
                    {"team_title": b["key"], "doc_count": b["doc_count"]} for b in resp["aggregations"]["by_team"]["buckets"]
                ],
            }
        )
    return rows


def _missing_location_countries(es) -> dict[str, Any]:
    """fb_insights docs with adset_targeting set but no location_countries
    — per team, plus an aggregate over the whole population."""
    resp = es.search(
        index="*fb_insights",
        body={
            "size": 0,
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "adset_targeting"}}],
                    "must_not": [{"exists": {"field": "location_countries"}}],
                }
            },
            "aggs": {"by_team": {"terms": {"field": "team_title", "size": 100}}},
        },
    )
    return {
        "total": resp["hits"]["total"]["value"],
        "by_team": [{"team_title": b["key"], "doc_count": b["doc_count"]} for b in resp["aggregations"]["by_team"]["buckets"]],
    }


def _missing_centroids(es) -> dict[str, Any]:
    """ISO2 codes in active fb_insights targeting that aren't in
    _COUNTRY_CENTROIDS. Walks adset_targeting via a sample-and-extract
    approach because the codes are nested under several different
    container keys."""
    resp = es.search(
        index="*fb_insights",
        body={
            "size": 1000,
            "query": {"exists": {"field": "adset_targeting"}},
            "_source": ["team_title", "adset_targeting.geo_locations"],
        },
        scroll="2m",
    )
    scroll_id = resp.get("_scroll_id")
    seen: dict[str, set[str]] = {}  # iso2 -> set of teams
    try:
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                break
            for h in hits:
                team = h["_source"].get("team_title", "?")
                geo = (h["_source"].get("adset_targeting") or {}).get("geo_locations") or {}
                codes = set()
                for c in geo.get("cities", []) or []:
                    if c.get("country"):
                        codes.add(str(c["country"]).upper())
                for r in geo.get("regions", []) or []:
                    if r.get("country"):
                        codes.add(str(r["country"]).upper())
                for cl in geo.get("custom_locations", []) or []:
                    if cl.get("country"):
                        codes.add(str(cl["country"]).upper())
                for gm in geo.get("geo_markets", []) or []:
                    if gm.get("country"):
                        codes.add(str(gm["country"]).upper())
                for c in geo.get("countries", []) or []:
                    if isinstance(c, dict) and c.get("country_code"):
                        codes.add(str(c["country_code"]).upper())
                for code in codes:
                    seen.setdefault(code, set()).add(team)
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

    covered = sorted(c for c in seen if c in _COUNTRY_CENTROIDS)
    missing = sorted(c for c in seen if c not in _COUNTRY_CENTROIDS)
    return {
        "covered": covered,
        "missing": [{"iso2": code, "teams": sorted(seen[code])} for code in missing],
    }


# Phase A — flag when a team stops producing FB ad docs.
# Days-since-last-doc before a team counts as stalled. A healthy daily
# import leaves yesterday's data date (1); teams that day-part campaigns
# off (e.g. one team runs nothing on Fridays) reach 2; 4+ has meant a dead
# credential or a mis-linked ad account every time it has happened
# (team A 7wk / team B 9wk / team C 19d — all found by hand, none alerted).
_CONTINUITY_STALL_DAYS = 4


def _normalized_title_index(title: str, suffix: str = "fb_insights") -> str:
    """Legacy title-keyed index name (pre-esid era): 'Team X' -> 'team_x_fb_insights'.

    ``suffix`` lets the GA window check ask for the ``ga_metrics`` /
    ``ga_metrics_window`` twins; the default keeps the FB callers unchanged."""
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    return f"{slug}_{suffix}" if slug else ""


def _pending_migrations(es) -> dict[str, Any]:
    """applied-vs-pending ES-data migrations for THIS cluster.

    update_vN scripts mutate ES runtime state git diffs cannot see; three went
    silently unapplied on prod (v16/v22/v27, found 2026-08-26 — the
    class). The de_meta_migrations marker index records what ran; anything
    shipped-but-unrecorded renders here as pending. ``unknown: True`` means the
    marker state could not be read — rendered as a warning, never as health.
    """
    from lib import migration_state

    st = migration_state.migration_status(es)
    return {
        "unknown": bool(st.get("unknown")),
        "shipped": len(st.get("available") or []),
        "applied": len(st.get("applied") or {}),
        "pending": [f"update_v{v}" for v in (st.get("pending") or [])],
    }


def _fb_import_continuity(es) -> dict[str, Any]:
    """Latest FB ad-doc date per FB-enabled team, flagging continuity breaks.

    A silently-dark team is indistinguishable from a quiet one in the logs
    (a scope-starved token returns an empty ad list, not an error;
    a mis-linked ad account imports nothing and logs nothing), so
    this check ignores WHY and alerts on the break itself: a team that has
    ad docs but none newer than _CONTINUITY_STALL_DAYS is stalled.

    Index names are lowercased by ES while team esids are mixed-case, so the
    index->team join lowercases the esid (comparing raw esids finds nothing —
    the investigation tripped on exactly this).
    """
    teams_resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"term": {"ds_facebook": "true"}},
            "_source": ["team_title", "organization_title", "team_fb_integration_status"],
        },
    )
    latest_resp = es.search(
        index="*fb_insights",
        body={
            "size": 0,
            "aggs": {
                "by_index": {
                    "terms": {"field": "_index", "size": 1000},
                    "aggs": {"latest": {"max": {"field": "kibana_date", "format": "yyyy-MM-dd"}}},
                }
            },
        },
    )
    latest_by_index: dict[str, str] = {}
    for b in latest_resp["aggregations"]["by_index"]["buckets"]:
        value = (b.get("latest") or {}).get("value_as_string")
        if value:
            latest_by_index[b["key"]] = value[:10]

    today = datetime.datetime.now(datetime.timezone.utc).date()
    stalled: list[dict[str, Any]] = []
    never_imported: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    healthy = 0
    for h in teams_resp["hits"]["hits"]:
        src = h["_source"]
        title = src.get("team_title", "")
        org = src.get("organization_title")
        if isinstance(org, list):
            org = org[0] if org else ""
        candidates = {f"{h['_id'].lower()}__fb_insights", _normalized_title_index(title)}
        dates = [latest_by_index[i] for i in candidates if i in latest_by_index]
        row = {
            "team_title": title,
            "organization_title": org or "",
            "integration_status": src.get("team_fb_integration_status", ""),
        }
        # a team whose integration status is explicitly NOT Active
        # (e.g. "Retired" — one team after the split from a partner org — or a
        # test team) is deliberately dark: flagging it forever is noise that
        # trains people to ignore the alert. Empty/missing status still gets
        # checked — fail toward alerting, never toward silence.
        status = str(row["integration_status"] or "").strip()
        if status and status != "Active":
            excluded.append(row)
            continue
        if not dates:
            never_imported.append(row)
            continue
        last = max(dates)
        days_since = (today - datetime.date.fromisoformat(last)).days
        if days_since >= _CONTINUITY_STALL_DAYS:
            stalled.append({**row, "team_esid": h["_id"], "last_doc_date": last, "days_since": days_since})
        else:
            healthy += 1
    stalled.sort(key=lambda r: -r["days_since"])

    # say WHY each team stalled. The check above deliberately ignores
    # cause, which made every stalled team look identical and hid team A / team B /
    # team C / team D being merely PAUSED rather than broken. Attribution turns
    # "stalled, unknown" into either "stalled BECAUSE <credential>" or the
    # explicit "credentials are fine, so this is probably paused campaigns".
    #
    # Best-effort and non-fatal: attribution failing must never take down the
    # continuity report, which is useful on its own.
    for row in stalled:
        try:
            from lib import credential_health as ch

            row["cause"] = ch.explain_team_stall(row.get("team_esid") or "")
        except Exception as exc:  # noqa: BLE001
            logging.warning("continuity attribution failed for %s: %s", row.get("team_title"), exc)
            row["cause"] = {
                "verdict": "unattributed",
                "detail": "Attribution unavailable for this run.",
                "credentials": [],
            }
    return {
        "threshold_days": _CONTINUITY_STALL_DAYS,
        "checked": len(teams_resp["hits"]["hits"]),
        "healthy": healthy,
        "stalled": stalled,
        "never_imported": never_imported,
        "excluded": excluded,
    }


def _latest_date_by_index(es, pattern: str, field: str, key_field: str = "_index") -> dict[str, str]:
    """``{key: newest <field> as yyyy-MM-dd}`` over every index the
    pattern matches (one terms agg; the FB continuity shape). ``key_field`` is
    ``_index`` for the per-team continuity checks; passes the per-asset
    id field (``fb_page_id`` / ``ig_user_id`` / ``ad_account_id``) to get one
    date per asset across all teams' indices."""
    resp = es.search(
        index=pattern,
        body={
            "size": 0,
            "aggs": {
                "by_index": {
                    "terms": {"field": key_field, "size": 1000},
                    "aggs": {"latest": {"max": {"field": field, "format": "yyyy-MM-dd"}}},
                }
            },
        },
    )
    out: dict[str, str] = {}
    for b in resp["aggregations"]["by_index"]["buckets"]:
        value = (b.get("latest") or {}).get("value_as_string")
        if value:
            out[b["key"]] = value[:10]
    return out


def _ga_window_continuity(es) -> dict[str, Any]:
    """GA4 dedup-window coverage per GA-enabled team.

    The GA4 first-look charts read ``*_ga_metrics_window``, which only the
    importer's best-effort window step writes. On prod (2026-09-04) 4 of 11
    ds_google_analytics teams had a window index; the other 7 had a healthy
    per-day ``*_ga_metrics`` index and blank charts, and nothing said so — the
    window step swallows its own failures and the parent import stays green.
    This check joins the two indices per team and flags a team that has per-day
    docs but no window docs (``missing_window``), or whose newest window ended
    ``_CONTINUITY_STALL_DAYS``+ days ago (``stale_window`` — the
    rolling upkeep leaves a window ending yesterday). Teams with no per-day
    docs at all are ``never_imported`` (the FB-continuity analogue). Same
    lowercased-esid index join as :func:`_fb_import_continuity`; the per-day
    pattern ``*ga_metrics`` does not match the ``_window`` twin, so the two
    lookups are disjoint.
    """
    teams_resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"term": {"ds_google_analytics": "true"}},
            "_source": ["team_title", "organization_title"],
        },
    )
    daily_latest = _latest_date_by_index(es, "*ga_metrics", "kibana_date")
    window_latest = _latest_date_by_index(es, "*ga_metrics_window", "insight_window_end")

    today = datetime.datetime.now(datetime.timezone.utc).date()
    missing_window: list[dict[str, Any]] = []
    stale_window: list[dict[str, Any]] = []
    never_imported: list[dict[str, Any]] = []
    healthy = 0
    for h in teams_resp["hits"]["hits"]:
        src = h["_source"]
        title = src.get("team_title", "")
        org = src.get("organization_title")
        if isinstance(org, list):
            org = org[0] if org else ""
        esid_lc = h["_id"].lower()
        daily_candidates = {f"{esid_lc}__ga_metrics", _normalized_title_index(title, "ga_metrics")}
        window_candidates = {f"{esid_lc}__ga_metrics_window", _normalized_title_index(title, "ga_metrics_window")}
        daily_dates = [daily_latest[i] for i in daily_candidates if i in daily_latest]
        window_dates = [window_latest[i] for i in window_candidates if i in window_latest]
        row = {"team_title": title, "organization_title": org or "", "team_esid": h["_id"]}
        if not daily_dates:
            never_imported.append(row)
            continue
        if not window_dates:
            missing_window.append({**row, "last_daily_doc_date": max(daily_dates)})
            continue
        last = max(window_dates)
        days_since = (today - datetime.date.fromisoformat(last)).days
        if days_since >= _CONTINUITY_STALL_DAYS:
            stale_window.append({**row, "last_window_end": last, "days_since": days_since})
        else:
            healthy += 1
    stale_window.sort(key=lambda r: -r["days_since"])
    return {
        "threshold_days": _CONTINUITY_STALL_DAYS,
        "checked": len(teams_resp["hits"]["hits"]),
        "healthy": healthy,
        "missing_window": missing_window,
        "stale_window": stale_window,
        "never_imported": never_imported,
    }


def _bare_property_id(value: Any) -> str:
    """GA4 property ids appear bare ("497881463") and as "properties/497881463"."""
    return str(value or "").strip().split("/")[-1]


def _ga4_connection_state(property_id: str) -> str:
    """why a quiet GA4 property is quiet, from stored state only (no
    Google call, no token decrypt): ``no_connection`` when no GA4 OAuth account
    with a token serves it (the importer skips it — reconnect in Data
    Connectors), ``connected_no_data`` when one does (Google answers with no
    rows — check the site's tag), ``unknown`` when the lookup fails."""
    try:
        from asset_management.web.services.connection_service import ConnectionService
        from lib.es_helpers import get_document_by_id

        account_esid = ConnectionService.get_ga4_oauth_account_esid_for_property(property_id)
        if not account_esid:
            return "no_connection"
        account = get_document_by_id("de_asset_ga4_accounts", account_esid) or {}
        return "connected_no_data" if account.get("ga4_account_token") else "no_connection"
    except Exception as exc:  # noqa: BLE001 — report-only; unknown is shown as unknown, never as healthy
        logging.warning("connection state for GA4 property %s failed: %s", property_id, exc)
        return "unknown"


def _ga_property_continuity(es) -> dict[str, Any]:
    """per-PROPERTY GA4 continuity for every property linked to a
    GA-enabled team.

    :func:`_ga_window_continuity` and the FB check look at a team's newest doc,
    so one live property hides a dead one. On prod (found 2026-09-29) team
    one team's web property had returned nothing for two months — the
    site's tag stopped sending to it — while the team's app property stayed
    current, and nothing said so for two months: Google answers a dead property
    with a successful empty report, which looks exactly like a quiet one.

    For each (team, linked property) this takes the newest ``kibana_date`` of
    that ``property_id`` in the team's ``*ga_metrics`` index and flags a
    property whose newest day is ``_CONTINUITY_STALL_DAYS``+ old (``stale``), a
    linked property with no docs at all (``never_imported``), and a link whose
    property doc is missing (``unresolved``). Each stale / never-imported row
    carries a ``reason`` (:func:`_ga4_connection_state`), because the two causes
    have different fixes: reconnect GA4, or fix the website's tag.
    """
    teams_resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"term": {"ds_google_analytics": "true"}},
            "_source": ["team_title", "organization_title", "team_ga4_property_esids"],
        },
    )
    teams = teams_resp["hits"]["hits"]
    prop_esids = sorted({str(e) for h in teams for e in (h["_source"].get("team_ga4_property_esids") or []) if e})
    props: dict[str, dict[str, str]] = {}
    if prop_esids:
        for doc in es.mget(index="de_asset_ga_properties", body={"ids": prop_esids}).get("docs", []):
            if doc.get("found"):
                src = doc.get("_source") or {}
                props[doc["_id"]] = {
                    "property_id": _bare_property_id(src.get("ga_property_id")),
                    "property_title": src.get("ga_property_title") or src.get("ga_property_display_name") or "",
                }

    resp = es.search(
        index="*ga_metrics",
        body={
            "size": 0,
            "aggs": {
                "by_index": {
                    "terms": {"field": "_index", "size": 1000},
                    "aggs": {
                        "by_property": {
                            "terms": {"field": "property_id", "size": 200},
                            "aggs": {"latest": {"max": {"field": "kibana_date", "format": "yyyy-MM-dd"}}},
                        }
                    },
                }
            },
        },
    )
    latest: dict[tuple[str, str], str] = {}
    for ib in resp["aggregations"]["by_index"]["buckets"]:
        for pb in ib["by_property"]["buckets"]:
            value = (pb.get("latest") or {}).get("value_as_string")
            if value:
                latest[(ib["key"], _bare_property_id(pb["key"]))] = value[:10]

    today = datetime.datetime.now(datetime.timezone.utc).date()
    stale: list[dict[str, Any]] = []
    never_imported: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    healthy = checked = 0
    for h in teams:
        src = h["_source"]
        title = src.get("team_title", "")
        org = src.get("organization_title")
        if isinstance(org, list):
            org = org[0] if org else ""
        indices = {f"{h['_id'].lower()}__ga_metrics", _normalized_title_index(title, "ga_metrics")}
        for prop_esid in src.get("team_ga4_property_esids") or []:
            checked += 1
            row = {"team_title": title, "organization_title": org or "", "team_esid": h["_id"], "property_esid": prop_esid}
            prop = props.get(str(prop_esid))
            if prop is None or not prop["property_id"]:
                unresolved.append(row)
                continue
            row.update(prop)
            dates = [latest[(i, prop["property_id"])] for i in indices if (i, prop["property_id"]) in latest]
            if not dates:
                never_imported.append({**row, "reason": _ga4_connection_state(prop["property_id"])})
                continue
            last = max(dates)
            days_since = (today - datetime.date.fromisoformat(last)).days
            if days_since >= _CONTINUITY_STALL_DAYS:
                stale.append(
                    {
                        **row,
                        "last_doc_date": last,
                        "days_since": days_since,
                        "reason": _ga4_connection_state(prop["property_id"]),
                    }
                )
            else:
                healthy += 1
    stale.sort(key=lambda r: -r["days_since"])
    return {
        "threshold_days": _CONTINUITY_STALL_DAYS,
        "checked": checked,
        "healthy": healthy,
        "stale": stale,
        "never_imported": never_imported,
        "unresolved": unresolved,
    }


def _demo_job_runs(es) -> dict[str, Any]:
    """last exit code of every tracked cron job.

    The 05:40 demo DT reseed exited 1 for five days (2026-09-02..07) and the only
    record was /var/log/kavanah_demo_dt_reseed.log. tools/run_tracked_job.py now
    writes one row per job to de_job_health; this lists them, plus every job in
    ``EXPECTED_DAILY_JOBS`` that has no row (``never_ran`` — on a cluster where
    the tracked cron lines were never provisioned, that is the finding).
    """
    jobs = jh.classify_runs(jh.list_runs(es))
    return {
        "jobs": jobs,
        "problems": sum(1 for j in jobs if j.get("state") in jh.PROBLEM_STATES),
        "never_ran": [j["job"] for j in jobs if j.get("state") == jh.STATE_NEVER_RAN],
        "threshold_hours": jh.DEFAULT_MAX_AGE_HOURS,
    }


def get_data_quality_report() -> dict[str, Any]:
    """Build the three-section data-quality report. Cached for 5 minutes."""
    now = time.time()
    if _cache["result"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
        cached = _cache["result"]
        return {**cached, "cached": True, "age_seconds": int(now - _cache["timestamp"])}

    es = get_elasticsearch_client()
    if es is None:
        return {
            "ok": False,
            "error": "Elasticsearch unavailable",
            "cached": False,
        }

    try:
        report = {
            "ok": True,
            "pending_migrations": _pending_migrations(es),
            "demo_job_runs": _demo_job_runs(es),
            "fb_import_continuity": _fb_import_continuity(es),
            "ga_window_continuity": _ga_window_continuity(es),
            "ga_property_continuity": _ga_property_continuity(es),
            "teams_without_country": _teams_without_country(es),
            "unknown_team_country_per_index": _unknown_team_country_per_index(es),
            "missing_location_countries": _missing_location_countries(es),
            "missing_centroids": _missing_centroids(es),
            "cached": False,
            "age_seconds": 0,
        }
    except Exception:
        logging.exception("data quality report failed")
        return {"ok": False, "error": "Data quality report failed."}

    _cache["result"] = report
    _cache["timestamp"] = now
    return report


def invalidate_cache() -> None:
    """Drop the cached report — used by tests and after data fixes."""
    _cache["result"] = None
    _cache["timestamp"] = 0.0
