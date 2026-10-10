import sys
import os
import re
import json
import logging
import requests
import lib.logging
from datetime import datetime, timezone, timedelta
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError
from facebook_business.adobjects.iguser import IGUser
from facebook_business.adobjects.igmedia import IGMedia
from import_scripts.facebook.lib.assets import (
    get_fb_teams,
    get_fb_apps,
    get_fb_app_users,
    get_ig_accounts,
    get_fb_app_user_for_instagram,
    get_fb_pages,
)
from import_scripts.facebook.get_insights import (
    _FB_RATE_LIMIT_ERROR_CODES,  # shared throttle code set
    _FB_RATE_LIMIT_ERROR_SUBCODES,  # shared throttle subcode set
    _RateLimitBailout,  # shared graceful-bail sentinel (backfill mode)
    _is_rate_limit_error,  # shared throttle-error recognizer
    check_and_pause_for_account,
    get_fb_app_user_token,
    save_data_to_file,
)
from import_scripts.facebook.get_page_insights import (
    get_page_access_token,
    _resolve_team_country,
)
from lib.encryption import decrypt
from lib.es_helpers import build_data_index_name, indexed_doc_present
from lib.fb_constants import FB_GRAPH_HTTP_TIMEOUT, FB_GRAPH_URL, FB_GRAPH_VERSION
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date


# module-global backfill-mode flag (mirrors get_insights._backfill_bailout).
# Set once in main from the --backfill_bailout param threads through the
# IG subprocess chain; read by check_and_pause_for_ig_user and the throttle
# recognition in fetch_instagram_user_insights. Default False = daily/scheduled
# behavior (no probe; rate limits monitored through normal API error responses).
_backfill_bailout = False


def check_and_pause_for_ig_user(instagram_user_id, token):
    """proactive Meta rate-limit probe for an Instagram graph ID.

    ``check_and_pause_for_account`` only understands ``act_``-prefixed ad-account
    IDs, so the IG loop historically had NO probe and a backfill kept hammering a
    throttled app — a rate limit only ever surfaced as returned sentinels deep in
    the per-profile handlers. This is the IG-shaped analog of
    ``check_and_pause_for_page``: one lightweight ``?fields=id`` call whose
    response carries the usage headers, and which a hard-throttled app rejects
    outright with a rate-limit code in the 400 body (the shape).

    Backfill mode only: raises ``_RateLimitBailout`` on a near-limit usage header
    or a throttle-rejected probe so the caller can break the profile loop and
    commit partial data. Daily/scheduled runs return immediately WITHOUT making
    the probe call — their behavior is unchanged. A probe failure
    that is not a throttle signal is non-fatal (a wrong-token 400 here, e.g.
    Graph code 100, is left for the token loop to report as today).
    """
    lib.logging.define("ig-get-insights")
    if not _backfill_bailout:
        return
    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/{instagram_user_id}",
            params={"fields": "id", "access_token": token},
            timeout=FB_GRAPH_HTTP_TIMEOUT,
        )
        # The requests-based probe does NOT raise on a 4xx, so a throttled app
        # returns a 400 whose JSON body carries the rate-limit code (e.g. 4 /
        # 80004, subcode 2446079). A non-throttle 4xx falls through untouched.
        if resp.status_code >= 400:
            try:
                err = (resp.json() or {}).get("error", {}) or {}
            except (ValueError, TypeError):
                err = {}
            if err.get("code") in _FB_RATE_LIMIT_ERROR_CODES or err.get("error_subcode") in _FB_RATE_LIMIT_ERROR_SUBCODES:
                logging.warning(
                    "backfill: usage-check probe for IG user %s was rate "
                    "limited (code %s subcode %s); bailing out to keep partial progress.",
                    instagram_user_id,
                    err.get("code"),
                    err.get("error_subcode"),
                )
                raise _RateLimitBailout(f"usage-check rate limited for IG user {instagram_user_id}")
            return
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
                if not objs:
                    # x-app-usage is a FLAT dict ({"call_count": .., "total_cputime": ..,
                    # "total_time": ..}), unlike x-business-use-case-usage's dict-of-lists —
                    # treat the dict itself as the usage object.
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
                    logging.warning(
                        "backfill: IG user %s near rate limit (%s%%); " "bailing out to keep partial progress.",
                        instagram_user_id,
                        pct,
                    )
                    raise _RateLimitBailout(f"near rate limit for IG user {instagram_user_id}")
    except _RateLimitBailout:
        # Bail-out sentinel must propagate to the profile loop, not be swallowed
        # by the broad non-fatal catch below.
        raise
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        if _is_rate_limit_error(e):
            raise _RateLimitBailout(f"usage-check rate limited for IG user {instagram_user_id}") from e
        logging.debug(f"check_and_pause_for_ig_user: non-fatal error for {instagram_user_id}: {e}")


def _resolve_wizard_ig_credentials(connection_esid):
    """follow-up — return (app_id, app_secret, access_token) for a
    wizard-cohort IG account.

    Mirrors ``_resolve_account_wide_credentials`` on the FB side.
    Wizard-cohort Instagram accounts carry ``wizard_connection_esid`` on
    their ``de_asset_ig_accounts`` doc (written by
    ``_bridge_instagram_assets``); the encrypted user token lives
    on ``de_connection.oauth_token_enc``; the Meta-app credentials come
    from the wizard's shared app via ``WizardOAuthService.get_meta_app``.
    The legacy IG importer auth model only walks
    ``de_asset_fb_app_users`` — wizard-cohort IG accounts have no
    fb_app_user row, so without this resolver the importer falls through
    every token, marks the team ``no_data``, and the wizard-user's
    Instagram surface stays empty even with asset chain in
    place.

    Returns ``(None, None, None)`` on any resolution failure; the caller
    skips the wizard candidate and continues to the legacy fb_app loop
    (preserving today's behaviour for legacy IG accounts that don't
    carry ``wizard_connection_esid``).
    """
    if not connection_esid:
        return None, None, None

    try:
        from asset_management.web.services.wizard_oauth_service import (
            WizardOAuthService,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-IG creds: WizardOAuthService import failed: %s", e)
        return None, None, None

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        logging.warning(
            "wizard-IG creds: WizardOAuthService returned no meta app "
            "for connection %s — wizard Meta-app row missing or ES outage",
            connection_esid,
        )
        return None, None, None

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logging.warning("wizard-IG creds: ES client unavailable")
        return None, None, None

    try:
        conn_doc = es.get(index="de_connection", id=connection_esid)
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "wizard-IG creds: de_connection get(%s) failed: %s",
            connection_esid,
            e,
        )
        return None, None, None

    conn_src = conn_doc.get("_source") or {}
    # pause imports when the user has disconnected this
    # connection via the Manage Connection page. Same shape as the
    # gates on the FB-Ads and FB-Pages resolvers.
    if (conn_src.get("status") or "") == "revoked":
        logging.info(
            "wizard-IG creds: skipping revoked connection_esid=%s",
            connection_esid,
        )
        return None, None, None
    encrypted = conn_src.get("oauth_token_enc") or ""
    if not encrypted:
        logging.warning(
            "wizard-IG creds: de_connection %s has no oauth_token_enc",
            connection_esid,
        )
        return None, None, None

    try:
        access_token = decrypt(encrypted)
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "wizard-IG creds: decrypt failed for connection %s: %s",
            connection_esid,
            e,
        )
        return None, None, None

    return meta_app.client_id, meta_app.client_secret, access_token


