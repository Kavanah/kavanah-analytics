"""Tier 2 — per-team Summary Report card overrides.

Tier 1 auto-hides Summary Report sections/cards whose data views hold no docs
for the team (or whose ds_* source toggle is off). This service adds the
explicit layer on top: a team can force-hide a card despite data (dormant
channel whose zeros look bad in a donor report) or force-show one despite
emptiness. Explicit choice always wins over the automatic behavior.

Storage: ``summary_card_overrides`` object on the ``de_asset_teams`` doc —
``{unit_key: "show" | "hide"}``, absent key = automatic. The unit-key
vocabulary is shared with charts.js' CV_UNITS registry (lockstep —
pinned by the release-plan test).

Multi-team merge (the availability endpoint may be scoped to several teams):
any team's "show" wins, else any team's "hide" wins, else automatic. The
single-team case — the normal one for a per-team report — is exact; for
unions this mirrors the data check's ANY-team-has-data posture.
"""

from __future__ import annotations

import logging

from lib.es_helpers import dual_term, dual_terms

TEAMS_INDEX = "de_asset_teams"
OVERRIDES_FIELD = "summary_card_overrides"

# Ordered vocabulary: (unit_key, panel label). unit_key MUST match charts.js
# CV_UNITS el keys — the lockstep test pins both directions.
SUMMARY_UNITS: list[tuple[str, str]] = [
    ("row-fb-reach", "Reach — Facebook"),
    ("row-ig-reach", "Reach — Instagram"),
    ("row-web-users", "Reach — Website Users"),
    ("row-meta-reach", "Reach — Facebook Page Viewers"),
    ("row-tt-video-views", "Reach — TikTok Video Views"),
    ("card-videos", "Engage — Videos Watched / App Installs"),
    ("card-fb", "Engage — Facebook"),
    ("card-ig", "Engage — Instagram"),
    ("card-yt", "Engage — YouTube"),
    ("card-tt", "Engage — TikTok"),
    ("cell-msg-contacts", "Interact — Messaging Contacts"),
    ("cell-msg-received", "Interact — Messages Received"),
    ("cell-msg-sent", "Interact — Messages Sent"),
    ("cell-comments", "Interact — Comments"),
    ("cell-spiritual", "Interact — Spiritual Conversations"),
    ("sec-journey", "Jesus Journey section"),
]
_UNIT_KEYS = {k for k, _ in SUMMARY_UNITS}
_VALID_STATES = ("show", "hide")

# a partial-doc update merges objects key by key, so writing {}
# changed nothing and a dropped key survived. The script replaces the whole
# map, and removes the field when no overrides are left.
_REPLACE_OVERRIDES_SCRIPT = (
    "if (params.overrides.isEmpty()) { ctx._source.remove(params.field) } "
    "else { ctx._source[params.field] = params.overrides }"
)

# Unit key -> the Summary section it lives in (whole-section rollup for the
# canvas seeder's seed-time skip).
UNIT_SECTION = {
    "row-fb-reach": "sec-reach",
    "row-ig-reach": "sec-reach",
    "row-web-users": "sec-reach",
    "row-meta-reach": "sec-reach",
    "row-tt-video-views": "sec-reach",
    "card-videos": "sec-engage",
    "card-fb": "sec-engage",
    "card-ig": "sec-engage",
    "card-yt": "sec-engage",
    "card-tt": "sec-engage",
    "cell-msg-contacts": "sec-interact",
    "cell-msg-received": "sec-interact",
    "cell-msg-sent": "sec-interact",
    "cell-comments": "sec-interact",
    "cell-spiritual": "sec-interact",
    "sec-journey": "sec-journey",
}

# Normalized data-view source suffix -> governing ds_* toggle. Single home for
# this map (app.py's availability endpoint aliases it as
# _REQUESTED_PATTERN_SOURCE_DS); doubles as the ?pattern= validation allowlist
# there. disciple_tools maps to None — no ds_* toggle exists for DT on team
# docs, so data existence alone governs it.
PATTERN_SOURCE_DS = {
    "fb_insights": "ds_facebook",
    # per-country delivery breakdown; governed by the Facebook toggle like the platform docs.
    "fb_insights_country": "ds_facebook",
    "fb_page_insights": "ds_facebook",
    "ig_insights": "ds_instagram",
    "google_ads": "ds_google_ads",
    "youtube_analytics": "ds_youtube",
    "ga_metrics": "ds_google_analytics",
    "echo_conversations": "ds_echo",
    "chatwoot_insights": "ds_chatwoot",
    # archived Echo months; governed by the Echo toggle like the raw docs.
    "conversation_rollups": "ds_echo",
    "tiktok_organic": "ds_tiktok",
    "disciple_tools": None,
}
_DS_FIELDS = sorted({v for v in PATTERN_SOURCE_DS.values() if v})


