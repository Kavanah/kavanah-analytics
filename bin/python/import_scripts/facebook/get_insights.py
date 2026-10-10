import sys
import os
import json
import logging
import lib.logging
import urllib.parse
import time  # (ADDED) We need 'time' for sleep
from datetime import datetime, timedelta, timezone
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.page import Page
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.campaign import Campaign
from import_scripts.facebook.lib.assets import *
from import_scripts.facebook.lib.fb_helpers import (
    account_wide_owner_ok,
    normalize_ad_account_id,
    select_team_ad_account_links,
    strip_ad_account_prefix,
)
from import_scripts.facebook.lib.team_helpers import _resolve_team_country
from elasticsearch.exceptions import NotFoundError

from lib.encryption import decrypt
from lib.es_helpers import (
    build_data_index_name,
    get_document_by_id,
    get_elasticsearch_client,
    indexed_doc_present,
    sanitize_team_title,
)
from lib.fb_constants import FB_GRAPH_URL as _FB_GRAPH_BASE
from lib.fb_constants import FB_GRAPH_HTTP_TIMEOUT, FB_GRAPH_SDK_TIMEOUT
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date
from lib.geocoding import geocode_all_targeting_levels
from migration.catalog_loader import disabled_es_fields, request_fields, request_fields_for_tenant

RETRY_DELAYS = [60, 120, 240]  # seconds for exponential backoff (3 retries)


class _RateLimitBailout(Exception):
    """raised (backfill mode only) when a Meta rate limit is hit so the
    ranged pull STOPS gracefully instead of sleeping/retrying.

    The daily/scheduled path never sets ``_backfill_bailout`` and so keeps the
    existing sleep-and-retry resilience (``check_and_pause_for_account`` /
    ``RETRY_DELAYS``). Only a user-triggered backfill opts in: on the sentinel the
    account loop breaks, main() falls through to its normal end, the child exits 0,
    and the JSON already written up to the limit is committed by the downstream
    send/archive steps (writes are per-ad files, not a two-phase commit). This is
    the ticket's "keep what was imported, report what remains" behavior.
    """


# module-global backfill-mode flag (mirrors ``_log_usage``). Set once
# in main() from the ``--backfill_bailout`` param; read by the rate-limit sites so
# they bail out instead of sleeping. Default False = daily/scheduled behavior.
_backfill_bailout = False


# Meta ads-management rate-limit signals. A throttled account can fail
# the request OUTRIGHT (400) rather than returning near-limit usage headers, so
# the bail-out must recognize the error itself, not only the header path. Codes:
# 4=app-level, 17=user-level, 613=calls-per-hour, 80000-80014=business-use-case
# throttles (80004 = ad-account, the code in the observed failure). Subcodes:
# 2446079 (too many calls to this ad account), 1504022, 1487742.
# https://developers.facebook.com/docs/graph-api/overview/rate-limiting#ads-management
_FB_RATE_LIMIT_ERROR_CODES = {4, 17, 613} | set(range(80000, 80015))
_FB_RATE_LIMIT_ERROR_SUBCODES = {2446079, 1504022, 1487742}


def _is_rate_limit_error(e):
    """True when a ``FacebookRequestError`` is a rate-limit/throttle signal.

    Reads the SDK accessors defensively — not every error exposes them.
    """
    try:
        code = e.api_error_code()
    except Exception:  # noqa: BLE001 — defensive: accessor not guaranteed
        code = None
    try:
        subcode = e.api_error_subcode()
    except Exception:  # noqa: BLE001 — defensive: accessor not guaranteed
        subcode = None
    return code in _FB_RATE_LIMIT_ERROR_CODES or subcode in _FB_RATE_LIMIT_ERROR_SUBCODES


# (PHASE-106): the per-ad insights request list is sourced from the field
# catalog (catalog/fb_ads.json) instead of a hardcoded literal. This is the
# distinct set of `api_field`s every requested `insight` entry whose
# `requested_in` includes "daily". Computed once at import (the catalog is the
# single source of truth that also generates the ES mapping). FB ignores field
# order, so the sorted list the loader returns is equivalent to the old literal.
_DAILY_INSIGHT_FIELDS = request_fields("fb_ads", "daily")

# (PHASE-107) — per-tenant field picker. A workspace owner's selections
# (de_asset_field_catalog, read via FieldCatalogService) narrow/extend the
# request list per team; _DAILY_INSIGHT_FIELDS remains the default for tenants
# with no selection doc AND the fallback on any store failure — the picker is
# config, never an import dependency. Overrides are read once per team per run.
_TEAM_OVERRIDES_CACHE = {}
_TEAM_DISABLED_CACHE = {}


def _overrides_for_team(team_esid):
    """The team's workspace field-picker overrides; {} (default set) on any miss."""
    if not team_esid or team_esid == "unknown":
        return {}
    if team_esid not in _TEAM_OVERRIDES_CACHE:
        try:
            from asset_management.web.services.field_catalog_service import FieldCatalogService

            _TEAM_OVERRIDES_CACHE[team_esid] = FieldCatalogService.get_overrides_for_team(team_esid) or {}
        except Exception as e:  # noqa: BLE001 — fail-soft: imports never break on the picker
            logging.warning("field-catalog lookup failed for team %s (%s); using default field set.", team_esid, e)
            _TEAM_OVERRIDES_CACHE[team_esid] = {}
    return _TEAM_OVERRIDES_CACHE[team_esid]


def _daily_fields_for_teams(team_esids):
    """UNION of the per-tenant daily request lists across ``team_esids``.

    An ad account shared by teams in different workspaces requests the union of
    their enabled sets (a superset is safe: the per-tenant doc filter below and
    each team's own index mapping bound what is stored). Empty/unresolvable
    input degrades to the default list.
    """
    esids = [t for t in (team_esids or []) if t and t != "unknown"]
    if not esids:
        return _DAILY_INSIGHT_FIELDS
    fields = set()
    for team_esid in esids:
        fields.update(request_fields_for_tenant("fb_ads", "daily", _overrides_for_team(team_esid)))
    return sorted(fields) if fields else _DAILY_INSIGHT_FIELDS


def _disabled_fields_for_team(team_esid):
    """ES doc keys this team's workspace disabled."""
    if not team_esid or team_esid == "unknown":
        return set()
    if team_esid not in _TEAM_DISABLED_CACHE:
        _TEAM_DISABLED_CACHE[team_esid] = disabled_es_fields("fb_ads", _overrides_for_team(team_esid))
    return _TEAM_DISABLED_CACHE[team_esid]


# per-team source on/off switches. Shared by all three Meta importers
# (get_insights_window and the page/IG importers call this via _daily / a
# direct import): team override → workspace default → ON. ON on any failure —
# the toggle is config, never an import dependency.
_SOURCE_ENABLED_CACHE = {}


def _source_enabled_for_team(team_esid, source):
    """Is ``source`` (fb_ads/fb_pages/instagram) on for this team? Cached per
    (team, source) per run; True on unknown team or any store failure."""
    if not team_esid or team_esid == "unknown":
        return True
    key = (team_esid, source)
    if key not in _SOURCE_ENABLED_CACHE:
        try:
            from asset_management.web.services.field_catalog_service import FieldCatalogService

            _SOURCE_ENABLED_CACHE[key] = bool(FieldCatalogService.source_enabled_for_team(team_esid, source))
        except Exception as e:  # noqa: BLE001 — fail-soft: imports never break on the toggle
            logging.warning("source-toggle lookup failed for team %s/%s (%s); importing.", team_esid, source, e)
            _SOURCE_ENABLED_CACHE[key] = True
    return _SOURCE_ENABLED_CACHE[key]


FB_GRAPH_URL = f"{_FB_GRAPH_BASE}/"
FB_BATCH_SIZE = 50  # Facebook max requests per batch call

# max span (in days) of ONE ranged account-level insights call. A
# level=ad + time_increment=1 pull returns ads × platforms × days rows; Meta's
# sync insights endpoint can refuse very large result sets ("please reduce the
# amount of data you're asking for"), so long backfills are chunked into slices
# of this many days. Each slice is still one call per account — a 90-day
# backfill costs 3 calls per account, not 90 subprocesses × 90 calls.
_RANGED_INSIGHTS_CHUNK_DAYS = 30

# Module-level flag for usage logging
_log_usage = False
_usage_logger = None

# FB OAuth codes that mean "the Facebook login is dead, RECONNECT"
# (not a transient/permission/rate-limit error). 190 = access-token error
# (expired session / invalid / revoked). A dead legacy de_asset_fb_apps token
# fails every account under that app identically, so we surface it ONCE per
# app (not once per account — that would spam a line per ad account).
_FB_RECONNECT_ERROR_CODES = {190}
# follow-up — the legacy FB app whose token is currently in use, so the
# surfaced error can NAME the app the operator must reconnect (the original
# message said only "this account", forcing the operator to guess which login
# died). Set by set_current_fb_app at the per-app loop; (None, None) on the cron
# path / before any app is initialised.
_current_fb_app_title: str | None = None
_current_fb_app_id: str | None = None
# Apps whose expired-token error we've already surfaced this run — dedups per
# app so a multi-app run with two dead logins still surfaces both, once each.
_expired_apps_recorded: set[str] = set()
# fb_app_id -> fb_app_title cache for the resolve-from-id fallback
# below. Populated lazily, only when a title is actually needed to surface an
# error, so the healthy path never touches ES.
_fb_app_title_by_id: dict[str, str] = {}
# follow-up — the current import MODE + the de_connection in use.
# A code-190 means different things per mode:
#   * page_filtered → the legacy de_asset_fb_apps APP token died → reconnect the
#     Facebook app (connection_expired copy, names the app).
#   * account_wide → the per-tenant de_connection token died (the shared wizard
#     app is fine) → reconnect THIS connection under Data Connectors
#     (connection_revoked copy, names the connection). Naming the shared app here
#     is actively misleading — every account_wide team uses the SAME healthy app.
_current_import_mode: str = "page_filtered"
_current_connection_esid: str | None = None
# connection_esid -> human label cache (lazy; only on the surfacing path).
_connection_label_by_esid: dict[str, str] = {}


def set_current_fb_app(fb_app_title, fb_app_id):
    """follow-up — record which legacy FB app is in use so an expired-
    token error can name it. Called alongside ``init_facebook_api`` at the per-
    app loop, where the human-readable ``fb_app_title`` is in scope (the SDK
    init only receives the numeric app id). Stringifies + trims defensively;
    blanks are tolerated (the descriptor resolves the title from the id, or
    degrades, but never raises)."""
    global _current_fb_app_title, _current_fb_app_id
    _current_fb_app_title = (str(fb_app_title).strip() if fb_app_title else "") or None
    _current_fb_app_id = (str(fb_app_id).strip() if fb_app_id else "") or None


def set_current_fb_import_context(import_mode, connection_esid):
    """follow-up — record the import mode + the de_connection in use, so
    a code-190 surfaces against the RIGHT credential: the legacy app
    (page_filtered) or the per-tenant connection (account_wide). Defensive; never
    raises."""
    global _current_import_mode, _current_connection_esid
    _current_import_mode = (str(import_mode).strip() if import_mode else "") or "page_filtered"
    _current_connection_esid = (str(connection_esid).strip() if connection_esid else "") or None


def _page_details_cache_key(actor_id) -> str:
    """page-details cache key scoped to the CREDENTIAL in use.

    A shared ad account is walked once per team, each pass under that team's own
    token (account_wide) or app (page_filtered). Page reachability is a property
    of the token, not the page: on 2026-09-03 a teammate's token could not read a team's
    page, his pass cached ``access_denied`` under the bare page id, and that team's own
    pass (a teammate's token, which CAN read it) inherited the verdict and dropped its
    ads. Keying by credential keeps one team's denial from blinding another.
    """
    credential = _current_connection_esid or _current_fb_app_id or _current_import_mode or "legacy"
    return f"{credential}:{actor_id}"


def _should_record_page_denial(owner_team) -> bool:
    """record a page access-denial only for the team whose data it is.

    In a shared account every sibling pass meets the owner team's ads too; a
    denial there is expected (Meta rule 5: access is not ownership) and costs the
    sibling nothing, because those ads are the owner's. Recording it stamped one
    owner-page failure onto six teams in the Backfill report. Record when the
    current pass IS the owner (its data is at stake) or when ownership/context is
    unknown (legacy entries, no ambient team) — never suppress on a guess.
    """
    from lib.import_run_context import get_current_team

    current = get_current_team()
    owner = str((owner_team or {}).get("_id") or "").strip()
    if not current or not owner:
        return True
    return owner == current


def _resolve_connection_label(connection_esid: str) -> str:
    """human label for a de_connection (what the operator sees under
    Data Connectors). Falls back to principal, then the esid. Cached per run;
    fail-soft to '' so surfacing never breaks."""
    if not connection_esid:
        return ""
    if connection_esid in _connection_label_by_esid:
        return _connection_label_by_esid[connection_esid]
    label = ""
    try:
        es = get_elasticsearch_client()
        if es is not None:
            doc = es.get(index="de_connection", id=connection_esid)
            src = (doc or {}).get("_source") or {}
            label = str(src.get("label") or src.get("principal") or "").strip()
    except Exception as exc:  # noqa: BLE001 — naming an error must never break the import
        logging.warning("de_connection label lookup for %s failed: %s", connection_esid, exc)
    _connection_label_by_esid[connection_esid] = label
    return label


def _resolve_fb_app_title(app_id: str) -> str:
    """look up an FB app's title from its id in de_asset_fb_apps.

    In ``account_wide`` mode the caller's ``fb_app_id`` is reassigned to the
    wizard-resolved Meta app, which no longer matches the ``fb_app`` loop doc the
    title was read from — so ``set_current_fb_app`` can be handed an id with a
    blank/mismatched title. Resolving the title from the SAME id we are about to
    report guarantees the two describe the same app. Cached per run; fail-soft to
    "" so surfacing never breaks on an ES hiccup."""
    if not app_id:
        return ""
    if app_id in _fb_app_title_by_id:
        return _fb_app_title_by_id[app_id]
    title = ""
    try:
        from import_scripts.facebook.lib.assets import get_fb_apps

        docs = get_fb_apps(filters={"fb_app_id": app_id}) or []
        for doc in docs:
            t = (doc.get("_source") or {}).get("fb_app_title")
            if t:
                title = str(t).strip()
                break
    except Exception as exc:  # noqa: BLE001 — naming an error must never break the import
        logging.warning("fb_app_title lookup for app %s failed: %s", app_id, exc)
    _fb_app_title_by_id[app_id] = title
    return title


def _current_fb_app_detail() -> str:
    """Build the operator-facing name of the FB app whose login died:
    ``'<title> (app <id>)'``, degrading to whichever part is known
    (``'<title>'`` / ``'app <id>'``) and ``''`` when neither is set. When the
    title is absent but the id is known, resolves the title from the id
     so account_wide mode still names the app, not just its number."""
    title = _current_fb_app_title or ""
    app_id = _current_fb_app_id or ""
    if app_id and not title:
        title = _resolve_fb_app_title(app_id)
    if title and app_id:
        return f"{title} (app {app_id})"
    if app_id:
        return f"app {app_id}"
    return title


def _maybe_record_expired_fb_token(e):
    """surface a reconnect-class FB OAuth error (code 190, expired
    session) to the Report Scheduler, once per affected credential per run.

    The legacy fb_app token-refresh job ([fb-check-token]) logs the expiry but
    can't surface it (separate subprocess, no progress_run_id). The dead token
    then fails the insights pull here with FacebookRequestError code 190.

     follow-up — what a 190 MEANS, and therefore what we tell the
    operator, depends on the import mode (set via set_current_fb_import_context):

    * ``page_filtered`` → the legacy ``de_asset_fb_apps`` APP token died. Record a
      string the classifier maps to ``connection_expired``, naming the app
      : "reconnect Facebook".
    * ``account_wide`` → the per-tenant ``de_connection`` token died; the shared
      wizard Meta app is healthy and is the SAME for every account_wide team, so
      naming it is misleading. Record a string the classifier maps to
      ``connection_revoked``, naming the CONNECTION: "reconnect under Data
      Connectors".

    Returns True when this looks like a reconnect-class error (so callers can
    branch), False otherwise. De-duped per credential via
    ``_expired_apps_recorded`` (record_user_error also dedups by category+detail)
    — a no-op on the cron path (no run_id bound). Never raises.
    """
    try:
        code = e.api_error_code()
    except Exception:  # noqa: BLE001 — defensive: not all errors expose the accessor
        code = None
    if code not in _FB_RECONNECT_ERROR_CODES:
        return False

    if _current_import_mode == "account_wide":
        # The dead credential is the de_connection token — point at Data Connectors.
        # NOTE: the raw wording is what drives classification — it must contain a
        # ``connection_revoked`` token ("revoked"/"disconnected") and must NOT
        # contain a ``connection_expired`` token ("code 190"/"login expired"),
        # which precedes connection_revoked in the classifier table and would
        # otherwise grab this. Mirrors the existing revoked-path wording in
        # _resolve_account_wide_credentials.
        label = _resolve_connection_label(_current_connection_esid or "")
        detail = label or (f"connection {_current_connection_esid}" if _current_connection_esid else "")
        raw = "Facebook connection revoked or disconnected — reconnect under Data Connectors to resume imports."
    else:
        # Legacy page-filtered path — the de_asset_fb_apps APP token died.
        detail = _current_fb_app_detail()
        raw = "Facebook login expired (OAuth code 190) — reconnect Facebook to resume imports."

    # Dedup key: scope by mode so an account_wide connection and a page_filtered
    # app with the same display text can't collide; sentinel when nothing's known.
    dedup_key = f"{_current_import_mode}:{detail}" if detail else f"{_current_import_mode}:<unknown>"
    if dedup_key not in _expired_apps_recorded:
        _expired_apps_recorded.add(dedup_key)
        try:
            from lib.import_run_context import record_user_error

            record_user_error(raw, connector_id="meta_ads", detail=detail or None)
        except Exception as rec_err:  # noqa: BLE001 — surfacing must never break the import
            logging.warning("failed to record expired-token error: %s", rec_err)
    return True