def get_all_teams_by_instagram_profile_id(team_ids=None):
    """
    Get all teams with team_instagram_profile_esids and resolve ESIDs to actual Instagram account IDs.

    when ``team_ids`` (comma-separated team ESIDs) is provided,
    the initial ds_instagram=true team query is scoped to just those
    teams' docs. Without this, the cron-day fleet-wide query also fired
    for every wizard-scoped run, enumerating all 15+ IG-enabled teams
    cluster-wide and logging a ``Resolved ESID...`` line for each of
    their IG accounts even though the caller's per-profile filter
    immediately discards everything except the wizard team. Cron path
    (team_ids=None) is unchanged.

    Returns: dict mapping instagram_account_id (numeric string) to team data
    """
    lib.logging.define("ig-get-insights")
    # Only get teams with ds_instagram enabled. When team_ids is set,
    # narrow the query to just those teams so we don't enumerate every
    # tenant's IG accounts on a wizard-scoped run.
    team_filters = {"ds_instagram": "true"}
    if team_ids:
        scoped_ids = [t.strip() for t in team_ids.split(",") if t.strip()]
        if scoped_ids:
            team_filters["_id"] = scoped_ids
    teams_docs = get_fb_teams(filters=team_filters)

    # drop teams under an inactive subscription (Condition A;
    # surfaced as a classified error on the scheduled path). The per-source over-cap
    # check (Condition B) for IG runs at the per-account loop below, where the IG
    # account esid / profile id is in scope to match the Current Plan badge. Fail-
    # open per team.
    from lib.billing_entitlement import filter_entitled_teams

    teams_docs = filter_entitled_teams(teams_docs, connector_id="meta_ads")

    logging.info(f"Found {len(teams_docs)} teams")

    # Get Instagram accounts for ESID lookup. When team_ids scoped the
    # team query above, narrow this lookup to the IG account ESIDs that
    # the matched teams actually reference — avoids enumerating every
    # cluster-wide IG account when the wizard scope only needs one.
    if team_ids and teams_docs:
        needed_esids = set()
        for td in teams_docs:
            for esid in td["_source"].get("team_instagram_profile_esids") or []:
                if esid:
                    needed_esids.add(esid)
        if needed_esids:
            ig_accounts_docs = get_ig_accounts(filters={"_id": list(needed_esids)})
        else:
            ig_accounts_docs = []
    elif team_ids:
        # team_ids was passed but matched ZERO teams. The old code
        # fell through to the unscoped branch below and logged a CLUSTER-WIDE
        # account count ("Found 43 Instagram accounts") on a run scoped to one
        # team, which read like partial success and was the line that made the
        # one team's outage look healthy. A scoped run must never enumerate the
        # fleet: keep it empty so the count reflects the requested scope.
        ig_accounts_docs = []
    else:
        ig_accounts_docs = get_ig_accounts(filters=None)

    esid_to_ig_account = {}
    for ig_doc in ig_accounts_docs:
        esid = ig_doc["_id"]
        ig_data = ig_doc["_source"]
        ig_account_id = ig_data.get("ig_account_id")
        if ig_account_id:
            esid_to_ig_account[esid] = {
                "ig_account_id": ig_account_id,
                "ig_account_username": ig_data.get("ig_account_username"),
                "ig_account_title": ig_data.get("ig_account_title"),
            }

    logging.info(f"Found {len(esid_to_ig_account)} Instagram accounts")

    profile_id_to_team = {}
    teams_with_missing_accounts = []
    # profiles claimed by more than one team. An Instagram Profile
    # belongs to exactly ONE Team (Meta rule 1), so unlike a shared AD ACCOUNT
    # (a billing container legitimately shared across teams — rules 6/7/10, see
    # ) a shared Profile is always a misconfiguration. The old plain
    # dict assignment let whichever team ES returned LAST silently overwrite the
    # other, so one team's Instagram data landed on another team's index with
    # attribution decided by result order. Track the claimants so an ambiguous
    # profile can be DROPPED with a user-facing error instead of guessed.
    profile_claimants: dict[str, list[str]] = {}
    # teams that passed the ds_instagram=true filter but carry no
    # profile esids to walk. This is a LEGITIMATE state, not a fault: the
    # account-wide wizard cohort sets ds_instagram from the connection's
    # instagram_basic scope without provisioning IG assets, because that flow
    # walks ad accounts rather than Pages and has no profiles to discover
    # (connector_status_service accepts it deliberately). But the
    # importer used to discard these teams with a BARE continue: counted in
    # "Found N teams", then evaporating with no log line, no counter, and no
    # summary entry. Meanwhile the DEAM card called them configured, so a team
    # produced no Instagram data and nothing anywhere said why.
    #
    # Note the asymmetry this removes: a DANGLING esid (present but
    # unresolvable) already got a warning plus a summary block, while an EMPTY
    # list — the LESS diagnosable case — got total silence.
    #
    # Diagnostics only. Whether ds_instagram should claim IG is enabled with no
    # walkable assets is a separate design question;
    # this makes the skip visible without presupposing that answer, and does
    # NOT record a user-facing error — the state is expected for this cohort
    # and surfacing it in the scheduler would cry wolf on every run.
    teams_without_profiles = []

    for team_doc in teams_docs:
        team = team_doc["_source"]
        team["_id"] = team_doc["_id"]
        team_title = team.get("team_title", "Unknown")
        profile_esids = team.get("team_instagram_profile_esids", [])

        if not profile_esids:
            teams_without_profiles.append({"team_title": team_title, "team_esid": team["_id"]})
            logging.info(
                "team %r (esid=%s) has ds_instagram=true but no "
                "team_instagram_profile_esids — skipping (no Instagram assets to "
                "walk). Expected for the account-wide wizard cohort, whose flag "
                "comes from the instagram_basic scope rather than from linked "
                "profiles; link an Instagram profile to the team if it should "
                "import Instagram data.",
                team_title,
                team["_id"],
            )
            continue

        if not isinstance(profile_esids, list):
            profile_esids = [profile_esids]

        logging.info(f"Team '{team_title}' has Instagram profile ESIDs: {profile_esids}")

        for esid in profile_esids:
            if not esid:  # Skip empty strings
                continue

            # Look up the actual Instagram account ID from the ESID
            ig_account = esid_to_ig_account.get(esid)
            if ig_account:
                ig_account_id = ig_account["ig_account_id"]
                ig_username = ig_account.get("ig_account_username", "unknown")
                logging.info(f"  Resolved ESID {esid} to Instagram account {ig_account_id} (@{ig_username})")

                # Store the Instagram account ID with team data
                team_with_ig = team.copy()
                team_with_ig["ig_account_username"] = ig_username
                team_with_ig["ig_account_esid"] = esid
                # record every claimant; the ambiguity check runs
                # after the loop so it sees ALL teams, not just the first two
                # (a 3-way collision must not resolve to a "winner" either).
                profile_claimants.setdefault(str(ig_account_id), []).append(team_title)
                profile_id_to_team[str(ig_account_id)] = team_with_ig
            else:
                logging.warning(f"  Team '{team_title}' has ESID {esid} that cannot be found in de_asset_ig_accounts")
                teams_with_missing_accounts.append(
                    {
                        "team_title": team_title,
                        "team_esid": team["_id"],
                        "missing_esid": esid,
                    }
                )

    if teams_with_missing_accounts:
        logging.warning(f"\n=== Teams with missing Instagram accounts ===")
        for item in teams_with_missing_accounts:
            logging.warning(
                f"Team: {item['team_title']} (ESID: {item['team_esid']}) - Missing IG Account ESID: {item['missing_esid']}"
            )

    # summary block for the flag-on/no-profiles cohort, mirroring the
    # dangling-esid block above so both skip reasons are visible in one place.
    # INFO, not WARNING: this state is expected for the account-wide cohort, and
    # warning on every run for every such team would train readers to ignore it.
    if teams_without_profiles:
        logging.info("\n=== Teams with ds_instagram=true but no Instagram profiles linked ===")
        for item in teams_without_profiles:
            logging.info(f"Team: {item['team_title']} (ESID: {item['team_esid']}) - no team_instagram_profile_esids; skipped")
        logging.info(
            "%d team(s) skipped for having no linked Instagram profile. These are "
            "counted in the 'Found %d teams' total above but contribute no data.",
            len(teams_without_profiles),
            len(teams_docs),
        )

    # drop any profile claimed by 2+ teams rather than letting ES
    # result order pick a winner. Attribution for a Profile runs
    # Profile -> Page -> Team (Meta rules 9 -> 1); when two teams claim one
    # Profile that chain is broken and there is no correct answer available
    # here, so guessing means writing one team's data onto another team's index
    # a cross-tenant error, and exactly what declined to do on the
    # Facebook side. Dropping is visible and recoverable; misattribution is
    # neither. Note the profile is OMITTED from the map rather than mapped to
    # None: callers do team.get(...) on the values (see the account_wide check
    # and the per-profile loop), so a None value would raise instead of
    # declining.
    for ig_account_id, claimants in sorted(profile_claimants.items()):
        if len(claimants) < 2:
            continue
        profile_id_to_team.pop(ig_account_id, None)
        team_list = ", ".join(sorted(claimants))
        logging.error(
            "Instagram profile %s is claimed by %d teams (%s). An "
            "Instagram Profile belongs to exactly ONE team, so this is a "
            "misconfiguration — SKIPPING it rather than guessing which team "
            "owns its data. Remove team_instagram_profile_esids=%s from every "
            "team except the one whose team_facebook_page_id holds the "
            "profile's linked_page_id.",
            ig_account_id,
            len(claimants),
            team_list,
            ig_account_id,
        )
        # Surface it to the Report Scheduler's Errors panel, not just the log.
        # Deduped by (category, detail), so one line per profile.
        try:
            from lib.import_run_context import record_user_error

            record_user_error(
                f"no active team is unambiguously linked to Instagram profile {ig_account_id}",
                connector_id="meta_ads",
                detail=f"Instagram profile {ig_account_id} is claimed by {len(claimants)} teams ({team_list})",
            )
        except Exception:  # noqa: BLE001 — reporting must never block the import
            logging.warning("record_user_error failed", exc_info=True)

    logging.info(f"Loaded {len(profile_id_to_team)} Instagram account ID to team mappings")
    return profile_id_to_team


