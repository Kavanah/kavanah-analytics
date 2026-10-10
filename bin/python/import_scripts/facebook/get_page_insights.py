import sys
import os
import json
import logging
import time
import lib.logging
from datetime import datetime, timezone, timedelta
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError
from import_scripts.facebook.lib.assets import (
    get_fb_teams,
    get_fb_apps,
    get_fb_app_users,
    get_fb_pages,
    get_fb_accounts,
    team_matches_team_ids,
    _title_match_allowed,
)
from import_scripts.facebook.send_post_json_to_elastic import INSIGHTS_FAILED_FLAG
from import_scripts.facebook.get_insights import (
    _FB_RATE_LIMIT_ERROR_CODES,  # shared throttle code set
    _FB_RATE_LIMIT_ERROR_SUBCODES,  # shared throttle subcode set
    _RateLimitBailout,  # shared graceful-bail sentinel (backfill mode)
    _is_rate_limit_error,  # shared throttle-error recognizer
    _source_enabled_for_team,  # shared per-team source toggle (cached per run)
    check_and_pause_for_account,
    get_fb_app_user_token,
    save_data_to_file,
)
from lib.encryption import decrypt
from lib.es_helpers import build_data_index_name, indexed_doc_present
from lib.fb_constants import FB_GRAPH_URL as _FB_GRAPH_BASE
from lib.fb_constants import FB_GRAPH_HTTP_TIMEOUT, FB_GRAPH_SDK_TIMEOUT
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date

RETRY_DELAYS = [60, 120, 240]  # seconds for exponential backoff (3 retries) — 429 rate-limit

# follow-up 2 — server-error (non-429) backoff for the flaky page
# video-views endpoint. Meta's Graph API returns transient HTTP 500s on the
# video-views call for many pages; the old 5/10/20s ladder (3 retries = 35s
# wasted PER failing page) dominated page-import runtime (~25min when many
# pages were flaky). A 5xx is server-side and rarely clears within seconds,
# so long waits don't help — retry briefly (2 short attempts) then degrade to
# 0 + the "re-run to backfill" log. Rate-limit (429) backoff is UNCHANGED
# (RETRY_DELAYS) — 429 genuinely needs the long waits to respect Meta's quota.
VIDEO_VIEWS_SERVER_ERROR_DELAYS = [3, 6]  # seconds, for non-429 (5xx) on video views
INTER_PAGE_SLEEP = 0.5  # seconds between pages to smooth API bursts
FB_GRAPH_URL = f"{_FB_GRAPH_BASE}/"
FB_BATCH_SIZE = 50  # Facebook max requests per batch call

# page-posts trailing refresh window. Post engagement (reactions,
# comments, shares) keeps mutating after publish, so every run re-pulls ALL
# posts published in the last N days (the ecosystem-standard lookback-window
# model — cf. Airbyte's insights_lookback_window, default 28) instead of the
# old "latest 25 posts" count snapshot, whose freshness depended on posting
# frequency. Deeper history comes from backfill: a --start_date earlier than
# the window widens the pull to cover it (see _posts_since_timestamp).
PAGE_POSTS_WINDOW_DAYS = 28
PAGE_POSTS_PAGE_SIZE = 100  # Graph cursor page size, NOT a semantic post cap
# Runaway-pagination backstop: 20 pages x 100 posts covers any sane window;
# hitting it logs the truncation loudly (no silent caps).
PAGE_POSTS_MAX_PAGES = 20
# when Meta answers a posts page with code 1 "Please reduce the amount
# of data you're asking for" (one prod team, 2026-10-01: a 2.5-year window of long
# posts), the whole fetch restarts at half the page size, down to this floor.
# The total cap stays PAGE_POSTS_PAGE_SIZE * PAGE_POSTS_MAX_PAGES posts.
PAGE_POSTS_MIN_PAGE_SIZE = 10

# per-post insight metrics for the posts table. Probed one metric
# per request on Graph v25.0 (staging, 2026-09-28, 6 posts across 3 pages):
# post_impressions, post_impressions_unique, post_engaged_users and the
# _organic impression variants all 400 "(#100) The value must be a valid
# insights metric". Because Meta validates the list all-or-nothing, those three
# (which this importer requested until now) 400'd every post's whole request
# and every per-post number (reactions and comments included) shipped as 0.
# Their live successors are post_media_view (views) and
# post_total_media_view_unique (viewers), the same swap made at page
# level; post_clicks is kept because it is the one click signal still offered.
# batch_post_insights self-heals around a metric Meta rejects later, so a
# future deprecation drops that metric instead of blanking the rest.
POST_INSIGHT_METRICS = [
    "post_activity_by_action_type",
    "post_reactions_by_type_total",
    "post_media_view",
    "post_total_media_view_unique",
    "post_clicks",
]

# per-post metrics Meta rejected earlier in this process (an
# invalid-metric 400). Metric validity is a property of the Graph version, not
# of the post, so once one post proves a metric invalid the later batches skip
# it instead of paying the failed round-trip and the split again.
_REJECTED_POST_METRICS: set[str] = set()