def sanitize_overrides(raw: dict | None) -> dict:
    """Keep only known unit keys with valid states. Anything else is dropped
    silently — an unknown key from a stale client degrades to automatic."""
    out: dict = {}
    for k, v in (raw or {}).items():
        if k in _UNIT_KEYS and v in _VALID_STATES:
            out[k] = v
    return out


def _scoped_team(es, team_esid: str, allowed_workspaces):
    """Return the team's _source, or None when missing OR out of the caller's
    workspace scope (deny collapses into not-found, per the pattern).
    ``allowed_workspaces`` is None for unrestricted callers."""
    try:
        doc = es.get(index=TEAMS_INDEX, id=team_esid)
    except Exception:  # noqa: BLE001 — missing/error both read as not-found
        return None
    source = doc.get("_source") or {}
    if allowed_workspaces is not None:
        if (source.get("workspace_esid") or "") not in set(allowed_workspaces):
            return None
    return source


def get_overrides(es, team_esid: str, allowed_workspaces) -> dict | None:
    """Current overrides for one team, or None when the team is not visible
    to the caller (missing / out of scope)."""
    source = _scoped_team(es, team_esid, allowed_workspaces)
    if source is None:
        return None
    return sanitize_overrides(source.get(OVERRIDES_FIELD))


def set_overrides(es, team_esid: str, raw_overrides: dict | None, allowed_workspaces) -> dict | None:
    """Replace the team's overrides with the sanitized map. Returns the map
    read back from the doc (never the input, so a write that did
    not land cannot report success), or None when the team is not visible to
    the caller."""
    source = _scoped_team(es, team_esid, allowed_workspaces)
    if source is None:
        return None
    overrides = sanitize_overrides(raw_overrides)
    try:
        es.update(
            index=TEAMS_INDEX,
            id=team_esid,
            script={
                "source": _REPLACE_OVERRIDES_SCRIPT,
                "lang": "painless",
                "params": {"field": OVERRIDES_FIELD, "overrides": overrides},
            },
            refresh="wait_for",
        )
        stored = es.get(index=TEAMS_INDEX, id=team_esid)
    except Exception as e:  # noqa: BLE001 — surfaced as a 500 by the route
        logging.warning("summary_visibility set_overrides %s: %s", team_esid, e)
        raise
    return sanitize_overrides((stored.get("_source") or {}).get(OVERRIDES_FIELD))