def fetch_instagram_user_metadata(instagram_user_id):
    """
    Fetch Instagram account metadata.

    Returns: dict of user metadata or None on error
    """
    lib.logging.define("ig-get-insights")
    try:
        user = IGUser(instagram_user_id).api_get(
            fields=[
                "id",
                "username",
                "name",
                "profile_picture_url",
                "followers_count",
                "follows_count",
                "media_count",
                "biography",
                "website",
            ]
        )
        return user.export_all_data()
    except FacebookRequestError as e:
        code = e.api_error_code()
        subcode = e.api_error_subcode()
        message = e.api_error_message()
        if code == 100:
            logging.debug(f"Instagram profile {instagram_user_id} error 100 (subcode: {subcode}): {message}")
            return "not_found"
        elif code == 10:
            logging.debug(f"Instagram profile {instagram_user_id} error 10 (subcode: {subcode}): {message}")
            return "access_denied"
        else:
            logging.error(
                f"Failed to fetch Instagram user metadata for {instagram_user_id}: code={code} subcode={subcode} message={message}"
            )
            return None


IG_WINDOW_MAX_DAYS = 30  # IG's own API cap: "(#100) There cannot be more than 30 days between since and until."


def fetch_instagram_window_reach(instagram_user_id, since_date, until_date, token):
    """IG's range-DEDUPLICATED reach for [since_date..until_date]
    (inclusive), in ONE call.

    Uses ``metric_type=total_value``, which returns IG's own deduplicated
    "Accounts reached" total for the requested range — verified empirically
    on prod 2026-08-26: 7d total 1,612 vs daily-sum 1,803; 30d total 6,354 vs
    daily-sum 9,129 (a 1.44x inflation the daily-sum tile shows today).

    IG's ``until`` is EXCLUSIVE (a since..until request returns days
    [since..until-1], verified against the daily series), so this requests
    ``until_date + 1 day`` to cover the inclusive window the doc is keyed on.

    Returns an int, or a string sentinel: ``"range_too_long"`` (the API caps
    since..until at 30 days — callers must not write a doc), or ``"error"``.
    """
    lib.logging.define("ig-get-insights")
    try:
        import requests

        since_obj = datetime.strptime(since_date, "%Y-%m-%d").date()
        until_obj = datetime.strptime(until_date, "%Y-%m-%d").date()
        span_days = (until_obj - since_obj).days + 1
        if span_days > IG_WINDOW_MAX_DAYS:
            logging.warning(
                "IG window %s..%s spans %d days > %d-day API cap — skipping (no doc written).",
                since_date,
                until_date,
                span_days,
                IG_WINDOW_MAX_DAYS,
            )
            return "range_too_long"

        response = requests.get(
            f"{FB_GRAPH_URL}/{instagram_user_id}/insights",
            params={
                "metric": "reach",
                "period": "day",
                "metric_type": "total_value",
                "since": str(since_obj),
                "until": str(until_obj + timedelta(days=1)),
                "access_token": token,
            },
            timeout=FB_GRAPH_HTTP_TIMEOUT,
        )
        payload = response.json()
        if "error" in payload:
            logging.warning(
                "IG window reach error for %s %s..%s: %s",
                instagram_user_id,
                since_date,
                until_date,
                str(payload["error"].get("message"))[:200],
            )
            return "error"
        for metric in payload.get("data", []):
            if metric.get("name") == "reach":
                return int((metric.get("total_value") or {}).get("value") or 0)
        return "error"
    except Exception as e:  # noqa: BLE001 — best-effort sibling step, never fail the run
        logging.warning("IG window reach fetch failed for %s: %s", instagram_user_id, e)
        return "error"


def massage_instagram_window_data(user_metadata, team, since_date, until_date, reach):
    """build the {team_esid}__ig_insights_window doc for one
    (profile, window). Mirrors the daily massage's team stamping; the window
    bounds use the FB window contract fields dedup_routing reads."""
    return {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "insight_window_start": since_date,
        "insight_window_end": until_date,
        "grain": "profile",
        "reach": int(reach),
        "ig_user_id": user_metadata.get("id"),
        "ig_user_username": user_metadata.get("username"),
        "ig_user_name": user_metadata.get("name"),
        "team_title": team.get("team_title"),
        "team_esid": team.get("_id"),
        "team_site_url": team.get("team_url"),
        "team_country": _resolve_team_country(team),
        "team_city": team.get("team_city"),
        "organization_title": team.get("organization_title", "Unknown"),
    }


def send_instagram_window_doc(doc, team):
    """write one window doc to {team_esid_lower}__ig_insights_window.

    Deterministic _id {profile}_{start}_{end}: re-pulling the same window
    overwrites in place (the FB window sender's idempotency contract)."""
    from lib.es_helpers import ensure_data_index, get_elasticsearch_client
    from migration.schema_loader import get_schema_for_index_type

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch unavailable — window doc not written.")
        return False
    team_esid = team.get("_id")
    index = ensure_data_index(es, team_esid, "ig_insights_window", get_schema_for_index_type("ig_insights_window"))
    doc_id = f"{doc['ig_user_id']}_{doc['insight_window_start']}_{doc['insight_window_end']}"
    es.index(index=index, id=doc_id, document=doc)
    logging.info(
        "wrote IG window doc %s to %s (reach=%s, window %s..%s)",
        doc_id,
        index,
        doc["reach"],
        doc["insight_window_start"],
        doc["insight_window_end"],
    )
    return True