_current_access_token = None  # stored by init_facebook_api for use in batch requests


def setup_usage_logging():
    """Setup a separate logger for rate limit usage metrics."""
    global _usage_logger
    _usage_logger = logging.getLogger("fb_usage")
    _usage_logger.setLevel(logging.INFO)

    # Create handler for usage log file (same directory as import logs)
    current_date = datetime.now().strftime("%Y%m%d")
    log_file = f"/app/data/logs/fb_usage_{current_date}.log"
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    _usage_logger.addHandler(handler)
    _usage_logger.propagate = False  # Don't send to root logger


##############################################################
#   Makes a quick raw API call to retrieve headers for the
#   specified ad account, then sleeps if usage is near limit.
##############################################################
def check_and_pause_for_account(account_id):
    """
    Makes a lightweight API call to the given ad account and checks
    the returned headers for usage/quota. If near or over threshold,
    sleeps to avoid rate limit errors.
    """
    try:
        # Normalize account_id to ensure 'act_' prefix
        normalized_id = normalize_ad_account_id(account_id)
        path_tuple = (normalized_id,)

        # Make a raw call to get minimal info (fields=id).
        api = FacebookAdsApi.get_default_api()
        response = api.call("GET", path_tuple, params={"fields": "id"})  # e.g. ('act_1234567',)

        if not response:
            return

        # We get the raw headers from the response
        headers = response.headers()
        if not headers:
            return  # no headers to parse

        # Convert to lowercase keys for easy matching
        lowered_headers = {k.lower(): v for k, v in headers.items()}

        # We check for these usage headers
        usage_keys = ["x-business-use-case-usage", "x-ad-account-usage", "x-app-usage"]

        for usage_key in usage_keys:
            usage_data = lowered_headers.get(usage_key, None)
            if usage_data:
                # Log usage data if logging is enabled
                if _log_usage and _usage_logger:
                    _usage_logger.info(f"account={account_id} header={usage_key} data={usage_data}")

                try:
                    usage = json.loads(usage_data)

                    if isinstance(usage, dict):
                        # usage can have nested structures
                        for _, usage_list in usage.items():
                            if isinstance(usage_list, list):
                                for usage_obj in usage_list:
                                    call_count = usage_obj.get("call_count", 0)
                                    total_cputime = usage_obj.get("total_cputime", 0)
                                    total_time = usage_obj.get("total_time", 0)
                                    estimated_time = usage_obj.get("estimated_time_to_regain_access", 0)

                                    # If usage is near 100%, we sleep
                                    if call_count >= 80 or total_cputime >= 80 or total_time >= 80:
                                        # a backfill bails out at the
                                        # near-limit line instead of sleeping 30 min:
                                        # the run stops, keeps what's written, and the
                                        # user re-runs to continue. Daily/scheduled
                                        # runs (flag off) keep the sleep.
                                        if _backfill_bailout:
                                            logging.warning(
                                                "backfill: AdAccount(%s) near rate limit "
                                                "(call=%s cpu=%s time=%s); bailing out to keep partial progress.",
                                                account_id,
                                                call_count,
                                                total_cputime,
                                                total_time,
                                            )
                                            raise _RateLimitBailout(f"near rate limit for {account_id}")
                                        logging.warning(
                                            f"check_and_pause_for_account: AdAccount({account_id}) near limit. Sleeping 30 minutes."
                                        )
                                        time.sleep(1800)
                                    elif estimated_time > 0:
                                        if _backfill_bailout:
                                            logging.warning(
                                                "backfill: AdAccount(%s) rate limited "
                                                "(regain in %s min); bailing out to keep partial progress.",
                                                account_id,
                                                estimated_time,
                                            )
                                            raise _RateLimitBailout(f"rate limited for {account_id}")
                                        logging.warning(
                                            f"check_and_pause_for_account: estimated_time_to_regain_access={estimated_time} minutes."
                                        )
                                        time.sleep(estimated_time * 60)
                except (ValueError, TypeError) as parse_err:
                    logging.warning(f"Couldn't parse usage data for {account_id}: {parse_err}")
    except FacebookRequestError as e:
        # a throttled account can reject the usage-check probe itself
        # (400 / code 80004), so the near-limit HEADER path above never runs. In
        # backfill mode that must still bail out instead of swallowing the error
        # and marching on to hammer an already-limited account. Daily/scheduled
        # runs (flag off) keep the old log-and-continue behavior.
        if _backfill_bailout and _is_rate_limit_error(e):
            logging.warning(
                "backfill: usage-check probe for AdAccount(%s) was itself "
                "rate limited (%s); bailing out to keep partial progress.",
                account_id,
                e,
            )
            raise _RateLimitBailout(f"usage-check rate limited for {account_id}") from e
        # Not fatal. We'll just log it and move on.
        logging.warning(f"Usage check call failed for {account_id}: {e}")


# Initialize the Facebook API
def init_facebook_api(app_id, app_secret, app_token):
    global _current_access_token
    # bound the SDK's HTTP so a wedged Graph socket can't block the
    # importer past the read timeout. Without this the on-demand backfill could
    # sleep in recv() indefinitely and never reach the cancellation check, so
    # the dashboard Cancel button was a silent no-op.
    FacebookAdsApi.init(app_id, app_secret, app_token, timeout=FB_GRAPH_SDK_TIMEOUT)
    _current_access_token = app_token


def batch_fb_metadata(requests_list, access_token):
    """
    Fetch metadata for multiple Facebook objects via the Graph API Batch endpoint.

    Sends groups of up to FB_BATCH_SIZE requests in a single POST, dramatically
    reducing round trips compared to individual SDK calls per object.

    Args:
        requests_list: list of (object_id, fields_string) tuples
        access_token: Facebook access token

    Returns:
        dict mapping str(object_id) -> response body dict
        Objects returning non-200 responses are silently omitted.
    """
    import requests as req_lib

    results = {}
    for chunk_start in range(0, len(requests_list), FB_BATCH_SIZE):
        chunk = requests_list[chunk_start : chunk_start + FB_BATCH_SIZE]
        batch_items = [{"method": "GET", "relative_url": f"{obj_id}?fields={fields}"} for obj_id, fields in chunk]
        payload = {"access_token": access_token, "batch": json.dumps(batch_items)}

        try:
            resp = req_lib.post(FB_GRAPH_URL, data=payload, timeout=FB_GRAPH_HTTP_TIMEOUT)
            resp.raise_for_status()
            batch_response = resp.json()
        except req_lib.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                raise  # propagate to caller for backoff
            logging.warning(f"batch_fb_metadata: request failed (chunk +{chunk_start}): {e}")
            continue
        except Exception as e:
            logging.warning(f"batch_fb_metadata: exception (chunk +{chunk_start}): {e}")
            continue

        for i, item in enumerate(batch_response):
            obj_id = str(chunk[i][0])
            if item.get("code") != 200:
                logging.debug(f"batch_fb_metadata: {obj_id} returned HTTP {item.get('code')} — skipping")
                continue
            try:
                body = json.loads(item.get("body", "{}"))
                results[obj_id] = body
            except (json.JSONDecodeError, TypeError):
                logging.debug(f"batch_fb_metadata: could not parse body for {obj_id}")

    return results


# Helper function to parse IDs
def parse_ids(ids):
    if ids is False:
        return False
    elif "," in ids:
        return ids.split(",")
    else:
        return [ids]


def _team_owner_import_blocked(team_src):
    """Phase 70 — entitlement check for the cron team sweep.

    Resolve a team doc to its owning principal (via the team's
    ``connection_esid`` → ``de_connection.principal``) and ask
    ``SubscriptionStateService.is_import_blocked``. Returns True ONLY when
    the owner is *definitively* import-blocked (lapsed or canceled).

    FAILS OPEN: a team with no connection_esid, an unresolvable principal,
    a missing service, or any error returns False (NOT blocked) — a
    transient lookup failure must never silently stop a paying team's
    nightly pull. The on-demand path already gates per-user (
    Phase 68); this extends the same entitlement to the 07:05 cron sweep.
    """
    connection_esid = (team_src or {}).get("connection_esid")
    if not connection_esid:
        return False  # legacy/operator team with no connection → can't resolve an owner; keep it
    try:
        es = get_elasticsearch_client()
        if es is None:
            return False
        conn = es.get(index="de_connection", id=connection_esid, ignore=[404])
        principal = ((conn or {}).get("_source") or {}).get("principal")
        if not principal:
            return False
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )

        return SubscriptionStateService.is_import_blocked(principal)
    except Exception as exc:  # noqa: BLE001 — boundary; fail OPEN
        logging.warning(
            "cron entitlement: owner check failed for connection_esid=%s: %s — keeping team",
            connection_esid,
            exc,
        )
        return False


def _filter_entitled_teams(teams_docs):
    """Drop teams under an inactive subscription (Phase 70;
    Condition A) and surface each as a classified error on the scheduled path.

    Delegates to the shared ``lib.billing_entitlement.filter_entitled_teams`` so
    FB uses the SAME billing verdict + error-surfacing as every other connector.
    FB's per-source over-cap check (Condition B) runs at the per-ad-account loop,
    where the ad-account id is in scope — the team filter only resolves the
    no-subscription condition for FB (its sources match by asset id, not team).
    """
    from lib.billing_entitlement import filter_entitled_teams as _shared_filter

    return _shared_filter(teams_docs, connector_id="meta_ads")


def get_all_teams_by_page_id_multi(team_esids=None):
    """build the page_id -> [team,...] MULTImap.

    A Facebook page can legitimately be claimed by more than one team's
    ``team_facebook_page_id`` (two teams both claim the same page).
    The old single-winner dict silently gave every shared page to whichever
    team ES listed last, which could reassign an account-wide team's entire
    ad spend to the other claimant. The multimap keeps every claimant so the
    per-ad resolver (:func:`_resolve_team_for_page_ad`) can pick the owner
    per ad; shared pages are surfaced with a loud warning here.

    Args:
        team_esids: Optional iterable of team _ids to restrict the query to.
            When provided, only those teams' page mappings are loaded —
            the ES query carries the _id filter so we don't load every
            ds_facebook team and then discard non-matches in Python.
            .

    Returns: dict mapping page_id (string) to a list of team documents in
        ES result order (last = the earlier dict winner).
    """
    lib.logging.define("fb-get-insights")
    # Only get teams with ds_facebook enabled. When a team_esids scope is
    # supplied, narrow the query to those teams.
    filters = {"ds_facebook": "true"}
    if team_esids:
        filters["_id"] = list(team_esids)
    teams_docs = get_fb_teams(filters=filters)

    # Phase 70 — drop teams whose owner is import-blocked
    # (lapsed/canceled) so the 07:05 cron honors the same entitlement as
    # the on-demand path. Fail-open per team.
    teams_docs = _filter_entitled_teams(teams_docs)

    logging.info(f"Found {len(teams_docs)} active teams")

    page_id_to_teams = {}
    for team_doc in teams_docs:
        team = team_doc["_source"]
        team["_id"] = team_doc["_id"]
        team_title = team.get("team_title", "Unknown")
        page_ids = team.get("team_facebook_page_id", [])

        if page_ids:
            page_count = len(page_ids) if isinstance(page_ids, list) else 1
            logging.info(f"Team '{team_title}' (esid={team.get('_id')}): {page_count} page(s) - {page_ids}")
        else:
            logging.debug(f"Team '{team_title}': No page IDs configured")

        if not isinstance(page_ids, list):
            page_ids = [page_ids] if page_ids else []
        for page_id in page_ids:
            page_id_to_teams.setdefault(str(page_id), []).append(team)

    for page_id, teams in page_id_to_teams.items():
        if len(teams) > 1:
            logging.warning(
                "Facebook page %s is claimed by %d teams %s — per-ad "
                "tie-break applies (the account-wide owner of the ad's account "
                "keeps its own account's ads; otherwise the page-filtered "
                "claimant wins).",
                page_id,
                len(teams),
                sorted(t.get("team_title", "?") for t in teams),
            )

    logging.info(f"Loaded {len(page_id_to_teams)} page ID to team mappings: {list(page_id_to_teams.keys())}")
    return page_id_to_teams


def get_all_teams_by_page_id(team_esids=None):
    """Single-winner page_id -> team view (earlier shape).

    Thin wrapper over :func:`get_all_teams_by_page_id_multi` for callers that
    need ONE team per page (no_data markers, the known-accounts pre-filter,
    scripts/backfill_no_data_markers.py). Keeps the historical
    last-claimant-wins semantics; per-ad attribution must NOT use this — it
    goes through :func:`_resolve_team_for_page_ad` on the multimap instead.

    Returns: dict mapping page_id (string) to team document
    """
    return {pid: teams[-1] for pid, teams in get_all_teams_by_page_id_multi(team_esids=team_esids).items()}


def _resolve_team_for_page_ad(page_id, bare_account, page_id_to_teams, ad_account_id_to_team):
    """resolve the owning team for an ad delivering from ``page_id``.

    Precedence, decided PER AD because the winner depends on the ad's account:

    1. Sole claimant — exactly the earlier behavior.
    2. Shared page: the claimant that account-wide-claims THIS ad's account.
       An account-wide team owns every ad in its own account (the documented
        semantics) — a foreign team's page claim must not steal them.
    3. Shared page in any other account: the sole page-filtered claimant.
       An account-wide team's page list exists for Echo referrer matching;
       its ads arrive via its account claim, not via pages.
    4. Deterministic last resort (several page-filtered claimants): lowest
       team ``_id``, with a warning — the old dict was ES-order roulette.

    Returns the team document, or ``None`` when no team claims the page
    (the caller falls through to the account-wide second chance).
    """
    candidates = page_id_to_teams.get(str(page_id)) or []
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # a None value means the account is SHARED by several teams, so it
    # cannot disambiguate a shared page either. Falls through to the page-filtered
    # branches below, which resolve on page linkage rather than account ownership.
    account_owner = ad_account_id_to_team.get(bare_account)
    if account_owner:
        for team in candidates:
            if team.get("_id") == account_owner.get("_id"):
                logging.debug(
                    "shared page %s in account %s -> account-wide owner %r",
                    page_id,
                    bare_account,
                    team.get("team_title"),
                )
                return team

    page_filtered = [t for t in candidates if t.get("fb_import_mode") != "account_wide"]
    if len(page_filtered) == 1:
        logging.debug(
            "shared page %s in account %s -> page-filtered claimant %r",
            page_id,
            bare_account,
            page_filtered[0].get("team_title"),
        )
        return page_filtered[0]

    pool = page_filtered or candidates
    winner = min(pool, key=lambda t: str(t.get("_id")))
    logging.warning(
        "page %s claimed by teams %s with no per-ad tie-break for "
        "account %s — defaulting to %r (lowest team _id); review "
        "team_facebook_page_id config.",
        page_id,
        sorted(t.get("team_title", "?") for t in candidates),
        bare_account,
        winner.get("team_title"),
    )
    return winner


def _out_of_scope(team, scope_set):
    """True iff a resolved ad owner falls outside a scoped run's
    --team_ids scope. A scoped run may PULL a shared ad account, but it must
    only WRITE docs for the scoped teams: writing the foreign owner's docs
    from someone else's request is surprising, and claiming the ad for the
    scoped team is the cross-tenant error this guard exists to prevent.
    Unscoped runs (scope_set falsy) never skip; unresolved ads (team None)
    are handled by the existing no-team drop, not here."""
    if not team or not scope_set:
        return False
    return team.get("_id") not in scope_set


# the ownership invariant is defined once in fb_helpers so
# the importer read side (here) and the wizard bridge write side share it.
# Kept as a module-level alias under the original private name because the
# release-plan tests reference ``fb_insights._account_wide_owner_ok``.
_account_wide_owner_ok = account_wide_owner_ok