def _title_query(titles) -> dict:
    should = []
    for t in titles:
        should.append({"term": {"team_title.keyword": t}})
        should.append({"term": {"team_title": t}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _ds_flags_for_titles(es, titles) -> dict:
    """Union of the teams' ds_* toggles (any team true -> true), matching the
    availability endpoint's `_get_team_data_source_flags` semantics. All-True
    on lookup failure — a broken flags read must not hide sections."""
    try:
        result = es.search(
            index=TEAMS_INDEX,
            body={"query": _title_query(titles), "size": len(titles) * 2, "_source": _DS_FIELDS},
            ignore_unavailable=True,
        )
        hits = result.get("hits", {}).get("hits", [])
    except Exception as e:  # noqa: BLE001 — fail open (visible)
        logging.warning("summary_visibility _ds_flags_for_titles: %s", e)
        return {k: True for k in _DS_FIELDS}
    if not hits:
        return {k: True for k in _DS_FIELDS}
    flags = {k: False for k in _DS_FIELDS}
    for h in hits:
        src = h.get("_source") or {}
        for k in _DS_FIELDS:
            if str(src.get(k, "")).lower() == "true":
                flags[k] = True
    return flags


def visible_patterns_for_titles(es, team_titles, patterns) -> dict:
    """seed-time — per-pattern visibility for a team scope:
    existence (any docs at all, msearch) AND the governing ds_* toggle.
    Same semantics as the availability endpoint's ?pattern= answer. Empty
    dict on no scope / no patterns / lookup failure (callers fail open)."""
    titles = [t for t in (team_titles or []) if t]
    pats = [p for p in (patterns or []) if p]
    if not titles or not pats:
        return {}
    team_query = _title_query(titles)
    body = []
    for p in pats:
        body.append({"index": p, "ignore_unavailable": True})
        body.append({"size": 0, "query": team_query})
    try:
        results = es.msearch(body=body)
    except Exception as e:  # noqa: BLE001 — fail open
        logging.warning("summary_visibility visible_patterns_for_titles: %s", e)
        return {}
    ds_flags = _ds_flags_for_titles(es, titles)
    out = {}
    for i, p in enumerate(pats):
        resp = (results.get("responses") or [{}] * len(pats))[i]
        total = resp.get("hits", {}).get("total", {})
        count = total.get("value", 0) if isinstance(total, dict) else (total or 0)
        visible = count > 0 and "error" not in resp
        ds_field = PATTERN_SOURCE_DS.get(p.lstrip("*_"))
        if ds_field and not ds_flags.get(ds_field, False):
            visible = False
        out[p] = visible
    return out


def teams_in_workspace(es, workspace_esid: str, team_titles) -> list[dict]:
    """the team docs titled ``team_titles`` INSIDE one workspace,
    as search hits (``_id`` = team ESID; ``_source`` carries the title, the
    overrides and the ds_* toggles). Titles collide across workspaces
    (every wizard signup's first team is "Team 1"), so the title-keyed
    helpers below let any tenant's same-titled team steer a report. Empty
    list on no workspace / no titles / lookup failure — callers fall back
    to their earlier behavior."""
    titles = [t for t in (team_titles or []) if t]
    if not workspace_esid or not titles:
        return []
    try:
        result = es.search(
            index=TEAMS_INDEX,
            body={
                "query": {"bool": {"filter": [dual_term("workspace_esid", workspace_esid), _title_query(titles)]}},
                "size": len(titles) * 2,
                # ds_* (not _DS_FIELDS): the availability endpoint reads its own
                # toggle list off these hits too.
                "_source": ["team_title", OVERRIDES_FIELD, "ds_*"],
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — callers fall back to the title path
        logging.warning("summary_visibility teams_in_workspace: %s", e)
        return []
    return result.get("hits", {}).get("hits", [])


def visible_patterns_for_teams(es, team_hits, patterns) -> dict:
    """``visible_patterns_for_titles`` keyed by team ESID: a
    pattern is visible when it holds docs whose ``team_esid`` is one of the
    teams AND the governing ds_* toggle is on for any of them. Empty dict on
    no teams / no patterns / lookup failure (callers fail open)."""
    esids = [h["_id"] for h in (team_hits or []) if h.get("_id")]
    pats = [p for p in (patterns or []) if p]
    if not esids or not pats:
        return {}
    team_query = dual_terms("team_esid", esids)
    body = []
    for p in pats:
        body.append({"index": p, "ignore_unavailable": True})
        body.append({"size": 0, "query": team_query})
    try:
        results = es.msearch(body=body)
    except Exception as e:  # noqa: BLE001 — fail open
        logging.warning("summary_visibility visible_patterns_for_teams: %s", e)
        return {}
    ds_flags = {k: any(str((h.get("_source") or {}).get(k, "")).lower() == "true" for h in team_hits) for k in _DS_FIELDS}
    out = {}
    for i, p in enumerate(pats):
        resp = (results.get("responses") or [{}] * len(pats))[i]
        total = resp.get("hits", {}).get("total", {})
        count = total.get("value", 0) if isinstance(total, dict) else (total or 0)
        visible = count > 0 and "error" not in resp
        ds_field = PATTERN_SOURCE_DS.get(p.lstrip("*_"))
        if ds_field and not ds_flags.get(ds_field, False):
            visible = False
        out[p] = visible
    return out


def merged_overrides_for_teams(team_hits) -> dict:
    """Merge the overrides carried on ``team_hits``: any 'show' wins, else
    any 'hide' wins. The single home of the merge rule (keys the
    teams by ESID; the title path below feeds its hits through here too)."""
    merged: dict = {}
    for hit in team_hits or []:
        for k, v in sanitize_overrides((hit.get("_source") or {}).get(OVERRIDES_FIELD)).items():
            if merged.get(k) == "show":
                continue  # an earlier team's 'show' already won this key
            merged[k] = v  # 'show' overwrites a 'hide'; 'hide' fills empty
    return merged


def merged_overrides_for_titles(es, team_titles) -> dict:
    """Merge overrides across the teams matching ``team_titles`` for the
    availability endpoint: any 'show' wins, else any 'hide' wins. Empty dict
    on no titles, lookup failure, or no opinions — automatic behavior.

    Title-keyed across every workspace — the fallback when no dashboard
    workspace is known; prefer ``teams_in_workspace`` +
    ``merged_overrides_for_teams``."""
    titles = [t for t in (team_titles or []) if t]
    if not titles:
        return {}
    try:
        should = []
        for t in titles:
            should.append({"term": {"team_title.keyword": t}})
            should.append({"term": {"team_title": t}})
        result = es.search(
            index=TEAMS_INDEX,
            body={
                "query": {"bool": {"should": should, "minimum_should_match": 1}},
                "size": len(titles) * 2,
                "_source": [OVERRIDES_FIELD],
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — overrides are an overlay; fail to automatic
        logging.warning("summary_visibility merged_overrides_for_titles: %s", e)
        return {}
    return merged_overrides_for_teams(result.get("hits", {}).get("hits", []))