def fetch_instagram_user_insights(instagram_user_id, date, token):
    """
    Fetch account-level insights for an Instagram Business Account for a specific date.

    The Instagram Insights API requires two separate calls:
    - Time-series metrics (reach): SDK get_insights(), parses values[0].value
    - Total-value metrics (views, profile_views, etc.): raw HTTP request required
      because the Facebook Business SDK silently drops the total_value field from
      responses. metric_type=total_value must be sent and total_value.value parsed.

    Returns: dict of insights data or None on error
    """
    lib.logging.define("ig-get-insights")
    try:
        user = IGUser(instagram_user_id)
        parsed_insights = {}

        # Convert date to Unix timestamps. The API treats since/until as a half-open
        # interval [since, until), so until must be the start of the *next* day.
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        since_timestamp = int(date_obj.replace(tzinfo=timezone.utc).timestamp())
        until_timestamp = since_timestamp + 86400  # next day

        # Call 1: time-series metrics via SDK
        # follower_count excluded — only supports last 30 days, and is
        # already captured in fetch_instagram_user_metadata (followers_count field)
        ts_insights = user.get_insights(
            params={
                "metric": ["reach"],
                "period": ["day"],
                "since": since_timestamp,
                "until": until_timestamp,
            }
        )
        for insight in ts_insights:
            metric_name = insight.get("name")
            values = insight.get("values", [])
            if values:
                parsed_insights[metric_name] = values[0].get("value", 0)

        # Call 2: total-value metrics via raw HTTP (SDK strips total_value field)
        tv_metrics = [
            "profile_views",
            "website_clicks",
            "views",
            "accounts_engaged",
            "total_interactions",
            "follows_and_unfollows",
        ]
        response = requests.get(
            f"{FB_GRAPH_URL}/{instagram_user_id}/insights",
            params={
                "metric": ",".join(tv_metrics),
                "metric_type": "total_value",
                "period": "day",
                "since": since_timestamp,
                "until": until_timestamp,
                "access_token": token,
            },
        )
        response.raise_for_status()
        for item in response.json().get("data", []):
            metric_name = item.get("name")
            total_value = item.get("total_value", {})
            parsed_insights[metric_name] = total_value.get("value", 0) if total_value else 0

        return parsed_insights
    except FacebookRequestError as e:
        if e.api_error_code() == 10:
            logging.warning(
                f"Instagram @{instagram_user_id} lacks insights permissions (error code 10). "
                f"Fix: Ensure the account is a Business/Creator account and the token has instagram_manage_insights permission."
            )
            return "not_business_account"
        elif e.api_error_code() == 100:
            logging.warning(f"Invalid parameters for Instagram insights {instagram_user_id} (error code 100)")
            return "invalid_params"
        else:
            # in backfill mode a throttle here must stop the profile
            # loop (keep partial data) instead of reading as a generic failure.
            # Codes 10 / 100 above keep their exact sentinels either way.
            if _backfill_bailout and _is_rate_limit_error(e):
                logging.warning(f"Instagram user insights for {instagram_user_id} on {date} rate limited: {e}")
                return "rate_limited"
            logging.error(f"Failed to fetch Instagram user insights for {instagram_user_id} on {date}: {e}")
            return None
    except requests.RequestException as e:
        # the total-value call is raw requests, so a throttle surfaces
        # as an HTTPError whose response body carries the rate-limit code.
        if _backfill_bailout:
            err_resp = getattr(e, "response", None)
            if err_resp is not None:
                try:
                    err = (err_resp.json() or {}).get("error", {}) or {}
                except (ValueError, TypeError):
                    err = {}
                if err.get("code") in _FB_RATE_LIMIT_ERROR_CODES or err.get("error_subcode") in _FB_RATE_LIMIT_ERROR_SUBCODES:
                    logging.warning(f"Total-value insights for {instagram_user_id} on {date} rate limited: {e}")
                    return "rate_limited"
        logging.error(f"HTTP error fetching total-value insights for {instagram_user_id} on {date}: {e}")
        return None


def fetch_instagram_media_list(instagram_user_id, date):
    """
    Fetch list of media posted on a specific date.

    Returns: list of media objects with id, media_type, timestamp
    """
    lib.logging.define("ig-get-insights")
    try:
        user = IGUser(instagram_user_id)
        media_list = user.get_media(
            fields=[
                "id",
                "media_type",
                "caption",
                "timestamp",
                "like_count",
                "comments_count",
                "media_url",
                "permalink",
            ]
        )

        # Filter media by date
        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
        filtered_media = []

        for media in media_list:
            media_data = media.export_all_data()
            timestamp_str = media_data.get("timestamp")
            if timestamp_str:
                # Parse ISO timestamp
                media_date = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")).date()
                if media_date == date_obj:
                    filtered_media.append(media_data)

        logging.info(f"Found {len(filtered_media)} media items for {instagram_user_id} on {date}")
        return filtered_media
    except FacebookRequestError as e:
        logging.error(f"Failed to fetch Instagram media list for {instagram_user_id}: {e}")
        return []


# media-insights metric sets per media type. Meta removed the
# "engagement" and "carousel_album_*" media metrics (replaced by
# "total_interactions"), renamed the reel "plays" metric to "views", and as
# of v22 rejects "impressions" for newer media ("views" is its successor
# there too — confirmed live on prod 2026-07-13, fbtrace ABIQV6Ip8r).
# ONE invalid metric 400s the whole get_insights call (Graph #100), which
# silently blanked media insights for every media item, so these lists must
# track the valid set in
# https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
_MEDIA_INSIGHT_METRICS = {
    "VIDEO": [
        "views",
        "reach",
        "total_interactions",
        "saved",
        "shares",
        "likes",
        "comments",
        "follows",
    ],
    "CAROUSEL_ALBUM": [
        "views",
        "reach",
        "total_interactions",
        "saved",
        "shares",
        "likes",
        "comments",
        "follows",
    ],
    "IMAGE": [
        "views",
        "reach",
        "total_interactions",
        "saved",
        "shares",
        "likes",
        "comments",
        "follows",
    ],
    # REEL and unknown types
    "REEL": [
        "views",
        "reach",
        "total_interactions",
        "saved",
        "shares",
        "likes",
        "comments",
        "follows",
    ],
}


# run-scoped record of metrics Meta rejected as invalid, keyed by
# media_type. Ported from the FB page-insights self-heal: one invalid
# metric 400s the WHOLE get_insights call (Graph #100), which pre-fix silently
# blanked every media item's insights the moment Meta deprecated a metric. Here
# we drop the named metric and retry, and remember the drop so subsequent media
# items of the same type don't re-pay the 400 round-trip. This cache is a runtime
# safety net; _MEDIA_INSIGHT_METRICS stays the source of truth and a drop should
# still prompt a KAN ticket to choose a replacement.
_MEDIA_METRICS_DROPPED = {}


def reset_media_metric_drop_cache():
    """clear the per-run dropped-metric cache. Called at the start of
    ``main`` so a long-lived process (tests, repeated invocations) doesn't carry a
    stale drop across runs."""
    _MEDIA_METRICS_DROPPED.clear()


def _parse_rejected_media_metric(message, requested_metrics):
    """pull the rejected metric name out of a Graph #100 message.

    Two shapes were seen live on prod 2026-07-13:
      * positional — ``"metric[2] must be one of the following values: ..."``:
        the bracketed number is the 0-based index into the metric list we sent,
        so we map it back through ``requested_metrics``.
      * named — ``"the plays metric is no longer supported for the queried media"``:
        the metric name is in the message text directly.

    Returns the metric name (a member of ``requested_metrics``) or ``None`` when
    the message is neither shape or names a metric we didn't request (so the
    caller doesn't loop on an un-droppable metric).
    """
    if not message:
        return None

    # Named shape first — it carries the literal metric name.
    named = re.search(r"\bthe\s+(\w+)\s+metric\s+is\s+no\s+longer\s+supported", message, re.IGNORECASE)
    if named:
        candidate = named.group(1)
        if candidate in requested_metrics:
            return candidate

    # Positional shape — "metric[N] must be one of ...".
    positional = re.search(r"metric\[(\d+)\]", message)
    if positional:
        idx = int(positional.group(1))
        if 0 <= idx < len(requested_metrics):
            return requested_metrics[idx]

    return None