def _posts_since_timestamp(dates, now=None):
    """the ``since`` Unix timestamp for this run's posts fetch.

    The trailing PAGE_POSTS_WINDOW_DAYS window from ``now``, widened to the
    earliest requested import date when a backfill range starts earlier —
    so a daily run refreshes the recent window and a deep backfill pulls
    the posts its range covers. ``dates`` is the run's ascending
    YYYY-MM-DD list (never empty by the time main() calls this).
    """
    now = now or datetime.now(timezone.utc)
    window_floor = now - timedelta(days=PAGE_POSTS_WINDOW_DAYS)
    earliest = datetime.strptime(dates[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(min(window_floor, earliest).timestamp())


# module-global backfill-mode flag (mirrors get_insights._backfill_bailout).
# Set once in main() from the --backfill_bailout param; read by check_and_pause_for_page
# so a throttled page bails out (keep partial progress) instead of sleeping. Default
# False = daily/scheduled behavior (sleep-and-continue).
_backfill_bailout = False

# page-level insight metrics requested per page-date. Meta validates
# this list ALL-OR-NOTHING: if ANY one metric is invalid for the current Graph
# version (v25.0), the whole /insights call 400s with "(#100) The value must be
# a valid insights metric" and the page-date's data is lost. Metric validity
# drifts unpredictably across Graph versions (swapped page_views_total →
# page_media_view when the former broke; as of 2026-06-26 page_views_total works
# again while page_posts_impressions now 400s), so a hardcoded list rots and
# silently wipes data. fetch_page_insights SELF-HEALS: on the invalid-metric 400
# it drops the offending metric(s) and retries, salvaging every valid metric
# instead of losing them all. This list is the single source of truth (the probe
# tool test_page_metrics.py mirrors it).
#
# page_posts_impressions (the organic+paid total) is INVALID on
# v25.0 (#100), as are _paid and _unique; only page_posts_impressions_organic
# survives. The self-heal was dropping the bare name every run, so NO
# post-impressions data was being collected at all. Request the _organic
# variant instead — it UNDERCOUNTS a true total (organic-only, excludes paid),
# which is why every dashboard label says "(Organic)". See DECISIONS.md
# 2026-07-31 and tools/repoint_page_posts_impressions.py.
PAGE_INSIGHTS_METRICS = [
    "page_media_view",
    "page_post_engagements",
    "page_daily_follows_unique",
    "page_follows",
    "page_actions_post_reactions_total",
    "page_posts_impressions_organic",
    "page_total_media_view_unique",
    "page_video_views",
    "page_daily_follows",
]


from import_scripts.facebook.lib.team_helpers import (
    _resolve_team_country,
)  # re-exported for existing importers


def _resolve_wizard_fb_page_credentials(connection_esid):
    """return ``(app_id, app_secret, access_token)`` for a
    wizard-cohort Facebook Page.

    Mirror of ``_resolve_wizard_ig_credentials`` on the IG
    side. Wizard-cohort Pages carry ``wizard_connection_esid`` on their
    ``de_asset_fb_pages`` doc (written by ``_bridge_fb_pages``);
    the encrypted user token lives on ``de_connection.oauth_token_enc``;
    the Meta-app credentials come from the wizard's shared app via
    ``WizardOAuthService.get_meta_app``. The legacy FB Page importer
    auth model only walks ``de_asset_fb_app_users`` — wizard-cohort
    Pages have no fb_app_user row, so without this resolver the
    importer falls through every legacy token, finds none accessible
    via ``/me/accounts``, and the wizard team's Page surface stays
    empty even with the asset chain in place.

    Returns ``(None, None, None)`` on any resolution failure — caller
    skips the wizard candidate and falls through to the legacy
    ``fb_app_map`` loop unchanged. Preserves legacy team behaviour
    when ``wizard_connection_esid`` is absent.
    """
    if not connection_esid:
        return None, None, None

    try:
        from asset_management.web.services.wizard_oauth_service import (
            WizardOAuthService,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-FB-Page creds: WizardOAuthService import failed: %s", e)
        return None, None, None

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        logging.warning(
            "wizard-FB-Page creds: WizardOAuthService returned no meta app "
            "for connection %s — wizard Meta-app row missing or ES outage",
            connection_esid,
        )
        return None, None, None

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logging.warning("wizard-FB-Page creds: ES client unavailable")
        return None, None, None

    try:
        conn_doc = es.get(index="de_connection", id=connection_esid)
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "wizard-FB-Page creds: de_connection get(%s) failed: %s",
            connection_esid,
            e,
        )
        return None, None, None

    conn_src = conn_doc.get("_source") or {}
    # provider guard. The team-level fallback feeds
    # de_asset_teams.connection_esid here, and that field is NOT
    # provider-specific (prod 2026-09-01: 36 facebook, 1 ga4, 1 google_ads).
    # A Google connection's decrypted token must never be sent to the Graph
    # API as a Facebook credential; reject and fall through to legacy.
    if (conn_src.get("provider") or "") != "facebook":
        logging.info(
            "wizard-FB-Page creds: connection %s has provider=%r (not facebook) — skipping",
            connection_esid,
            conn_src.get("provider"),
        )
        return None, None, None
    # pause imports when the user has disconnected this
    # connection via the Manage Connection page. Same shape as the
    # _resolve_account_wide_credentials gate in get_insights.py.
    if (conn_src.get("status") or "") == "revoked":
        logging.info(
            "wizard-FB-Page creds: skipping revoked connection_esid=%s",
            connection_esid,
        )
        return None, None, None
    encrypted = conn_src.get("oauth_token_enc") or ""
    if not encrypted:
        logging.warning(
            "wizard-FB-Page creds: de_connection %s has no oauth_token_enc",
            connection_esid,
        )
        return None, None, None

    try:
        access_token = decrypt(encrypted)
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "wizard-FB-Page creds: decrypt failed for connection %s: %s",
            connection_esid,
            e,
        )
        return None, None, None

    return meta_app.client_id, meta_app.client_secret, access_token


def _wizard_connection_for(page_details, team):
    """the wizard-connection esid for a page, in pinned order:

    1. the PAGE asset's ``wizard_connection_esid`` stamp (the bridge
       writes it; prod 2026-09-01: populated on 50/54 pages — the working path);
    2. the team doc's ``wizard_connection_esid`` (populated on 0/38 prod teams
       today, kept for forward-compat);
    3. the team doc's ``connection_esid`` (37/38 teams) — NOT provider-specific,
       which is why ``_resolve_wizard_fb_page_credentials`` carries the
       provider!=facebook guard: a Google connection here degrades cleanly to
       the legacy fb_app_users path instead of sending a Google token to Graph.

    earlier only source 1 existed, so a page without a stamp (or a team
    whose page asset is missing entirely) silently lost the wizard path even
    when the team's own Facebook connection could serve it.
    """
    return (
        (page_details or {}).get("wizard_connection_esid") or team.get("wizard_connection_esid") or team.get("connection_esid")
    )


# token-health ranking for the duplicate-page resolver below.
# Higher rank wins. A page whose wizard connection holds a live, usable
# token must beat one whose connection is revoked/expired, because the
# whole point of the fix is that an operator who refreshes the OBVIOUS
# (live) connection's token sees that token actually used at import time.
_CONN_STATUS_RANK = {
    "active": 3,
    "pending_token": 1,
    "expired": 0,
    "revoked": 0,
}
# Legacy (non-wizard) page docs carry no wizard_connection_esid; rank
# them between a healthy wizard connection and a dead one so a legacy
# row neither shadows a live wizard connection nor loses to a revoked
# one. Matches the legacy fb_app_map fall-through the token loop already
# performs for these pages.
_LEGACY_PAGE_RANK = 2


def _connection_health_rank(es, connection_esid, _cache):
    """Return a sortable health rank for a wizard ``de_connection``.

    ``active`` with a non-expired ``token_expires_at`` ranks highest;
    ``revoked`` / ``expired`` rank lowest. Cached per importer run so a
    page shared by N duplicate rows costs at most one ES get per
    distinct connection. Unreadable / missing connections rank as dead
    (0) so a dangling wizard_connection_esid never wins over a healthy
    sibling. ``None`` connection_esid is the legacy case — handled by
    the caller, not here.
    """
    if connection_esid in _cache:
        return _cache[connection_esid]

    rank = 0
    try:
        conn = es.get(index="de_connection", id=connection_esid)
        src = conn.get("_source") or {}
        status = (src.get("status") or "").strip().lower()
        rank = _CONN_STATUS_RANK.get(status, 0)
        # An active connection whose token has already expired is no
        # better than an expired one for picking a usable token.
        if rank == _CONN_STATUS_RANK["active"]:
            expires_at = src.get("token_expires_at")
            if expires_at and _is_past(expires_at):
                rank = _CONN_STATUS_RANK["expired"]
    except Exception as e:  # noqa: BLE001 — boundary, dead-rank on any failure
        logging.warning(
            "could not read de_connection %s for dup-page ranking: %s",
            connection_esid,
            e,
        )
        rank = 0

    _cache[connection_esid] = rank
    return rank


def _is_past(iso_ts):
    """True if ``iso_ts`` (an ISO-8601 string) is strictly in the past.

    Tolerant of a trailing ``Z`` and of naive timestamps (assumed UTC).
    Returns False on any parse failure so an unparseable expiry never
    demotes an otherwise-active connection.
    """
    if not iso_ts:
        return False
    try:
        parsed = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def _pick_winning_page_doc(candidates, es, _conn_rank_cache):
    """Deterministically choose ONE page doc among duplicates sharing an
    ``fb_page_id``.

    ``candidates`` is a list of ``(esid, page_source_dict)`` tuples — the
    raw ``de_asset_fb_pages`` rows for a single Meta page id. earlier
    the importer's ``page_id_to_page[str(page_id)] = ...`` assignment let
    the LAST row in ES scan order win, which is non-deterministic across
    runs and meant an operator's token refresh on the obvious (newest /
    live) connection silently had no effect — the importer kept using a
    different duplicate's connection. See.

    Ordering (first key dominates):

    1. Connection token health — a live wizard connection beats a
       revoked/expired one; a legacy (no-connection) row sits in between.
    2. Newest ``@timestamp`` — the most recently connected row wins the
       tie, matching operator intuition ("the connection I just made").
    3. esid as a final, fully-deterministic tiebreak so two rows with
       identical health and timestamp still resolve the same way every
       run.

    Single-candidate is the common case and returns immediately.
    """
    if len(candidates) == 1:
        return candidates[0]

    def _rank(item):
        esid, src = item
        wcid = src.get("wizard_connection_esid")
        health = _connection_health_rank(es, wcid, _conn_rank_cache) if wcid else _LEGACY_PAGE_RANK
        ts = str(src.get("@timestamp") or "")
        return (health, ts, str(esid))

    winner = max(candidates, key=_rank)
    page_id = candidates[0][1].get("fb_page_id")
    logging.warning(
        "%d duplicate de_asset_fb_pages rows for fb_page_id=%s; "
        "deterministically selected esid=%s (wizard_connection_esid=%s). "
        "Others ignored this run: %s",
        len(candidates),
        page_id,
        winner[0],
        winner[1].get("wizard_connection_esid"),
        [c[0] for c in candidates if c[0] != winner[0]],
    )
    return winner


def _warn_on_page_team_collision(page_id_to_team, page_id_str, team):
    """log a WARNING when a Facebook page id is claimed by more
    than one team while building the page_id→team map.

    The map holds one team per page id (last writer wins). Before this
    helper, a second team claiming the same ``team_facebook_page_id``
    silently overwrote the first — the underlying data collision (the
     "Kavanah" / "Kavanah Team" title-collision pair) was
    invisible. Now the overwrite is loud so the stale claim can be cleaned
    up at the data layer.

    No-op when the page id is not yet in the map, or when the existing
    entry belongs to the same team (a team legitimately listing the same
    page id twice).
    """
    existing = page_id_to_team.get(page_id_str)
    if not existing:
        return
    existing_esid = existing.get("_id")
    new_esid = team.get("_id")
    if existing_esid == new_esid:
        return
    logging.warning(
        "Facebook page id %s is claimed by more than one team — "
        "team %r (%s) overwrites team %r (%s) in the page→team map "
        "(last-writer-wins). This is a data collision; the foreign team's "
        "team_facebook_page_id should be cleared.",
        page_id_str,
        team.get("team_title", "Unknown"),
        new_esid,
        existing.get("team_title", "Unknown"),
        existing_esid,
    )


def get_all_teams_by_page_id(team_ids_filter=None):
    """
    Get all teams with team_facebook_page_id and resolve page IDs to page details.

    Args:
        team_ids_filter: Optional ``--team_ids`` value (comma-separated team
            ESIDs; titles only under the FB_IMPORT_ALLOW_TITLE_MATCH legacy
            hatch — see ``_team_id_filter_matches``). When provided, teams are
            filtered DOWN to the requested set BEFORE the page_id→team map is
            built. The map is one team per page id (last writer
            wins): when two title-colliding teams claim the same
            ``team_facebook_page_id`` (the "Kavanah" / "Kavanah
            Team" pair), a foreign team scanned later would overwrite the
            correct owner's entry, and the post-build filter then ran over the
            already-collapsed map and resolved the wanted team to zero pages —
            silently importing nothing. Pre-filtering keeps only the requested
            team's claims in the map so a foreign team can no longer overwrite
            it. The page load itself is also scoped to the filtered teams'
            ``team_facebook_page_id`` values instead of loading
            every page cluster-wide. ``None`` (the cron path) keeps the full
            cluster-wide mapping and the unfiltered page load.

    Returns: dict mapping fb_page_id (numeric string) to team data with page info
    """
    lib.logging.define("fb-get-page-insights")
    # Only get teams with ds_facebook enabled. when --team_ids is
    # provided (and the legacy title hatch is off, so every token is a team
    # ESID by the contract), carry those _ids into the ES query so a
    # single-team run no longer enumerates every FB-enabled team across all
    # workspaces ("Found 29 teams"). Under the title hatch a token may be a
    # title, which can't be queried by _id — that legacy path keeps the
    # cluster-wide load and relies on the pre-filter below.
    teams_filters = {"ds_facebook": "true"}
    if team_ids_filter and not _title_match_allowed():
        filter_esids = [t.strip() for t in str(team_ids_filter).split(",") if t.strip()]
        if filter_esids:
            teams_filters["_id"] = filter_esids
    teams_docs = get_fb_teams(filters=teams_filters)

    logging.info(f"Found {len(teams_docs)} teams")

    # pre-filter teams to the requested --team_ids BEFORE building
    # the page_id→team map, so a foreign team that shares a page id with the
    # requested team can never enter the map and overwrite the correct owner
    # (last-writer-wins). Kept after the query-time scoping as the
    # only filter under the title hatch and as defense-in-depth. The same
    # predicate runs again on the collapsed map in main() — a harmless no-op
    # after this pre-filter.
    if team_ids_filter:
        teams_docs = [
            team_doc
            for team_doc in teams_docs
            if _team_id_filter_matches({**team_doc["_source"], "_id": team_doc["_id"]}, team_ids_filter)
        ]
        logging.info(f"pre-filtered to {len(teams_docs)} team(s) for team_ids={team_ids_filter!r}")

    # scope the page load to the (pre-filtered) teams' referenced
    # page ids instead of loading every page across all workspaces and
    # filtering in Python. The page_id_to_page map below is only ever looked
    # up with the teams' team_facebook_page_id values, so page docs outside
    # that set can never be used — loading them scaled with cluster size, not
    # team size. Query by fb_page_id rather than team_facebook_page_esids:
    # the esids companion field can be empty/stale while
    # team_facebook_page_id is the field the mapping loop below reads. All
    # docs sharing a scoped fb_page_id are still loaded, so the
    # winner-pick over multi-connection duplicates is unaffected. No team
    # filter (the cron path) keeps loading all pages, unchanged.
    if team_ids_filter:
        scoped_page_ids = set()
        for team_doc in teams_docs:
            page_ids = team_doc["_source"].get("team_facebook_page_id", []) or []
            if not isinstance(page_ids, list):
                page_ids = [page_ids]
            scoped_page_ids.update(str(pid) for pid in page_ids if pid)
        fb_pages_docs = get_fb_pages(filters={"fb_page_id": sorted(scoped_page_ids)}) if scoped_page_ids else []
        logging.info(
            f"scoped page load to {len(scoped_page_ids)} page id(s) referenced by "
            f"the filtered team(s); loaded {len(fb_pages_docs)} page doc(s)"
        )
    else:
        # Get all Facebook pages for ESID lookup (cron path — no team filter)
        fb_pages_docs = get_fb_pages(filters=None)
    esid_to_page = {}
    # group raw page docs by fb_page_id BEFORE building the
    # fb_page_id -> details map, so that when the same Meta page is wired
    # by multiple connections/principals (legitimate
    # per-workspace isolation) the importer picks ONE deterministically
    # via _pick_winning_page_doc instead of last-write-wins by ES scan
    # order. Pre-fix this was a plain `page_id_to_page[str(page_id)] = …`
    # assignment whose final value depended on scan order, so a token
    # refresh on the obvious (live) connection silently had no effect.
    from lib.es_helpers import get_elasticsearch_client

    es_for_ranking = get_elasticsearch_client()
    _conn_rank_cache: dict = {}
    candidates_by_page_id: dict = {}
    for page_doc in fb_pages_docs:
        esid = page_doc["_id"]
        page_data = page_doc["_source"]
        page_id = page_data.get("fb_page_id")
        if page_id:
            candidates_by_page_id.setdefault(str(page_id), []).append((esid, page_data))

    page_id_to_page = {}
    for page_id_str, candidates in candidates_by_page_id.items():
        esid, page_data = _pick_winning_page_doc(candidates, es_for_ranking, _conn_rank_cache)
        esid_to_page[esid] = {
            "fb_page_id": page_data.get("fb_page_id"),
            "fb_page_name": page_data.get("fb_page_name"),
            "fb_page_slug": page_data.get("fb_page_slug"),
            "fb_account_esid": page_data.get("fb_account_esid"),
            # wizard-cohort Pages carry wizard_connection_esid
            # + wizard_principal on the asset doc; the importer's
            # token-acquisition loop uses these to resolve credentials
            # via _resolve_wizard_fb_page_credentials
            # (de_connection.oauth_token_enc) instead of the legacy
            # de_asset_fb_app_users chain. Legacy Pages have these
            # unset and fall through to fb_app_map unchanged.
            "wizard_connection_esid": page_data.get("wizard_connection_esid"),
            "wizard_principal": page_data.get("wizard_principal"),
        }
        page_id_to_page[page_id_str] = esid_to_page[esid]

    logging.info(f"Found {len(page_id_to_page)} Facebook pages")

    page_id_to_team = {}
    teams_with_missing_pages = []

    for team_doc in teams_docs:
        team = team_doc["_source"]
        team["_id"] = team_doc["_id"]
        team_title = team.get("team_title", "Unknown")

        # Get page IDs - can be array or single value
        page_ids = team.get("team_facebook_page_id", [])
        if not page_ids:
            continue

        if not isinstance(page_ids, list):
            page_ids = [page_ids]

        logging.debug(f"Team '{team_title}' has Facebook page IDs: {page_ids}")

        for page_id in page_ids:
            if not page_id:  # Skip empty strings
                continue

            # Convert to string for consistent lookup
            page_id_str = str(page_id)

            # Look up page details
            page_details = page_id_to_page.get(page_id_str)
            if page_details:
                page_name = page_details.get("fb_page_name", "unknown")
                logging.debug(f"  Found page {page_id} ({page_name})")

                # Store the page ID with team data
                team_with_page = team.copy()
                team_with_page["fb_page_id"] = page_id_str
                team_with_page["fb_page_name"] = page_name
                team_with_page["fb_page_slug"] = page_details.get("fb_page_slug", "")
                team_with_page["fb_account_esid"] = page_details.get("fb_account_esid")
                # copy wizard_connection_esid + wizard_principal
                # from the Page asset onto the team-with-page dict so the
                # per-page token loop can resolve wizard credentials via
                # _resolve_wizard_fb_page_credentials. a page
                # without the asset stamp now falls back to the TEAM's own
                # connection (pinned order in _wizard_connection_for; the
                # resolver's provider guard keeps non-facebook connections
                # out). Pages with neither still walk the legacy
                # fb_app_map unchanged.
                team_with_page["wizard_connection_esid"] = _wizard_connection_for(page_details, team)
                team_with_page["wizard_principal"] = page_details.get("wizard_principal")
                _warn_on_page_team_collision(page_id_to_team, page_id_str, team)
                page_id_to_team[page_id_str] = team_with_page
            else:
                logging.warning(f"  Team '{team_title}' has page ID {page_id} that cannot be found in de_asset_fb_pages")
                teams_with_missing_pages.append(
                    {
                        "team_title": team_title,
                        "team_esid": team["_id"],
                        "missing_page_id": page_id,
                    }
                )
                # Still add to mapping - we can try to fetch from API
                team_with_page = team.copy()
                team_with_page["fb_page_id"] = page_id_str
                team_with_page["fb_page_name"] = None
                team_with_page["fb_page_slug"] = None
                # no page asset at all: the team's own connection
                # is the only wizard-credential lead (provider-guarded in the
                # resolver). Pre-fix this branch left the raw team doc's
                # wizard_connection_esid (populated on 0/38 prod teams), so
                # the wizard path could never fire here.
                team_with_page["wizard_connection_esid"] = _wizard_connection_for(None, team)
                _warn_on_page_team_collision(page_id_to_team, page_id_str, team)
                page_id_to_team[page_id_str] = team_with_page

    if teams_with_missing_pages:
        logging.warning(f"\n=== Teams with missing Facebook pages ===")
        for item in teams_with_missing_pages:
            logging.warning(
                f"Team: {item['team_title']} (ESID: {item['team_esid']}) - Missing Page ID: {item['missing_page_id']}"
            )

    logging.info(f"Loaded {len(page_id_to_team)} Facebook page ID to team mappings")
    return page_id_to_team


def get_all_accessible_pages(user_access_token, user_title="Unknown"):
    """
    Get all pages accessible to the user token with their page access tokens.

    Args:
        user_access_token: User/system access token
        user_title: Display name for the user (used in log messages)

    Returns: dict mapping page_id to page data (including access_token)
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        url = f"{FB_GRAPH_URL}me/accounts"
        params = {"access_token": user_access_token, "limit": 100}

        response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
        throttle = _graph_throttle(response)
        if throttle:
            raise _PageTokenThrottled(*throttle)
        response.raise_for_status()
        data = response.json()

        pages = {}
        for page in data.get("data", []):
            page_id = page.get("id")
            if page_id:
                pages[str(page_id)] = page
                logging.debug(f"Found accessible page: {page.get('name')} (ID: {page_id})")

        logging.debug(f"Found {len(pages)} pages accessible to this token via /me/accounts")
        return pages

    except _PageTokenThrottled:
        raise  # a throttle is not "no pages"; the caller skips the page
    except Exception as e:
        logging.warning(f"Failed to get accessible pages for user '{user_title}': {e}")
        return {}


# Meta rate-limit codes that mean "this token is throttled right now",
# not "this token cannot see the page". The shared set plus 32 (Page
# request limit reached), which only the page path meets.
_PAGE_TOKEN_THROTTLE_CODES = _FB_RATE_LIMIT_ERROR_CODES | {32}


class _PageTokenThrottled(Exception):
    """a token-resolution call (/me/accounts or the direct page-token
    request) was rate-limited by Meta. Raised instead of returning "no pages" so
    the caller skips the page this run rather than falling back to another token
    family: on 2026-10-01 the shared wizard app hit "(#4) Application request
    limit reached", the 403 read as "page not visible to this login", and the
    importer fell back to legacy system-user tokens that list posts but return
    empty insights — writing blank post metrics and zero page docs."""

    def __init__(self, code, message):
        super().__init__(f"(#{code}) {message}")
        self.code = code
        self.message = message


def _graph_throttle(response):
    """``(code, message)`` when a ``requests`` response carries a Meta rate-limit
    error body, else ``None``. The requests path does not raise on a 4xx by
    itself, and Meta answers a throttle with 400/403 + a JSON error."""
    if response is None or response.status_code < 400:
        return None
    try:
        err = (response.json() or {}).get("error") or {}
    except ValueError:
        return None
    code, subcode = err.get("code"), err.get("error_subcode")
    if code in _PAGE_TOKEN_THROTTLE_CODES or subcode in _FB_RATE_LIMIT_ERROR_SUBCODES:
        return code, str(err.get("message") or "")
    return None


def _is_reduce_data_error(response):
    """True for Meta's code 1 "Please reduce the amount of data
    you're asking for, then retry your request" (sent as an HTTP 500)."""
    if response is None:
        return False
    try:
        err = (response.json() or {}).get("error") or {}
    except ValueError:
        return False
    return err.get("code") == 1 and "reduce the amount of data" in str(err.get("message") or "").lower()


def get_page_access_token(page_id, user_access_token):
    """
    Try to get a page access token directly for a specific page.
    This works for Business Manager pages where the user has permission
    but the page doesn't appear in /me/accounts.

    Args:
        page_id: Facebook page ID
        user_access_token: User access token with page permissions

    Returns: dict with page data including access_token, or None on error
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        url = f"{FB_GRAPH_URL}{page_id}"
        params = {"fields": "id,name,access_token", "access_token": user_access_token}

        response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
        throttle = _graph_throttle(response)
        if throttle:
            raise _PageTokenThrottled(*throttle)
        response.raise_for_status()
        data = response.json()

        if "access_token" in data:
            logging.debug(f"Got page access token for {page_id} ({data.get('name', 'unknown')})")
            return data
        else:
            logging.warning(f"No access_token returned for page {page_id} - user may lack page permissions")
            return None

    except _PageTokenThrottled:
        raise  # see get_all_accessible_pages
    except Exception as e:
        logging.debug(f"Could not get page access token for {page_id}: {e}")
        return None


def check_and_pause_for_page(page_id, page_access_token):
    """
    Makes a lightweight GET /{page_id}?fields=id call and inspects x-business-use-case-usage
    and x-app-usage response headers. Sleeps if >= 80% on any usage dimension. Non-fatal.
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        resp = requests.get(
            f"{FB_GRAPH_URL}{page_id}",
            params={"fields": "id", "access_token": page_access_token},
            timeout=FB_GRAPH_HTTP_TIMEOUT,
        )
        # the requests-based probe does NOT raise on a 4xx (unlike the
        # SDK path), so a throttled page returns a 400 whose JSON body carries the
        # rate-limit code (80004 / subcode 2446079). In backfill mode recognize
        # that and bail out; otherwise fall through (the header check below is a
        # no-op on an error body, and the daily path stays log-and-continue).
        if _backfill_bailout and resp.status_code >= 400:
            try:
                err = (resp.json() or {}).get("error", {}) or {}
            except (ValueError, TypeError):
                err = {}
            if err.get("code") in _FB_RATE_LIMIT_ERROR_CODES or err.get("error_subcode") in _FB_RATE_LIMIT_ERROR_SUBCODES:
                logging.warning(
                    "backfill: usage-check probe for page %s was rate "
                    "limited (code %s subcode %s); bailing out to keep partial progress.",
                    page_id,
                    err.get("code"),
                    err.get("error_subcode"),
                )
                raise _RateLimitBailout(f"usage-check rate limited for page {page_id}")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        for header_key in ["x-business-use-case-usage", "x-app-usage"]:
            raw = headers.get(header_key)
            if not raw:
                continue
            try:
                usage = json.loads(raw)
            except (ValueError, TypeError):
                continue
            objs = []
            if isinstance(usage, dict):
                for v in usage.values():
                    if isinstance(v, list):
                        objs.extend(v)
                if not objs and _backfill_bailout:
                    # x-app-usage is a FLAT dict ({"call_count":..,
                    # "total_cputime": .., "total_time": ..}), unlike
                    # x-business-use-case-usage's dict-of-lists, so it never
                    # produced objs and this header was silently ignored.
                    # Deliberately gated to backfill mode: honoring it on daily
                    # runs would activate the sleep branch below with no
                    # estimated_time_to_regain_access field (30-min default) —
                    # a new daily behavior that needs its own review (
                    # owner's call), not a drive-by.
                    objs = [usage]
            elif isinstance(usage, list):
                objs = usage
            for obj in objs:
                pct = max(
                    obj.get("call_count", 0),
                    obj.get("total_cputime", 0),
                    obj.get("total_time", 0),
                )
                if pct >= 80:
                    # a backfill bails out at the near-limit line instead
                    # of sleeping; the page loop breaks and partial data is kept.
                    # Daily/scheduled runs (flag off) keep the sleep.
                    if _backfill_bailout:
                        logging.warning(
                            "backfill: page %s near rate limit (%s%%); " "bailing out to keep partial progress.",
                            page_id,
                            pct,
                        )
                        raise _RateLimitBailout(f"near rate limit for page {page_id}")
                    wait_min = obj.get("estimated_time_to_regain_access", 30)
                    logging.warning(
                        f"check_and_pause_for_page: page {page_id} at {pct}% API usage. " f"Sleeping {wait_min} minute(s)."
                    )
                    time.sleep(wait_min * 60)
    except _RateLimitBailout:
        # Bail-out sentinel must propagate to the page loop, not be swallowed by
        # the broad non-fatal catch below.
        raise
    except Exception as e:
        # a hard-throttled page can reject the usage probe itself
        # (400 / rate-limit code). In backfill mode that must bail out rather than
        # be swallowed and let the loop keep hammering. requests failures aren't
        # FacebookRequestError, so recognize both via _is_rate_limit_error (SDK
        # accessors) and the throttle wording on a requests HTTPError-style error.
        if _backfill_bailout and _is_rate_limit_error(e):
            logging.warning(
                "backfill: usage-check probe for page %s was itself rate "
                "limited (%s); bailing out to keep partial progress.",
                page_id,
                e,
            )
            raise _RateLimitBailout(f"usage-check rate limited for page {page_id}") from e
        logging.debug(f"check_and_pause_for_page: non-fatal error for page {page_id}: {e}")


def fetch_page_metadata(page_id, access_token):
    """
    Fetch Facebook page metadata.

    Args:
        page_id: Facebook page ID
        access_token: Facebook page access token

    Returns: dict of page metadata or None on error
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        url = f"{FB_GRAPH_URL}{page_id}"
        params = {"fields": "id,name,username,fan_count", "access_token": access_token}

        response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except FacebookRequestError as e:
        if e.api_error_code() == 100:
            logging.warning(f"Facebook page {page_id} not found or not accessible (error code 100)")
            return "not_found"
        elif e.api_error_code() == 10:
            logging.warning(f"Access denied for Facebook page {page_id} (error code 10)")
            return "access_denied"
        else:
            logging.error(f"Failed to fetch Facebook page metadata for {page_id}: {e}")
            return None
    except Exception as e:
        logging.error(f"Failed to fetch page metadata for {page_id}: {e}")
        return None


def _is_invalid_metric_400(response) -> bool:
    """True iff an HTTP 400 is Meta's "(#100) The value must be a
    valid insights metric" (a permanently-invalid metric in the request), as
    opposed to a no-data 400 or any other 400. Distinguished by the message
    text so the caller drops the bad metric rather than the whole page-date.
    A transient 5xx is NOT this — it's a server hiccup and the metric stays.
    """
    if response is None or response.status_code != 400:
        return False
    try:
        msg = (response.json().get("error", {}) or {}).get("message", "") or ""
    except Exception:  # noqa: BLE001 — unparseable body is not an invalid-metric signal
        return False
    return "valid insights metric" in msg.lower()


def _parse_page_insights(data) -> dict:
    """parse a /insights ``data`` payload into a flat metric dict.
    Extracted from fetch_page_insights so the self-healing requester reuses the
    exact same parsing (including the dict-valued reaction/feedback breakdowns)."""
    parsed_insights = {}
    for insight in (data or {}).get("data", []):
        metric_name = insight.get("name")
        values = insight.get("values", [])
        if not (values and len(values) > 0):
            continue
        value = values[0].get("value", 0)
        # page_positive_feedback_by_type is a dict → explode into per-type fields.
        if metric_name == "page_positive_feedback_by_type" and isinstance(value, dict):
            for feedback_type, count in value.items():
                parsed_insights[f"{metric_name}_{feedback_type}"] = count
        # page_actions_post_reactions_total → store total AND per-reaction fields.
        elif metric_name == "page_actions_post_reactions_total" and isinstance(value, dict):
            parsed_insights[metric_name] = sum(value.values())
            for reaction_type, count in value.items():
                parsed_insights[f"{metric_name}_{reaction_type}"] = count
        else:
            parsed_insights[metric_name] = value
    return parsed_insights


def _fetch_page_metrics(page_id, date, metrics, since_timestamp, until_timestamp, access_token):
    """request ``metrics`` for one page-date, self-healing around an
    invalid metric.

    Meta validates the metric list all-or-nothing: one invalid metric 400s the
    whole call with "(#100) The value must be a valid insights metric", which
    pre-fix wiped the page-date's data. Here, on that specific 400 we split the
    metric list in half and recurse on each half — binary elimination that
    isolates the bad metric in ~log2(N) extra calls (only on the rare failure
    path) and returns the union of every VALID metric's data. A single metric
    that still 400s IS the bad one: we log it and return {} for it (dropped).

    A non-invalid-metric error (5xx, 429, auth, no-data 400) is NOT self-healed
    here — it is re-raised so fetch_page_insights' existing except block applies
    its retry/skip semantics unchanged (a transient 5xx must not drop a metric).
    """
    import requests

    url = f"{FB_GRAPH_URL}{page_id}/insights"
    params = {
        "metric": ",".join(metrics),
        "period": "day",
        "since": since_timestamp,
        "until": until_timestamp,
        "access_token": access_token,
    }

    # brief retry on a transient 5xx. Meta returns flaky 500s
    # ("An unexpected error has occurred") on otherwise-VALID metrics
    # (page_total_media_view_unique was empirically confirmed to 500 once then
    # succeed on retry against a staging page, 2026-06-26). Without this, a 500
    # landing on a sub-group MID-SELF-HEAL would propagate and abort the whole
    # salvage — losing valid metrics the split had already isolated. Short waits
    # (a 5xx rarely clears instantly) mirroring VIDEO_VIEWS_SERVER_ERROR_DELAYS.
    response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
    for delay in VIDEO_VIEWS_SERVER_ERROR_DELAYS:
        if response.status_code < 500:
            break
        time.sleep(delay)
        response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)

    if response.status_code == 200:
        return _parse_page_insights(response.json())

    # Self-heal ONLY the invalid-metric 400; everything else propagates.
    if _is_invalid_metric_400(response):
        if len(metrics) == 1:
            logging.warning(
                "page %s: dropping invalid metric %r (Meta rejects it for this Graph "
                "version); other metrics for %s are unaffected.",
                page_id,
                metrics[0],
                date,
            )
            return {}
        # Binary elimination: split and recurse. Isolates the bad metric in
        # ~log2(N) extra calls (failure path only) and unions every valid half.
        mid = len(metrics) // 2
        left = _fetch_page_metrics(page_id, date, metrics[:mid], since_timestamp, until_timestamp, access_token)
        right = _fetch_page_metrics(page_id, date, metrics[mid:], since_timestamp, until_timestamp, access_token)
        merged = dict(left or {})
        merged.update(right or {})
        return merged

    # A 5xx that survived the brief retry above: skip THIS group rather than
    # abort the whole salvage (other groups in a self-heal split still return).
    # Matches the importer's "transient server error → skip, re-run backfills"
    # stance and never permanently drops the metric.
    if response.status_code >= 500:
        logging.warning(
            "page %s: persistent 5xx on metrics %s for %s; skipping this group "
            "(transient server error, will backfill on a later run).",
            page_id,
            metrics,
            date,
        )
        return {}

    # Not an invalid-metric 400 and not a 5xx → let fetch_page_insights handle it
    # (429 rate-limit, auth, no-data 400) via raise_for_status + its except block.
    response.raise_for_status()
    return _parse_page_insights(response.json())


def fetch_page_insights(page_id, date, access_token, _attempt=0):
    """
    Fetch page-level insights for a Facebook Page for a specific date.

    Args:
        page_id: Facebook page ID
        date: Date string in YYYY-MM-DD format
        access_token: Facebook access token

    Returns: dict of insights data or None on error
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        # Convert date to Unix timestamps
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        since_timestamp = int(date_obj.replace(tzinfo=timezone.utc).timestamp())
        until_timestamp = since_timestamp + 86400  # Add 24 hours

        # request the metric list with self-healing on an invalid
        # metric. Meta 400s the WHOLE call ("(#100) The value must be a valid
        # insights metric") if any one metric is invalid for the current Graph
        # version, which would wipe the page-date's data; _fetch_page_metrics
        # drops the offending metric(s) and retries so the valid ones survive.
        return _fetch_page_metrics(page_id, date, list(PAGE_INSIGHTS_METRICS), since_timestamp, until_timestamp, access_token)
    except FacebookRequestError as e:
        if e.api_error_code() == 10:
            logging.warning(f"Facebook page {page_id} is not accessible or lacks insight permissions (error code 10)")
            return "not_accessible"
        elif e.api_error_code() == 100:
            logging.warning(f"Invalid parameters for Facebook page insights {page_id} (error code 100)")
            return "invalid_params"
        else:
            logging.error(f"Failed to fetch Facebook page insights for {page_id} on {date}: {e}")
            return None
    except requests.exceptions.HTTPError as e:
        # Handle HTTP errors from requests library
        if e.response.status_code == 400:
            # Parse Facebook's error response for detailed information
            try:
                error_data = e.response.json()
                error = error_data.get("error", {})
                error_message = error.get("message", "Unknown error")
                error_type = error.get("type", "Unknown")
                error_code = error.get("code", "N/A")
                error_subcode = error.get("error_subcode", "N/A")
                fbtrace_id = error.get("fbtrace_id", "N/A")

                logging.warning(
                    f"Facebook Insights API returned 400 for page {page_id} on {date}:\n"
                    f"  Message: {error_message}\n"
                    f"  Type: {error_type}\n"
                    f"  Code: {error_code}\n"
                    f"  Subcode: {error_subcode}\n"
                    f"  Trace ID: {fbtrace_id}\n"
                    f"  (This may indicate no data, invalid metric, or permission issue)"
                )
            except:
                # Fallback if we can't parse the error response
                logging.info(
                    f"No insights data available for page {page_id} on {date} (Facebook returned 400 - details unavailable)"
                )
            return None
        elif e.response.status_code == 429:
            if _attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[_attempt]
                logging.warning(
                    f"Rate limit hit fetching page insights for {page_id} on {date} (HTTP 429). "
                    f"Sleeping {delay}s, retry {_attempt + 1}/{len(RETRY_DELAYS)}..."
                )
                time.sleep(delay)
                return fetch_page_insights(page_id, date, access_token, _attempt=_attempt + 1)
            logging.warning(
                f"Rate limit hit fetching page insights for {page_id} on {date} (HTTP 429) "
                f"after {len(RETRY_DELAYS)} retries. Skipping."
            )
            return None
        elif e.response.status_code in [401, 403]:
            logging.warning(
                f"Access denied to page insights for {page_id} on {date}: {e.response.status_code} {e.response.reason}"
            )
            return "not_accessible"
        else:
            logging.error(f"HTTP {e.response.status_code} error fetching page insights for {page_id} on {date}: {e}")
            return None
    except Exception as e:
        logging.error(f"Failed to fetch page insights for {page_id} on {date}: {e}")
        return None


def _batch_item_is_invalid_metric(item) -> bool:
    """the Batch-API twin of ``_is_invalid_metric_400``: True iff a
    batch item is Meta's "(#100) The value must be a valid insights metric"."""
    if item.get("code") != 400:
        return False
    try:
        body = json.loads(item.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    msg = ((body.get("error") or {}).get("message") or "").lower()
    return "valid insights metric" in msg


def _batch_post_insights_once(post_ids, metrics, access_token):
    """One Batch-API pass for ``metrics`` over ``post_ids``.

    Returns ``(results, invalid_metric_ids, failures)``: per-post metric dicts,
    the posts whose item was an invalid-metric 400 (the caller re-asks those
    with a smaller metric list), and ``(post_id, reason)`` for every other
    non-200 item. An HTTPError on the batch POST itself (429) propagates, as
    before, so callers keep their backoff.
    """
    import requests

    metric_string = ",".join(metrics)
    results = {}
    invalid_ids = []
    failures: list[tuple[str, str]] = []
    for chunk_start in range(0, len(post_ids), FB_BATCH_SIZE):
        chunk = post_ids[chunk_start : chunk_start + FB_BATCH_SIZE]
        batch_items = [{"method": "GET", "relative_url": f"{pid}/insights?metric={metric_string}"} for pid in chunk]
        payload = {"access_token": access_token, "batch": json.dumps(batch_items)}

        try:
            resp = requests.post(FB_GRAPH_URL, data=payload, timeout=FB_GRAPH_HTTP_TIMEOUT)
            resp.raise_for_status()  # raises on 429 — propagates to caller for backoff
            batch_response = resp.json()
        except requests.exceptions.HTTPError:
            raise  # let fetch_page_video_views handle 429 with exponential backoff
        except Exception as e:
            logging.warning(f"batch_post_insights: request failed for chunk starting at {chunk_start}: {e}")
            continue

        for i, item in enumerate(batch_response):
            post_id = chunk[i]
            if item.get("code") != 200:
                if _batch_item_is_invalid_metric(item):
                    invalid_ids.append(post_id)
                    continue
                logging.debug(f"batch_post_insights: post {post_id} returned HTTP {item.get('code')} — skipping")
                failures.append((post_id, f"HTTP {item.get('code')}"))
                continue
            try:
                body = json.loads(item.get("body", "{}"))
            except (json.JSONDecodeError, TypeError):
                logging.debug(f"batch_post_insights: could not parse body for post {post_id}")
                failures.append((post_id, "unparseable body"))
                continue
            post_metrics = {}
            lifetime_seen = set()
            for insight in body.get("data", []):
                values = insight.get("values", [])
                value = values[0].get("value", 0) if values else 0
                name = insight.get("name")
                # Meta can return one metric once per period:
                # post_total_media_view_unique comes back as a "lifetime" entry
                # (the real viewer count) AND a "day" entry whose values are 0.
                # Keyed by name alone, the later day entry overwrote the
                # lifetime value with 0. The lifetime entry wins; any other
                # period only fills a name nothing has claimed yet.
                if insight.get("period") == "lifetime":
                    post_metrics[name] = value
                    lifetime_seen.add(name)
                elif name not in lifetime_seen and name not in post_metrics:
                    post_metrics[name] = value
            if post_metrics:
                results[post_id] = post_metrics
            else:
                # a 200 with an empty ``data`` list is a failure, not
                # "no metrics": Meta answers this way for a token that can list
                # posts but not read their insights (prod 2026-10-01: 508 posts
                # on two teams lost every metric with nothing in the log).
                failures.append((post_id, "HTTP 200 with empty insights data"))
    return results, invalid_ids, failures


def _batch_post_insights_healing(post_ids, metrics, access_token):
    """``_batch_post_insights_once`` that survives a rejected metric.

    Meta validates a post's metric list all-or-nothing, so one retired metric
    400s the whole item and, before this, blanked every metric for every post.
    On an invalid-metric item the post is re-asked with each half of the list
    (binary elimination, the page-level scheme) until the bad metric is
    alone; that one is remembered in ``_REJECTED_POST_METRICS`` and logged once,
    and every valid metric still lands. Extra calls happen only on the failure
    path, and the cache keeps later batches off it.
    """
    results, invalid_ids, failures = _batch_post_insights_once(post_ids, metrics, access_token)
    if not invalid_ids:
        return results, failures
    if len(metrics) == 1:
        if metrics[0] not in _REJECTED_POST_METRICS:
            _REJECTED_POST_METRICS.add(metrics[0])
            logging.warning(
                "batch_post_insights: dropping per-post metric %r (Meta rejects it for this "
                "Graph version); the other metrics for these posts are unaffected.",
                metrics[0],
            )
        return results, failures
    mid = len(metrics) // 2
    for half in (metrics[:mid], metrics[mid:]):
        half = [m for m in half if m not in _REJECTED_POST_METRICS]
        if not half:
            continue
        sub_results, sub_failures = _batch_post_insights_healing(invalid_ids, half, access_token)
        for pid, values in sub_results.items():
            results.setdefault(pid, {}).update(values)
        failures.extend(sub_failures)
    return results, failures


def batch_post_insights(post_ids, metric_string, access_token, page_id=None):
    """
    Fetch post-level insights for multiple post IDs using the Facebook Batch API.

    Sends a single POST to the Graph API with up to FB_BATCH_SIZE requests per call,
    instead of one HTTP request per post. Dramatically reduces API call count for pages
    with many video posts.

    Args:
        post_ids: list of post ID strings
        metric_string: comma-joined metric name(s), e.g. "post_video_views_15s"
        access_token: Facebook page access token
        page_id: optional — named in the dropped-posts WARN

    Returns:
        dict mapping post_id -> {metric_name: value}
        Posts returning non-200 responses (deleted, no access) are omitted; the
        omissions are summarized in ONE WARN per call (this was the
        most invisible casualty of the legacy-app scope gap: dropped batch items
        logged only at debug, so the attrition never reached anyone).
    """
    # skip metrics Meta already rejected in this process.
    metrics = [m for m in metric_string.split(",") if m and m not in _REJECTED_POST_METRICS]
    if not metrics or not post_ids:
        return {}

    results, failures = _batch_post_insights_healing(list(post_ids), metrics, access_token)
    # A post counts as dropped only if it came back with NO metrics at all; a
    # post that lost one rejected metric but kept the rest is not a casualty.
    first_reason: dict[str, str] = {}
    for pid, reason in failures:
        if pid not in results:
            first_reason.setdefault(pid, reason)
    dropped = list(first_reason.items())

    # one aggregated WARN per call (not one per post: a page with
    # dozens of scope-denied posts must be visible without flooding the log).
    # Per-post detail stays at debug above.
    if dropped:
        reasons = sorted({reason for _pid, reason in dropped})
        sample = ", ".join(pid for pid, _reason in dropped[:5])
        logging.warning(
            "batch_post_insights: %d of %d posts dropped from the batch (%s) — e.g. %s%s. "
            "These posts ship NO %s metrics this run.%s",
            len(dropped),
            len(post_ids),
            "; ".join(reasons),
            sample,
            "" if len(dropped) <= 5 else ", …",
            metric_string,
            f" Page {page_id}." if page_id else "",
        )

    return results


def fetch_page_video_views(page_id, date, access_token, _attempt=0):
    """
    Fetch video statistics for a Facebook page on a specific date.

    - 15s views: summed from ALL recent video posts — a
      lifetime-to-date snapshot over the recent-post window, NOT per-day
      views. The route exhaustion (tested with full read_insights on
      the prod token) proved Meta exposes no true daily page-level 15s metric:
      page_uploaded_views_15s_count doesn't exist on v25/v26, video_insights'
      total_video_15s_views covers a different asset set, and
      post_video_views_15s period=day returns the lifetime value per date.
      The previous publish-day-only cohort read 0 on any day without a new
      video post regardless of real viewing. Dashboards must aggregate this
      field with MAX (latest snapshot), never sum — summing lifetime
      snapshots across dates multiplies rather than accumulates.
    - avg time watched: weighted average from all recent video posts (rolling
      approximation, since Facebook does not provide daily avg watch time at
      the page level)

    Args:
        page_id: Facebook page ID
        date: Date string in YYYY-MM-DD format
        access_token: Facebook access token

    Returns: tuple (total_15s_views, avg_sec_watched, total_shares) — or
    ``(None, None, None)`` when the fetch ultimately failed, so the caller
    leaves the fields ABSENT instead of writing confident zeros (:
    a permanent empty response, e.g. a sub-100-like page, must degrade
    visibly, not as 0s indistinguishable from real no-activity days).
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        date_obj = datetime.strptime(date, "%Y-%m-%d").date()

        # Step 1: Get recent posts
        url = f"{FB_GRAPH_URL}{page_id}/posts"
        params = {
            "fields": "id,created_time,shares,attachments{type,media_type}",
            "access_token": access_token,
            "limit": 100,
        }

        response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
        response.raise_for_status()
        posts_data = response.json()

        # Step 2: Build lists and sum shares for posts published on target date
        video_posts_today = []
        video_posts_recent = []
        total_shares = 0
        for post in posts_data.get("data", []):
            created_time_str = post.get("created_time")
            if created_time_str:
                created_datetime = datetime.fromisoformat(created_time_str.replace("Z", "+00:00"))
                if created_datetime.date() == date_obj:
                    total_shares += post.get("shares", {}).get("count", 0)

            attachments = post.get("attachments", {}).get("data", [])
            is_video = any(a.get("media_type") == "video" or a.get("type") == "video_inline" for a in attachments)
            if not is_video:
                continue

            video_posts_recent.append(post)
            if created_time_str:
                created_datetime = datetime.fromisoformat(created_time_str.replace("Z", "+00:00"))
                if created_datetime.date() == date_obj:
                    video_posts_today.append(post)

        all_posts = posts_data.get("data", [])
        logging.info(
            f"Page {page_id} on {date}: fetched {len(all_posts)} posts, "
            f"{len(video_posts_today)} video(s) published today, "
            f"{len(video_posts_recent)} recent video(s) total, "
            f"{total_shares} share(s) on target date"
        )

        # Step 3: sum 15s views across ALL recent videos (batched) —
        # the same rolling window the avg uses. post_video_views_15s is a
        # LIFETIME metric, so this is a lifetime-to-date snapshot over recent
        # videos; the publish-day-only cohort it replaces read 0 on any day
        # without a new post even while existing videos were being watched
        # (one-team repro, 2026-08-02).
        total_15s_views = 0
        if video_posts_recent:
            recent_video_ids = [p["id"] for p in video_posts_recent]
            views_results = batch_post_insights(recent_video_ids, "post_video_views_15s", access_token)
            for metrics in views_results.values():
                total_15s_views += metrics.get("post_video_views_15s", 0)

        logging.info(
            f"15s video views (lifetime, {len(video_posts_recent)} recent videos) "
            f"for page {page_id} on {date}: {total_15s_views}"
        )

        # Step 4: Compute weighted avg time watched from all recent video posts (batched)
        # post_video_avg_time_watched is a lifetime metric in milliseconds; convert to seconds
        # post_video_view_time (lifetime TOTAL view time, ms) rides
        # the same batch call at zero extra API cost. Per-video lifetime values
        # become the day's snapshot; the caller deltas it against the previous
        # snapshot to derive a real daily watch-time total (Meta deprecated the
        # daily page-level view-time metric, so lifetime-delta is the only way).
        avg_sec_watched = 0.0
        view_time_snapshot = {}
        if video_posts_recent:
            created_by_id = {p["id"]: (p.get("created_time") or "")[:10] for p in video_posts_recent}
            recent_ids = [p["id"] for p in video_posts_recent]
            recent_results = batch_post_insights(
                recent_ids, "post_video_avg_time_watched,post_video_views,post_video_view_time", access_token
            )
            total_weighted = 0.0
            total_weight = 0
            for post_id2, metrics in recent_results.items():
                post_avg_time_ms = metrics.get("post_video_avg_time_watched", 0)
                post_views = metrics.get("post_video_views", 0)
                if post_views > 0 and post_avg_time_ms > 0:
                    total_weighted += post_avg_time_ms * post_views
                    total_weight += post_views
                view_time_ms = metrics.get("post_video_view_time")
                if view_time_ms is not None:
                    view_time_snapshot[post_id2] = {"ms": int(view_time_ms), "created": created_by_id.get(post_id2, "")}
            if total_weight > 0:
                avg_sec_watched = (total_weighted / total_weight) / 1000
                logging.info(
                    f"Page {page_id} on {date}: avg_sec_watched={avg_sec_watched:.1f}s "
                    f"from {len(recent_results)} recent video(s)"
                )

        return total_15s_views, avg_sec_watched, total_shares, view_time_snapshot

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        # follow-up 2 — 429 (rate-limit) keeps the long RETRY_DELAYS
        # ladder; everything else (chiefly Meta's flaky 5xx on video views)
        # uses the short VIDEO_VIEWS_SERVER_ERROR_DELAYS ladder so a server
        # blip doesn't burn 35s/page. Both degrade to 0 + a backfill hint.
        schedule = RETRY_DELAYS if status == 429 else VIDEO_VIEWS_SERVER_ERROR_DELAYS
        if _attempt < len(schedule):
            delay = schedule[_attempt]
            logging.warning(
                f"[fb-get-page-insights] HTTP {status} fetching video views for page {page_id} on {date}. "
                f"Sleeping {delay}s, retry {_attempt + 1}/{len(schedule)}..."
            )
            time.sleep(delay)
            return fetch_page_video_views(page_id, date, access_token, _attempt=_attempt + 1)
        logging.warning(
            f"[fb-get-page-insights] HTTP {status} fetching video views for page {page_id} on {date} "
            f"after {len(schedule)} retries. Video fields will be ABSENT — re-run this date to backfill."
        )
        # absent, not 0: a failed fetch must be distinguishable
        # from a real zero-activity day on every dashboard.
        return None, None, None, None
    except Exception as e:
        logging.warning(f"Could not fetch video views for page {page_id} on {date}: {e} (fields will be absent)")
        return None, None, None, None


def compute_view_time_daily_min(today_snapshot, prior_snapshot, prior_date):
    """derive the day's total video watch time (MINUTES) from two
    lifetime snapshots ({post_id: {"ms": int, "created": "YYYY-MM-DD"}}).

    Meta exposes no daily page-level view-time metric, so the daily total is
    reconstructed per video as today's lifetime ``post_video_view_time`` minus
    the most recent prior snapshot's value:

    * video in BOTH snapshots → delta, clamped at 0 (Meta occasionally revises
      lifetime counters down; a negative day is a correction, not viewing);
    * video only in TODAY's snapshot → its full lifetime counts ONLY when it
      was created after ``prior_date`` (genuinely new watch time). A video
      missing from the prior snapshot but created before it is a window/scope
      artifact (batch drop, permission blip) — counting its whole lifetime
      would spike the day, so it is skipped;
    * video only in the PRIOR snapshot (fell out of the recent-post window)
      contributes nothing.

    Returns minutes (float), or ``None`` when ``prior_snapshot`` is ``None`` —
    the first day a page ships the snapshot has no baseline, and the field
    must stay ABSENT, never a confident 0. An EMPTY prior
    snapshot ({}) is a real baseline (page had no videos) and yields a number.
    """
    if today_snapshot is None or prior_snapshot is None:
        return None
    total_ms = 0
    for post_id, entry in today_snapshot.items():
        today_ms = int(entry.get("ms", 0))
        prior_entry = prior_snapshot.get(post_id)
        if prior_entry is not None:
            total_ms += max(0, today_ms - int(prior_entry.get("ms", 0)))
        elif prior_date and (entry.get("created") or "") > prior_date:
            total_ms += today_ms
    return total_ms / 60000.0


def fetch_prior_view_time_snapshot(page_es_index, page_id, date):
    """the most recent prior day's view-time snapshot for a page.

    Reads the latest ``{team}__fb_page_insights`` doc for ``fb_page_id`` with
    ``kibana_date`` strictly before ``date`` that carries
    ``page_video_view_time_snapshot``. Returns ``(snapshot_dict, its_date)``
    or ``(None, None)`` when no prior snapshot exists (field ships absent) or
    on any lookup error — fail-absent, never fail-zero.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None, None
        result = es.search(
            index=page_es_index,
            body={
                "size": 1,
                "sort": [{"kibana_date": "desc"}],
                "_source": ["page_video_view_time_snapshot", "kibana_date"],
                "query": {
                    "bool": {
                        "must": [{"term": {"fb_page_id": page_id}}],
                        "filter": [
                            {"range": {"kibana_date": {"lt": date}}},
                            {"exists": {"field": "page_video_view_time_snapshot"}},
                        ],
                    }
                },
            },
        )
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return None, None
        src = hits[0]["_source"]
        return json.loads(src["page_video_view_time_snapshot"]), src.get("kibana_date")
    except Exception as e:  # noqa: BLE001 — lookup is best-effort; absent beats wrong
        logging.warning(f"prior view-time snapshot lookup failed for page {page_id} before {date}: {e}")
        return None, None


def massage_page_data(
    page_metadata,
    page_insights,
    video_views,
    avg_sec_watched,
    total_shares,
    team,
    date,
    view_time_snapshot=None,
    view_time_daily_min=None,
):
    """
    Transform Facebook Page API response to Elasticsearch schema.

    Transformations:
    - Add @timestamp, kibana_date
    - Add page metadata (id, name, username)
    - Flatten page insights
    - Add video views total
    - Add team metadata

    Returns: dict formatted for Elasticsearch
    """
    lib.logging.define("fb-get-page-insights")

    transformed = {}

    # Add timestamps
    transformed["@timestamp"] = datetime.now(timezone.utc).isoformat()
    transformed["kibana_date"] = date

    # Add page metadata
    if page_metadata:
        transformed["fb_page_id"] = page_metadata.get("id")
        transformed["fb_page_name"] = page_metadata.get("name")
        transformed["fb_page_username"] = page_metadata.get("username")
        # page audience size. fan_count is the page's total likes,
        # already fetched by fetch_page_metadata but previously dropped here.
        # It is a fetch-TIME snapshot stamped on the doc for `date` (same
        # semantics as the IG importer's followers_count metadata snapshot):
        # exact for the daily cron's yesterday/today docs, approximate on a
        # historical backfill. Total FOLLOWS is the separate page_follows
        # insight metric, already imported. Only stamped when present so a
        # page that hides likes leaves the field absent, not 0.
        if page_metadata.get("fan_count") is not None:
            transformed["page_fan_count"] = page_metadata["fan_count"]

    # Add page insights with direct field names
    if page_insights:
        for metric, value in page_insights.items():
            transformed[metric] = value

    # Add video views and shares. None means the fetch failed:
    # leave the fields ABSENT so a failure can never masquerade as a real
    # zero-activity day (the old write-0-on-failure made a sub-100-like
    # page's permanent empty responses look like confident zeros forever).
    if video_views is not None:
        transformed["page_video_views_15s_total"] = video_views
    if avg_sec_watched is not None:
        transformed["page_video_avg_sec_watched"] = avg_sec_watched
    if total_shares is not None:
        transformed["page_posts_shares_total"] = total_shares

    # daily video watch time via lifetime-delta. The snapshot
    # (per-video lifetime ms, JSON string, stored-not-indexed) is tomorrow's
    # baseline; the lifetime sum is a visibility aid; the daily minutes field
    # is the dashboardable number. All three follow absent-not-zero:
    # a failed fetch or missing baseline leaves them off the doc entirely.
    if view_time_snapshot is not None:
        transformed["page_video_view_time_snapshot"] = json.dumps(view_time_snapshot, sort_keys=True)
        transformed["page_video_view_time_lifetime_ms"] = sum(int(e.get("ms", 0)) for e in view_time_snapshot.values())
    if view_time_daily_min is not None:
        transformed["page_video_view_time_daily_min"] = round(view_time_daily_min, 2)

    # Ensure all expected metric fields exist (default to 0 if missing)
    # follow-up (2026-06-13): the deprecated names page_views_total
    # and page_impressions_unique are now DROPPED from this list. Every
    # dashboard / report surface that read them has been swapped to the
    # replacements (page_media_view / page_total_media_view_unique) — the
    # trends + maps dashboards, and the summary-report HTML +
    # debug/testing surfaces in this follow-up. New docs therefore no longer
    # carry the deprecated names even as 0. The schema STILL defines them so
    # historical docs read cleanly (see unified_schema_fb_page_insights.json).
    # (2026-07-31): same treatment for page_posts_impressions — the
    # bare total is invalid on Graph v25.0; new docs carry the _organic
    # replacement instead (never the bare name, not even as 0).
    expected_metrics = [
        "page_media_view",  # replacement for page_views_total
        "page_post_engagements",
        "page_daily_follows_unique",
        "page_follows",
        "page_actions_post_reactions_total",
        "page_posts_impressions_organic",  # replacement for page_posts_impressions
        "page_total_media_view_unique",
        "page_video_views",
        "page_daily_follows",
        # the video/shares trio is deliberately NOT in this list —
        # a failed fetch leaves those fields ABSENT (see massage_page_data's
        # None guards); defaulting them to 0 here would make a failure
        # indistinguishable from a real zero-activity day, the exact
        # confident-zeros trap this ticket closes.
    ]

    for metric in expected_metrics:
        if metric not in transformed:
            transformed[metric] = 0

    # Add team metadata
    if team:
        transformed["team_title"] = team.get("team_title")
        transformed["team_esid"] = team.get("_id")
        transformed["team_site_url"] = team.get("team_url")
        transformed["team_country"] = _resolve_team_country(team)
        transformed["team_city"] = team.get("team_city")
        transformed["organization_title"] = team.get("organization_title", "Unknown")

    return transformed


def fetch_page_posts(page_id, access_token, since_timestamp, _attempt=0, _page_size=PAGE_POSTS_PAGE_SIZE):
    """Fetch a page's published posts since ``since_timestamp`` + their
    per-post engagement.

    Demonstrates ``pages_read_engagement``: returns one dict per post with
    content (message, type, created_time, permalink) and engagement
    (reactions, comments, shares, impressions, engaged users). Used by the
    "Recent FB Page Posts" table (KAN App-Review feature). the
    fetch is a trailing time WINDOW (``since`` + cursor pagination), not a
    post-count snapshot — every run re-pulls all posts in the window so
    their still-mutating engagement stays fresh; main() resolves the
    window once per run via ``_posts_since_timestamp``.

    Mirrors ``fetch_page_video_views``'s retry shape: 429 uses the long
    ``RETRY_DELAYS`` ladder, other HTTP errors the short
    ``VIDEO_VIEWS_SERVER_ERROR_DELAYS`` ladder, then degrades to ``[]``.
    A retry restarts the WHOLE window fetch (not the failed cursor page) —
    post docs are keyed by post_id downstream, so a re-pulled post
    overwrites, never duplicates.

    the assumption that used to sit here (post_impressions,
    post_impressions_unique and post_engaged_users "are stable") was wrong —
    all three 400 on v25.0 and took every other per-post metric down with them.
    The live list is POST_INSIGHT_METRICS, and batch_post_insights now drops a
    metric Meta rejects instead of losing the whole post.

    Returns: list of post dicts (raw Graph shape merged with insight
    metrics under ``_insights``), newest-first. Empty list on failure.
    """
    lib.logging.define("fb-get-page-insights")
    try:
        import requests

        url = f"{FB_GRAPH_URL}{page_id}/published_posts"
        # NOTE (verified on staging 2026-05-30): comments.summary(true) and
        # reactions.summary(true) on the /published_posts edge throw
        # OAuthException code 10 ("requires pages_read_engagement") EVEN
        # when the token holds that scope — the summary aggregates are
        # gated harder than the base post read. The base fields below
        # (incl. shares.count) return 200. Comment + reaction COUNTS are
        # instead pulled from the (ungated) post-insights batch via
        # post_activity_by_action_type / post_reactions_by_type_total.
        params = {
            "fields": "id,message,created_time,status_type,permalink_url,shares",
            "access_token": access_token,
            "since": since_timestamp,
            "limit": _page_size,
        }
        # a smaller page size keeps the same total post cap.
        max_pages = -(-PAGE_POSTS_PAGE_SIZE * PAGE_POSTS_MAX_PAGES // _page_size)

        # Cursor pagination: page 1 uses our params; Graph's paging.next is a
        # complete URL (cursor + params baked in), so follow-up requests pass
        # no params of their own.
        posts = []
        pages_fetched = 0
        while url:
            response = requests.get(url, params=params, timeout=FB_GRAPH_HTTP_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            posts.extend(payload.get("data", []))
            pages_fetched += 1
            if pages_fetched >= max_pages:
                if (payload.get("paging") or {}).get("next"):
                    logging.warning(
                        f"Page {page_id}: posts window TRUNCATED at "
                        f"{max_pages * _page_size} posts "
                        f"(pagination backstop) — older posts in the window were NOT fetched."
                    )
                break
            url = (payload.get("paging") or {}).get("next")
            params = None

        if not posts:
            logging.info(f"Page {page_id}: no published posts returned")
            return []

        # Batch per-post insights so a page with N posts is one extra call,
        # not N. Reuses the same batch helper the video-views path uses.
        # post_activity_by_action_type carries {comment, share, like, ...};
        # post_reactions_by_type_total carries {like, love, ...} (summed for
        # the reaction total). Both verified ungated on staging.
        post_ids = [p["id"] for p in posts if p.get("id")]
        # see POST_INSIGHT_METRICS for why the impression and
        # engaged-user metrics are gone.
        insights_by_post = batch_post_insights(post_ids, ",".join(POST_INSIGHT_METRICS), access_token, page_id=page_id)
        for post in posts:
            post["_insights"] = insights_by_post.get(post.get("id"), {})
            # the batch returned NO metrics for this post (dropped,
            # denied, or 200 with empty data). Flag it so the doc carries no
            # insight-derived field and the sender keeps the stored values
            # instead of overwriting them with 0 / absent.
            if post.get("id") not in insights_by_post:
                post[INSIGHTS_FAILED_FLAG] = True

        logging.info(f"Page {page_id}: fetched {len(posts)} published post(s) with engagement")
        return posts

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        # Surface Graph's error body. A 400 is a malformed-request signal
        # (bad field / unsupported edge / deprecated metric), NOT a
        # transient server blip — retrying the same request can't fix it,
        # and without the body the failure is undebuggable. A 4xx other
        # than 429 therefore returns immediately with the reason logged;
        # only 429 (rate limit) and 5xx (server) take the retry ladder.
        body_text = ""
        try:
            body_text = e.response.text[:500]
        except Exception:  # noqa: BLE001 — diagnostic best-effort
            pass
        # "reduce the amount of data" is about the response page
        # size, not a transient blip: retrying the same request fails the same
        # way. Restart the window at half the page size until the floor.
        if _is_reduce_data_error(e.response) and _page_size // 2 >= PAGE_POSTS_MIN_PAGE_SIZE:
            smaller = _page_size // 2
            logging.warning(
                f"[fb-get-page-insights] page {page_id} posts request too large at limit={_page_size}; "
                f"retrying the window at limit={smaller}."
            )
            return fetch_page_posts(page_id, access_token, since_timestamp, _attempt=0, _page_size=smaller)
        if status == 429 or status >= 500:
            schedule = RETRY_DELAYS if status == 429 else VIDEO_VIEWS_SERVER_ERROR_DELAYS
            if _attempt < len(schedule):
                delay = schedule[_attempt]
                logging.warning(
                    f"[fb-get-page-insights] HTTP {status} fetching posts for page {page_id}. "
                    f"Sleeping {delay}s, retry {_attempt + 1}/{len(schedule)}... body={body_text}"
                )
                time.sleep(delay)
                return fetch_page_posts(page_id, access_token, since_timestamp, _attempt=_attempt + 1, _page_size=_page_size)
            logging.warning(
                f"[fb-get-page-insights] HTTP {status} fetching posts for page {page_id} "
                f"after {len(schedule)} retries. Returning no posts. body={body_text}"
            )
            return []
        logging.warning(
            f"[fb-get-page-insights] HTTP {status} (no retry — malformed request) "
            f"fetching posts for page {page_id}. Returning no posts. body={body_text}"
        )
        return []
    except Exception as e:
        logging.warning(f"Could not fetch posts for page {page_id}: {e} (returning [])")
        return []


def massage_post_data(post, page_metadata, team):
    """Transform one Facebook post (+ its insights) to the ES doc schema.

    Mirrors ``massage_page_data``'s team/org-metadata stamping so the post
    doc is org/team-filterable identically to page-insights docs. The full
    team block is what lets the posts table honor the dashboard custom
    filter and Layer-1 scoping (see plan, acceptance criteria).

    Returns: dict formatted for the ``{team_esid}__fb_page_posts`` index.
    """
    lib.logging.define("fb-get-page-insights")

    transformed = {}

    insights = post.get("_insights", {}) or {}
    created_time = post.get("created_time")  # ISO8601 from Graph, e.g. 2026-05-15T12:00:00+0000

    # Timestamps. kibana_date mirrors page-insights' YYYY-MM-DD day field,
    # derived from the post's created date (not import time).
    transformed["@timestamp"] = datetime.now(timezone.utc).isoformat()
    transformed["post_created_time"] = created_time
    if created_time:
        try:
            transformed["kibana_date"] = datetime.fromisoformat(created_time.replace("Z", "+00:00")).date().isoformat()
        except (ValueError, AttributeError):
            transformed["kibana_date"] = None

    # Post content
    transformed["post_id"] = post.get("id")
    transformed["post_message"] = post.get("message", "")
    transformed["post_type"] = post.get("status_type", "")
    transformed["post_permalink_url"] = post.get("permalink_url", "")

    # Engagement. Comments + reactions come from the post-insights batch
    # (the /published_posts summary aggregates are gated — see fetch_page_posts
    # note). Both metrics return a dict-valued payload:
    #   post_activity_by_action_type → {"comment": N, "share": N, "like": N, ...}
    #   post_reactions_by_type_total → {"like": N, "love": N, ...}  (sum = total)
    # Default to 0 so the table renders "0" rather than a missing field.
    activity = insights.get("post_activity_by_action_type") or {}
    reactions_by_type = insights.get("post_reactions_by_type_total") or {}
    shares_field = (post.get("shares", {}) or {}).get("count")
    if post.get(INSIGHTS_FAILED_FLAG):
        # the insights call returned nothing for this post: write
        # no insight-derived count (a 0 would overwrite a real stored value)
        # and mark the doc so send_post_json_to_elastic carries the stored
        # counts forward. shares.count is an ungated post field, so it still
        # lands when Meta returned it.
        transformed[INSIGHTS_FAILED_FLAG] = True
        if shares_field is not None:
            transformed["post_shares"] = shares_field
    else:
        transformed["post_comments"] = activity.get("comment", 0) if isinstance(activity, dict) else 0
        transformed["post_reactions_total"] = (
            sum(v for v in reactions_by_type.values() if isinstance(v, (int, float)))
            if isinstance(reactions_by_type, dict)
            else 0
        )
        # shares: prefer the post's own shares.count field (ungated, exact);
        # fall back to the activity metric's share count if the field is absent.
        if shares_field is None and isinstance(activity, dict):
            shares_field = activity.get("share", 0)
        transformed["post_shares"] = shares_field or 0
    # views / viewers / clicks replace the retired post_impressions,
    # post_impressions_unique and post_engaged_users (400 on Graph v25.0; see
    # POST_INSIGHT_METRICS). New docs carry the replacements and never the
    # retired names, not even as 0 (the page-level treatment); the
    # schema keeps the old fields so historical docs still read. Unlike the
    # counts above, these are written only when Meta returned them: a fetch
    # failure leaves them ABSENT rather than a 0 that reads as "nobody saw it"
    # (the rule).
    for metric in ("post_media_view", "post_total_media_view_unique", "post_clicks"):
        if isinstance(insights.get(metric), (int, float)):
            transformed[metric] = insights[metric]

    # Page context
    if page_metadata:
        transformed["fb_page_id"] = page_metadata.get("id")
        transformed["fb_page_name"] = page_metadata.get("name")

    # Team / org metadata — IDENTICAL block to massage_page_data so the doc
    # is filterable the same way. Do not trim this; the custom filter +
    # Layer-1 scoping depend on team_title / organization_title / team_esid.
    if team:
        transformed["team_title"] = team.get("team_title")
        transformed["team_esid"] = team.get("_id")
        transformed["team_site_url"] = team.get("team_url")
        transformed["team_country"] = _resolve_team_country(team)
        transformed["team_city"] = team.get("team_city")
        transformed["organization_title"] = team.get("organization_title", "Unknown")

    return transformed


def _team_id_filter_matches(team, team_ids_filter):
    """does ``team`` match the ``--team_ids`` filter value?

    The filter value is a comma-separated string of team **ESIDs** (the
    collision-safe key; a title matches only under the
    ``FB_IMPORT_ALLOW_TITLE_MATCH`` legacy escape hatch — exact, logged). The
    wizard kickoff (run_daily_imports → run_page_daily_imports → here), the
    import-data UI, and the report scheduler all pass the resolved
    ESID.

    the matching rules themselves live in the shared
    ``team_matches_team_ids`` (lib.assets), the same predicate
    ``resolve_team_filter_scope`` uses, so the two resolvers can no longer
    drift on team-scope semantics (the title-vs-ESID mismatch class).
    This wrapper only adapts the page importer's team-dict shape.
    """
    return team_matches_team_ids(team.get("_id"), team.get("team_title"), team_ids_filter)


def _dates_needed_for_page(dates, *, team_esid, page_id, page_es_index, force_refetch):
    """pre-check — which of ``dates`` still need an insight import.

    A date is "done" only when its file is archived AND the ES doc
    ``{page_id}_{date}`` exists in the team's ``fb_page_insights`` index
    (archived-but-unindexed means an interrupted send and must re-import).

    ``force_refetch`` bypasses the check outright: every requested
    date is re-fetched and overwritten. Without this the Import Data page's
    "Force refresh" checkbox was a silent no-op for facebook_pages, so a
    stale or degraded stored day could not be corrected from the UI.
    """
    if force_refetch:
        return list(dates)
    return [
        date
        for date in dates
        if not (
            os.path.exists(f"/app/data/facebook/page_insights_imported/{team_esid}/{date}_{page_id}.json")
            and indexed_doc_present(page_es_index, doc_id=f"{page_id}_{date}")
        )
    ]


def _resolve_page_token(page_id, team_title, fb_apps_to_try, accessible_pages_cache, throttled_tokens):
    """Find a page access token for ``page_id`` from the candidate apps/tokens.

    Returns ``(resolved, throttle)``: ``resolved`` is ``{"page_data",
    "fb_app_esid", "fb_app_title"}`` for the first token that sees the page (via
    ``/me/accounts``, else the direct page-token request), in ``fb_apps_to_try``
    order; ``throttle`` is ``(user_title, _PageTokenThrottled)`` when a candidate
    tried before any match was rate-limited — the page is then
    skipped, never resolved through a later token family. ``(None, None)`` when
    no token can see the page.

    ``accessible_pages_cache`` (token → pages) and ``throttled_tokens`` (token →
    throttle) are shared across pages so each token is asked at most once per run.
    """
    for fb_app_esid, fb_app_info in fb_apps_to_try:
        for user_info in fb_app_info["tokens"]:
            user_token = user_info["token"]
            user_title = user_info["title"]
            fb_app_title = fb_app_info["app_title"]
            if user_token in throttled_tokens:
                return None, throttled_tokens[user_token]
            try:
                # Skip /me/accounts for Ads-role users — system user tokens are app-level
                # and have no "me", so this call always fails with 400 for them
                if user_info.get("role") == "Ads":
                    accessible_pages = {}
                elif user_token in accessible_pages_cache:
                    accessible_pages = accessible_pages_cache[user_token]
                else:
                    accessible_pages = get_all_accessible_pages(user_token, user_title)
                    accessible_pages_cache[user_token] = accessible_pages
                if page_id in accessible_pages:
                    logging.info(
                        f"Page {page_id} ({team_title}) found via /me/accounts with user '{user_title}' from FB App '{fb_app_title}'"
                    )
                    return {
                        "page_data": accessible_pages[page_id],
                        "fb_app_esid": fb_app_esid,
                        "fb_app_title": fb_app_title,
                    }, None

                # Try direct token request
                page_data = get_page_access_token(page_id, user_token)
            except _PageTokenThrottled as thr:
                throttled_tokens[user_token] = (user_title, thr)
                return None, (user_title, thr)
            if page_data and "access_token" in page_data:
                logging.info(
                    f"Page {page_id} ({team_title}) accessible via direct request with user '{user_title}' from FB App '{fb_app_title}'"
                )
                return {"page_data": page_data, "fb_app_esid": fb_app_esid, "fb_app_title": fb_app_title}, None
    return None, None


def main(params):
    """
    Main orchestration function for Facebook page insights retrieval.

    Parameters:
    - date (required): YYYY-MM-DD format
    - team_ids (required): comma-separated team ESIDs
      (collision-safe; title match only under the FB_IMPORT_ALLOW_TITLE_MATCH
      legacy escape hatch)
    - fb_app_ids (optional): comma-separated FB app ESIDs
    - fb_page_id (optional): single FB page ID for debugging (still needs
      team_ids for scope)
    """
    lib.logging.define("fb-get-page-insights")

    # bind this run's progress doc so per-page user-actionable errors
    # (permission denied / page not found) recorded below surface in the Report
    # Scheduler's "Errors" button. No-op on the cron path (no progress_run_id).
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    # worker-side backstop matching every other connector's worker
    # (echo/ga4/google_ads/youtube/ig): a scope-less run is a misconfiguration,
    # and falling through to the fleet-wide ds_facebook walk below is a
    # cross-workspace exposure. Placed after set_run_id so the classified
    # error surfaces on the scheduler's progress doc when one is bound.
    from lib.import_scope import require_scope

    require_scope(params, scope_param="team_ids", connector_id="meta_ads")

    # backfill mode: bail out on a Meta rate limit (keep partial data)
    # instead of sleeping/continuing. Set once here from the param the scheduler's
    # backfill path passes (--backfill_bailout); daily/scheduled runs omit it.
    global _backfill_bailout
    _backfill_bailout = params.get("backfill_bailout") in (True, "true", "True", "1", 1)

    # force re-pull: bypass the already-imported skip
    # (dates_needed pre-check + per-date belt-and-suspenders) so an
    # already-imported date is re-fetched and overwritten. Same convention as
    # the FB-ads importer's --force_refetch. Without it the
    # Import Data page's "Force refresh" checkbox was a silent no-op for
    # facebook_pages: a stale or degraded stored day (e.g. a 0 for 15s video
    # views) could not be corrected from the UI at all.
    force_refetch = params.get("force_refetch") in (True, "true", "True", "1", 1)
    # posts-only: refresh the posts window (since the earliest
    # requested date) and skip the per-date page-insights fetch, so a posts
    # backfill can never write fb_page_insights docs. On 2026-10-01 a backfill
    # keyed on each team's oldest post also fetched page insights FOR that date
    # and wrote all-zero docs outside the teams' history.
    posts_only = params.get("posts_only") in (True, "true", "True", "1", 1)
    if posts_only:
        logging.info("posts-only run — page insights are not fetched or written.")
    if force_refetch:
        logging.info("force_refetch=true — already-imported dates will be re-fetched and overwritten.")

    # Determine date(s) to process
    single_date = params.get("date")
    start_date = params.get("start_date")
    end_date = params.get("end_date")

    if start_date and end_date:
        # Validate date formats
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            logging.error(f"Invalid date format. Expected YYYY-MM-DD, got start_date={start_date}, end_date={end_date}")
            return
        if start_dt > end_dt:
            logging.error(f"start_date ({start_date}) must be before end_date ({end_date})")
            return
        dates = []
        current = start_dt
        while current <= end_dt:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        logging.info(f"Starting Facebook page insights import for date range: {start_date} to {end_date} ({len(dates)} days)")
    elif single_date:
        try:
            datetime.strptime(single_date, "%Y-%m-%d")
        except ValueError:
            logging.error(f"Invalid date format: {single_date}. Expected format: YYYY-MM-DD")
            return
        dates = [single_date]
        logging.info(f"Starting Facebook page insights import for date: {single_date}")
    else:
        logging.error("Missing required parameter: --date or --start_date/--end_date")
        return

    # Get optional fb_page_id parameter for debugging
    fb_page_id = params.get("fb_page_id")

    # pass --team_ids into the map build so the requested team's
    # claims are kept BEFORE the last-writer-wins collapse, instead of
    # filtering the already-collapsed map (which dropped a tenant's page to
    # zero when a foreign team shared the page id). The post-build filter
    # below still runs (now a no-op for the scoped path) for the title hatch.
    team_ids_filter = params.get("team_ids")

    # Get team to page mapping
    page_id_to_team = get_all_teams_by_page_id(team_ids_filter=team_ids_filter)

    if not page_id_to_team:
        logging.warning("No teams found with Facebook page IDs. Exiting.")
        return

    # Filter teams by --team_ids if specified (ESID-exact; title only under the
    # FB_IMPORT_ALLOW_TITLE_MATCH hatch). the map is already
    # pre-filtered to this set in get_all_teams_by_page_id, so this is a no-op
    # for the scoped path; it stays for defense-in-depth and to log the result.
    if team_ids_filter:
        page_id_to_team = {
            pid: team for pid, team in page_id_to_team.items() if _team_id_filter_matches(team, team_ids_filter)
        }
        matched_team_names = sorted({team.get("team_title", "Unknown") for team in page_id_to_team.values()})
        logging.info(f"=== Team filter: {', '.join(matched_team_names)} ({len(page_id_to_team)} page(s)) ===")

    # Filter by fb_page_id if specified (for debugging)
    if fb_page_id:
        page_id_to_team = {pid: team for pid, team in page_id_to_team.items() if str(pid) == str(fb_page_id)}
        logging.info(f"DEBUG MODE: Filtered to single page ID: {fb_page_id} (found: {len(page_id_to_team)} match(es))")

    # per-team Page Insights switch: opted-out teams' pages drop out
    # BEFORE the fb_app/token map build, so no Graph calls are made for them.
    _kaf27_off = sorted(
        {team.get("_id") for team in page_id_to_team.values() if not _source_enabled_for_team(team.get("_id"), "fb_pages")}
    )
    if _kaf27_off:
        logging.info("fb_pages disabled for team(s) %s — dropping their page(s) from this run.", ", ".join(_kaf27_off))
        page_id_to_team = {pid: team for pid, team in page_id_to_team.items() if team.get("_id") not in _kaf27_off}
        if not page_id_to_team:
            logging.info("every in-scope team opted out of fb_pages — nothing to import.")
            return

    # when team_ids was passed but the team filter resolved to zero
    # pages, exit cleanly BEFORE the fb_app_map build — walking every legacy
    # fb_app's token chain on the way out leaks cross-tenant noise
    # (connection_shadow lookups, token decrypts, etc.). The cron path
    # (team_ids is None) is unaffected — page_id_to_team is the full cluster
    # mapping and the walk is meaningful.
    #
    # (2026-05-27): the original message here claimed "wizard-cohort
    # teams have no fb_pages chain (that's)" — which became FALSE
    # once shipped and gave wizard teams a Page chain. The real
    # cause of a wizard-scoped run resolving to zero used to be that this
    # filter matched team_title only while the wizard passes a team ESID;
    # the filter above now matches ESID OR title, so a genuinely-empty
    # result here means the team really has no pages (legacy team with no
    # fb_pages chain, or a typo'd filter) — not a wizard provisioning gap.
    if team_ids_filter and not page_id_to_team:
        logging.info(
            "team_ids=%r resolved to zero Facebook pages; skipping fb_app token map build. "
            "The team has no de_asset_fb_pages chain reachable from team_facebook_page_id "
            "(legacy team without pages, or an unmatched team_ids filter).",
            team_ids_filter,
        )
        return

    # Get FB apps. on a scoped (--team_ids) run, load ONLY the apps
    # linked to the in-scope pages' FB accounts instead of every app on the
    # instance: the token walk below decrypts every loaded app's fb_app_user
    # tokens and logs a WARNING per token-less app, so loading all 9 apps on a
    # one-team run burned Graph/ES round-trips and produced a wall of
    # misleading foreign-workspace warnings. The apps-load now sits AFTER the
    # team filter + zero-page early return, so it sees the final page scope.
    # A scoped run with zero linked accounts (wizard-only team) legitimately
    # loads zero apps — the wizard-aware check after the fb_app_map build
    # decides whether that is fatal. The cron path (no team filter) keeps the
    # unfiltered load and its hard "no apps" abort, unchanged.
    if team_ids_filter:
        in_scope_account_esids = sorted(
            {t.get("fb_account_esid") for t in page_id_to_team.values() if t.get("fb_account_esid")}
        )
        fb_apps = get_fb_apps(filters={"fb_account_esid": in_scope_account_esids}) if in_scope_account_esids else []
        logging.info(
            f"scoped FB app load to {len(fb_apps)} app(s) linked to "
            f"{len(in_scope_account_esids)} in-scope account(s)"
        )
    else:
        in_scope_account_esids = None
        fb_apps = get_fb_apps(filters=None)
        if not fb_apps:
            logging.error("No Facebook apps found. Cannot proceed without API credentials.")
            return

    logging.info(f"Found {len(fb_apps)} Facebook app(s)")

    # Filter by fb_app_ids if specified
    fb_app_ids_filter = params.get("fb_app_ids")
    if fb_app_ids_filter:
        fb_app_ids_list = fb_app_ids_filter.split(",")
        fb_apps = [app for app in fb_apps if app["_id"] in fb_app_ids_list]
        logging.info(f"Filtered to {len(fb_apps)} Facebook app(s)")

    # Build mapping of FB App ESID → {app data, tokens}
    # Also build reverse mapping of FB Account ESID → FB App ESID
    if not fb_apps and not team_ids_filter:
        # Unscoped (cron) runs cannot proceed without legacy app credentials.
        # A scoped run with zero apps falls through to the wizard-aware check
        # below — wizard-cohort pages need no legacy fb_app.
        logging.error("No Facebook apps available")
        return

    fb_app_map = {}
    fb_account_to_app = {}  # Reverse mapping: account ESID → app ESID

    for fb_app_doc in fb_apps:
        fb_app_esid = fb_app_doc["_id"]
        fb_app = fb_app_doc["_source"]
        fb_app_title = fb_app.get("fb_app_title", "Unknown")
        fb_app_id = fb_app.get("fb_app_id")
        fb_app_secret = decrypt(fb_app.get("fb_app_secret"))
        fb_account_esid = fb_app.get("fb_account_esid")

        # Build reverse mapping from account to app
        if fb_account_esid:
            fb_account_to_app[fb_account_esid] = fb_app_esid

        # Get ALL FB app users for this app (not just Pages role)
        all_fb_app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid})

        # Collect all valid user tokens for this FB app
        user_tokens = []
        for user_doc in all_fb_app_users:
            user_esid = user_doc["_id"]
            user_title = user_doc["_source"].get("fb_app_user_title", "Unknown")
            user_role = user_doc["_source"].get("fb_app_user_role", "Unknown")
            token = get_fb_app_user_token(user_esid)
            if token:
                user_tokens.append(
                    {
                        "esid": user_esid,
                        "title": user_title,
                        "role": user_role,
                        "token": token,
                    }
                )

        if user_tokens:
            fb_app_map[fb_app_esid] = {
                "app_id": fb_app_id,
                "app_secret": fb_app_secret,
                "app_title": fb_app_title,
                "fb_account_esid": fb_account_esid,
                "tokens": user_tokens,
            }
            if fb_account_esid:
                logging.info(
                    f"FB App '{fb_app_title}' (ESID: {fb_app_esid}) → Account ESID: {fb_account_esid}: {len(user_tokens)} user token(s)"
                )
            else:
                logging.info(
                    f"FB App '{fb_app_title}' (ESID: {fb_app_esid}) [No account linked]: {len(user_tokens)} user token(s)"
                )
        else:
            logging.warning(f"FB App '{fb_app_title}' (ESID: {fb_app_esid}): No user tokens found")

    # don't abort when fb_app_map is empty if at least one Page
    # in scope has wizard_connection_esid — the wizard branch in the
    # per-page loop below can still authenticate those pages via the
    # de_connection token, even with no legacy fb_app_users.
    has_wizard_pages = any(t.get("wizard_connection_esid") for t in page_id_to_team.values())
    if not fb_app_map and not has_wizard_pages:
        logging.error("No FB apps with valid user tokens found and no wizard-cohort Pages. Cannot proceed.")
        return

    if not fb_app_map and has_wizard_pages:
        logging.info(
            "no legacy fb_app_users but %d wizard-cohort Page(s) in scope; " "proceeding with wizard-only auth path",
            sum(1 for t in page_id_to_team.values() if t.get("wizard_connection_esid")),
        )

    logging.info(f"Total: {len(fb_app_map)} FB app(s) with tokens ready")
    logging.info(f"Account→App mappings: {len(fb_account_to_app)}")

    # Build ESID → title lookup for FB accounts (used in log messages).
    # scoped runs only ever look up the in-scope pages' accounts,
    # so load just those; the cron path keeps the full load.
    fb_account_titles = {}
    if team_ids_filter:
        account_docs = get_fb_accounts(filters={"_id": in_scope_account_esids}) if in_scope_account_esids else []
    else:
        account_docs = get_fb_accounts(filters=None)
    for acc_doc in account_docs:
        fb_account_titles[acc_doc["_id"]] = acc_doc["_source"].get("fb_account_title", acc_doc["_id"])

    # Initialize Facebook API with first available token (for SDK calls if needed).
    # when fb_app_map is empty (wizard-only environment), pull
    # the init token from the first wizard-cohort Page's resolved
    # credential triple. The per-page loop below will re-resolve the
    # same triple for actual page-token exchange; reusing the seed
    # here just keeps the SDK initialized.
    if fb_app_map:
        first_app = list(fb_app_map.values())[0]
        seed_app_id = first_app["app_id"]
        seed_app_secret = first_app["app_secret"]
        seed_token = first_app["tokens"][0]["token"]
    else:
        # wizard-only seed
        seed_app_id = seed_app_secret = seed_token = None
        for team in page_id_to_team.values():
            wcid = team.get("wizard_connection_esid")
            if not wcid:
                continue
            wid, wsec, wtok = _resolve_wizard_fb_page_credentials(wcid)
            if wid and wsec and wtok:
                seed_app_id, seed_app_secret, seed_token = wid, wsec, wtok
                break
        if not (seed_app_id and seed_app_secret and seed_token):
            logging.error("No FB apps and no wizard credentials resolvable. Cannot proceed.")
            return
    try:
        # bound SDK HTTP (see init_facebook_api in get_insights.py).
        FacebookAdsApi.init(seed_app_id, seed_app_secret, seed_token, timeout=FB_GRAPH_SDK_TIMEOUT)
    except Exception as e:
        logging.error(f"Failed to initialize Facebook API: {e}")
        return

    # Build pages to process - use page's FB Account → FB App chain
    pages_to_process = {}
    pages_not_accessible = []
    accessible_pages_cache = {}  # token → {page_id: page_data} — avoids duplicate /me/accounts calls
    # rate-limited tokens (token → (user_title, _PageTokenThrottled))
    # and the pages skipped because of them; a backfill stops resolving on the first.
    throttled_tokens = {}
    pages_rate_limited = []
    stopped_on_throttle = False

    for page_id, team in page_id_to_team.items():
        team_title = team.get("team_title", "Unknown")
        page_fb_account_esid = team.get("fb_account_esid")  # This comes from the page asset
        page_found = False

        # Determine which FB App to use based on page's account
        fb_apps_to_try = []
        if page_fb_account_esid and page_fb_account_esid in fb_account_to_app:
            # Page has FB Account configured - look up the FB App for that account
            page_fb_app_esid = fb_account_to_app[page_fb_account_esid]
            if page_fb_app_esid in fb_app_map:
                fb_apps_to_try = [(page_fb_app_esid, fb_app_map[page_fb_app_esid])]
                logging.info(
                    f"Page {page_id} ({team_title}): Using FB App '{fb_app_map[page_fb_app_esid]['app_title']}' via page's FB Account '{fb_account_titles.get(page_fb_account_esid, page_fb_account_esid)}'"
                )
            else:
                logging.warning(
                    f"Page {page_id} ({team_title}): Page's account links to FB App ESID '{page_fb_app_esid}' which has no tokens. Trying all apps."
                )
                fb_apps_to_try = list(fb_app_map.items())
        else:
            # No page account configured or account not linked to any FB App - try all available FB Apps
            fb_apps_to_try = list(fb_app_map.items())
            if page_fb_account_esid:
                logging.warning(
                    f"Page {page_id} ({team_title}): Page's FB Account '{fb_account_titles.get(page_fb_account_esid, page_fb_account_esid)}' not linked to any FB App. Trying all apps."
                )
            else:
                logging.info(f"Page {page_id} ({team_title}): No FB Account configured for page. Trying all available apps.")

        # wizard-cohort Pages carry wizard_connection_esid on
        # the asset doc (written by _bridge_fb_pages). Their token
        # lives on de_connection, not on de_asset_fb_app_users, so the
        # legacy fb_app loop above can't see it. Resolve the wizard
        # credential triple and PREPEND it as a synthetic fb_apps_to_try
        # entry so it's tried first; legacy entries remain as fallback
        # in case the wizard token was revoked between bridge time and
        # importer run. Legacy Pages (no wizard_connection_esid) skip
        # this branch and run unchanged.
        wizard_connection_esid = team.get("wizard_connection_esid")
        if wizard_connection_esid:
            wiz_app_id, wiz_app_secret, wiz_token = _resolve_wizard_fb_page_credentials(wizard_connection_esid)
            if wiz_app_id and wiz_app_secret and wiz_token:
                wizard_entry = (
                    f"wizard:{wizard_connection_esid}",
                    {
                        "app_id": wiz_app_id,
                        "app_secret": wiz_app_secret,
                        "app_title": f"Wizard ({wizard_connection_esid})",
                        "fb_account_esid": None,
                        "tokens": [
                            {
                                "esid": f"wizard:{wizard_connection_esid}",
                                "title": f"wizard:{team.get('wizard_principal') or ''}",
                                "role": "wizard",
                                "token": wiz_token,
                            }
                        ],
                    },
                )
                fb_apps_to_try = [wizard_entry] + fb_apps_to_try
                logging.info(
                    f"Page {page_id} ({team_title}):  wizard token resolved via connection "
                    f"{wizard_connection_esid}; prepended to fb_apps_to_try"
                )

        # Try tokens from selected FB App(s)
        resolved, throttle = _resolve_page_token(page_id, team_title, fb_apps_to_try, accessible_pages_cache, throttled_tokens)
        if resolved:
            pages_to_process[page_id] = dict(resolved, team=team)
            page_found = True
        elif throttle:
            # rate-limited, not inaccessible: skip this page this run
            # and keep its stored data; never fall back to another token family.
            pages_rate_limited.append(
                {"page_id": page_id, "team_title": team_title, "team_esid": team.get("_id"), "throttle": throttle}
            )
            if _backfill_bailout:
                # a backfill stops resolving on the first
                # throttle (every later page shares the exhausted app budget);
                # pages already resolved still run.
                stopped_on_throttle = True
                break
            continue

        if not page_found:
            pages_not_accessible.append(
                {
                    "page_id": page_id,
                    "team_title": team_title,
                    # carried so the recorded error is team-attributed.
                    "team_esid": team.get("_id"),
                    "page_fb_account_esid": page_fb_account_esid,
                }
            )

    # imported once for both the not-accessible sweep and the
    # per-page loop below.
    from lib.import_run_context import set_current_team

    if pages_not_accessible:
        logging.warning(f"\n=== Pages not accessible with any token ===")
        # surface "no token could read this page" to the Report
        # Scheduler. This is the prod symptom ("No accessible pages" for
        # one team / its agency ad account): a page in scope that every available
        # token was denied on. Classifies as permission_denied (the raw
        # carries "access denied"); deduped per team via detail=team_title so
        # N inaccessible pages on one team collapse to one scheduler entry.
        # No-op on the cron path (no progress_run_id bound). Skipped for pages
        # whose account links to no FB App — that's a config gap already warned
        # above, not a per-page permission failure.
        from lib.import_run_context import record_user_error

        for item in pages_not_accessible:
            # per-item team binding (items span teams).
            set_current_team(item.get("team_esid"))
            if item["page_fb_account_esid"]:
                # Skip pages whose account isn't linked to any FB App - that warning was already shown above
                if item["page_fb_account_esid"] not in fb_account_to_app:
                    continue
                logging.warning(
                    f"Page ID {item['page_id']} (Team: {item['team_title']}) - Not accessible with any tokens (page's FB Account: '{fb_account_titles.get(item['page_fb_account_esid'], item['page_fb_account_esid'])}')"
                )
            else:
                logging.warning(
                    f"Page ID {item['page_id']} (Team: {item['team_title']}) - Not accessible with any available tokens (no FB Account configured for page)"
                )
            record_user_error(
                f"Facebook page {item['page_id']} access denied — not accessible with any token",
                connector_id="meta_ads",
                detail=item["team_title"],
            )

    # pages skipped because Meta rate-limited their token. A backfill
    # that stopped on the first throttle also skips every page it never reached.
    if stopped_on_throttle:
        listed = {x["page_id"] for x in pages_rate_limited} | {x["page_id"] for x in pages_not_accessible}
        first = pages_rate_limited[0]["throttle"] if pages_rate_limited else None
        for pid, t in page_id_to_team.items():
            if pid not in pages_to_process and pid not in listed:
                pages_rate_limited.append(
                    {
                        "page_id": pid,
                        "team_title": t.get("team_title", "Unknown"),
                        "team_esid": t.get("_id"),
                        "throttle": first,
                    }
                )
    if pages_rate_limited:
        from lib.import_run_context import record_user_error

        logging.warning(
            "%d page(s) skipped this run — Meta rate-limited their token while resolving it; "
            "stored data is untouched and the next run retries.",
            len(pages_rate_limited),
        )
        for item in pages_rate_limited:
            user_title, thr = item["throttle"] if item["throttle"] else ("?", None)
            set_current_team(item.get("team_esid"))
            logging.warning(
                f"page {item['page_id']} (Team: {item['team_title']}) skipped — token '{user_title}' rate-limited: {thr}"
            )
            record_user_error(
                f"Facebook page {item['page_id']} skipped: Meta rate limit ({thr}) — retried on the next run",
                connector_id="meta_ads",
                detail=item["team_title"],
            )

    if not pages_to_process and pages_rate_limited:
        logging.warning(
            "no page resolved this run — every candidate token was rate-limited. Exiting without writing."
        )
        return

    if not pages_to_process:
        logging.error("No accessible pages to process. Check that the FB app user has page permissions in Business Manager.")
        # still write no_data markers for the in-scope teams so a
        # team whose every page failed auth doesn't produce a silent gap
        # (no real docs + no markers). Without this, the early-return
        # defeats for teams that go entirely dark.
        for date in dates:
            write_no_data_markers(date, set(), "_fb_page_insights", team_mapping=page_id_to_team)
        return

    if team_ids_filter:
        logging.info(f"Processing {len(pages_to_process)} accessible page(s) for team: {', '.join(matched_team_names)}")
    else:
        logging.info(f"Processing {len(pages_to_process)} accessible pages (out of {len(page_id_to_team)} total in teams)")

    # Process each accessible Facebook page
    total_pages = len(pages_to_process)
    processed_count = 0
    error_count = 0
    teams_with_data = set()  # Track teams that had data saved (for no_data markers)

    # resolve the posts window ONCE per run: trailing
    # PAGE_POSTS_WINDOW_DAYS from now, widened to the earliest requested
    # date when a backfill range starts earlier.
    posts_since = _posts_since_timestamp(dates)
    logging.info(
        f"page-posts window since {datetime.fromtimestamp(posts_since, tz=timezone.utc).date().isoformat()} "
        f"(trailing {PAGE_POSTS_WINDOW_DAYS}d, widened by backfill range when earlier)"
    )

    for page_id, page_info in pages_to_process.items():
        team = page_info["team"]
        page_data = page_info["page_data"]
        page_access_token = page_data.get("access_token")

        team_title = team.get("team_title", "Unknown")
        team_esid = team.get("_id")
        # bind this page's team so recorded errors (permission
        # denied, invalid params) carry durable per-team attribution.
        set_current_team(team_esid)
        page_name = page_data.get("name", "Unknown")

        logging.info(
            f"Processing Facebook page {page_id} ({page_name}) for team '{team_title}' ({processed_count + 1}/{total_pages})"
        )

        if not page_access_token:
            logging.error(f"No page access token available for {page_id}. Skipping.")
            error_count += len(dates)
            continue

        logging.debug(f"Using page-specific access token for {page_id}")

        # Proactive rate-limit check (one lightweight call per page) before
        # any other Graph call for this page — metadata, posts, insights.
        # in backfill mode a throttle here raises _RateLimitBailout;
        # STOP the whole page loop gracefully so pages already written keep their
        # data and later pages are left for the next run (matches the account
        # importer's behavior). Off backfill mode this never raises.
        try:
            check_and_pause_for_page(page_id, page_access_token)
        except _RateLimitBailout as bail:
            logging.warning(
                "backfill: rate-limit bail-out at page %s (%d/%d): %s. "
                "Keeping %d processed page(s); stopping so partial data is committed.",
                page_id,
                processed_count + 1,
                total_pages,
                bail,
                processed_count,
            )
            break

        # Fetch page metadata up front. Needed both for the posts step
        # (below, date-independent) and the per-date insights loop, so it
        # must happen BEFORE the date pre-check — otherwise a range whose
        # insight dates are all already imported would skip the page
        # entirely and never fetch posts.
        page_metadata = fetch_page_metadata(page_id, page_access_token)
        if page_metadata in ["not_found", "access_denied"]:
            logging.warning(f"Skipping page {page_id}: {page_metadata}")
            # surface the per-page failure to the user. access_denied
            # (Graph code 10) → permission_denied; not_found (code 100) →
            # asset_not_found. Raw strings chosen to hit the classifier rules.
            from lib.import_run_context import record_user_error

            _raw = (
                "Facebook page access denied (code 10)"
                if page_metadata == "access_denied"
                else (f"Facebook page {page_id} not found or not accessible (code 100)")
            )
            record_user_error(_raw, connector_id="meta_ads", detail=team_title)
            error_count += len(dates)
            continue
        elif not page_metadata:
            logging.error(f"Failed to fetch page metadata for {page_id}")
            error_count += len(dates)
            continue

        logging.info(f"Fetched metadata for page {page_metadata.get('name', page_id)}")

        # KAN App-Review (pages_read_engagement): fetch the posts window +
        # per-post engagement ONCE per page. Runs BEFORE the date pre-check
        # below so it fires even when every requested insight date is
        # already imported (the common case on a re-pull) — the window is a
        # refresh of still-mutating engagement, so the insight-date skip
        # must not gate it. Best-effort: a failure here must not block
        # insights.
        try:
            posts = fetch_page_posts(page_id, page_access_token, posts_since)
            for post in posts:
                post_doc = massage_post_data(post, page_metadata, team)
                post_id_val = post_doc.get("post_id")
                if not post_id_val:
                    continue
                posts_file = f"/app/data/facebook/page_posts/{team_esid}/{post_id_val}.json"
                save_data_to_file(post_doc, posts_file)
            if posts:
                logging.info(f"Saved {len(posts)} post(s) for page {page_id} ({page_name})")
        except Exception as e:
            logging.warning(f"Page posts step failed for {page_id} (continuing with insights): {e}")

        if posts_only:
            # no per-date page-insights fetch in a posts-only run.
            processed_count += len(dates)
            continue

        # Pre-check: skip the per-date insight loop if all dates are already
        # imported. (Posts above already ran — they're date-independent.)
        # a date is "done" only when its file is archived AND the ES
        # doc exists ({page_id}_{date} in {team_esid}__fb_page_insights). A
        # file archived-but-unindexed (interrupted/partial send) must re-import;
        # the ES check fires only for already-archived dates, so a first import
        # pays no extra ES call.
        page_es_index = build_data_index_name(team_esid, "fb_page_insights")
        dates_needed = _dates_needed_for_page(
            dates,
            team_esid=team_esid,
            page_id=page_id,
            page_es_index=page_es_index,
            force_refetch=force_refetch,
        )
        if not dates_needed:
            logging.info(
                f"Page {page_id} ({page_name}): all {len(dates)} insight date(s) already imported. Skipping insights."
            )
            processed_count += len(dates)
            continue

        # in-run view-time baseline. Within a multi-date run the
        # prior date's doc is only a local JSON (send-to-ES happens later), so
        # the ES prior-snapshot lookup would miss it; carry the last snapshot
        # forward in-process instead. dates_needed is ascending, so each date
        # deltas against the nearest earlier baseline.
        run_snapshot = None
        run_snapshot_date = None

        # Fetch insights for each date that needs importing
        for date in dates_needed:
            # Check if already imported
            file_path = f"/app/data/facebook/page_insights/{team_esid}/{date}_{page_id}.json"
            imported_file_path = f"/app/data/facebook/page_insights_imported/{team_esid}/{date}_{page_id}.json"

            # skip only when archived AND indexed; the dates_needed
            # pre-check already applies the same gate, this is the per-date
            # belt-and-suspenders for the single-page path. —
            # force_refetch bypasses this skip too (both guards or neither).
            if (
                not force_refetch
                and os.path.exists(imported_file_path)
                and indexed_doc_present(page_es_index, doc_id=f"{page_id}_{date}")
            ):
                logging.info(f"Page {page_id} for {date} already imported and indexed. Skipping.")
                processed_count += 1
                continue
            if not force_refetch and os.path.exists(imported_file_path):
                logging.warning(
                    "recovery: page %s for %s is archived but ES doc %s_%s "
                    "is MISSING from %s — re-fetching for re-send.",
                    page_id,
                    date,
                    page_id,
                    date,
                    page_es_index,
                )

            # Fetch page insights
            page_insights = fetch_page_insights(page_id, date, page_access_token)
            if page_insights in ["not_accessible", "invalid_params"]:
                logging.warning(f"Skipping insights for page {page_id} on {date}: {page_insights}")
                # surface only "not_accessible" (Graph code 10 =
                # lacks insight permissions → permission_denied, user-actionable).
                # "invalid_params" (code 100) is an our-side request-shape bug,
                # NOT the user's to fix — deliberately not recorded (matches the
                # classifier's "don't surface internal issues" scope on).
                # Deduped per team via detail=team_title, so the same page failing
                # across N dates collapses to one scheduler entry.
                if page_insights == "not_accessible":
                    from lib.import_run_context import record_user_error

                    record_user_error(
                        f"Facebook page {page_id} lacks insights permissions (code 10)",
                        connector_id="meta_ads",
                        detail=team_title,
                    )
                error_count += 1
                continue
            elif not page_insights:
                logging.warning(f"No page insights available for {page_id} on {date}")
                page_insights = {}

            logging.info(f"Fetched page insights for {date}: {len(page_insights)} metrics")

            # Fetch video views, avg watch time, shares, and the per-video
            # lifetime view-time snapshot.
            video_views, avg_sec_watched, total_shares, view_time_snapshot = fetch_page_video_views(
                page_id, date, page_access_token
            )

            # daily watch time = today's lifetime snapshot minus
            # the nearest prior one (in-run baseline first, ES otherwise).
            view_time_daily_min = None
            if view_time_snapshot is not None:
                prior_snap, prior_snap_date = run_snapshot, run_snapshot_date
                if prior_snap is None:
                    prior_snap, prior_snap_date = fetch_prior_view_time_snapshot(page_es_index, page_id, date)
                view_time_daily_min = compute_view_time_daily_min(view_time_snapshot, prior_snap, prior_snap_date)
                run_snapshot, run_snapshot_date = view_time_snapshot, date

            # Transform data for Elasticsearch
            transformed_data = massage_page_data(
                page_metadata,
                page_insights,
                video_views,
                avg_sec_watched,
                total_shares,
                team,
                date,
                view_time_snapshot=view_time_snapshot,
                view_time_daily_min=view_time_daily_min,
            )

            # Save to JSON file
            try:
                save_data_to_file(transformed_data, file_path)
                logging.info(f"Saved Facebook page insights to {file_path}")
                processed_count += 1
                teams_with_data.add(team_esid)
            except Exception as e:
                logging.error(f"Failed to save data to file: {e}")
                error_count += 1

        # Small delay between pages to smooth out API burst traffic
        time.sleep(INTER_PAGE_SLEEP)

    logging.info(
        f"Facebook page insights import completed. Processed: {processed_count}, Errors: {error_count}, Total pages: {total_pages}, Dates: {len(dates)}"
    )

    # Write no_data markers for each date where teams had no page insights.
    # Before writing the markers, delete any stale markers for teams that
    # got real data this run.
    for date in dates:
        delete_stale_markers_for_date("*_fb_page_insights", date, teams_with_data)
        write_no_data_markers(date, teams_with_data, "_fb_page_insights", team_mapping=page_id_to_team)