def get_account_wide_teams_by_ad_account_id(team_esids=None, window_start=None, window_end=None):
    """Defect A — return a dict mapping ``fb_ad_account_id`` to team.

    an account-wide team may only claim an ad account it is the
    REGISTERED owner of (``de_asset_fb_ad_accounts.team_esid``). A stale/wrong
    ``fb_ad_account_esid`` back-link — a team pointing at another team's
    ad-account doc — otherwise lets the second-chance resolver stamp foreign
    ads onto the wrong team's index (one team claimed a sibling team's primary org on 2026-07-14).
    The guard drops only the mismatched mapping entry; the foreign ads then fall
    through to the existing "no team match" skip. A correctly-registered team's
    asset doc has ``team_esid == team._id``, so this is a silent no-op for the
    legitimate account-wide cohort (see :func:`_account_wide_owner_ok`).

    Account-wide teams (``fb_import_mode == "account_wide"``) own every ad
    that runs under their selected ad-account, regardless of which Facebook
    Page hosts the ad. Resolution is keyed off the ad-account ID rather
    than the actor_id/page_id.

    The wizard's step-6 finish writes ``fb_import_mode="account_wide"`` and
    ``fb_ad_account_esid=<asset_doc_id>`` onto the team doc; this helper
    reverses the link by looking up the ad-account's ``fb_ad_account_id``
    via the linked ``de_asset_fb_ad_accounts`` doc, and stores it as the
    map key. The importer's per-ad lookup then consults this map as a
    SECOND-CHANCE resolver when ``page_id_to_team`` misses — which is
    every ad for account-wide teams.

    Page-filtered teams continue to use ``team_facebook_page_id``; this
    helper ignores them.

     Phase 2 — a team with ``fb_ad_account_links`` claims every
    account whose link is relevant to the import window (open links when
    no window is given; window-overlapping links, including ENDED ones,
    when ``window_start``/``window_end`` are passed — that's what lets a
    backfill attribute a switched team's old-account ads). For link-based
    teams the LINK IS THE AUTHORITY: the asset doc's ``team_esid`` is not
    consulted (the guard inverts to the team side, so a sibling
    team's wizard re-auth stamping the doc can no longer stop this team's
    import). Teams without links keep the scalar path + doc
    guard unchanged. When two teams claim the same account for the same
    window, the lowest team ``_id`` wins with a loud warning (the
    deterministic last resort) — genuine concurrent sharing is an open
    design question and must not be silent.
    """
    lib.logging.define("fb-get-insights")
    # Only account-wide teams (ds_facebook still gates participation in the
    # importer; account_wide is the FETCH MODE, not the on/off switch).
    filters = {"ds_facebook": "true", "fb_import_mode": "account_wide"}
    if team_esids:
        filters["_id"] = list(team_esids)
    teams_docs = get_fb_teams(filters=filters)
    teams_docs = _filter_entitled_teams(teams_docs)  # Phase 70

    if not teams_docs:
        logging.info("No account-wide teams found")
        return {}

    ad_account_id_to_team = {}
    # accounts found to be shared by 2+ teams. Their map entry is set to
    # None (AMBIGUOUS): the second-chance resolver must decline rather than guess,
    # because the Page linkage that would decide attribution (rules 9/1) is absent.
    _ambiguous_accounts: set = set()
    for team_doc in teams_docs:
        team = team_doc["_source"]
        team["_id"] = team_doc["_id"]
        team_title = team.get("team_title", "Unknown")

        # candidate (ad_account_esid, link-or-None) pairs.
        links = select_team_ad_account_links(team, window_start, window_end)
        if links:
            candidates = [(link.get("ad_account_esid"), link) for link in links]
        else:
            candidates = [(team.get("fb_ad_account_esid"), None)]

        for ad_account_esid, link in candidates:
            if not ad_account_esid:
                logging.warning(
                    "Team %r (esid=%s) has fb_import_mode=account_wide but no "
                    "fb_ad_account_esid; skipping. The wizard step-6 should "
                    "have stamped this field — investigate.",
                    team_title,
                    team.get("_id"),
                )
                continue

            # The link carries fb_ad_account_id (wizard-written); resolve the
            # asset doc only when it doesn't (seeded links, scalar fallback).
            # The Graph API returns ``account_id`` as bare digits — the map key.
            fb_ad_account_id = (link or {}).get("fb_ad_account_id")
            asset_doc = None
            if not fb_ad_account_id:
                asset_doc = get_document_by_id("de_asset_fb_ad_accounts", ad_account_esid)
                if not asset_doc:
                    logging.warning(
                        "Team %r (esid=%s): fb_ad_account_esid=%s does not resolve "
                        "in de_asset_fb_ad_accounts; skipping. The asset doc may "
                        "have been deleted out from under the team.",
                        team_title,
                        team.get("_id"),
                        ad_account_esid,
                    )
                    continue
                fb_ad_account_id = asset_doc.get("fb_ad_account_id")
            if not fb_ad_account_id:
                logging.warning(
                    "Team %r (esid=%s): asset doc %s has no fb_ad_account_id; skipping",
                    team_title,
                    team.get("_id"),
                    ad_account_esid,
                )
                continue

            # scalar-fallback teams only: reject a team claiming an
            # ad account whose doc registers a different owner. Link-based
            # teams skip this — the team-side link is the authority.
            if link is None:
                if asset_doc is None:
                    asset_doc = get_document_by_id("de_asset_fb_ad_accounts", ad_account_esid) or {}
                if not _account_wide_owner_ok(asset_doc, team.get("_id")):
                    logging.warning(
                        "account-wide team %r (esid=%s) back-links "
                        "fb_ad_account_esid=%s (fb_ad_account_id=%s), but that "
                        "de_asset_fb_ad_accounts doc registers team_esid=%r — NOT this "
                        "team. Dropping the mapping so its ads are not stamped onto the "
                        "wrong index; fix the team's back-link.",
                        team_title,
                        team.get("_id"),
                        ad_account_esid,
                        fb_ad_account_id,
                        asset_doc.get("team_esid"),
                    )
                    continue

            bare = strip_ad_account_prefix(str(fb_ad_account_id))
            incumbent = ad_account_id_to_team.get(bare)
            if incumbent is not None and incumbent.get("_id") != team.get("_id"):
                # two teams legitimately share this ad account. That is
                # the EXPECTED Meta topology, not a misconfiguration: a Business
                # Portfolio holds many Ad Accounts (rule 6), an Ad belongs to one Ad
                # Account (rule 7), and Ads for the same Page can run from DIFFERENT
                # Ad Accounts (rule 10). Attribution is therefore decided by the
                # Ad's Page (rule 9 -> rule 1), which _resolve_team_for_page_ad
                # already does FIRST for every ad; this map is only the second-chance
                # resolver for an ad whose Page maps to no team at all.
                #
                # Pre-fix this picked the lowest team _id, which silently sent one
                # team's ads to another team's index. There is no correct answer
                # here — the Page linkage that would decide is missing — so we mark
                # the account AMBIGUOUS (None) and let the caller drop the ad with a
                # user-facing error naming the Page to link. Guessing is a
                # cross-tenant data error; dropping is recoverable.
                if bare not in _ambiguous_accounts:
                    _ambiguous_accounts.add(bare)
                    logging.warning(
                        "ad account %s is shared by teams %r AND %r. This "
                        "is legitimate (rules 6/7/10) and each ad is attributed via "
                        "its Facebook page (rules 9/1). Any ad in this account whose "
                        "page is linked to NO team will be DROPPED rather than "
                        "guessed onto one of these teams — link the page to its team "
                        "to import those ads.",
                        bare,
                        incumbent.get("team_title"),
                        team_title,
                    )
                ad_account_id_to_team[bare] = None
                continue
            if bare in _ambiguous_accounts:
                # A third+ team for an account already known to be shared.
                continue
            ad_account_id_to_team[bare] = team
            logging.info(
                "Account-wide team %r (esid=%s) -> fb_ad_account_id=%s%s",
                team_title,
                team.get("_id"),
                bare,
                " (link)" if link is not None else "",
            )

    logging.info(
        "Loaded %d account-wide ad_account_id -> team mappings: %s",
        len(ad_account_id_to_team),
        list(ad_account_id_to_team.keys()),
    )
    return ad_account_id_to_team


def _resolve_ad_account_id_field_paths(es, index_names):
    """Return {field_path: [indices]} grouping by which path supports a terms agg.

    Most team indices map `ad_account_id` as a top-level keyword (newer
    schema) — that's what gets queried as `ad_account_id`. One legacy
    text-mapped index (pre-schema-bump) maps it as `text` with a `.keyword`
    subfield — that needs `ad_account_id.keyword`. Indices missing the
    field entirely are skipped.

    Querying all indices with one field name fails with "Fielddata is
    disabled" when the field is text on any index in the pattern. Group
    indices by shape and run one terms agg per group.
    """
    paths = {}
    try:
        mappings = es.indices.get_mapping(index=index_names, ignore_unavailable=True, allow_no_indices=True)
    except Exception as e:
        logging.warning(f"Pre-filter: Could not fetch mappings for {index_names}: {e}")
        return {}

    for idx, m in mappings.items():
        props = m.get("mappings", {}).get("properties", {})
        field = props.get("ad_account_id")
        if not field:
            # Legitimate: index exists but has only no_data markers, never
            # any real ad docs that carry ad_account_id.
            continue
        if field.get("type") == "keyword":
            paths.setdefault("ad_account_id", []).append(idx)
        elif field.get("type") == "text" and "keyword" in field.get("fields", {}):
            paths.setdefault("ad_account_id.keyword", []).append(idx)
        # text without keyword subfield: not queryable — silently skip.
    return paths