def fetch_instagram_media_insights(media_id, media_type):
    """
    Fetch insights for a specific media object.

    Metrics vary by media_type — see ``_MEDIA_INSIGHT_METRICS``.

    self-heals around a metric Meta has deprecated. Meta validates the
    metric list all-or-nothing: one invalid metric 400s the whole call with Graph
    #100, which pre-fix blanked ALL media insights for every team the moment a
    metric was removed. On that specific #100 we parse the rejected metric out of
    the message, drop it, and retry with the remainder — so a deprecation degrades
    one metric instead of everything. Retries are bounded by the metric-list length
    (never unbounded), and the drop is logged once per run per (media_type, metric)
    and cached so later media items of the same type skip the 400 round-trip.

    Returns: dict of media insights ({} on error).
    """
    lib.logging.define("ig-get-insights")

    base_metrics = _MEDIA_INSIGHT_METRICS.get(media_type, _MEDIA_INSIGHT_METRICS["REEL"])
    cache_key = media_type or "REEL"
    already_dropped = _MEDIA_METRICS_DROPPED.setdefault(cache_key, set())
    # Start from the known-good subset — metrics dropped earlier this run stay out.
    metrics = [m for m in base_metrics if m not in already_dropped]

    # Bounded: at most one retry per metric that could still be rejected. Each
    # iteration either succeeds, hits a non-invalid-metric error, or drops exactly
    # one metric — so the loop can run at most len(base_metrics) times.
    for _ in range(len(base_metrics) + 1):
        if not metrics:
            # Everything got dropped — nothing left to ask for.
            return {}
        try:
            media = IGMedia(media_id)
            insights = media.get_insights(params={"metric": metrics}, fields=["name", "values"])

            # Parse insights response
            parsed_insights = {}
            for insight in insights:
                metric_name = insight.get("name")
                if insight.get("values") and len(insight["values"]) > 0:
                    value = insight["values"][0].get("value", 0)
                    parsed_insights[metric_name] = value

            return parsed_insights
        except FacebookRequestError as e:
            code = e.api_error_code()
            if code == 10:
                logging.debug(f"Insights not available for media {media_id} (error code 10) - may be too old or story")
                return {}
            if code == 100:
                bad_metric = _parse_rejected_media_metric(e.api_error_message(), metrics)
                if bad_metric:
                    # Warn once per run per (media_type, metric); cache the drop so
                    # subsequent media items of this type don't re-pay the 400.
                    if bad_metric not in already_dropped:
                        logging.warning(
                            "IG media insights: Meta rejected metric %r for media_type %s "
                            "(Graph #100) — dropping it and retrying with the remaining metrics. "
                            "Update _MEDIA_INSIGHT_METRICS and file a replacement ticket.",
                            bad_metric,
                            cache_key,
                        )
                        already_dropped.add(bad_metric)
                    metrics = [m for m in metrics if m != bad_metric]
                    continue
                # A #100 we can't attribute to a specific requested metric — don't
                # loop blindly; fall through to the generic handler below.
            logging.warning(f"Failed to fetch media insights for {media_id}: {e}")
            return {}

    # Retry budget exhausted (should be unreachable — each pass drops a metric).
    logging.warning(
        "IG media insights: retry budget exhausted for media %s (media_type %s); "
        "giving up to avoid an unbounded loop.",
        media_id,
        cache_key,
    )
    return {}


def massage_instagram_data(data, team, date):
    """
    Transform Instagram API response to Elasticsearch schema.

    Transformations:
    - Prefix user fields with 'ig_user_'
    - Prefix insight fields with 'ig_insight_'
    - Prefix media fields with 'ig_media_'
    - Add @timestamp, kibana_date
    - Add team metadata

    Returns: dict formatted for Elasticsearch
    """
    lib.logging.define("ig-get-insights")

    transformed = {}

    # Add timestamps
    transformed["@timestamp"] = datetime.now(timezone.utc).isoformat()
    transformed["kibana_date"] = date

    # Add user metadata with ig_user_ prefix
    user_fields = [
        "id",
        "username",
        "name",
        "profile_picture_url",
        "followers_count",
        "follows_count",
        "media_count",
        "biography",
        "website",
    ]
    for field in user_fields:
        if field in data:
            transformed[f"ig_user_{field}"] = data[field]

    # Add user insights with ig_insight_ prefix
    if "insights" in data:
        for metric, value in data["insights"].items():
            transformed[f"ig_insight_{metric}"] = value

    # Add media data with ig_media_ prefix (as nested array)
    if "media" in data and data["media"]:
        transformed["ig_media"] = []
        for media_item in data["media"]:
            media_obj = {}

            # Basic media fields
            media_obj["ig_media_id"] = media_item.get("id")
            media_obj["ig_media_type"] = media_item.get("media_type")
            media_obj["ig_media_caption"] = media_item.get("caption")
            media_obj["ig_media_timestamp"] = media_item.get("timestamp")
            media_obj["ig_media_like_count"] = media_item.get("like_count")
            media_obj["ig_media_comments_count"] = media_item.get("comments_count")
            media_obj["ig_media_url"] = media_item.get("media_url")
            media_obj["ig_media_permalink"] = media_item.get("permalink")

            # Media insights
            if "insights" in media_item:
                for metric, value in media_item["insights"].items():
                    media_obj[f"ig_media_insight_{metric}"] = value

            transformed["ig_media"].append(media_obj)

        # Add top-level totals so Kibana can aggregate without nested queries
        transformed["ig_media_total_likes"] = sum(m.get("ig_media_insight_likes", 0) for m in transformed["ig_media"])
        transformed["ig_media_total_shares"] = sum(m.get("ig_media_insight_shares", 0) for m in transformed["ig_media"])
        transformed["ig_media_total_comments"] = sum(m.get("ig_media_insight_comments", 0) for m in transformed["ig_media"])
        # non-reel "impressions" is v22-removed; "views" is its
        # successor. Reels are excluded from the impressions fallback so a
        # reel's views aren't double-counted into both the impressions and
        # plays roll-ups (matching the pre-rename split).
        transformed["ig_media_total_impressions"] = sum(
            m.get(
                "ig_media_insight_impressions",
                0 if m.get("ig_media_type") == "REEL" else m.get("ig_media_insight_views", 0),
            )
            for m in transformed["ig_media"]
        )
        transformed["ig_media_total_reach"] = sum(m.get("ig_media_insight_reach", 0) for m in transformed["ig_media"])
        # Meta removed "engagement" (→ total_interactions) and
        # renamed reel "plays" (→ views). Keep the historical roll-up fields
        # populated from the replacement metrics so dashboards reading
        # ig_media_total_engagement / ig_media_total_plays don't go dark;
        # docs written before the rename still resolve via the old key.
        transformed["ig_media_total_engagement"] = sum(
            m.get("ig_media_insight_engagement", m.get("ig_media_insight_total_interactions", 0))
            for m in transformed["ig_media"]
        )
        transformed["ig_media_total_saves"] = sum(m.get("ig_media_insight_saved", 0) for m in transformed["ig_media"])
        transformed["ig_media_total_video_views"] = sum(
            m.get("ig_media_insight_video_views", 0) for m in transformed["ig_media"]
        )
        transformed["ig_media_total_plays"] = sum(
            m.get(
                "ig_media_insight_plays",
                m.get("ig_media_insight_views", 0) if m.get("ig_media_type") == "REEL" else 0,
            )
            for m in transformed["ig_media"]
        )
        transformed["ig_media_total_interactions"] = sum(
            m.get("ig_media_insight_total_interactions", 0) for m in transformed["ig_media"]
        )

    # Add team metadata
    if team:
        transformed["team_title"] = team.get("team_title")
        transformed["team_esid"] = team.get("_id")
        transformed["team_site_url"] = team.get("team_url")
        transformed["team_country"] = _resolve_team_country(team)
        transformed["team_city"] = team.get("team_city")
        transformed["organization_title"] = team.get("organization_title", "Unknown")

    return transformed


def main(params):
    """
    Main orchestration function for Instagram insights retrieval.

    Parameters:
    - date (required): YYYY-MM-DD format
    - team_ids (optional): comma-separated team ESIDs
    - fb_app_ids (optional): comma-separated FB app ESIDs
    """
    lib.logging.define("ig-get-insights")

    # start each run with a clean dropped-metric cache so a metric
    # dropped in a prior run isn't silently omitted from this one.
    reset_media_metric_drop_cache()

    # bind this run's progress doc so a per-profile permission failure
    # (code 10) surfaces in the Report Scheduler's "Errors" button. No-op on cron.
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    # backfill mode: bail out on a Meta rate limit (keep partial data)
    # instead of marching profile-by-profile into a throttled app.
    # threads --backfill_bailout through run_daily_imports →
    # run_instagram_daily_imports → here; daily/scheduled runs omit it.
    global _backfill_bailout
    _backfill_bailout = params.get("backfill_bailout") in (True, "true", "True", "1", 1)

    # Validate date parameter
    # window mode: --window_since/--window_until (inclusive) pull
    # IG's range-DEDUPLICATED reach per profile into {esid}__ig_insights_window
    # instead of running the per-day pipeline. Discovery, token resolution,
    # and team scoping are IDENTICAL to the daily path — the loop below
    # branches per profile after authentication.
    window_since = params.get("window_since")
    window_until = params.get("window_until")
    if bool(window_since) != bool(window_until):
        logging.error("--window_since and --window_until must be provided together.")
        return
    window_mode = bool(window_since and window_until)

    date = params.get("date") or (window_since if window_mode else None)
    if not date:
        logging.error("Missing required parameter: date (format: YYYY-MM-DD)")
        return

    # Validate date format
    dates_to_validate = [date] + ([window_since, window_until] if window_mode else [])
    for d in dates_to_validate:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            logging.error(f"Invalid date format: {d}. Expected format: YYYY-MM-DD")
            return

    if window_mode:
        logging.info(f"Starting Instagram WINDOW dedup-reach pull for {window_since}..{window_until}")
    else:
        logging.info(f"Starting Instagram insights import for date: {date}")

    # require an explicit team scope BEFORE any lookup.
    # Cron is dead (deployment/03_provision_cron_jobs.sh migrated the importer
    # jobs to the Report Scheduler), the manual Import Data Page
    # always sends the selected team, and backfill reuses the scheduler dialog.
    # So no legitimate caller omits --team_ids: an absent flag is a
    # misconfiguration and must fail loudly rather than fall through to a
    # cluster-wide scan (the cross-tenant exposure class behind).
    #
    # ImportService._resolve_scope_arg (import_service.py:450-451) DELIBERATELY
    # omits the flag when a team resolves to no linked account, documenting that
    # it relies on this guard to surface a classifiable "must be team-scoped"
    # error instead of the UI hanging on a 0-byte log. That contract only holds
    # if the guard actually exists here.
    from lib.import_scope import require_scope, scope_resolution_error

    team_ids_filter_early = require_scope(params, scope_param="team_ids", connector_id="meta_ads")

    # Pass team_ids early so the initial ds_instagram=true query is scoped to the
    # wizard team only — avoids the cluster-wide enumeration on wizard-scoped
    # runs.
    profile_id_to_team = get_all_teams_by_instagram_profile_id(team_ids=team_ids_filter_early)

    if not profile_id_to_team:
        # A scope flag that RESOLVES to zero is the second half of the same
        # guard: the caller named specific teams and none produced a usable IG
        # profile. Pre-fix this exited rc 0 with only a WARNING, so the Report
        # Scheduler booked it a success — six prod teams imported nothing for
        # weeks and nothing alerted (same shape as other teams before). Raising lets
        # import.py's handler convert it to a non-zero exit, and
        # scope_resolution_error records the classifiable no_data_source cause.
        # Mirrors Facebook's strict mode (get_insights.py:1783-1794).
        raise scope_resolution_error(
            scope_param="team_ids",
            scope_value=team_ids_filter_early,
            connector_id="meta_ads",
        )

    # Get FB apps (reuse existing Facebook apps)
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

    # Filter teams by team_ids if specified
    team_ids_filter = params.get("team_ids")
    if team_ids_filter:
        team_ids_list = team_ids_filter.split(",")
        profile_id_to_team = {pid: team for pid, team in profile_id_to_team.items() if team.get("_id") in team_ids_list}
        logging.info(f"Filtered to {len(profile_id_to_team)} Instagram profile(s)")

    # Filter by specific Instagram profile ID if specified
    profile_id_filter = params.get("profile-id") or params.get("profile_id")
    if profile_id_filter:
        profile_id_to_team = {pid: team for pid, team in profile_id_to_team.items() if str(pid) == str(profile_id_filter)}
        logging.info(f"Filtered to profile ID {profile_id_filter}: {len(profile_id_to_team)} profile(s) found")

    # when every team in scope is account_wide (wizard cohort),
    # skip building the legacy fb_app token map entirely. Wizard IG accounts
    # are resolved via _resolve_wizard_ig_credentials using
    # de_connection.oauth_token_enc; the legacy fb_app_map is only used as
    # a fallback for non-wizard IG accounts that need to walk through a
    # registered fb_app_user token. Mirrors the get_insights.py fix that
    # eliminates page-filtered fan-out for workspace-isolated runs.
    all_account_wide = bool(profile_id_to_team) and all(
        t.get("fb_import_mode") == "account_wide" for t in profile_id_to_team.values()
    )

    # Build mapping of FB App ESID → {app data, tokens}
    # Also build mappings from Meta Business Portfolio ESID and title → FB App ESID
    # This allows us to select the correct token directly from the IG account's fb_account_esid
    fb_app_map = {}
    fb_account_esid_to_app = {}  # fb_account_esid → app ESID (preferred, reliable)
    fb_account_title_to_app = {}  # fb_account_title → app ESID (fallback for unlinked accounts)

    if all_account_wide:
        logging.info(
            "skipping fb_app token map build — %d profile(s) all account_wide; "
            "legacy fb_app chain not needed (wizard tokens resolved per-account via "
            "_resolve_wizard_ig_credentials).",
            len(profile_id_to_team),
        )
        fb_apps_for_loop = []
    else:
        logging.info("Building FB App token mappings...")
        fb_apps_for_loop = fb_apps

    for fb_app_doc in fb_apps_for_loop:
        fb_app_esid = fb_app_doc["_id"]
        fb_app = fb_app_doc["_source"]
        fb_app_title = fb_app.get("fb_app_title", "Unknown")
        fb_app_id = fb_app.get("fb_app_id")
        fb_app_secret = decrypt(fb_app.get("fb_app_secret"))
        fb_account_title = fb_app.get("fb_account_title")
        fb_account_esid = fb_app.get("fb_account_esid")

        # Get ALL FB app users for this app
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
                "fb_account_title": fb_account_title,
                "fb_account_esid": fb_account_esid,
                "tokens": user_tokens,
            }
            # Only map portfolio → app for apps that have valid tokens
            if fb_account_esid:
                fb_account_esid_to_app[fb_account_esid] = fb_app_esid
            if fb_account_title:
                fb_account_title_to_app[fb_account_title] = fb_app_esid
            if fb_account_title:
                logging.info(
                    f"FB App '{fb_app_title}' (ESID: {fb_app_esid}) → Portfolio: '{fb_account_title}' (ESID: {fb_account_esid}): {len(user_tokens)} user token(s)"
                )
            else:
                logging.info(
                    f"FB App '{fb_app_title}' (ESID: {fb_app_esid}) [No portfolio linked]: {len(user_tokens)} user token(s)"
                )
        else:
            logging.warning(f"FB App '{fb_app_title}' (ESID: {fb_app_esid}): No user tokens found")

    # skip the "no fb_apps -> abort" check when every profile in
    # scope is account_wide. The legacy fb_app map is empty by design in
    # that branch; wizard tokens come from the per-account resolver below.
    if not fb_app_map and not all_account_wide:
        logging.error("No FB apps with valid user tokens found. Cannot proceed.")
        return

    if fb_app_map:
        logging.info(f"Total: {len(fb_app_map)} FB app(s) with tokens ready")
        logging.info(f"Portfolio→App mappings: {len(fb_account_esid_to_app)} by ESID, {len(fb_account_title_to_app)} by title")

    # Process each Instagram profile
    total_profiles = len(profile_id_to_team)
    processed_count = 0
    error_count = 0
    teams_with_data = set()

    for instagram_profile_id, team in profile_id_to_team.items():
        team_title = team.get("team_title", "Unknown")
        team_esid = team.get("_id")
        ig_account_esid = team.get("ig_account_esid")
        # bind this profile's team so recorded errors (code-10
        # not-business-account, etc.) carry durable per-team attribution.
        # The ambiguous-claimants record site runs at MAP-BUILD time, before
        # this loop, and stays deliberately unattributed (no single team).
        from lib.import_run_context import set_current_team

        set_current_team(team_esid)

        # per-team Instagram switch: an opted-out team's profile is
        # skipped before any Graph call (same shape as the pre-check
        # below). Shared cached resolver from the daily FB importer.
        from import_scripts.facebook.get_insights import _source_enabled_for_team

        if not _source_enabled_for_team(team_esid, "instagram"):
            logging.info(
                "instagram disabled for team %s — skipping IG profile %s.",
                team_esid,
                instagram_profile_id,
            )
            continue

        # over-cap (Condition B) billing pre-check per IG source. The
        # org rollup lists IG accounts under the Meta provider's ig_linked_assets
        # (id == IG account esid, provider_id == IG profile id), so match on either.
        # Subscription-inactive (Condition A) was already handled at the team filter.
        # Surfaces a classified error on the scheduled path (no-op on cron); fail-open.
        from lib.billing_entitlement import record_billing_skip, source_billing_block

        _ig_block = source_billing_block(
            team_esid=team_esid,
            asset_esid=ig_account_esid,
            provider_id=str(instagram_profile_id),
        )
        if _ig_block:
            logging.info(
                "billing pre-check: skipping IG profile %s for team %s (reason=%s).",
                instagram_profile_id,
                team_esid,
                _ig_block,
            )
            record_billing_skip(_ig_block, connector_id="meta_ads", detail=team_title)
            continue

        logging.info(
            f"Processing Instagram profile {instagram_profile_id} for team '{team_title}' ({processed_count + 1}/{total_profiles})"
        )

        # Get Instagram account to resolve Page → Account → App chain
        ig_username = team.get("ig_account_username", "unknown")

        ig_account_docs = get_ig_accounts(filters={"_id": ig_account_esid})
        if not ig_account_docs:
            logging.error(f"Instagram account ESID {ig_account_esid} not found. Skipping.")
            error_count += 1
            continue

        ig_account = ig_account_docs[0]["_source"]
        ig_fb_account_esid = ig_account.get("fb_account_esid")
        ig_fb_account_title = ig_account.get("fb_account_title")

        # Look up the numeric FB page ID via fb_page_esid — the fb_page_id field on the
        # IG account stores the display value (page title), not the numeric page ID.
        ig_fb_page_id = None
        ig_fb_page_esid = ig_account.get("fb_page_esid")
        if ig_fb_page_esid:
            fb_page_docs = get_fb_pages(filters={"_id": ig_fb_page_esid})
            if fb_page_docs:
                ig_fb_page_id = str(fb_page_docs[0]["_source"].get("fb_page_id", ""))
                logging.info(f"  Resolved FB page ESID {ig_fb_page_esid} → page ID {ig_fb_page_id}")
            else:
                logging.warning(f"  Could not find FB page for ESID {ig_fb_page_esid}")

        # follow-up — wizard-cohort IG accounts have no
        # de_asset_fb_pages row (the wizard never wrote one), so the
        # fb_page_esid lookup above misses. The bridge does
        # stamp the raw Meta page id on the IG asset doc as
        # ``linked_page_id``; fall back to that so page-token exchange
        # can still run for wizard accounts.
        if not ig_fb_page_id:
            linked_page_id = ig_account.get("linked_page_id")
            if linked_page_id:
                ig_fb_page_id = str(linked_page_id)
                logging.info(f"  Using  linked_page_id → page ID {ig_fb_page_id}")

        # Select appropriate FB App — prefer ESID match, fall back to title match
        if ig_fb_account_esid and ig_fb_account_esid in fb_account_esid_to_app:
            fb_app_esid = fb_account_esid_to_app[ig_fb_account_esid]
            fb_apps_to_try = [(fb_app_esid, fb_app_map[fb_app_esid])]
            logging.info(f"  Instagram @{ig_username} → Portfolio ESID {ig_fb_account_esid} → FB App ESID: {fb_app_esid}")
        elif ig_fb_account_title and ig_fb_account_title in fb_account_title_to_app:
            fb_app_esid = fb_account_title_to_app[ig_fb_account_title]
            fb_apps_to_try = [(fb_app_esid, fb_app_map[fb_app_esid])]
            logging.info(f"  Instagram @{ig_username} → Portfolio title '{ig_fb_account_title}' → FB App ESID: {fb_app_esid}")
        else:
            # No portfolio configured or not matched - try all apps (fallback)
            fb_apps_to_try = list(fb_app_map.items())
            if ig_fb_account_esid or ig_fb_account_title:
                logging.warning(
                    f"  Instagram @{ig_username} → Portfolio '{ig_fb_account_title}' (ESID: {ig_fb_account_esid}) has no matching FB App - trying all apps"
                )
            else:
                logging.warning(f"  Instagram @{ig_username} → No Meta Business Portfolio configured - trying all apps")

        # follow-up — wizard-cohort IG accounts carry
        # wizard_connection_esid on de_asset_ig_accounts (written by the
        # bridge). Their token lives on de_connection, not on
        # de_asset_fb_app_users, so the legacy fb_app loop above can't
        # see it. Resolve the wizard credential triple and PREPEND it
        # as a synthetic fb_apps_to_try entry so it's tried first; the
        # legacy entries remain as fallback in case the wizard token
        # was revoked between bridge time and importer run.
        wizard_connection_esid = ig_account.get("wizard_connection_esid")
        if wizard_connection_esid:
            wiz_app_id, wiz_app_secret, wiz_token = _resolve_wizard_ig_credentials(wizard_connection_esid)
            if wiz_app_id and wiz_app_secret and wiz_token:
                wizard_entry = (
                    f"wizard:{wizard_connection_esid}",
                    {
                        "app_id": wiz_app_id,
                        "app_secret": wiz_app_secret,
                        "app_title": f"Wizard ({wizard_connection_esid})",
                        "fb_account_title": None,
                        "fb_account_esid": None,
                        "tokens": [
                            {
                                "esid": f"wizard:{wizard_connection_esid}",
                                "title": f"wizard:{ig_account.get('wizard_principal') or ''}",
                                "role": "wizard",
                                "token": wiz_token,
                            }
                        ],
                    },
                )
                fb_apps_to_try = [wizard_entry] + fb_apps_to_try
                logging.info(
                    f"  Instagram @{ig_username} →  wizard token resolved via connection {wizard_connection_esid}; "
                    f"prepended to fb_apps_to_try"
                )

        # proactive rate-limit probe before ANY Graph call for this
        # profile, using the first candidate token (throttles are app/user-scoped,
        # so a hard-throttled app rejects this probe no matter which token would
        # later win the auth loop — the probe-rejected shape). In
        # backfill mode a throttle raises _RateLimitBailout: STOP the profile
        # loop so profiles already written keep their data and the send step
        # commits the partial run (mirrors check_and_pause_for_account /
        # check_and_pause_for_page). Daily/scheduled runs skip the probe.
        if fb_apps_to_try and fb_apps_to_try[0][1].get("tokens"):
            try:
                check_and_pause_for_ig_user(instagram_profile_id, fb_apps_to_try[0][1]["tokens"][0]["token"])
            except _RateLimitBailout as bail:
                logging.warning(
                    "backfill: rate-limit bail-out at IG profile %s (%d/%d): %s. "
                    "Keeping %d processed profile(s); stopping so partial data is committed.",
                    instagram_profile_id,
                    processed_count + 1,
                    total_profiles,
                    bail,
                    processed_count,
                )
                break

        # Try each app/token combination until one works
        api_initialized = False
        user_metadata = None
        last_failure_reason = None  # track why all tokens failed

        for fb_app_esid, fb_app_data in fb_apps_to_try:
            if api_initialized:
                break

            logging.info(f"  Trying FB App: {fb_app_data['app_title']} (ESID: {fb_app_esid})")

            for user_token_data in fb_app_data["tokens"]:
                try:
                    # Try to exchange user token for page access token.
                    # Page tokens have Instagram access even without instagram_basic on the user token.
                    token_to_use = user_token_data["token"]
                    token_type = "user"
                    if ig_fb_page_id:
                        page_data = get_page_access_token(ig_fb_page_id, user_token_data["token"])
                        if page_data and page_data.get("access_token"):
                            token_to_use = page_data["access_token"]
                            token_type = "page"
                            logging.info(f"    Exchanged for page access token (page {ig_fb_page_id})")
                        else:
                            logging.debug(f"    Could not get page token for page {ig_fb_page_id}, falling back to user token")

                    FacebookAdsApi.init(
                        fb_app_data["app_id"],
                        fb_app_data["app_secret"],
                        token_to_use,
                        api_version=FB_GRAPH_VERSION,
                    )
                    logging.info(f"    Using {token_type} token: {user_token_data['title']} (role: {user_token_data['role']})")

                    # Test the token by attempting to fetch metadata
                    test_metadata = fetch_instagram_user_metadata(instagram_profile_id)
                    if test_metadata not in ["not_found", "access_denied", None]:
                        # Token works!
                        api_initialized = True
                        current_token = token_to_use
                        user_metadata = test_metadata  # Save for later use
                        logging.info(f"    ✓ Token successfully authenticated for @{ig_username}")
                        break
                    else:
                        last_failure_reason = test_metadata
                        logging.debug(f"    Token {user_token_data['title']}: {test_metadata}, trying next token")

                except FacebookRequestError as e:
                    if e.api_error_code() == 100:
                        last_failure_reason = "not_found"
                        logging.debug(f"    Token {user_token_data['title']}: Error 100 (not accessible), trying next token")
                        continue
                    else:
                        logging.error(
                            f"    Token {user_token_data['title']}: API Error {e.api_error_code()}: {e.api_error_message()}"
                        )
                        continue
                except Exception as e:
                    logging.error(f"    Failed to initialize Facebook API with token {user_token_data['title']}: {e}")
                    continue

        if not api_initialized or not user_metadata:
            if last_failure_reason == "access_denied":
                logging.warning(
                    f"Instagram @{ig_username} (ID: {instagram_profile_id}) is not a Business/Creator account. "
                    f"Fix: In Instagram app → Settings & activity → Account type and tools → Switch to Professional Account → Business."
                )
            elif last_failure_reason == "not_found":
                logging.warning(
                    f"Instagram @{ig_username} (ID: {instagram_profile_id}) not found — stored ID is stale or invalid. "
                    f"Fix: Update the Instagram account ID in DEAM. Run: "
                    f"python3 /app/python/import.py instagram test_page_instagram_insights --fb_page_id=<LINKED_PAGE_ID>"
                )
            else:
                logging.error(f"Instagram @{ig_username} (ID: {instagram_profile_id}): no valid tokens found. Skipping.")
            error_count += 1
            continue

        # Rate limits: check_and_pause_for_account() only understands Facebook Ad
        # Accounts (act_ prefix), so IG probes via check_and_pause_for_ig_user
        # above — active in backfill mode only. Daily/scheduled runs
        # keep monitoring Instagram rate limits through normal API error responses.

        # user_metadata was already fetched during token testing above
        logging.info(f"Verified access to @{user_metadata.get('username')}")

        # window mode: one deduplicated-range call per profile,
        # write the window doc, and move on. The whole daily pipeline (user
        # insights, media list, JSON archive) is bypassed.
        if window_mode:
            reach_value = fetch_instagram_window_reach(instagram_profile_id, window_since, window_until, current_token)
            if isinstance(reach_value, int):
                window_doc = massage_instagram_window_data(user_metadata, team, window_since, window_until, reach_value)
                if send_instagram_window_doc(window_doc, team):
                    processed_count += 1
                else:
                    error_count += 1
            else:
                # range_too_long / error — logged inside the fetch; no doc.
                error_count += 1
            continue

        # Fetch Instagram user insights
        user_insights = fetch_instagram_user_insights(instagram_profile_id, date, current_token)
        if user_insights == "rate_limited":
            # backfill-mode throttle mid-profile: stop the loop so the
            # profiles already written keep their data (the send step commits them).
            logging.warning(
                "backfill: rate-limit bail-out at IG profile %s (%d/%d) during "
                "user insights. Keeping %d processed profile(s); stopping so partial "
                "data is committed.",
                instagram_profile_id,
                processed_count + 1,
                total_profiles,
                processed_count,
            )
            break
        if user_insights in ["not_business_account", "invalid_params"]:
            logging.warning(f"Skipping insights for profile {instagram_profile_id}: {user_insights}")
            # only not_business_account (Graph code 10) is user-fixable:
            # the user must convert to a Business/Creator account and grant
            # instagram_manage_insights. invalid_params (code 100) is OUR bad
            # request, not something the user can correct — don't surface it.
            if user_insights == "not_business_account":
                from lib.import_run_context import record_user_error

                record_user_error(
                    "Instagram account lacks insights permissions (code 10): "
                    "ensure it is a Business/Creator account with instagram_manage_insights",
                    connector_id="meta_ads",
                    detail=(f"@{ig_username}" if ig_username and ig_username != "unknown" else team_title),
                )
            error_count += 1
            continue
        elif not user_insights:
            logging.warning(f"No user insights available for {instagram_profile_id} on {date}")
            user_insights = {}

        logging.info(f"Fetched user insights: {len(user_insights)} metrics")

        # Fetch Instagram media list for the date
        media_list = fetch_instagram_media_list(instagram_profile_id, date)

        # Fetch insights for each media item
        for media_item in media_list:
            media_id = media_item.get("id")
            media_type = media_item.get("media_type")

            media_insights = fetch_instagram_media_insights(media_id, media_type)
            media_item["insights"] = media_insights

        logging.info(f"Fetched insights for {len(media_list)} media items")

        # Combine all data
        combined_data = {
            **user_metadata,
            "insights": user_insights,
            "media": media_list,
        }

        # Transform data for Elasticsearch
        transformed_data = massage_instagram_data(combined_data, team, date)

        # Save to JSON file
        file_path = f"/app/data/instagram/ig_insights/{fb_app_esid}/{team_esid}/{date}_{instagram_profile_id}.json"
        imported_file_path = (
            f"/app/data/instagram/ig_insights_imported/{fb_app_esid}/{team_esid}/{date}_{instagram_profile_id}.json"
        )

        # Check if already imported. an archived file is NOT proof the
        # doc is in ES (the send→archive steps aren't gated on the send
        # succeeding, so an interrupted/partial send can strand a file
        # archived-but-unindexed). Skip ONLY when the file is archived AND the
        # ES doc exists; otherwise re-write so the next send recovers it. The
        # _id matches the sender's _build_doc_id: {ig_user_id}_{kibana_date} in
        # {team_esid}__ig_insights. ES check fires only on the archived branch.
        if os.path.exists(imported_file_path):
            es_index = build_data_index_name(team_esid, "ig_insights")
            es_doc_id = f"{instagram_profile_id}_{date}"
            if indexed_doc_present(es_index, doc_id=es_doc_id):
                logging.info(f"Profile {instagram_profile_id} for {date} already imported and indexed. Skipping.")
                processed_count += 1
                continue
            logging.warning(
                "recovery: IG profile %s for %s is archived but ES doc %s "
                "is MISSING from %s — re-writing for re-send.",
                instagram_profile_id,
                date,
                es_doc_id,
                es_index,
            )

        # Save data
        try:
            save_data_to_file(transformed_data, file_path)
            logging.info(f"Saved Instagram insights to {file_path}")
            processed_count += 1
            teams_with_data.add(team_esid)
        except Exception as e:
            logging.error(f"Failed to save data to file: {e}")
            error_count += 1

    logging.info(
        f"Instagram insights import completed. Processed: {processed_count}, Errors: {error_count}, Total: {total_profiles}"
    )

    # Delete any stale markers for teams that got real data this run, then
    # write fresh markers for teams that didn't.
    delete_stale_markers_for_date("*_ig_insights", date, teams_with_data)
    write_no_data_markers(date, teams_with_data, "_ig_insights", team_mapping=profile_id_to_team)