def get_known_ad_accounts_for_teams(page_id_to_team, import_params):
    """
    Query team-specific ES insights indices to find which ad_account_id
    values have historical data for the given teams, then expand the set
    to include any sibling ad accounts that share an fb_app_esid with a
    historical account.

    The sibling expansion covers the case where a team's ad account is
    swapped for a new one under the same FB app — without it, the new
    account would be filtered out because it has no historical data yet.

    Args:
        page_id_to_team: dict mapping page_id -> team doc (already filtered
                         by team_ids if applicable)
        import_params: list of ad account entries built by set_import_params,
                       used to resolve ad_account_id -> fb_app_esid and find
                       siblings under the same app.

    Returns:
        set of ad_account_id strings (without act_ prefix) that should be
        imported, or None if the lookup could not be performed (meaning
        the caller should NOT filter).
    """
    lib.logging.define("fb-get-insights")

    team_titles = set()
    for team in page_id_to_team.values():
        title = team.get("team_title")
        if title:
            team_titles.add(title)

    if not team_titles:
        logging.info("Pre-filter: No team titles found in page_id_to_team, skipping pre-filter")
        return None

    index_names = [f"{sanitize_team_title(t)}_fb_insights" for t in team_titles]
    logging.info(f"Pre-filter: Querying {len(index_names)} team indices for known ad accounts: {index_names}")

    try:
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("Pre-filter: Could not connect to ES, skipping pre-filter")
            return None

        index_pattern = ",".join(index_names)

        # Newer team indices map ad_account_id as keyword (top-level). One
        # legacy text-mapped index (predates the schema bump) still
        # maps it as text with a .keyword subfield. Terms aggs need keyword,
        # so pick the right path per index — text-only would silently skip
        # the pre-filter without this. (KAN: legacy mapping cleanup.)
        field_paths = _resolve_ad_account_id_field_paths(es, index_names)
        if not field_paths:
            logging.info("Pre-filter: No usable ad_account_id mapping found, skipping pre-filter")
            return None

        # Build a multi-search body: one terms agg per field shape, each
        # scoped to the indices that use that shape. Merge bucket keys at
        # the end. This avoids the "Fielddata is disabled" error that came
        # from running a single agg against a mixed-mapping index pattern.
        known_accounts = set()
        for field_path, indices in field_paths.items():
            sub_pattern = ",".join(indices)
            result = es.search(
                index=sub_pattern,
                body={
                    "size": 0,
                    "aggs": {
                        "ad_accounts": {
                            "terms": {
                                "field": field_path,
                                "size": 1000,
                            }
                        }
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
            buckets = result.get("aggregations", {}).get("ad_accounts", {}).get("buckets", [])
            known_accounts.update(bucket["key"] for bucket in buckets)

        # Recompose into the shape the rest of the code expects.
        result = {"aggregations": {"ad_accounts": {"buckets": [{"key": k} for k in known_accounts]}}}

        buckets = result.get("aggregations", {}).get("ad_accounts", {}).get("buckets", [])
        known_accounts = {bucket["key"] for bucket in buckets}

        if not known_accounts:
            logging.info(
                "Pre-filter: No historical ad account data found in team indices (first import?), skipping pre-filter"
            )
            return None

        logging.info(f"Pre-filter: Found {len(known_accounts)} historical ad accounts in team indices: {known_accounts}")

        # Expand to sibling ad accounts under the same FB app(s) so a newly
        # added ad account that replaces an old one isn't filtered out.
        known_app_esids = set()
        for entry in import_params:
            ad_account = entry.get("fb_ad_account", {})
            acct_id = strip_ad_account_prefix(ad_account.get("fb_ad_account_id", ""))
            if acct_id in known_accounts:
                app_esid = ad_account.get("fb_app_esid")
                if app_esid:
                    known_app_esids.add(app_esid)

        sibling_accounts = set()
        for entry in import_params:
            ad_account = entry.get("fb_ad_account", {})
            if ad_account.get("fb_app_esid") in known_app_esids:
                acct_id = strip_ad_account_prefix(ad_account.get("fb_ad_account_id", ""))
                if acct_id:
                    sibling_accounts.add(acct_id)

        added_siblings = sibling_accounts - known_accounts
        if added_siblings:
            logging.info(
                f"Pre-filter: Added {len(added_siblings)} sibling ad accounts under the same FB app(s) "
                f"as historical accounts: {added_siblings}"
            )

        return known_accounts | sibling_accounts

    except Exception as e:
        logging.warning(f"Pre-filter: ES query failed ({e}), skipping pre-filter")
        return None


def _intersect_ids(*id_lists):
    """Intersect multiple optional `_id`-filter lists.

    Each argument is either None / False (no constraint from that source)
    or an iterable of IDs. Returns a sorted list of the intersection if any
    constraint applied, else None (callers should treat None as "no filter").
    """
    constraints = [set(ids) for ids in id_lists if ids]
    if not constraints:
        return None
    return sorted(set.intersection(*constraints))


# Build dict for api calls
def set_import_params(
    fb_account_ids=False,
    fb_ad_account_ids=False,
    fb_app_ids=False,
    team_ids=False,
    scope=None,
    window_start=None,
    window_end=None,
):
    """Walk fb_accounts → fb_apps → fb_ad_accounts and return per-account entries.

    When `scope` (from `resolve_team_filter_scope`) is provided, every layer's
    `_id` filter is narrowed to the scoped ESIDs *before* the ES query, so
    we don't load assets that will be discarded later..

    A scope with empty inner sets is treated as "no scope" — caller should
    have already detected the empty resolution and either fallen back or
    aborted the import.
    """
    lib.logging.define("fb-get-insights")
    # Parse the IDs
    fb_account_ids = parse_ids(fb_account_ids)
    fb_ad_account_ids = parse_ids(fb_ad_account_ids)
    fb_app_ids = parse_ids(fb_app_ids)
    team_ids = parse_ids(team_ids)

    # Resolve scope-derived ESIDs to per-layer filter inputs. An empty set at
    # any inner layer means the team_filter resolved to nothing — in that case
    # we deliberately leave the filter unconstrained for that layer so the
    # caller's defensive fallback path can take over instead of running the
    # import against zero assets.
    scope_account_ids = list(scope["fb_account_esids"]) if scope and scope.get("fb_account_esids") else None
    scope_app_ids = list(scope["fb_app_esids"]) if scope and scope.get("fb_app_esids") else None
    scope_ad_account_ids = list(scope["fb_ad_account_esids"]) if scope and scope.get("fb_ad_account_esids") else None

    logging.info(
        f"Import parameters set: fb_account_ids={fb_account_ids}, fb_ad_account_ids={fb_ad_account_ids}, "
        f"fb_app_ids={fb_app_ids}, team_ids={team_ids}, "
        f"scope_accounts={'all' if scope_account_ids is None else len(scope_account_ids)}, "
        f"scope_apps={'all' if scope_app_ids is None else len(scope_app_ids)}, "
        f"scope_ad_accounts={'all' if scope_ad_account_ids is None else len(scope_ad_account_ids)}"
    )

    ad_accounts_list = []

    # when every matched team is account_wide (the wizard-cohort
    # case), skip the page-filtered fb_accounts → fb_apps → fb_ad_accounts
    # walk entirely. The wizard-cohort teams have no legacy fb_app chain
    # tied to them, so the page-filtered walk has nothing to do — pre-fix
    # it fanned out into every legacy fb_app and ran their `mode=page_filtered`
    # tokens against ad accounts in scope for OTHER tenants. The
    # account_wide entries below still get added unconditionally so the
    # wizard team's actual ads import normally.
    skip_page_filtered = bool(scope and scope.get("all_account_wide"))
    if skip_page_filtered:
        logging.info("skipping page-filtered walk (scope.all_account_wide=True)")
        fb_accounts_docs = []
    else:
        # Step 1: Get Facebook Accounts (scoped to team filter when available).
        fb_accounts_filters = {}
        account_id_filter = _intersect_ids(fb_account_ids, scope_account_ids)
        if account_id_filter is not None:
            fb_accounts_filters["_id"] = account_id_filter

        fb_accounts_docs = get_fb_accounts(filters=fb_accounts_filters)

    for fb_account_doc in fb_accounts_docs:
        fb_account = fb_account_doc["_source"]
        fb_account_esid = fb_account_doc["_id"]

        # Step 2: Get Facebook Apps for this account (scoped to team filter).
        fb_apps_filters = {"fb_account_esid": fb_account_esid}
        app_id_filter = _intersect_ids(fb_app_ids, scope_app_ids)
        if app_id_filter is not None:
            fb_apps_filters["_id"] = app_id_filter

        fb_apps_docs = get_fb_apps(filters=fb_apps_filters)

        for fb_app_doc in fb_apps_docs:
            fb_app = fb_app_doc["_source"]
            fb_app_esid = fb_app_doc["_id"]
            fb_app["_id"] = fb_app_esid

            # Decrypt encrypted fields
            fb_app["fb_app_secret"] = decrypt(fb_app.get("fb_app_secret", ""))

            # Step 3: Get Facebook Ad Accounts for this app (scoped).
            fb_ad_accounts_filters = {"fb_app_esid": fb_app_esid}
            ad_account_id_filter = _intersect_ids(fb_ad_account_ids, scope_ad_account_ids)
            if ad_account_id_filter is not None:
                fb_ad_accounts_filters["_id"] = ad_account_id_filter

            fb_ad_accounts_docs = get_fb_ad_accounts(filters=fb_ad_accounts_filters)

            for fb_ad_account_doc in fb_ad_accounts_docs:
                fb_ad_account = fb_ad_account_doc["_source"]
                fb_ad_account_esid = fb_ad_account_doc["_id"]
                fb_ad_account["_id"] = fb_ad_account_esid

                # Add ad account to list - team matching now happens via page_id_to_team in main()
                ad_account_entry = {
                    "fb_account": fb_account,
                    "fb_app": fb_app,
                    "fb_ad_account": fb_ad_account,
                    # Defect A — explicit mode marker so main's
                    # token-resolution branch knows which path to take.
                    # The page-filtered chain carries fb_app credentials +
                    # fb_app_user token.
                    "import_mode": "page_filtered",
                }

                ad_accounts_list.append(ad_account_entry)

    # Defect A — account-wide teams own an ad-account doc with no
    # fb_app parent. The page-filtered walk above misses them (it requires
    # an fb_account → fb_app → fb_ad_account chain). Append a synthetic
    # entry per account-wide team here. fb_account + fb_app are populated
    # with empty placeholders so downstream code that reads fb_app_id /
    # fb_account.get(...) doesn't NPE; the actual Graph credentials come
    # from main()'s token-resolution branch (de_connection.oauth_token_enc
    # + WizardOAuthService.get_meta_app).
    account_wide_entries = _build_account_wide_entries(
        scope_team_esids=(scope or {}).get("team_esids"),
        window_start=window_start,
        window_end=window_end,
    )
    if account_wide_entries:
        logging.info(
            "Added %d account-wide ad-account entries to import_params",
            len(account_wide_entries),
        )
        ad_accounts_list.extend(account_wide_entries)

    return ad_accounts_list


def _build_account_wide_entries(scope_team_esids=None, window_start=None, window_end=None):
    """Defect A — synthesize import_params entries for account-wide teams.

    Returns a list of entries shaped like the page-filtered output of
    set_import_params, except:

    * ``fb_account`` is an empty dict (no parent fb_account doc — the
      wizard cohort doesn't go through the asset-manager FB-app upload).
    * ``fb_app`` is an empty dict with placeholders main() can detect.
    * ``fb_ad_account`` is the actual de_asset_fb_ad_accounts doc the
      wizard step-6 wrote for the team.
    * ``import_mode`` = ``"account_wide"`` — main() branches on this.
    * ``team_esid`` and ``connection_esid`` are stamped so main() can
      resolve the token without re-walking ES.

     Phase 2 — a team with ``fb_ad_account_links`` yields ONE
    entry per link relevant to the import window (open links when no
    window is given; window-overlapping links — including ENDED ones —
    when it is), each with the LINK's connection_esid so the token always
    matches the account. A team without links falls back to the scalar
    pair exactly as before (pre-update_v26 behavior).
    """
    filters = {"ds_facebook": "true", "fb_import_mode": "account_wide"}
    if scope_team_esids:
        filters["_id"] = list(scope_team_esids)
    teams_docs = get_fb_teams(filters=filters)
    teams_docs = _filter_entitled_teams(teams_docs)  # Phase 70
    if not teams_docs:
        return []

    entries = []
    for team_doc in teams_docs:
        team_src = team_doc["_source"]
        team_esid = team_doc["_id"]

        # link-based resolution. Each candidate is
        # (ad_account_esid, connection_esid, origin-label).
        links = select_team_ad_account_links(team_src, window_start, window_end)
        if links:
            candidates = [
                (
                    link.get("ad_account_esid"),
                    link.get("connection_esid") or team_src.get("connection_esid"),
                    "link",
                )
                for link in links
            ]
        else:
            # Scalar fallback (team not yet migrated, or all links out of
            # window with no scalar-era history — keep earlier shape).
            candidates = [
                (
                    team_src.get("fb_ad_account_esid"),
                    team_src.get("connection_esid"),
                    "scalar",
                )
            ]

        seen_esids = set()
        for ad_account_esid, connection_esid, origin in candidates:
            if not ad_account_esid:
                logging.warning(
                    "Team esid=%s has fb_import_mode=account_wide but no "
                    "fb_ad_account_esid (%s); skipping account-wide entry",
                    team_esid,
                    origin,
                )
                continue
            if ad_account_esid in seen_esids:
                continue
            seen_esids.add(ad_account_esid)
            if not connection_esid:
                logging.warning(
                    "Team esid=%s (%s): no connection_esid for ad account %s; " "skipping (token cannot be resolved)",
                    team_esid,
                    origin,
                    ad_account_esid,
                )
                continue
            asset_doc = get_document_by_id("de_asset_fb_ad_accounts", ad_account_esid)
            if not asset_doc:
                logging.warning(
                    "Team esid=%s (%s): fb_ad_account_esid=%s missing in " "de_asset_fb_ad_accounts; skipping",
                    team_esid,
                    origin,
                    ad_account_esid,
                )
                continue
            asset_doc["_id"] = ad_account_esid
            entries.append(
                {
                    "fb_account": {},
                    "fb_app": {},
                    "fb_ad_account": asset_doc,
                    "import_mode": "account_wide",
                    "team_esid": team_esid,
                    "connection_esid": connection_esid,
                }
            )
    return entries


def _parse_iso_utc(value):
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    these two timestamps are written by different modules
    (``connection_service._now_iso`` and ``credential_health._now``), so
    comparing them as raw strings only works while both keep the identical
    format. A 'Z' suffix on one side, or a naive value, would silently compare
    wrong and either skip a live import or use a dead token. Parsing removes the
    coupling; unparseable input returns None and the caller treats the staleness
    check as inconclusive (and proceeds).
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _connection_token_reported_dead(connection_esid, conn_src):
    """has the health probe reported THIS connection's token dead?

    Returns a short reason string when the import should be skipped, or "" when
    it should proceed. Consumes ``de_credential_health`` verdict, so
    there is no extra Meta round-trip per import.

    Three deliberate constraints:

    **Only ``token_dead`` skips.** ``renewal_required`` means the 90-day
    data-access clock is running, NOT that the credential is dead right now —
    skipping on it would dark a perfectly working connection for up to the full
    warning window. ``permission_lost`` is per-asset and partial; the import
    should still run and fetch what it can.

    **The verdict must be NEWER than the connection row.** A re-auth committed
    between probe cycles updates ``de_connection.updated_at`` but leaves the
    stale ``token_dead`` verdict in place until the next sweep (up to 6h). Acting
    on it would ignore a token the user just fixed — turning a monitoring lag
    into an outage. When the connection is newer, we proceed and let the import
    tell us the truth.

    **Fail OPEN.** Any error reading health returns "" (proceed). Monitoring
    infrastructure must never be able to halt imports: a missing index, an ES
    blip, or a bug in this function should degrade to today's behaviour (fail
    mid-run, which surfaces), never to a fleet-wide import stop.
    """
    try:
        from lib import credential_health as ch

        health = ch.get_health("de_connection", connection_esid) or {}
        if (health.get("status") or "") != ch.STATUS_TOKEN_DEAD:
            return ""

        probed_at = _parse_iso_utc(health.get("last_probed_at") or health.get("updated_at"))
        conn_updated = _parse_iso_utc(conn_src.get("updated_at"))
        if probed_at and conn_updated and conn_updated > probed_at:
            logging.info(
                "connection %s has a token_dead verdict from %s but the "
                "connection was updated at %s (newer) — treating the verdict as "
                "stale and proceeding",
                connection_esid,
                probed_at,
                conn_updated,
            )
            return ""

        return str(health.get("reason") or "token_dead")
    except Exception as exc:  # noqa: BLE001 — fail OPEN, see docstring
        logging.warning(
            "could not read credential health for %s (%s) — proceeding " "with the import rather than blocking it",
            connection_esid,
            type(exc).__name__,
        )
        return ""


def _resolve_account_wide_credentials(connection_esid):
    """Defect A — return (fb_app_id, fb_app_secret, access_token)
    for an account-wide team.

    The wizard-cohort token lives in ``de_connection.oauth_token_enc``
    (Fernet-encrypted via lib.encryption); the Meta app credentials come
    from the wizard's shared Meta app (the first de_asset_fb_apps doc,
    fetched via WizardOAuthService.get_meta_app — same path the OAuth
    callback used to exchange the auth code).

    Returns ``(None, None, None)`` when any of the three can't be
    resolved; the caller treats that as "skip this ad account, log a
    warning". Per-error logging happens here so the caller's warning
    just names the failure mode.
    """
    if not connection_esid:
        logging.warning("_resolve_account_wide_credentials: connection_esid is empty")
        return None, None, None

    # Meta app credentials. Cheap to call; fetches the (sole) wizard app.
    try:
        from asset_management.web.services.wizard_oauth_service import (
            WizardOAuthService,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("WizardOAuthService import failed: %s", e)
        return None, None, None

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        logging.warning(
            "_resolve_account_wide_credentials: WizardOAuthService "
            "returned no meta app — wizard Meta-app row missing or ES outage"
        )
        return None, None, None

    # Connection doc — fetch and decrypt the token.
    es = get_elasticsearch_client()
    if es is None:
        logging.warning("_resolve_account_wide_credentials: ES client unavailable")
        return None, None, None
    try:
        conn_doc = es.get(index="de_connection", id=connection_esid)
    except NotFoundError:
        # the team pointer references a DELETED connection doc
        # (e.g. a earlier duplicate that was cleaned up, or a row the
        # sweep removed). This is user-actionable — a Meta reconnect
        # re-mints the credential and re-stamps the pointer — so it must reach
        # the Report Scheduler error surface, not just the container log.
        # The "disconnected" token maps it to connection_revoked ("Connection
        # disconnected — reconnect it under Data Connectors"), the accurate
        # copy for a credential that no longer exists.
        logging.warning(
            "_resolve_account_wide_credentials: de_connection %s not "
            "found (deleted doc) — team pointer is dangling; skipping",
            connection_esid,
        )
        from lib.import_run_context import record_user_error

        record_user_error(
            f"Meta connection {connection_esid} was deleted or disconnected — " f"reconnect Meta under Data Connectors.",
            connector_id="meta_ads",
        )
        return None, None, None
    except Exception as e:  # noqa: BLE001
        # Non-404 ES failures (cluster down, timeout) are transient/internal —
        # log-only per the scope; never framed as the user's problem.
        logging.warning(
            "_resolve_account_wide_credentials: de_connection get(%s) " "failed: %s",
            connection_esid,
            e,
        )
        return None, None, None

    conn_src = (conn_doc or {}).get("_source") or {}
    # the team-doc connection_esid was historically stamped
    # last-writer-wins by MULTIPLE wizards (Meta, YouTube,
    # GA4), so it can point at a non-facebook connection whose token
    # is a Google credential. Presenting that to the FB API fails with a
    # misleading code-190 "disconnected" naming the wrong (healthy)
    # connection. Refuse it here and surface the real problem instead.
    # Absent provider (defensive; de_connection rows always carry one) keeps
    # the prior behavior.
    _provider = (conn_src.get("provider") or "").strip().lower()
    if _provider and _provider != "facebook":
        logging.warning(
            "_resolve_account_wide_credentials: connection %s has "
            "provider=%r (not facebook) — the team pointer was overwritten by "
            "another connector's wizard; skipping",
            connection_esid,
            _provider,
        )
        from lib.import_run_context import record_user_error

        record_user_error(
            f"Meta connection pointer overwritten by another connector "
            f"(provider={_provider}) — reconnect Meta under Data Connectors.",
            connector_id="meta_ads",
            detail=(conn_src.get("label") or "").strip() or None,
        )
        return None, None, None
    # pause imports when the user has disconnected this
    # connection via the Manage Connection page. The status flip is the
    # signal; the encrypted token may still be present on the doc (we
    # soft-delete, not hard-delete) but the importer must not use it.
    if (conn_src.get("status") or "") == "revoked":
        logging.info(
            "_resolve_account_wide_credentials: skipping revoked " "connection_esid=%s",
            connection_esid,
        )
        # the user disconnected this connection; tell them to reconnect
        # (classifier: revoked → connection_revoked). No-op on the cron path.
        from lib.import_run_context import record_user_error

        record_user_error(
            f"Facebook connection revoked (disconnected) for connection_esid={connection_esid}",
            connector_id="meta_ads",
        )
        return None, None, None

    # a connection can sit at status="active" with a token Meta has
    # already rejected. `status` only ever means "did a human press Disconnect"
    # (the soft-delete); it says nothing about whether the credential
    # still works. Confirmed on staging 2026-06-29: all 7 facebook connections
    # were status="active" and the import still failed with a raw code-190.
    #
    # 6-hourly probe already /debug_token's every de_connection row
    # and records token_dead in de_credential_health. Reading that verdict here
    # turns a mid-import failure into a pre-flight skip with an actionable
    # message, at zero extra Meta calls.
    #
    # NOTE the ticket's suggested fix (a) — "populate + check token_expires_at"
    # — is dead on arrival: the 2026-08-27 prod audit found NO Meta token
    # reports an expires_at at all (only data_access_expires_at), which is the
    # same reason refresh_connection_tokens no-ops on every row. A check on that
    # field could never fire.
    _dead_reason = _connection_token_reported_dead(connection_esid, conn_src)
    if _dead_reason:
        logging.info(
            "_resolve_account_wide_credentials: skipping connection %s " "— health probe reports the token dead (%s)",
            connection_esid,
            _dead_reason,
        )
        from lib.import_run_context import record_user_error

        # Wording is load-bearing: the classifier tries connection_expired FIRST
        # (matching "code 190" / "session has expired"), and that copy points at
        # the LEGACY fb_app login, not Data Connectors. This is a wizard
        # connection, so the raw must carry a connection_revoked token
        # ("disconnected") and must avoid the expired tokens entirely.
        record_user_error(
            "Facebook connection token is no longer accepted by Meta "
            "(effectively disconnected) — reconnect it under Data Connectors.",
            connector_id="meta_ads",
            detail=(conn_src.get("label") or "").strip() or None,
        )
        return None, None, None

    encrypted = conn_src.get("oauth_token_enc") or ""
    if not encrypted:
        logging.warning(
            "_resolve_account_wide_credentials: de_connection %s has " "no oauth_token_enc (status=%r)",
            connection_esid,
            conn_src.get("status"),
        )
        return None, None, None

    try:
        access_token = decrypt(encrypted)
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "_resolve_account_wide_credentials: decrypt failed for " "connection %s: %s",
            connection_esid,
            e,
        )
        return None, None, None

    return meta_app.client_id, meta_app.client_secret, access_token


# Function to fetch ad set details (NEW)
def fetch_adset_details(ad_set_id):
    lib.logging.define("fb-get-insights")
    try:
        adset = AdSet(ad_set_id).api_get(
            fields=[
                "id",
                "name",
                "targeting",
                "optimization_goal",
                "promoted_object",
                "billing_event",
                "bid_strategy",
            ]
        )
        return adset.export_all_data()
    except FacebookRequestError as e:
        logging.error(f"Failed to fetch ad set details for {ad_set_id}: {e}")
        return None


# Function to fetch campaign details
def fetch_campaign_details(campaign_id):
    lib.logging.define("fb-get-insights")
    try:
        campaign = Campaign(campaign_id).api_get(fields=["id", "name", "start_time", "stop_time"])
        return campaign.export_all_data()
    except FacebookRequestError as e:
        logging.error(f"Failed to fetch campaign details for {campaign_id}: {e}")
        return None


def get_fb_app_user_by_esid(esid):
    """Retrieve fb_app_user document by esid from de_asset_fb_app_users index."""
    return get_document_by_id("de_asset_fb_app_users", esid)


def get_fb_app_user_token(fb_app_user_esid):
    lib.logging.define("fb-get-insights")
    """Retrieve and decrypt the fb_app_user_token from the fb_app_user document."""
    if not fb_app_user_esid:
        logging.warning("fb_app_user_esid is missing.")
        return None

    fb_app_user = get_fb_app_user_by_esid(fb_app_user_esid)
    if not fb_app_user:
        logging.warning(f"No fb_app_user found or invalid document structure for esid {fb_app_user_esid}.")
        return None

    fb_app_user_token = fb_app_user.get("fb_app_user_token")
    if not fb_app_user_token:
        logging.warning(f"No token found for fb_app_user {fb_app_user.get('fb_app_user_title', 'Unknown')}.")
        return None

    plaintext = decrypt(fb_app_user_token)

    # PR-C: shadow-mode parity check. Cross-check the legacy
    # token decrypt against the migrated connection so we can catch
    # backfill drift before the eventual hard cutover. Disabled by
    # setting CONNECTION_SHADOW_DISABLED=1 — useful if this ever logs
    # noise during an incident. Telemetry-only; never blocks the return.
    if os.environ.get("CONNECTION_SHADOW_DISABLED") != "1":
        try:
            from asset_management.web.services.connection_service import ConnectionService

            shadow = ConnectionService.shadow_compare_legacy_token(
                legacy_asset_index="de_asset_fb_app_users",
                legacy_asset_esid=fb_app_user_esid,
                legacy_plaintext_token=plaintext,
            )
            status = shadow.get("status")
            if status == "match":
                logging.info(
                    "connection_shadow status=match legacy_asset_esid=%s connection_esid=%s",
                    fb_app_user_esid,
                    shadow.get("connection_esid"),
                )
            elif status == "missing":
                # 'missing' is the expected state until
                # connection_backfill.py runs on a given env — log at INFO
                # so operator-actionable signals (mismatch / decrypt_error /
                # exception) keep their WARNING level and aren't drowned out.
                logging.info(
                    "connection_shadow status=missing legacy_asset_esid=%s detail=%s",
                    fb_app_user_esid,
                    shadow,
                )
            else:
                # Anything other than 'match' / 'missing' is grep-bait —
                # surface at WARNING so a weekly log scan picks it up.
                logging.warning(
                    "connection_shadow status=%s legacy_asset_esid=%s detail=%s",
                    status,
                    fb_app_user_esid,
                    shadow,
                )
        except Exception as e:
            # Shadow mode must never crash an importer. Log and move on.
            logging.warning(
                "connection_shadow exception legacy_asset_esid=%s err=%s",
                fb_app_user_esid,
                e,
            )

    return plaintext


def _report_range_progress(progress_run_id, days_base, span_days, accounts_done, accounts_total):
    """post day-unit progress for the ranged pull.

    The per-date subprocess fan-out used to let the PARENT bump days_fetched once
    per child exit; a single ranged run advances the pill itself, per ad account:
    ``days_fetched = days_base + span_days * accounts_done // accounts_total``.
    Day units are preserved on purpose — the run's progress doc counts days
    (days_total = the requested range), and the denominator rebase is
    exclusive to dedup-only runs. No-op on the cron path (no run_id); soft-fails
    so a progress write can never abort the pull.
    """
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        done = days_base + (span_days * accounts_done) // max(accounts_total, 1)
        ImportProgressService.update_progress(run_id=progress_run_id, days_fetched=done)
    except Exception as exc:  # noqa: BLE001 — boundary; progress must never abort the import
        logging.warning("ranged progress write failed (run_id=%r): %s", progress_run_id, exc)


def _record_backfill_bailout(progress_run_id, accounts_completed, accounts_total, dates):
    """stamp a ``backfill_bailout`` note on the run's progress doc so the
    Report Scheduler card + History can tell the user the backfill stopped at a Meta
    rate limit and what remains.

    Additive: the run still completes as ``succeeded`` (the data written before the
    limit is real, committed data — not a failure). This just records that MORE data
    is available on a re-run. Soft-fails so a note write can never abort the import.
    No-op on the cron path (no run_id).
    """
    if not progress_run_id:
        return
    span = sorted(d for d in (dates or ()) if d)
    note = {
        "stopped_at_rate_limit": True,
        "accounts_completed": int(accounts_completed),
        "accounts_total": int(accounts_total),
        "date_from": span[0] if span else "",
        "date_to": span[-1] if span else "",
    }
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.record_backfill_bailout(run_id=progress_run_id, note=note)
    except Exception as exc:  # noqa: BLE001 — boundary; a note must never abort the import
        logging.warning("backfill bail-out note write failed (run_id=%r): %s", progress_run_id, exc)


def _chunk_date_range(since, until, chunk_days):
    """yield inclusive (since, until) sub-ranges of ≤ ``chunk_days`` days.

    A single day (since == until) yields itself. ``until`` before ``since`` is
    clamped to a single-day range (defensive; callers pass sorted bounds).
    """
    start = datetime.strptime(since, "%Y-%m-%d").date()
    end = datetime.strptime(until, "%Y-%m-%d").date()
    if end < start:
        end = start
    step = max(int(chunk_days), 1)
    while start <= end:
        stop = min(start + timedelta(days=step - 1), end)
        yield start.strftime("%Y-%m-%d"), stop.strftime("%Y-%m-%d")
        start = stop + timedelta(days=1)


# Main function to handle params passed from import.py
def main(params):
    global _log_usage, _backfill_bailout

    lib.logging.define("fb-get-insights")

    # backfill mode: on a Meta rate limit, bail out gracefully (keep
    # what's written, report what remains) instead of the daily path's sleep-and-
    # retry. Set ONLY by ReportScheduleService.backfill_now (via run_daily_imports);
    # the cron/scheduled path never passes it. Accepts CLI "true"/"1" or a real bool.
    _backfill_bailout = params.get("backfill_bailout") in (True, "true", "True", "1", 1)
    if _backfill_bailout:
        logging.info("backfill mode ON: rate limits will bail out and keep partial progress.")

    # bind this run's progress doc so the strict-mode failures below
    # (zero teams, no traversable asset chain) and a revoked connection surface in
    # the Report Scheduler's "Errors" button instead of being lost behind the
    # dispatcher's generic non-zero-exit stamp. No-op on the cron path.
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    # Check for usage logging flag (--log_usage)
    if params.get("log_usage"):
        _log_usage = True
        setup_usage_logging()
        logging.info("Usage logging enabled - writing to /app/data/logs/fb_usage_YYYYMMDD.log")

    # this importer fetches a whole DATE SET in one run. ``--dates``
    # (comma-separated YYYY-MM-DD list) is what run_daily_imports passes; the
    # legacy ``--date`` single day is kept for back-compat (manual invocations,
    # debug tooling) and behaves as a 1-element set. The account-level insights
    # call below asks Meta for [since..until] with time_increment=1 (per-day
    # rows), so N days cost O(N / chunk) calls per account instead of N
    # subprocesses × N calls. The set may be NON-contiguous (the day
    # skip removes settled middle days); rows for uncovered days are dropped at
    # the per-row day gate in the write loop.
    dates_param = params.get("dates") or params.get("date")
    if not dates_param:
        raise ValueError("The 'dates' (or 'date') parameter is required.")
    dates = sorted({d.strip() for d in str(dates_param).split(",") if d.strip()})
    for _d in dates:
        try:
            datetime.strptime(_d, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid date format: {_d}. Expected YYYY-MM-DD.")
    since, until = dates[0], dates[-1]
    wanted_days = set(dates)
    window_label = since if since == until else f"{since}..{until}"

    # Get optional parameters or set default to False
    fb_account_ids = params.get("fb_account_ids", False)
    fb_ad_account_ids = params.get("fb_ad_account_ids", False)
    fb_app_ids = params.get("fb_app_ids", False)
    team_ids = params.get("team_ids", False)
    fb_page_id = params.get("fb_page_id", False)
    # when set, re-WRITE every ad even if its file is already archived
    # and the ES doc exists, so a forced re-pull overwrites (re-map / attribution
    # maturation) instead of the guard discarding the fresh data. Accepts
    # the string "true"/"1" (CLI) or a real bool. Default False = the cron keeps
    # the idempotency skip.
    force_refetch = params.get("force_refetch") in (True, "true", "True", "1", 1)
    # per-day force-overwrite subset (the maturation tail).
    # A row whose day is in force_dates bypasses the archived+indexed
    # write guard exactly as force_refetch does, but only for that day. Replaces
    # the per-subprocess boolean the per-date fan-out used to forward:
    # one ranged run needs day-level granularity. Global force_refetch supersedes.
    force_dates = {d.strip() for d in str(params.get("force_dates") or "").split(",") if d.strip()}
    # progress moves its bump point INTO this child: days
    # arrive interleaved across accounts, so ad accounts are the only monotonic
    # work unit a single ranged run can report on. progress_days_base is how many
    # days the parent already accounted for via the skip pass.
    progress_run_id = params.get("progress_run_id") or None
    try:
        progress_days_base = int(params.get("progress_days_base") or 0)
    except (TypeError, ValueError):
        progress_days_base = 0

    # resolve --team_ids to upstream asset ESIDs upfront so the
    # asset-chain walk inside set_import_params can scope each layer's ES
    # query. Falls back to unfiltered behavior if the resolver returns
    # empty inner sets — that means the team filter matched nothing
    # traversable, and the caller's pre-filter will catch it.
    #
    # when the resolver flagged all_account_wide (every matched
    # team has fb_import_mode="account_wide" — the wizard-cohort case),
    # the empty fb_*_esids sets are EXPECTED, not a misconfiguration.
    # Keep the scope and pass it through so the account_wide branch
    # downstream can still narrow by team_esids; signal the page-
    # filtered walk to skip entirely. Pre-fix, this branch fell into
    # the "fall back to unfiltered" path and the importer fanned out
    # into every legacy fb_app chain, leaking API calls + no_data
    # marker writes into 12+ other tenants' indices.
    scope = resolve_team_filter_scope(team_ids) if team_ids else None
    if scope:
        # strict mode. When --team_ids is passed, the caller is
        # explicitly scoping the import to a specific team. If the resolver
        # couldn't match the requested team_ids to anything traversable,
        # we MUST abort rather than fall back to fleet-wide behaviour.
        # Pre-fix, the legacy "fall back to unfiltered" path silently
        # leaked the wizard kickoff into 12+ foreign tenants' Meta API
        # quotas + ES indices.
        #
        # Three branches:
        #
        #  1. team_esids EMPTY -> resolver matched zero teams. Strict abort.
        #  2. team_esids non-empty BUT fb_ad_account_esids empty +
        #     all_account_wide=True -> wizard-cohort case. Keep scope,
        #     skip page-filtered walk.
        #  3. team_esids non-empty AND fb_ad_account_esids non-empty ->
        #     legacy case. Normal narrowed walk.
        #
        # Branch 2's signal is set by resolve_team_filter_scope when every
        # matched team has fb_import_mode='account_wide'.
        if not scope.get("team_esids"):
            # record the user-readable cause BEFORE the abort so it
            # survives the dispatcher's generic non-zero-exit stamp. Classifies as
            # no_data_source ("zero teams").
            from lib.import_run_context import record_user_error

            record_user_error("strict mode: team_ids resolved to ZERO teams", connector_id="meta_ads")
            raise ValueError(
                f"strict mode: team_ids={team_ids!r} resolved to ZERO teams. "
                f"Aborting to prevent cross-tenant data leak from a fleet-wide fallback. "
                f"If this is a legitimate cron call, omit --team_ids."
            )
        if not scope.get("fb_ad_account_esids"):
            if scope.get("all_account_wide"):
                logging.info(
                    f"team_ids={team_ids!r} resolved to {len(scope.get('team_esids') or set())} "
                    f"account_wide team(s); skipping page-filtered asset chain"
                )
                # Scope stays; set_import_params reads scope.all_account_wide and
                # skips the page-filtered walk internally.
            else:
                # team_esids matched, but every matched team has zero
                # traversable fb_ad_accounts AND isn't account_wide.
                # That's a misconfigured legacy team; aborting is safer
                # than falling back to fleet-wide.
                # record before abort; classifies as no_data_source
                # ("no traversable" asset chain).
                from lib.import_run_context import record_user_error

                record_user_error(
                    "strict mode: matched team(s) have no traversable " "fb_account -> fb_app -> fb_ad_account chain",
                    connector_id="meta_ads",
                )
                raise ValueError(
                    f"strict mode: team_ids={team_ids!r} matched team(s) "
                    f"{sorted(scope.get('team_esids') or set())} but none have a traversable "
                    f"fb_account → fb_app → fb_ad_account chain AND none are account_wide. "
                    f"Refusing to fall back to fleet-wide (cross-tenant leak risk). "
                    f"Fix the team's asset configuration or remove --team_ids."
                )

    # Send parameters to set_import_params and assign to import_params
    import_params = set_import_params(
        fb_account_ids=fb_account_ids,
        fb_ad_account_ids=fb_ad_account_ids,
        fb_app_ids=fb_app_ids,
        team_ids=team_ids,
        scope=scope,
        # link selection is window-aware so a backfill over a
        # switched team's old window pulls the ENDED link's account too.
        window_start=since,
        window_end=until,
    )

    logging.info(f"Processing with dates: {window_label} ({len(dates)} day(s))")

    # Log debug mode if filtering by page ID
    if fb_page_id:
        logging.info(f"DEBUG MODE: Filtering to single page ID: {fb_page_id}")

    # Build mapping of page_id to team. When the team filter resolved to a
    # non-empty set of team_esids, scope the ES query to those teams
    # — that replaces the previous load-then-filter-in-Python approach.
    # the multimap keeps EVERY claimant of a shared page; the
    # single-winner view below feeds the earlier call sites (known-
    # accounts pre-filter, page-mapping summary, no_data markers).
    scope_team_esids = scope.get("team_esids") if scope else None
    _scope_set = set(scope_team_esids) if scope_team_esids else None
    # the attribution maps are ALWAYS built from ALL teams,
    # regardless of --team_ids scope. A scoped map on a SHARED ad account
    # blinds both protections at once: foreign teams' page-ads miss the page
    # map, and the account claimant map holds a single claimant (no
    # ambiguity), so the second-chance resolver stamped EVERY delivering ad
    # on the account onto the scoped team (cross-tenant writes, found
    # 2026-08-24 during the year backfill). Scope now limits which
    # ACCOUNTS are pulled and which teams may be WRITTEN — never which teams
    # exist for attribution. Out-of-scope owners are skipped per-ad below.
    page_id_to_teams = get_all_teams_by_page_id_multi(team_esids=None)

    # per-team Ad Insights switch (page_filtered): attribution is
    # per-page, so opted-out teams drop out of the claiming map BEFORE the
    # single-winner view / pre-filter / no_data markers derive from it —
    # their docs are never written and no false no_data markers appear.
    # (The account_wide equivalent is the per-account skip in the main loop.)
    _kaf27_off = sorted(
        {
            t.get("_id")
            for _teams in page_id_to_teams.values()
            for t in _teams
            if not _source_enabled_for_team(t.get("_id"), "fb_ads")
        }
    )
    if _kaf27_off:
        logging.info("fb_ads disabled for team(s) %s — dropping their pages from this run.", ", ".join(_kaf27_off))
        page_id_to_teams = {
            pid: kept
            for pid, _teams in page_id_to_teams.items()
            for kept in [[t for t in _teams if t.get("_id") not in _kaf27_off]]
            if kept
        }

    page_id_to_team = {pid: teams[-1] for pid, teams in page_id_to_teams.items()}

    # SCOPED single-winner view for the bookkeeping paths that
    # must stay scope-limited: the known-accounts pre-filter and the
    # no_data/stale-marker writes. Attribution keeps the full map above.
    if _scope_set:
        scoped_page_id_to_team = {
            pid: kept[-1]
            for pid, teams in page_id_to_teams.items()
            for kept in [[t for t in teams if t.get("_id") in _scope_set]]
            if kept
        }
    else:
        scoped_page_id_to_team = page_id_to_team

    # Defect A — account-wide teams resolve by ad-account id, not
    # by page id. Built once here so the per-ad lookup below can fall back
    # to this map when the page_id_to_team lookup misses.
    # built UNSCOPED so shared accounts resolve to the
    # AMBIGUOUS sentinel even in scoped runs (see the comment above).
    ad_account_id_to_team = get_account_wide_teams_by_ad_account_id(
        team_esids=None,
        window_start=since,
        window_end=until,
    )

    if team_ids:
        matched_teams = sorted({t.get("team_title", "Unknown") for t in scoped_page_id_to_team.values()})
        # skip None (shared/ambiguous accounts, see the resolver).
        matched_aw = sorted(
            {
                t.get("team_title", "Unknown")
                for t in ad_account_id_to_team.values()
                if t and (not _scope_set or t.get("_id") in _scope_set)
            }
        )
        logging.info(
            f"Team filter applied: page_filtered={matched_teams} "
            f"({len(page_id_to_team)} page(s)); account_wide={matched_aw} "
            f"({len(ad_account_id_to_team)} account(s))"
        )

    # Pre-filter ad accounts when team filter is active
    if team_ids and scoped_page_id_to_team:
        known_accounts = get_known_ad_accounts_for_teams(scoped_page_id_to_team, import_params)
        if known_accounts is not None:
            original_count = len(import_params)
            # Defect A — account-wide entries are wizard-newcomer
            # teams; they ALWAYS have zero historical data on the first
            # import. The pre-filter would otherwise drop them and
            # reproduce the "succeeded with zero docs" symptom. Preserve
            # them unconditionally; the regular pre-filter still applies
            # to page-filtered entries.
            import_params = [
                entry
                for entry in import_params
                if entry.get("import_mode") == "account_wide"
                or strip_ad_account_prefix(entry["fb_ad_account"].get("fb_ad_account_id", "")) in known_accounts
            ]
            skipped = original_count - len(import_params)
            if skipped > 0:
                logging.info(
                    f"Pre-filter: Reduced ad accounts from {original_count} to {len(import_params)} "
                    f"(skipped {skipped} accounts with no historical data for filtered teams)"
                )
            else:
                logging.info(f"Pre-filter: All {original_count} ad accounts matched, none skipped")
        else:
            logging.info("Pre-filter: Skipped (no historical data or lookup failed), querying all ad accounts")

    # Initialize cache for page details to avoid redundant API calls
    page_details_cache = {}

    # Track which page IDs are actually encountered in ads
    encountered_page_ids = set()

    # Track which teams had at least one ad saved (for no_data markers)
    teams_with_data = set()

    # Loop over the ad accounts
    accounts_total = len(import_params)
    # track how many accounts finished before a rate-limit bail-out (if
    # any) so the run can report completed-vs-remaining. ``bailed_out`` is set by the
    # _RateLimitBailout handler wrapping the loop; only reachable in backfill mode.
    accounts_completed = 0
    bailed_out = False
    for account_idx, ad_account_entry in enumerate(import_params, start=1):
        # report the PREVIOUS account's completion at the top of the
        # loop (the per-account `continue` branches below would skip an
        # end-of-iteration bump). The final all-done bump fires after the loop.
        _report_range_progress(progress_run_id, progress_days_base, len(dates), account_idx - 1, accounts_total)
        # bind this entry's team (None for legacy entries without a
        # team_esid stamp) so every record_user_error fired while processing
        # this account — credential resolution, expired-token surfacing —
        # carries durable per-team attribution and never inherits the
        # previous entry's team.
        from lib.import_run_context import set_current_team

        set_current_team(ad_account_entry.get("team_esid"))
        fb_account = ad_account_entry["fb_account"]
        fb_app = ad_account_entry["fb_app"]
        fb_ad_account = ad_account_entry["fb_ad_account"]
        import_mode = ad_account_entry.get("import_mode", "page_filtered")

        # over-cap (Condition B) billing pre-check per FB ad-account
        # source. The org rollup lists FB ad accounts under the Meta provider
        # (id == ad-account esid, provider_id == fb_ad_account_id), so match on
        # the asset esid or the ad-account id (raw + act_-stripped). The
        # no-subscription condition (A) was handled at the team filter. Surfaces a
        # classified error on the scheduled path (no-op on cron); fail-open.
        # asset_esid is the reliable match: the org rollup keys FB ad-account
        # sources by their de_asset_fb_ad_accounts esid, which is exactly the
        # fb_ad_account["_id"] set when the entry was built. provider_id is passed
        # too as a belt-and-suspenders match on the raw fb_ad_account_id.
        _fb_ad_account_esid = fb_ad_account.get("_id")
        _fb_ad_account_id_raw = str(fb_ad_account.get("fb_ad_account_id") or "")
        _fb_team_esid = ad_account_entry.get("team_esid")
        from lib.billing_entitlement import record_billing_skip, source_billing_block

        _fb_block = source_billing_block(
            team_esid=_fb_team_esid,
            asset_esid=_fb_ad_account_esid,
            provider_id=_fb_ad_account_id_raw,
        )
        if _fb_block:
            logging.info(
                "billing pre-check: skipping FB ad account %s (team %s, reason=%s).",
                fb_ad_account.get("fb_ad_account_title", "Unknown"),
                _fb_team_esid,
                _fb_block,
            )
            record_billing_skip(_fb_block, connector_id="meta_ads", detail=fb_ad_account.get("fb_ad_account_title"))
            continue

        # Defect A — token + Meta-app resolution branches on mode.
        if import_mode == "account_wide":
            # Account-wide path (wizard cohort): the token lives in
            # de_connection.oauth_token_enc; the Meta app credentials come
            # from the wizard's shared Meta app (WizardOAuthService).
            (
                fb_app_id,
                fb_app_secret,
                fb_ad_account_user_token,
            ) = _resolve_account_wide_credentials(
                connection_esid=ad_account_entry.get("connection_esid"),
            )
            if not fb_ad_account_user_token:
                logging.warning(
                    "account-wide: could not resolve credentials for "
                    "team_esid=%s connection_esid=%s; skipping ad account %s",
                    ad_account_entry.get("team_esid"),
                    ad_account_entry.get("connection_esid"),
                    fb_ad_account.get("fb_ad_account_title", "Unknown"),
                )
                continue
        else:
            # Page-filtered path (existing): fb_app credentials + per-account
            # user token stored under de_asset_fb_app_users.
            fb_app_id = fb_app.get("fb_app_id")
            fb_app_secret = fb_app.get("fb_app_secret")
            fb_ad_account_user_token = get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))
            if not fb_ad_account_user_token:
                logging.warning(
                    f"Could not get user token for ad account {fb_ad_account.get('fb_ad_account_title', 'Unknown')}. Skipping this ad account."
                )
                continue

        # Initialize the Facebook API with the ad account user's token
        logging.info(f"Initing FB with ad_account token (mode={import_mode}).")
        init_facebook_api(fb_app_id, fb_app_secret, fb_ad_account_user_token)
        # follow-up — remember which legacy FB app this token belongs to
        # so an expired-token (code 190) error names the app to reconnect, not a
        # bare "this account". fb_app is the de_asset_fb_apps doc in scope for both
        # modes; the wizard account-wide path may have no title (degrades to id).
        set_current_fb_app(fb_app.get("fb_app_title"), fb_app_id)
        # follow-up — record the mode + connection so a code-190 surfaces
        # against the RIGHT credential: account_wide names the de_connection
        # (Data Connectors), page_filtered names the legacy app.
        set_current_fb_import_context(import_mode, ad_account_entry.get("connection_esid"))

        # ---------------------------------------------------------
        # Before fetching ads, let's do a usage check for this ad account
        # ---------------------------------------------------------
        ad_account_id = fb_ad_account.get("fb_ad_account_id")

        # per-team Ad Insights switch (account_wide): the account has
        # exactly one owning team, so an opted-out owner skips the whole
        # account before any Meta call. (The page_filtered equivalent is the
        # claiming-map filter after get_all_teams_by_page_id_multi above.)
        if import_mode == "account_wide" and not _source_enabled_for_team(_fb_team_esid, "fb_ads"):
            logging.info(
                "fb_ads disabled for team %s — skipping ad account %s.",
                _fb_team_esid,
                fb_ad_account.get("fb_ad_account_title", "Unknown"),
            )
            continue
        # the three network fetches below are where a Meta rate limit is
        # surfaced. In backfill mode they raise _RateLimitBailout (instead of
        # sleeping); catch it here to STOP the whole loop gracefully. Accounts
        # already finished keep their written data (per-ad JSON is committed
        # downstream); this account and every later one are left for the next run.
        # Off backfill mode the raises never happen, so this is a no-op wrapper.
        try:
            check_and_pause_for_account(ad_account_id)

            # Fetch ads for the ad account (token passed for batch API calls).
            # ONE windowed delivery probe + metadata batch for the whole
            # range (the `until` param): date-invariant ad/creative/campaign/
            # ad-set metadata is fetched once per RUN, not once per day.
            ads_data = fetch_ads(ad_account_id, since, access_token=fb_ad_account_user_token, until=until)

            # fetch ALL ad insights for this account in ONE paginated
            # account-level call, indexed by ad_id. Replaces the per-ad
            # fetch_ad_insights round-trip (and its paired throttle probe) inside the
            # loop — ~2N serial calls collapse to O(pages). Each ad below looks up its
            # rows by id; the per-platform fan-out is byte-identical to the per-ad path.
            # `until` extends the call over the whole range with
            # time_increment=1, so each row is a (day, platform) cell.
            # per-tenant field picker: request the union of the enabled
            # sets across every team this account's data can attribute to.
            # account_wide entries carry their single owning team; page_filtered
            # attribution is per-page, so the candidate set is every scoped
            # page-claiming team (a superset — safe, see _daily_fields_for_teams).
            if import_mode == "account_wide":
                _kaf16_teams = [_fb_team_esid]
            else:
                _kaf16_teams = [t.get("_id") for _teams in page_id_to_teams.values() for t in _teams]
            account_ad_insights = fetch_account_ad_insights(
                ad_account_id, since, until=until, fields=_daily_fields_for_teams(_kaf16_teams)
            )
        except _RateLimitBailout as bail:
            logging.warning(
                "backfill: rate-limit bail-out at account %d/%d (%s): %s. "
                "Keeping %d completed account(s); stopping so partial data is committed.",
                account_idx,
                accounts_total,
                fb_ad_account.get("fb_ad_account_title", "Unknown"),
                bail,
                account_idx - 1,
            )
            accounts_completed = account_idx - 1
            bailed_out = True
            break

        for ad in ads_data:
            # mirror the weekly importer's per-ad boundary. Without
            # it, one ad raising an uncaught exception (e.g. IndexError from
            # an empty insights list, Nominatim failure inside
            # geocode_all_targeting_levels) silently kills the entire ad
            # account's batch with no log signal.
            try:
                if "creative_details" in ad and ad["creative_details"]:
                    actor_id = ad["creative_details"].get("actor_id")  # This is the page ID
                    if actor_id:
                        # Track this page ID as encountered
                        encountered_page_ids.add(str(actor_id))

                        # Look up team by page ID. a page can be
                        # claimed by more than one team, and the winner
                        # depends on the ad's account, so resolution is
                        # per-ad on the multimap.
                        bare_account = strip_ad_account_prefix(str(ad_account_id))
                        team = _resolve_team_for_page_ad(actor_id, bare_account, page_id_to_teams, ad_account_id_to_team)
                        if not team:
                            # Defect A — second-chance lookup for
                            # account-wide teams. The wizard cohort doesn't
                            # collect Pages; their team claims every ad on
                            # the selected ad-account regardless of which
                            # Page hosted it. Falls through to the original
                            # "no team match" skip when the ad-account isn't
                            # owned by an account-wide team either (the
                            # legitimate "agency tenant whose Page list is
                            # incomplete" case where dropping the ad is the
                            # right move).
                            team = ad_account_id_to_team.get(bare_account)
                            if team:
                                logging.debug(
                                    "account-wide claim: ad %s "
                                    "(page_id=%s not mapped) -> team %r via "
                                    "ad_account_id=%s",
                                    ad.get("id"),
                                    actor_id,
                                    team.get("team_title"),
                                    bare_account,
                                )
                        if not team:
                            logging.warning(
                                f"Ad {ad.get('id')}: No team match for page_id={actor_id} (ad_name='{ad.get('name', 'unknown')}', ad_account={ad_account_id})"
                            )
                            # surface the drop to the Report Scheduler
                            # instead of only logging it. Per the Meta model an Ad
                            # belongs to exactly one Page (rule 9) and a Page to
                            # exactly one team (rule 1), so attribution is decided by
                            # the Page. Reaching here means this Page is linked to NO
                            # team, which is a configuration gap — and with an ad
                            # account shared across teams (rules 6/7/10) we must NOT
                            # guess which team gets the ad: putting it on the wrong
                            # team's index is a cross-tenant data error. So the ad is
                            # dropped, and the user is told which Page to link.
                            # Deduped by (category, detail), so one line per Page no
                            # matter how many ads it hosts.
                            #
                            # Name the page, not just its id — a bare numeric id is
                            # not actionable for the person who has to go link it.
                            #
                            # The name MUST come from the inventory map
                            # (de_asset_fb_pages, fetched once per run) rather than
                            # page_details_cache: that cache is filled further down
                            # this same loop, so it is EMPTY on a page's first ad and
                            # populated afterwards. Since the classifier dedups by
                            # (category, detail) and builds the user-facing message
                            # from ``detail``, an order-dependent name would file the
                            # same page twice — once bare, once named. The inventory
                            # map is stable for the whole run.
                            #
                            # Fail-open: a page absent from the inventory (exactly the
                            # unlinked case, often) keeps the id alone. A missing name
                            # must never suppress the report.
                            _page_name = (_inventoried_page_names().get(str(actor_id)) or "").strip()
                            _page_label = f"{actor_id} ({_page_name})" if _page_name else str(actor_id)

                            from lib.import_run_context import record_user_error

                            record_user_error(
                                f"no active team is linked to Facebook page {actor_id}",
                                connector_id="meta_ads",
                                detail=f"Facebook page {_page_label}",
                            )
                            continue

                        # the ad resolved to a real owner, but that
                        # owner is outside this run's --team_ids scope (a
                        # shared-account sibling). Skip it: the owner's docs
                        # arrive via its own runs; claiming it here was the
                        # cross-tenant bug.
                        if _out_of_scope(team, _scope_set):
                            logging.info(
                                "ad %s (page_id=%s) belongs to out-of-scope team %r — skipped in scoped run",
                                ad.get("id"),
                                actor_id,
                                team.get("team_title"),
                            )
                            continue

                        # Check cache first to avoid redundant API calls
                        # key by credential, not page: see _page_details_cache_key.
                        _cache_key = _page_details_cache_key(actor_id)
                        if _cache_key in page_details_cache:
                            page_details = page_details_cache[_cache_key]
                            if page_details and page_details != "access_denied":
                                logging.debug(
                                    f"Using cached page details for page_id={actor_id}, name={page_details.get('name', 'unknown')}"
                                )
                            else:
                                logging.debug(f"Using cached access_denied status for page_id={actor_id}")
                        else:
                            # Fetch page details using the ad account token (already initialized)
                            check_and_pause_for_account(ad_account_id)
                            page_details = fetch_page_details(actor_id)
                            # cache a copy, not the reference. Downstream
                            # callers (e.g. massage_ad_data) may mutate the dict
                            # they receive; mutating the cached object would
                            # empty future cache hits and silently drop ads.
                            if isinstance(page_details, dict):
                                page_details_cache[_cache_key] = dict(page_details)
                            else:
                                page_details_cache[_cache_key] = page_details

                        if page_details and page_details != "access_denied":
                            ad["page_details"] = page_details

                            # look up this ad's insights (list
                            # of per-platform rows) from the account-level batch
                            # fetched once before the loop — no per-ad round-trip.
                            ad_insights_list = account_ad_insights.get(str(ad["id"]), [])
                        elif page_details == "access_denied":
                            logging.warning(
                                f"Access denied for page_id={actor_id} (team='{team.get('team_title', 'unknown')}'). Skipping ad {ad.get('id')} ({ad.get('name', 'unknown')})"
                            )
                            # surface this user-actionable per-page
                            # permission failure to the Report Scheduler's "Errors"
                            # button (the ad-loop site missed; the
                            # page-insights sites were wired there). The raw carries
                            # "access denied" so it classifies to permission_denied;
                            # detail=team_title dedups N skipped ads on one page to a
                            # single per-team entry. No-op on the cron path.
                            # only the OWNER team's pass records it; a sibling
                            # pass in a shared account hitting the owner's page is expected
                            # (access ≠ ownership) and would otherwise stamp the same
                            # failure onto every team sharing the account.
                            if _should_record_page_denial(team):
                                from lib.import_run_context import record_user_error

                                record_user_error(
                                    f"Facebook page access denied (code 10) for page_id={actor_id}",
                                    connector_id="meta_ads",
                                    detail=team.get("team_title") or "",
                                )
                            else:
                                logging.info(
                                    "page_id=%s belongs to team %r, not the current pass — "
                                    "denial not recorded against the sibling",
                                    actor_id,
                                    team.get("team_title"),
                                )
                            continue
                        else:
                            # Defect A — page_details is None (the
                            # token lacks pages_read_engagement, or the page
                            # was deleted/invisible). Pre-fix this fell
                            # through to the no-team-stamp section below
                            # WITHOUT assigning ad_insights_list, causing
                            # an UnboundLocalError on the `if not
                            # ad_insights_list` check downstream.
                            #
                            # Account-wide wizard tokens carry
                            # pages_show_list but not pages_read_engagement;
                            # fetch_page_details returns None for every ad
                            # under such a token. The right behaviour is to
                            # proceed without page_details enrichment —
                            # ad-spend numbers (the headline value for the
                            # wizard cohort) come from /insights, not from
                            # the Page node, so dropping the page metadata
                            # doesn't lose anything load-bearing.
                            logging.info(
                                "page_details=None for ad %s (page_id=%s, ad_account=%s); "
                                "proceeding without page enrichment",
                                ad.get("id"),
                                actor_id,
                                ad_account_id,
                            )
                            # same account-level lookup as the
                            # page_details-present branch (no per-ad round-trip).
                            ad_insights_list = account_ad_insights.get(str(ad["id"]), [])
                    else:
                        logging.warning(f"No actor_id found in creative_details for ad {ad.get('id')}. Skipping.")
                        continue
                else:
                    logging.warning(f"No creative_details found for ad {ad.get('id')}. Skipping.")
                    continue

                # Massage Data — platform-independent (renames, creative/page
                # extraction, targeting geocode). Runs once per ad; per-platform
                # fan-out happens after.
                ad = massage_ad_data(ad)

                # Stamp team metadata onto the ad before fan-out so every
                # platform clone inherits it.
                if team:
                    ad["team_site_url"] = team.get("team_url", "unknown")
                    ad["team_title"] = team.get("team_title", "unknown")
                    ad["team_country"] = _resolve_team_country(team) or "unknown"
                    ad["team_city"] = team.get("team_city", "unknown")
                    ad["team_esid"] = team.get("_id", "unknown")
                    ad["organization_title"] = team.get("organization_title", "Unknown")
                    ad["@timestamp"] = datetime.now(timezone.utc).isoformat()
                else:
                    # No matching team found
                    logging.warning(f"Team not found for ad {ad.get('ad_id')}.")
                    continue

                # fan out one ES doc per (ad, publisher_platform).
                # FB returns 0–4 rows per ad-day depending on which platforms
                # delivered. Each clone merges its platform's insights, then
                # compute_optimization_result reads per-platform spend/results.
                # with the ranged time_increment=1 fetch each row is a
                # (day, platform) cell; the row's own day drives the doc grain.
                if not ad_insights_list:
                    logging.info(f"No insights rows for ad {ad.get('ad_id')} on {window_label} — nothing to write")
                    continue

                for platform_insights in ad_insights_list:
                    platform = platform_insights.get("publisher_platform", "unknown")
                    # per-row day gate. The requested date set may be
                    # NON-contiguous (the skip removed settled middle
                    # days) while the Meta call covers [since..until]; dropping
                    # rows for uncovered days preserves the skip semantics.
                    row_day = platform_insights.get("insight_date_start") or since
                    if row_day not in wanted_days:
                        continue
                    # day-level force: a maturation-tail day must
                    # re-WRITE past the guard while other days keep the
                    # idempotency skip. Global force_refetch forces every day.
                    day_forced = force_refetch or (row_day in force_dates)
                    platform_ad = dict(ad)
                    platform_ad.update(platform_insights)
                    platform_ad = compute_optimization_result(platform_ad)

                    # per-tenant doc filter. Action metrics ride the
                    # always-requested actions/cost_per_action_type parents, so a
                    # disabled field can only be dropped here, from the doc this
                    # team stores (plain metrics were already out of the request).
                    for _dropped_field in _disabled_fields_for_team(platform_ad.get("team_esid")):
                        platform_ad.pop(_dropped_field, None)

                    # write any ad-day with DELIVERY (spend OR
                    # impressions), matching Ads Manager; the old insight_spend>0
                    # gate silently dropped zero-spend delivery days a second
                    # time after the fetch_ads discovery filter.
                    if _ad_day_had_delivery(platform_ad.get("insight_spend"), platform_ad.get("insight_impressions")):
                        fb_ad_account_esid = fb_ad_account.get("_id")
                        fb_app_esid = fb_app.get("_id")
                        team_esid = platform_ad.get("team_esid", "unknown")
                        ad_id = platform_ad.get("ad_id")

                        imported_file_path = f"/app/data/facebook/ad_insights_imported/{fb_ad_account_esid}/{fb_app_esid}/{team_esid}/{row_day}_{ad_id}_{platform}.json"
                        # an archived file is NOT proof the doc is in
                        # ES. The send→archive steps aren't gated on the send
                        # succeeding, so an interrupted/partial send can leave a
                        # file archived-but-unindexed; trusting the archive alone
                        # then skips the re-fetch forever (silent permanent loss).
                        # Skip ONLY when the file is archived AND the ES doc
                        # exists. The _id matches the sender's _build_doc_id:
                        # {ad_id}_{day}_{platform} in {team_esid}__fb_insights.
                        # The ES check fires only on the archived branch, so a
                        # normal first import pays no extra ES call.
                        es_index = build_data_index_name(team_esid, "fb_insights")
                        es_doc_id = f"{ad_id}_{row_day}_{platform}"
                        # a forced day bypasses the idempotency skip:
                        # a deliberate re-pull (manual backfill, mapping/field-catalog
                        # change, attribution maturation) MUST re-write so the fresh
                        # data overwrites the stored doc. Without this, the re-fetched
                        # data is discarded here and the doc is frozen (the bug: a
                        # forced re-pull re-hit FB but sent 0 documents).
                        if (
                            not day_forced
                            and os.path.exists(imported_file_path)
                            and indexed_doc_present(es_index, doc_id=es_doc_id)
                        ):
                            logging.info(f"Skipping file creation because {imported_file_path} already exists and is indexed.")
                        elif not day_forced and os.path.exists(imported_file_path):
                            logging.warning(
                                "recovery: %s is archived but ES doc %s is "
                                "MISSING from %s — re-writing for re-send.",
                                imported_file_path,
                                es_doc_id,
                                es_index,
                            )
                            file_name = f"/app/data/facebook/ad_insights/{fb_ad_account_esid}/{fb_app_esid}/{team_esid}/{row_day}_{ad_id}_{platform}.json"
                            save_data_to_file(platform_ad, file_name)
                        else:
                            file_name = f"/app/data/facebook/ad_insights/{fb_ad_account_esid}/{fb_app_esid}/{team_esid}/{row_day}_{ad_id}_{platform}.json"
                            save_data_to_file(platform_ad, file_name)
                        teams_with_data.add(team_esid)
                    else:
                        logging.info(
                            f"Ad {platform_ad.get('ad_id')} platform={platform} had no delivery "
                            f"(spend={platform_ad.get('insight_spend')}, impressions={platform_ad.get('insight_impressions')}) — skipping write"
                        )
            except Exception as e:
                # per-ad boundary. One bad ad must not kill the batch.
                logging.warning(
                    f"drop: ad_id={ad.get('id')} ad_name='{ad.get('name', 'unknown')}' "
                    f"ad_account={ad_account_id} dates={window_label} actor_id={ad.get('creative_details', {}).get('actor_id') if isinstance(ad.get('creative_details'), dict) else None} "
                    f"error={e!r}",
                    exc_info=True,
                )
                continue

    # all accounts done: report the full span (covers the integer-
    # division rounding of the per-account bumps above). on a bail-out
    # report only the accounts that finished (not the full span) so the pill reflects
    # partial progress, and stamp a backfill_bailout note the card/History reads.
    if bailed_out:
        _report_range_progress(progress_run_id, progress_days_base, len(dates), accounts_completed, accounts_total)
        _record_backfill_bailout(progress_run_id, accounts_completed, accounts_total, dates)
    else:
        _report_range_progress(progress_run_id, progress_days_base, len(dates), accounts_total, accounts_total)

    # Log cache statistics
    logging.info(f"Page details cache summary: {len(page_details_cache)} unique pages cached (avoided redundant API calls)")

    # Log page mapping analysis
    mapped_page_ids = set(page_id_to_team.keys())
    pages_not_encountered = mapped_page_ids - encountered_page_ids

    logging.info(
        f"Page mapping summary: {len(mapped_page_ids)} page IDs mapped to teams, {len(encountered_page_ids)} page IDs found in ads"
    )

    if pages_not_encountered:
        logging.warning(f"Found {len(pages_not_encountered)} page IDs in team mapping but no ads were found for them:")
        for page_id in sorted(pages_not_encountered):
            team = page_id_to_team.get(page_id, {})
            team_title = team.get("team_title", "Unknown")
            logging.warning(
                f"  - Page ID {page_id} (team: '{team_title}') - No ads found in the queried ad accounts for this date"
            )
    else:
        logging.info("All mapped page IDs were found in ads")

    # Delete any stale no_data markers for teams that just got real data
    # (a re-run with ds_facebook=true that previously wrote a marker
    # because the team had no ads — without this delete, the marker becomes
    # a zombie and inflates doc counts on the Import Status page).
    # once per requested day (both helpers are no-ops today;
    # the per-day call shape is preserved in case the subsystem is revived).
    for _day in dates:
        delete_stale_markers_for_date("*_fb_insights", _day, teams_with_data)

        # Write no_data marker docs for teams with zero ads on this date
        write_no_data_markers(_day, teams_with_data, "_fb_insights", team_mapping=scoped_page_id_to_team)


# Function to fetch ads for a given ad account and date or date range
def _ad_day_had_delivery(spend, impressions):
    """True when an ad-day spent OR delivered impressions.

    The delivery test both gates share (fetch_ads discovery + the per-ad write
    gate in main). Ads Manager includes zero-spend days with delivery, so
    "spend > 0" alone systematically understated every summed metric. Values
    arrive as strings from the API / as floats from reformat; absent or
    malformed values count as zero (fail-closed to "no delivery")."""
    for value in (spend, impressions):
        try:
            if float(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _budget_minor_to_major(value):
    """Meta returns lifetime_budget/daily_budget as strings in the
    account currency's MINOR units (cents), unlike insight spend which arrives
    in major units. Convert so the stored budget is directly comparable to
    insight_spend. Assumes an offset-100 currency (all current accounts are
    USD); a zero-decimal currency (e.g. JPY) would need Meta's currency-offset
    table — caveat recorded on the catalog entries. None on absent/zero/
    malformed input: Meta omits the field (or returns "0") on the level that
    does NOT hold the budget (CBO vs ad-set-budgeted), and absence is the
    signal the dashboard's campaign-level fallback keys on."""
    if value in (None, ""):
        return None
    try:
        minor = int(value)
    except (TypeError, ValueError):
        logging.warning(f"unparseable budget value {value!r} — dropping")
        return None
    if minor <= 0:
        return None
    return minor / 100.0


def fetch_ads(account_id, date, access_token=None, until=None):
    lib.logging.define("fb-get-insights")
    """
    Fetch ads that delivered on a specific date or across a date window.

    When `until` is provided, the FB delivery probe queries
    {since: date, until: until} so the weekly importer can ask "which ads
    ran at all during this week" with one call instead of seven. When
    `until` is None (default), behavior is unchanged: single-day probe.

    Uses two rounds of Facebook Batch API calls to fetch ad metadata, creative,
    campaign, and adset details — replacing the previous per-ad sequential SDK calls.

    Round 1: Batch all ad fields including nested creative and campaign expansion.
    Round 2: Batch all adset lookups using adset_id values from Round 1.

    For N ads, this reduces the call count from ~4N sequential calls to
    2 × ceil(N/50) batch calls.
    """
    token = access_token or _current_access_token
    ads_data = []

    # Normalize account_id for API calls
    normalized_account_id = normalize_ad_account_id(account_id)

    window_until = until or date
    window_label = date if window_until == date else f"{date}..{window_until}"

    # Step 1: Query insights to find ads that delivered on the date (unchanged)
    try:
        check_and_pause_for_account(account_id)
        insights = AdAccount(normalized_account_id).get_insights(
            fields=["ad_id", "spend", "impressions"],
            params={
                "time_range": {"since": date, "until": window_until},
                "level": "ad",
                "limit": 1000,
            },
        )

        # "ran" means DELIVERED, not "spent". Ads Manager includes
        # zero-spend ad-days with impressions (free/credited delivery, paused
        # ads still serving); the old spend>0 filter dropped them here (and the
        # write gate dropped them again) so impression/view/engagement sums
        # understated. impressions was already fetched and ignored.
        ads_that_ran = [
            insight["ad_id"] for insight in insights if _ad_day_had_delivery(insight.get("spend"), insight.get("impressions"))
        ]

    except FacebookRequestError as e:
        logging.error(f"Failed to fetch insights for account {account_id}: {e}")
        return []

    logging.info(f"Found {len(ads_that_ran)} ads that ran on {window_label}")

    if not ads_that_ran:
        return []

    # Step 2 (Round 1): Batch fetch all ad metadata with nested creative and campaign expansion.
    # This replaces N individual Ad.api_get() + AdCreative.api_get() + Campaign.api_get() calls.
    # also fetch effective_status — FB's computed delivery state
    # (factors in parent ad set / campaign / account). It can disagree with
    # the ad's own configured `status`; Data Studio's "Effective Delivery"
    # column reads effective_status, so mirroring it lets DEAM dashboards
    # match Data Studio without forcing dashboard authors to choose.
    ad_fields = (
        "id,name,account_id,adset_id,campaign_id,adlabels,bid_amount,"
        "preview_shareable_link,status,effective_status,created_time,"
        "creative{id,actor_id,title,body,image_url,video_id,object_story_spec},"
        "campaign{id,name,start_time,stop_time,lifetime_budget,daily_budget}"
    )
    check_and_pause_for_account(account_id)
    ad_requests = [(ad_id, ad_fields) for ad_id in ads_that_ran]
    ad_results = batch_fb_metadata(ad_requests, token)
    logging.info(f"Batch round 1: fetched metadata for {len(ad_results)}/{len(ads_that_ran)} ads")

    # Step 3 (Round 2): Batch fetch adset details using adset_id values from Round 1.
    adset_fields = (
        "id,name,targeting,optimization_goal,promoted_object,billing_event,bid_strategy,lifetime_budget,daily_budget"
    )
    adset_ids = list({v["adset_id"] for v in ad_results.values() if v.get("adset_id")})
    adset_results = {}
    if adset_ids:
        check_and_pause_for_account(account_id)
        adset_requests = [(aid, adset_fields) for aid in adset_ids]
        adset_results = batch_fb_metadata(adset_requests, token)
        logging.info(f"Batch round 2: fetched {len(adset_results)}/{len(adset_ids)} adsets")

    # Step 4: Assemble final ad_data structures from batch results
    for ad_id in ads_that_ran:
        ad_data = ad_results.get(str(ad_id))
        if not ad_data:
            logging.warning(f"No data returned from batch for ad {ad_id} — skipping")
            continue

        # Promote nested creative to creative_details key (matches downstream expectations)
        if "creative" in ad_data:
            ad_data["creative_details"] = ad_data.pop("creative")

        # Promote nested campaign fields to flat keys (matches downstream expectations)
        campaign_obj = ad_data.pop("campaign", None)
        if campaign_obj:
            ad_data["ad_campaign_name"] = campaign_obj.get("name")
            ad_data["ad_campaign_start_time"] = campaign_obj.get("start_time")
            ad_data["ad_campaign_stop_time"] = campaign_obj.get("stop_time")
            if "campaign_id" not in ad_data:
                ad_data["campaign_id"] = campaign_obj.get("id")
            # CBO campaigns hold the budget here; ad-set-budgeted
            # campaigns omit it. Set only when present so absence stays the
            # fallback signal.
            for _fb_key, _es_key in (
                ("lifetime_budget", "ad_campaign_lifetime_budget"),
                ("daily_budget", "ad_campaign_daily_budget"),
            ):
                _budget = _budget_minor_to_major(campaign_obj.get(_fb_key))
                if _budget is not None:
                    ad_data[_es_key] = _budget

        # Promote adset fields from Round 2 results
        adset_id = ad_data.get("adset_id")
        if adset_id:
            adset_info = adset_results.get(str(adset_id))
            if adset_info:
                ad_data["adset_name"] = adset_info.get("name")
                ad_data["adset_targeting"] = adset_info.get("targeting")
                ad_data["adset_optimization_goal"] = adset_info.get("optimization_goal")
                ad_data["adset_promoted_object"] = adset_info.get("promoted_object")
                ad_data["adset_billing_event"] = adset_info.get("billing_event")
                ad_data["adset_bid_strategy"] = adset_info.get("bid_strategy")
                # absent under Advantage campaign budget (CBO); the
                # campaign-level fields above carry the budget in that case.
                for _fb_key, _es_key in (
                    ("lifetime_budget", "adset_lifetime_budget"),
                    ("daily_budget", "adset_daily_budget"),
                ):
                    _budget = _budget_minor_to_major(adset_info.get(_fb_key))
                    if _budget is not None:
                        ad_data[_es_key] = _budget

        ads_data.append(ad_data)

    return ads_data


# Function to fetch creative details for a given creative ID
def fetch_creative_details(creative_id):
    lib.logging.define("fb-get-insights")
    try:
        creative = AdCreative(creative_id).api_get(
            fields=[
                "id",
                "actor_id",
                "title",
                "body",
                "image_url",
                "video_id",
                "object_story_spec",
            ]
        )
        return creative.export_all_data()
    except FacebookRequestError as e:
        logging.error(f"Failed to fetch creative details for {creative_id}: {e}")
        return None


# Function to fetch page details for a given actor_id
def fetch_page_details(actor_id):
    lib.logging.define("fb-get-insights")
    try:
        page = Page(actor_id).api_get(
            fields=[
                "name",
                "link",
                "engagement",
                "followers_count",
                "new_like_count",
                "website",
                "connected_instagram_account",
                "fan_count",
                "is_permanently_closed",
                "is_published",
                "is_unclaimed",
                "talking_about_count",
                "username",
                "verification_status",
            ]
        )
        logging.info(f"Successfully retrieved page details for page_id={actor_id}, name={page.get('name', 'unknown')}")
        return page.export_all_data()
    except FacebookRequestError as e:
        if e.api_error_code() == 10:  # Assuming error code 10 indicates access issues
            return "access_denied"
        # a dead legacy FB login (code 190) also fails page-detail
        # fetches; surface it once per run (page-only dashboards hit this path).
        _maybe_record_expired_fb_token(e)
        # follow-up — a code-100 here is a PERMISSION GRANT problem, not
        # a bug and not a token-ordering problem: this fetch uses the ad
        # account's already-initialized token, and Meta grants page-node access
        # at the Business Portfolio level. Raw-dumping the Graph exception (the
        # old behavior) made it read like a generic API failure and cost real
        # diagnosis time on one page-access case. Name the credential in use and
        # the fix, so the log says who needs to grant what.
        if e.api_error_code() == 100:
            logging.error(
                "Page details denied for page_id=%s using %s: the token lacks "
                "page-level access (needs pages_read_engagement, or Page Public "
                "Content/Metadata Access). Ad data is UNAFFECTED — only page "
                "enrichment (follower/fan counts, verification) is skipped. Fix: "
                "grant this login page access on the Meta Business Portfolio that "
                "owns the page, then re-authorize. Graph said: %s",
                actor_id,
                _page_detail_credential_label(),
                e.api_error_message() or e,
            )
            return None
        logging.error(f"Failed to fetch page details for {actor_id}: {e}")
        return None


def _page_detail_credential_label():
    """follow-up — describe the credential ``fetch_page_details`` just
    used, for the code-100 grant message.

    Page details ride the ad account's initialized token, so the credential is
    whichever the current import mode selected: the per-tenant
    ``de_connection`` in ``account_wide`` mode, or the legacy
    ``de_asset_fb_apps`` app in ``page_filtered`` mode. Fail-soft to a generic
    phrase — naming the credential must never break the import."""
    try:
        if _current_import_mode == "account_wide" and _current_connection_esid:
            label = _resolve_connection_label(_current_connection_esid)
            if label:
                return f"connection {label!r} ({_current_connection_esid})"
            return f"connection {_current_connection_esid}"
        if _current_fb_app_id:
            title = _resolve_fb_app_title(_current_fb_app_id)
            if title:
                return f"FB app {title!r} ({_current_fb_app_id})"
            return f"FB app {_current_fb_app_id}"
    except Exception:  # noqa: BLE001 — labeling must never break the import
        pass
    return "the ad account's token"


# Function to save data to a file
def save_data_to_file(data, file_name):
    os.makedirs(os.path.dirname(file_name), exist_ok=True)
    with open(file_name, "w") as f:
        json.dump(obj=data, fp=f, sort_keys=True)


def fetch_account_ad_insights(ad_account_id, date, until=None, fields=None):
    """fetch ad insights for an ENTIRE ad account in ONE paginated call.

    Replaces the per-ad ``fetch_ad_insights`` loop (N serial Graph round-trips per
    date) with a single ``AdAccount(...).get_insights(level="ad", ...)`` call that
    returns every (ad, publisher_platform) row in one paginated request — the same
    account-level pattern ``get_insights_window.py`` already uses at ad-set grain,
    and that ``fetch_ads`` step 1 already uses for spend discovery. Collapses ~N
    calls to O(pages).

    ``until`` extends the fetch to the whole ``[date..until]`` range
    with ``time_increment=1``: Meta returns per-ad PER-DAY rows, so an N-day
    backfill costs O(N / 30) calls per account instead of N calls across N
    subprocesses. Each row carries its own day (``insight_date_start`` after
    reformatting) — the caller keys the doc grain off that. Ranges longer than
    ``_RANGED_INSIGHTS_CHUNK_DAYS`` are split into slices so Meta's sync endpoint
    never sees an oversized result set. ``until=None`` (or == ``date``) keeps the
    single-day call byte-identical to the shape (no ``time_increment``).

    Returns ``dict[ad_id -> list[per-platform reformatted dict]]`` so the caller
    can look up each ad's rows by id and fan out per platform EXACTLY as the
    per-ad path did. The per-platform dicts are byte-identical to
    ``fetch_ad_insights`` output: same fields (``_DAILY_INSIGHT_FIELDS`` via
    ``reformat_insights``), same bare ``publisher_platform`` key, same
    ``insight_publisher_platform`` drop.

    ``ad_id`` is requested explicitly because it is NOT in
    ``_DAILY_INSIGHT_FIELDS``; it is read from the RAW row as the dict key (the
    join back to each ad) and its ``insight_ad_id`` reformatted dup is dropped so
    the written doc is unchanged — the doc's real ``ad_id`` still comes from the
    ad metadata via ``massage_ad_data`` in the caller. Mirrors the ``adset_id``
    handling in ``get_insights_window.py``.

    Rate-limit handling (subcode 1504022 backoff) is re-homed here from
    ``fetch_ad_insights`` and wraps each chunk's paginated call. A chunk that
    fails soft (non-transient error / retries exhausted) contributes no rows;
    the other chunks still merge.
    """
    lib.logging.define("fb-get-insights")
    window_until = until or date
    by_ad = {}
    for chunk_since, chunk_until in _chunk_date_range(date, window_until, _RANGED_INSIGHTS_CHUNK_DAYS):
        chunk = _fetch_account_ad_insights_chunk(ad_account_id, chunk_since, chunk_until, fields=fields)
        for ad_id, rows in chunk.items():
            by_ad.setdefault(ad_id, []).extend(rows)
    return by_ad


def _insights_request_params(since, until):
    """request params for the account-level daily Ads Insights call.

    ``use_unified_attribution_setting: true`` makes each action's ``value``
    follow the AD ACCOUNT's own attribution setting — what Ads Manager shows —
    instead of the API default. The old ``attribution_setting`` request param
    it replaces was never valid (it is a response FIELD name, still requested
    via the catalog; as a param Meta silently ignored it).

    Staging probe (2026-08-19): with two windows requested, each
    action item carries per-window values as EXTRA KEYS ("value", "7d_click",
    "1d_view") — items are NOT duplicated per window, so reformat_insights()'s
    read of item["value"] has no overwrite hazard.
    """
    params = {
        "time_range": {"since": since, "until": until},
        "level": "ad",
        "action_breakdowns": ["action_type", "action_video_type"],
        "action_attribution_windows": ["7d_click", "1d_view"],
        "use_unified_attribution_setting": True,
        "breakdowns": ["publisher_platform"],
        "limit": 1000,
    }
    if until != since:
        # per-day rows for the whole range in one paginated call.
        params["time_increment"] = 1
    return params


def _fetch_account_ad_insights_chunk(ad_account_id, since, until, fields=None):
    """One account-level insights call over ``[since..until]`` (body,
    parameterized by range). ``time_increment=1`` is added only for
    a multi-day range so the single-day call stays byte-identical.

    ``fields`` is the per-tenant request list (defaults to the global
    catalog list for callers with no tenant context)."""
    insight_fields = fields if fields is not None else _DAILY_INSIGHT_FIELDS
    normalized_account_id = normalize_ad_account_id(ad_account_id)
    request_params = _insights_request_params(since, until)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            insights = AdAccount(normalized_account_id).get_insights(
                fields=["ad_id", *insight_fields],
                params=request_params,
            )

            by_ad = {}
            for row in insights:  # SDK Cursor auto-paginates on iteration
                ad_id = row.get("ad_id")
                if not ad_id:
                    continue
                platform = row.get("publisher_platform")
                reformatted = reformat_insights(row)
                if not reformatted:
                    continue
                # reformat_insights prefixes scalars with "insight_"; drop the
                # prefixed identity dups so the doc matches the per-ad path. The
                # bare publisher_platform (set below) is what the fan-out reads.
                reformatted.pop("insight_publisher_platform", None)
                reformatted.pop("insight_ad_id", None)
                if platform:
                    reformatted["publisher_platform"] = platform
                by_ad.setdefault(str(ad_id), []).append(reformatted)
            return by_ad
        except FacebookRequestError as e:
            if e.api_error_subcode() == 1504022:
                # a backfill bails out on the first rate-limit signal
                # instead of sleeping/retrying: stop, keep what's written, let the
                # user re-run to continue. Daily/scheduled runs (flag off) keep the
                # exponential backoff below.
                if _backfill_bailout:
                    logging.warning(
                        "backfill: rate limit fetching account-level insights for %s; "
                        "bailing out to keep partial progress.",
                        ad_account_id,
                    )
                    raise _RateLimitBailout(f"rate limited fetching insights for {ad_account_id}") from e
                backoff = 60 * (2**attempt)  # 60s, 120s, 240s
                logging.warning(
                    f"rate limit hit fetching account-level insights for {ad_account_id} "
                    f"(attempt {attempt + 1}/{max_retries}). Sleeping {backoff}s before retry."
                )
                time.sleep(backoff)
                check_and_pause_for_account(ad_account_id)
            else:
                # surface an expired/dead FB login (code 190) to the
                # Report Scheduler once per run; the legacy [fb-check-token] job
                # can't (separate subprocess, no run_id). Other errors just log.
                _maybe_record_expired_fb_token(e)
                logging.error(f"failed to fetch account-level insights for {ad_account_id}: {e}")
                return {}
    logging.error(f"failed to fetch account-level insights for {ad_account_id} after {max_retries} retries.")
    return {}


# Function to reformat insights data
def reformat_insights(insights):
    lib.logging.define("fb-get-insights")
    reformatted_data = {}

    for key, value in insights.items():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            for item in value:
                if "action_type" in item and "value" in item:
                    action_type_clean = item["action_type"].replace(".", "_")
                    if "action_video_type" in item:
                        video_type_clean = item["action_video_type"].replace(".", "_")
                        new_key = f"insight_{key}_{action_type_clean}_{video_type_clean}"
                    elif key == "cost_per_action_type":
                        new_key = f"insight_cost_per_action_type_{action_type_clean}"
                    elif key == "actions":
                        new_key = f"insight_actions_{action_type_clean}"
                    else:
                        # every OTHER list-valued field keys by its own
                        # source name. The old catch-all funneled them all into
                        # insight_actions_<type>, colliding with the real actions
                        # family (cost_per_thruplay's cost overwrote the
                        # insight_actions_video_view count, video percentile
                        # counts overwrote each other, conversion custom types
                        # leaked into the actions namespace).
                        new_key = f"insight_{key}_{action_type_clean}"
                    reformatted_data[new_key] = float(item["value"])
        elif isinstance(value, (str, int, float)):
            reformatted_key = f"insight_{key}"
            reformatted_data[reformatted_key] = value

    return reformatted_data


_KAN1269_PAGE_NAMES = None


def _inventoried_page_names():
    """``{fb_page_id: fb_page_name}`` from de_asset_fb_pages.

    Fetched lazily ONCE per run (module cache; the importer is a per-run
    subprocess) and only consulted when the live Page-node read left
    ``page_name`` absent. Mirrors the window importer's ``_page_name_map``.
    Fail-open: names are cosmetic, page_id always lands.
    """
    global _KAN1269_PAGE_NAMES
    if _KAN1269_PAGE_NAMES is None:
        try:
            _KAN1269_PAGE_NAMES = {
                str(p["_source"].get("fb_page_id")): p["_source"].get("fb_page_name")
                for p in (get_fb_pages() or [])
                if p["_source"].get("fb_page_id") is not None and p["_source"].get("fb_page_name")
            }
        except Exception as exc:  # noqa: BLE001 — names are cosmetic; ids always land
            logging.warning("fb_pages name lookup failed: %s — docs keep page_id only.", exc)
            _KAN1269_PAGE_NAMES = {}
    return _KAN1269_PAGE_NAMES


def massage_ad_data(ad):
    lib.logging.define("fb-get-insights")

    # This dictionary replaces fb keys with my es keys. eg: id becomes ad_id
    ad_mapping = {
        "id": "ad_id",
        "name": "ad_name",
        "account_id": "ad_account_id",
        "campaign_id": "ad_campaign_id",
        "preview_shareable_link": "ad_preview",
        "status": "ad_status",
        "created_time": "ad_created",
    }

    for old_key, new_key in ad_mapping.items():
        if old_key in ad:
            ad[new_key] = ad.pop(old_key)

    # Extract campaign_name from campaign object
    if "campaign" in ad:
        campaign = ad.pop("campaign")
        if "name" in campaign:
            ad["ad_campaign_name"] = campaign["name"]
        if "start_time" in campaign:
            ad["ad_campaign_start_time"] = campaign["start_time"]
        if "stop_time" in campaign:
            ad["ad_campaign_stop_time"] = campaign["stop_time"]

    if "creative" in ad and "id" in ad["creative"]:
        ad["creative_id"] = ad.pop("creative")["id"]

    # remember the creative's actor_id before creative_details is
    # popped below. It IS the delivering Page's id and is fetched for every ad
    # (it drives team mapping), so it backstops page_id when the Page-node read
    # is unavailable.
    creative_actor_id = None
    if "creative_details" in ad:
        creative_details = ad["creative_details"]
        creative_actor_id = creative_details.get("actor_id")
        if "object_story_spec" in creative_details:
            link_data = creative_details["object_story_spec"].get("link_data", {})
            ad["ad_link_href"] = link_data.get("link")
            ad["ad_link_name"] = link_data.get("name")
            call_to_action = link_data.get("call_to_action", {})
            ad["ad_call_to_action_type"] = call_to_action.get("type")
            if "value" in call_to_action and "app_destination" in call_to_action["value"]:
                ad["ad_call_to_action_app_destination"] = call_to_action["value"]["app_destination"]
        ad["creative_title"] = creative_details.get("title")
        ad["creative_body"] = creative_details.get("body")
        ad["creative_video_id"] = creative_details.get("video_id")
        ad["creative_image_url"] = creative_details.get("image_url")
        # Extract page_username if available
        ad["page_username"] = creative_details.get("page_username")
    ad.pop("creative_details", None)

    if "page_details" in ad:
        page_mapping = {
            "id": "page_id",
            "name": "page_name",
            "link": "page_link",
            "followers_count": "page_followers_count",
            "fan_count": "page_fan_count",
            "is_permanently_closed": "page_is_permanently_closed",
            "is_published": "page_is_published",
            "is_unclaimed": "page_is_unclaimed",
            "talking_about_count": "page_talking_about_count",
            "username": "page_username",
            "verification_status": "page_verification_status",
        }
        # work on a local copy. ad["page_details"] is a reference to
        # the same dict stored in page_details_cache; .pop() on it would empty
        # the cached entry, so the next ad on the same page would get {} from
        # the cache, fail the `if page_details and ...` guard in the per-ad
        # loop, skip fetch_ad_insights, and be silently dropped.
        page_details_local = dict(ad["page_details"])
        for old_key, new_key in page_mapping.items():
            if old_key in page_details_local:
                ad[new_key] = page_details_local.pop(old_key)
        ad.pop("page_details", None)

    # account-wide wizard tokens carry pages_show_list but
    # not pages_read_engagement, so fetch_page_details returns None and the
    # block above never runs: those teams' docs had NO page fields at all,
    # silently excluding them from every page_id row/sub-row grouping
    #. The creative actor_id is the same Page id, so stamp it as the
    # grouping key.
    if not ad.get("page_id") and creative_actor_id:
        ad["page_id"] = str(creative_actor_id)

    # when the Page-node read was denied (or otherwise left the
    # page fields absent), fall back to the DEAM inventory name so page_name
    # groupings keep the page (one page vanished from page_name tables
    # while page_id showed it). Mirrors the window flush. Pages
    # outside the inventory keep page_id-only behavior.
    if ad.get("page_id") and not ad.get("page_name"):
        inventory_name = _inventoried_page_names().get(str(ad["page_id"]))
        if inventory_name:
            ad["page_name"] = inventory_name

    # Geocode targeting location levels for map visualization.
    # cache_only=True keeps city/region resolution off the live
    # Nominatim path so a cold cache or new-targeting day never stalls the
    # importer on the 1 req/sec rate-limit wall. Offline-centroid levels
    # (custom_locations, countries) are unaffected — country + radius dots
    # still render. Out-of-band city/region population:.
    targeting = ad.get("adset_targeting", {})
    geo_levels = geocode_all_targeting_levels(targeting, cache_only=True)
    if "city" in geo_levels:
        ad["location_cities"] = geo_levels["city"]
    if "region" in geo_levels:
        ad["location_regions"] = geo_levels["region"]
    if "country" in geo_levels:
        ad["location_countries"] = geo_levels["country"]

    return ad


def compute_optimization_result(ad):
    """
    Compute the result count based on the optimization goal.
    Maps optimization_goal to the appropriate action metric.
    """
    OPTIMIZATION_GOAL_MAP = {
        "LINK_CLICKS": {
            "action_field": "insight_actions_link_click",
            "fallback_field": "insight_inline_link_clicks",
            "cost_field": "insight_cost_per_action_type_link_click",
            "label": "Link Clicks",
        },
        "CONVERSATIONS": {
            "action_field": "insight_actions_onsite_conversion_messaging_conversation_started_7d",
            "fallback_field": None,
            "cost_field": "insight_cost_per_action_type_onsite_conversion_messaging_conversation_started_7d",
            "label": "Conversations Started",
        },
        # REPLIES (optimize for message replies) reports its Results
        # as messaging conversations started, exactly like CONVERSATIONS — the
        # Ads Manager convention ("Results: … / Messaging conversation"). It was
        # unmapped, so every REPLIES-optimized ad set showed blank Results on
        # every DEAM surface (found via the asset-grain validation: the MMH
        # LUMO ad sets are REPLIES-optimized).
        "REPLIES": {
            "action_field": "insight_actions_onsite_conversion_messaging_conversation_started_7d",
            "fallback_field": None,
            "cost_field": "insight_cost_per_action_type_onsite_conversion_messaging_conversation_started_7d",
            "label": "Conversations Started",
        },
        "LANDING_PAGE_VIEWS": {
            "action_field": "insight_actions_landing_page_view",
            "fallback_field": None,
            "cost_field": "insight_cost_per_action_type_landing_page_view",
            "label": "Landing Page Views",
        },
        "POST_ENGAGEMENT": {
            "action_field": "insight_actions_post_engagement",
            "fallback_field": "insight_inline_post_engagement",
            "cost_field": "insight_cost_per_action_type_post_engagement",
            "label": "Post Engagements",
        },
        "IMPRESSIONS": {
            "action_field": None,
            "fallback_field": "insight_impressions",
            "cost_field": None,
            "label": "Impressions",
        },
        "REACH": {
            "action_field": None,
            "fallback_field": "insight_reach",
            "cost_field": None,
            "label": "People Reached",
        },
        "LEAD_GENERATION": {
            "action_field": "insight_actions_onsite_conversion_lead_grouped",
            "fallback_field": None,
            "cost_field": "insight_cost_per_action_type_onsite_conversion_lead_grouped",
            "label": "Leads",
        },
        "OFFSITE_CONVERSIONS": {
            "action_field": "insight_actions_offsite_conversion_fb_pixel_purchase",
            "fallback_field": None,
            "cost_field": "insight_cost_per_action_type_offsite_conversion_fb_pixel_purchase",
            "label": "Conversions",
        },
        "THRUPLAY": {
            # the TRUE ThruPlay count (played to completion or >=15s),
            # requested via the catalog since 2026-08-19. The _total suffix is
            # what reformat_insights() actually emits: the video family arrives
            # with action_video_type='total' (verified on staging docs — the
            # suffix-less form appears in 0 docs). The old primary,
            # insight_actions_video_view, is a 3-SECOND view count that
            # overstated "results" for THRUPLAY goals; it remains only as the
            # fallback for docs ingested before the field existed.
            "action_field": "insight_video_thruplay_watched_actions_video_view_total",
            "fallback_field": "insight_actions_video_view",
            # cost_per_thruplay is list-valued at the API; the flattened
            # key carries its action_type. The bare insight_cost_per_thruplay this
            # used to read never existed on any doc.
            "cost_field": "insight_cost_per_thruplay_video_view",
            "label": "ThruPlays",
        },
    }

    # Mapping of optimization goals to their specific cost field names
    COST_FIELD_MAP = {
        "LINK_CLICKS": "cost_result_link_clicks",
        "CONVERSATIONS": "cost_result_conversations",
        "REPLIES": "cost_result_conversations",  # same result family
    }

    # Get optimization goal (prefer adset level, fall back to insight level)
    optimization_goal = ad.get("adset_optimization_goal") or ad.get("insight_optimization_goal")

    if not optimization_goal or optimization_goal not in OPTIMIZATION_GOAL_MAP:
        ad["result_type"] = None
        ad["result_count"] = None
        ad["result_label"] = None
        ad["cost_per_result"] = None
        return ad

    goal_config = OPTIMIZATION_GOAL_MAP[optimization_goal]

    # Try action field first, then fallback
    result_count = None
    if goal_config["action_field"]:
        result_count = ad.get(goal_config["action_field"])
    if result_count is None and goal_config["fallback_field"]:
        result_count = ad.get(goal_config["fallback_field"])

    ad["result_type"] = optimization_goal
    ad["result_count"] = float(result_count) if result_count is not None else 0
    ad["result_label"] = goal_config["label"]

    # Initialize goal-specific cost fields to 0
    ad["cost_result_conversations"] = 0
    ad["cost_result_link_clicks"] = 0

    # Compute cost per result
    cost_per_result = None
    if goal_config.get("cost_field"):
        cost_per_result = ad.get(goal_config["cost_field"])
        if cost_per_result is not None:
            logging.debug(f"Using Facebook cost_per_result from {goal_config['cost_field']}: {cost_per_result}")

    # Fallback: calculate from spend / result_count
    if cost_per_result is None and ad["result_count"] > 0:
        spend = float(ad.get("insight_spend", 0)) if ad.get("insight_spend") else 0
        if spend > 0:
            cost_per_result = spend / ad["result_count"]
            logging.debug(
                f"Calculated cost_per_result from spend/result_count: {cost_per_result} (field {goal_config.get('cost_field')} not found)"
            )
        else:
            logging.warning(f"Cannot calculate cost_per_result: spend is {spend}, result_count is {ad['result_count']}")

    ad["cost_per_result"] = float(cost_per_result) if cost_per_result else 0

    # Store in goal-specific field
    if optimization_goal in COST_FIELD_MAP:
        specific_field = COST_FIELD_MAP[optimization_goal]
        ad[specific_field] = float(cost_per_result) if cost_per_result else 0
        logging.debug(f"Stored cost in {specific_field}: {ad[specific_field]}")

    return ad


if __name__ == "__main__":
    # Test standalone execution
    # Usage: python get_insights.py --date 2024-01-15 [--fb_account_ids id1,id2] [--fb_ad_account_ids id1] [--team_ids id1]
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Facebook ad insights")
    parser.add_argument("--date", default=None, help="Single date in YYYY-MM-DD format")
    parser.add_argument("--dates", default=None, help="Comma-separated YYYY-MM-DD dates")
    parser.add_argument("--force_dates", default=None, help="Comma-separated dates to force-overwrite")
    parser.add_argument("--backfill_bailout", default=None, help="Bail out on rate limit, keep partial data")
    parser.add_argument("--fb_account_ids", default=False, help="Comma-separated FB account IDs")
    parser.add_argument("--fb_ad_account_ids", default=False, help="Comma-separated FB ad account IDs")
    parser.add_argument("--fb_app_ids", default=False, help="Comma-separated FB app IDs")
    parser.add_argument("--team_ids", default=False, help="Comma-separated team IDs")
    parser.add_argument("--fb_page_id", default=False, help="Single FB page ID for debugging")

    args = parser.parse_args()

    params = {
        "date": args.date,
        "dates": args.dates,
        "force_dates": args.force_dates,
        "backfill_bailout": args.backfill_bailout,
        "fb_account_ids": args.fb_account_ids,
        "fb_ad_account_ids": args.fb_ad_account_ids,
        "fb_app_ids": args.fb_app_ids,
        "team_ids": args.team_ids,
        "fb_page_id": args.fb_page_id,
    }

    main(params)
