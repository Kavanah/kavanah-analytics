import base64
import json
import os
import sys
import subprocess
import requests
import time
import yaml
from datetime import datetime, timezone


# Function to install a package if it is missing
def install_package(package_name):
    try:
        __import__(package_name)
    except ImportError:
        print(f"Installing {package_name}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])


# Install necessary packages
install_package("flask")
install_package("gunicorn")
install_package("requests")
install_package("elasticsearch")

# Import Flask and other modules after ensuring they are installed
from flask import Flask, request, jsonify, abort, make_response
from markupsafe import escape as html_escape
from werkzeug.exceptions import HTTPException
from lib.es_helpers import get_elasticsearch_client
from lib.encryption import decrypt, encrypt
from lib.google_oauth_client import load_google_oauth_client
from lib.fb_constants import FB_GRAPH_URL, FB_OAUTH_DIALOG_URL
import lib.oauth_state as oauth_state_lib
from lib.oauth_state import new_oauth_state, oauth_state_expired, oauth_state_expiry
from lib.tiktok_client import (
    TikTokError,
    exchange_code as tiktok_exchange_code,
    get_user_info as tiktok_get_user_info,
    granted_scopes as tiktok_granted_scopes,
    load_client_credentials as tiktok_load_client_credentials,
    refresh_expiry_iso as tiktok_refresh_expiry_iso,
)
from lib.tiktok_constants import (
    AUTHORIZE_URL as TIKTOK_AUTHORIZE_URL,
    CALLBACK_PATH as TIKTOK_CALLBACK_PATH,
    REQUIRED_SCOPES as TIKTOK_REQUIRED_SCOPES,
    SCOPES as TIKTOK_SCOPES,
    tiktok_enabled,
)

# Set up logging for OAuth debugging
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fb_oauth")

# this process exchanges and stores OAuth tokens, so it gets the
# same handler filters as DEAM and the importers: CR/LF scrubbing
# and credential redaction (requests errors repeat token-bearing URLs).
from lib.log_filters import install_filter_on_all_handlers  # noqa: E402 — after basicConfig

install_filter_on_all_handlers()

# Create and configure the Flask app
app = Flask(__name__)


@app.route("/")
def home():
    return "Page not found", 200


@app.route("/status", methods=["GET"])
def status():
    return {"status": "running"}, 200


YOUTUBE_OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"
YOUTUBE_SCOPES = [
    # openid (non-sensitive; no re-verification of the
    # client needed) makes the token response carry an id_token whose `sub`
    # identifies the AUTHORIZED identity. One Gmail can front several Brand
    # Account tiles at Google's chooser, and each tile is a distinct identity
    # (distinct sub) whose token serves only its own channels — the email can
    # never discriminate them, so the callback keys identity on the sub.
    "openid",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# GA4 OAuth shares the SAME Google client as YouTube
# (youtube-oauth-client.json.enc). verifies one OAuth client for all
# sensitive scopes (youtube.readonly, yt-analytics.readonly, analytics.readonly,
# adwords), so the GA4 authorize leg reuses the YouTube client and only adds the
# analytics.readonly scope.
GA4_OAUTH_CLIENT_PATH = YOUTUBE_OAUTH_CLIENT_PATH
GA4_SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
]

# Google Ads per-user OAuth shares the SAME verified Google client
# as YouTube/GA4 (verifies one client for all sensitive scopes,
# including adwords). OAuth replaces only the service-account IDENTITY; the
# Google Ads developer_token + login_customer_id are still required by the API
# regardless of auth method. Unlike the GA4/YouTube legs, the token
# lands on a de_connection row (not a token-carrier asset doc) — the wizard
# seeds oauth_state on a pending connection and this host looks it up.
GOOGLE_ADS_OAUTH_CLIENT_PATH = YOUTUBE_OAUTH_CLIENT_PATH
GOOGLE_ADS_SCOPES = [
    "https://www.googleapis.com/auth/adwords",
]


def get_fboauth_hostname():
    """Retrieve the base URL from the master-config.yaml file."""
    config_path = "/app/reporting_configs/master-config.yaml"
    try:
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
            base_url = config.get("fboauth", {}).get("hostname", "")
            return base_url if base_url else ""
    except Exception as e:
        print(f"Error reading base URL from config: {e}")
        return ""


def _oauth_success_page(reauth=False):
    """Self-closing HTML page returned after a successful OAuth callback
    (shared by the FB / YouTube / GA4 / Google Ads legs).

    ``reauth=True`` swaps the copy for an in-place reauthorization,
    where the existing account/channel selection is reused and there is NOTHING
    further to confirm. The default copy below deliberately pushes the user back
    into the wizard, which would be a dead end for a reauth.

    The data-connectors wizard opens the OAuth URL in a popup and polls
    the user-status endpoint; ``window.close()`` lets the popup close
    itself so the wizard tab can advance. The visible fallback text
    covers the (rare) case where the user opened the URL in a regular
    tab — modern browsers block scripted window.close() on non-popup
    contexts, so the message has to stand on its own.

    the copy must NOT read as "you're done." Authorization only
    grants the token; the connector is not configured until the user returns
    to the wizard and finishes selecting accounts/channels/properties (that
    Confirm step is what writes the de_connection the connector card requires).
    The old "Authorized — you can close this window" told users to stop early,
    leaving a token with no connection (the card then read "disconnected").
    """
    if reauth:
        headline = "&#10003; Reauthorized"
        detail = "Your access has been refreshed. This window closes on its own &mdash; nothing else is needed."
    else:
        headline = "&#10003; Authorized"
        detail = (
            "Return to the connector wizard to finish setup &mdash; "
            "your connection isn&rsquo;t saved until you select your accounts and confirm."
        )
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Authorized</title></head><body>"
        "<div style='font-family:system-ui,sans-serif;font-size:16px;"
        "max-width:480px;margin:48px auto;text-align:center;line-height:1.5;'>"
        "<p style='font-size:17px;font-weight:600;margin:0 0 8px;'>" + headline + "</p>"
        "<p style='margin:0;'>" + detail + "</p></div>"
        "<script>setTimeout(function(){try{window.close();}catch(e){}}, 800);</script>"
        "</body></html>"
    )
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


_YOUTUBE_RETRY_HINT = (
    "Close this window and try again, choosing the channel's own "
    "<strong>Brand Account tile</strong> at the Google account chooser. "
    "Owner access to the channel is required (Manager access is not enough)."
)


def _oauth_error_page(message, status=403, hint=_YOUTUBE_RETRY_HINT):
    """user-facing HTML error page for a REFUSED OAuth callback.

    Unlike ``_oauth_success_page`` this does NOT self-close: the user must read
    why the authorization was rejected (wrong Brand Account tile selected at
    Google's chooser) before retrying. ``message`` is escaped — it can embed
    channel titles that came from the YouTube API.

    ``hint`` is trusted static HTML from this module — never pass
    provider or request data in it. The default is the YouTube retry advice.
    """
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Authorization not saved</title></head><body>"
        "<div style='font-family:system-ui,sans-serif;font-size:15px;"
        "max-width:560px;margin:48px auto;'>"
        "<p style='font-size:17px;font-weight:600;'>&#9888; Authorization not saved</p>"
        f"<p>{html_escape(message)}</p>"
        f"<p>{hint}</p></div></body></html>"
    )
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


# ── YouTube authorized-identity capture ───────────────────────────
#
# One de_asset_youtube_accounts doc holds ONE refresh token, but the Google
# email on the doc can front several Brand Account identities — each a separate
# tile at Google's OAuth chooser, each minting a token that the YouTube
# Analytics API serves ONLY for the channels that identity owns. Re-authorizing
# a doc as a different tile silently overwrote the token last-writer-wins and
# 403-broke every channel the previous identity owned (the partner-app
# ping-pong: three owner identities in three days on one token slot).
#
# The callback therefore probes WHO was just authorized (id_token `sub` +
# `channels.list(mine=true)` owned-channel set) and:
#   1. refuses a token that owns no channel at all (structurally useless for
#      YouTube Analytics — the manager-not-owner case),
#   2. refuses to overwrite a tokened doc when the new identity does not own
#      the channels already linked to it (the ping-pong guard),
#   3. lands the token on the EXISTING doc for this identity when one exists
#      (post-callback identity dedup — the typed email can't discriminate
#      tiles, so step-1 email dedup alone would mint one doc per retry), and
#   4. stamps the doc with the identity + owned-channel set so 2/3 have data
#      to compare against on the next re-auth.
# Every probe is fail-soft: if Google can't tell us who authorized (network
# error, probe 5xx), the callback saves the token exactly as.

_YT_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def _probe_youtube_token_identity(token_response):
    """Return ``(sub, owned)`` for a fresh token-endpoint response.

    ``sub`` — the authorized Google identity id from the id_token (openid
    scope), or ``""`` when absent/undecodable. The id_token came straight from
    Google's token endpoint over TLS, so its payload is read without signature
    verification (standard practice for the direct token-exchange leg).

    ``owned`` — list of ``{"id", "title"}`` channels the token's identity OWNS
    via ``channels.list(mine=true)``; ``[]`` when the identity owns no channel
    (the API 401s with reason ``youtubeSignupRequired`` for identities that
    have no channel); ``None`` when the probe failed and ownership is UNKNOWN
    (callers must fail-soft to the earlier behavior).
    """
    sub = ""
    id_token = token_response.get("id_token") or ""
    if id_token:
        try:
            payload_b64 = id_token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            sub = str(json.loads(base64.urlsafe_b64decode(payload_b64)).get("sub") or "")
        except Exception as e:
            logger.warning(f"id_token decode failed: {e}")

    access_token = token_response.get("access_token") or ""
    if not access_token:
        return sub, None
    try:
        resp = requests.get(
            _YT_CHANNELS_URL,
            params={"part": "id,snippet", "mine": "true", "maxResults": 50},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except Exception as e:
        logger.warning(f"owned-channel probe failed: {e}")
        return sub, None

    if resp.status_code == 200:
        items = (resp.json() or {}).get("items") or []
        owned = [{"id": str(i.get("id")), "title": (i.get("snippet") or {}).get("title") or ""} for i in items if i.get("id")]
        return sub, owned
    if resp.status_code == 401 and "youtubeSignupRequired" in (resp.text or ""):
        # The authorized identity has no YouTube channel at all.
        return sub, []
    logger.warning(f"owned-channel probe HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    return sub, None


def _dual_term(field, value):
    """Bool-should term clause matching ``field`` on BOTH the bare field and its
    ``.keyword`` sub-field. The de_asset_* mappings differ per environment
    (prod: text+keyword; staging: bare keyword — the trap
    class); a term on a missing sub-field matches nothing rather than erroring,
    so OR-ing both shapes works everywhere without a mapping probe.
    """
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {f"{field}.keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


def _youtube_linked_channel_ids(es, account_id):
    """Channel ids of de_asset_youtube_channels rows linked to this account doc,
    as ``{channel_id: title}``. Fail-soft: {} on error (the overwrite guard is
    then skipped — earlier behavior — rather than blocking a save)."""
    try:
        result = es.search(
            index="de_asset_youtube_channels",
            body={"query": _dual_term("youtube_account_esid", account_id), "size": 500},
        )
        linked = {}
        for hit in result.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            cid = str(src.get("youtube_channel_id") or "").strip()
            if cid:
                linked[cid] = src.get("youtube_channel_title") or cid
        return linked
    except Exception as e:
        logger.warning(f"linked-channel lookup failed for account '{account_id}': {e}")
        return {}


def _find_youtube_account_by_identity(es, sub, exclude_id):
    """(doc_id, source) of an EXISTING de_asset_youtube_accounts row already
    stamped with this authorized identity, or None. Fail-soft: None on error."""
    if not sub:
        return None
    try:
        result = es.search(
            index="de_asset_youtube_accounts",
            body={"query": _dual_term("youtube_account_identity", sub), "size": 5},
        )
        for hit in result.get("hits", {}).get("hits", []):
            # Exclude in code, not only in the query — belt and braces against
            # mapping-shape surprises returning the state doc itself.
            if hit.get("_id") and hit["_id"] != exclude_id:
                return hit["_id"], hit.get("_source") or {}
        return None
    except Exception as e:
        logger.warning(f"identity-dedup lookup failed: {e}")
        return None


def _find_youtube_account_by_owned_overlap(es, owned_ids, exclude_id):
    """(doc_id, source) of an EXISTING de_asset_youtube_accounts row whose
    stamped owned-channel set OVERLAPS ``owned_ids``, restricted to docs that
    carry NO ``youtube_account_identity``. Or None. Fail-soft: None on error.

    the sub-miss fallback. Legacy grants (pre-openid scope) yield no
    id_token ``sub``, so ``_find_youtube_account_by_identity`` is blind for the
    handful of prod docs could only stamp with owned-channel sets. When
    a same-identity re-auth comes back sub-less, match it to its existing doc by
    owned-channel overlap instead of minting a twin (the bug shape).

    Guards:
      - identity-less docs ONLY. A doc already stamped with a DIFFERENT sub is
        a different identity — merging recreates the ping-pong. Those
        are matched (or refused) by the sub path, never here.
      - empty incoming owned set never matches (caller guarantees ``owned_ids``
        is non-empty before calling); a candidate with an empty stamped set
        can't overlap and is skipped.
      - the ping-pong guard on linked channels still runs at the call site
        before the token lands.
    """
    if not owned_ids:
        return None
    try:
        result = es.search(
            index="de_asset_youtube_accounts",
            body={
                "query": {"terms": {"youtube_account_owned_channel_ids": sorted(owned_ids)}},
                "size": 25,
            },
        )
        for hit in result.get("hits", {}).get("hits", []):
            if not hit.get("_id") or hit["_id"] == exclude_id:
                continue
            src = hit.get("_source") or {}
            # Identity-less docs only — never touch a doc that already names a
            # (necessarily different) identity.
            if src.get("youtube_account_identity"):
                continue
            stamped = {str(c) for c in (src.get("youtube_account_owned_channel_ids") or [])}
            if stamped & owned_ids:
                return hit["_id"], src
        return None
    except Exception as e:
        logger.warning(f"owned-overlap dedup lookup failed: {e}")
        return None


def _clear_youtube_reauth_flag(es, account_id, source, was_reauth):
    """persist the removal of ``youtube_account_reauth_pending`` on a
    path that does NOT otherwise re-index the doc (the refusals).

    ``source`` has already had the key popped in memory; this writes that back.
    No-op when the flag wasn't set. Fail-soft: a failure here only risks
    mislabeled success copy on a later run, never the token itself, so it must
    not turn a clean refusal into a 500.
    """
    if not was_reauth:
        return
    try:
        es.index(index="de_asset_youtube_accounts", id=account_id, document=source)
    except Exception as e:  # noqa: BLE001 — cosmetic flag; never mask the refusal
        logger.warning(f"could not clear reauth flag on {account_id}: {e}")


def _stamp_youtube_identity(source, sub, owned):
    """Stamp the authorized identity + owned-channel set onto an account source
    (mutates in place; caller indexes the doc)."""
    if sub:
        source["youtube_account_identity"] = sub
    source["youtube_account_owned_channel_ids"] = [c["id"] for c in owned]
    source["youtube_account_owned_channel_titles"] = [c["title"] for c in owned]
    source["youtube_account_identity_checked"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


# ── per-attempt, time-boxed OAuth state ───────────────────────────
#
# The FB / YouTube / GA4 legs correlate a provider callback to an asset doc by
# the ``state`` stored on that doc. Until that state was minted once,
# at asset creation, with ``random.choices``, and honoured forever. Now the
# INITIATE leg mints a fresh CSPRNG state on every click and stamps
# ``<state_field>_expires_at`` (10 min parity); the CALLBACK refuses a
# state past its expiry BEFORE it spends the provider code, and clears the
# state on success so each value is single-use. A doc that predates this
# carries no expiry and keeps working until its next initiate click re-mints.


def _issue_oauth_state(es, index, doc_id, state_field):
    """Mint a fresh state, persist it (+ expiry) on the asset doc, return it."""
    state = new_oauth_state()
    es.update(
        index=index,
        id=doc_id,
        doc={state_field: state, f"{state_field}_expires_at": oauth_state_expiry()},
        refresh="wait_for",
    )
    return state


def _clear_oauth_state(es, index, doc_id, state_field, label):
    """Best-effort: null the state + expiry on a doc whose state was refused."""
    try:
        es.update(index=index, id=doc_id, doc={state_field: None, f"{state_field}_expires_at": None})
    except Exception as e:  # noqa: BLE001 — the refusal already stands; never turn it into a 500
        logger.warning(f"{label}: could not clear expired state on {doc_id}: {e}")


def _expired_state_response(es, index, doc_id, source, state_field, label):
    """``(response, 400)`` when the doc's state has expired, else ``None``."""
    if not oauth_state_expired(source.get(f"{state_field}_expires_at")):
        return None
    _clear_oauth_state(es, index, doc_id, state_field, label)
    logger.warning(f"{label}: state expired for {index}/{doc_id} — refusing the callback")
    return jsonify({"error": "Authorization expired. Restart the authorization and try again."}), 400


def _consume_oauth_state(source, state_field):
    """Single-use: drop the state from the in-memory source so the success
    path's re-index of ``source`` persists the clear."""
    source[state_field] = None
    source[f"{state_field}_expires_at"] = None


# ── the attempt is bound to the browser that started it ───────────
#
# See lib.oauth_state for the threat (consent phishing) and the cookie design.
# Every initiate leg returns its authorize link through _bound_initiate_response,
# which sets the per-state binding cookie; every callback calls
# _unbound_browser_response first and refuses a state the presenting browser
# never received. Checked BEFORE the state lookup, so a refused callback spends
# neither the provider code nor the state (the browser that did start the flow
# can still finish it).

_BIND_RETRY_HINT = (
    "Open the authorization link again in this browser and finish it here. "
    "If someone sent you a sign-in link for this page, do not use it &mdash; "
    "start from Data Connectors in your own account instead."
)


def _token_error_summary(response, response_data):
    """the loggable part of a failed token-endpoint response: the
    HTTP status and the provider's error CODE fields only. The body can carry
    access_token / id_token material next to the error, so it is never logged
    or echoed whole."""
    status = getattr(response, "status_code", None)
    data = response_data if isinstance(response_data, dict) else {}
    error = data.get("error")
    if isinstance(error, dict):  # Graph API: {"error": {"code", "type", "error_subcode", ...}}
        return (
            f"status={status} error_code={error.get('code')} "
            f"error_subcode={error.get('error_subcode')} error_type={error.get('type')}"
        )
    return f"status={status} error={error if isinstance(error, str) else None}"


def _bound_initiate_response(body, state):
    """The initiate leg's HTML response, with the binding cookie for ``state``."""
    response = make_response(body, 200)
    response.set_cookie(
        oauth_state_lib.oauth_bind_cookie_name(state),
        oauth_state_lib.oauth_bind_cookie_value(state),
        max_age=oauth_state_lib.OAUTH_STATE_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


def _unbound_browser_response(state, label):
    """``(page, 403)`` when this browser did not start the attempt for ``state``, else ``None``."""
    if oauth_state_lib.oauth_browser_bound(state, request.cookies):
        return None
    logger.warning(f"{label}: state '{state[:8]}...' presented by a browser that did not start it — refusing")
    return _oauth_error_page(
        "This authorization was not started in this browser, so it was not saved.",
        status=403,
        hint=_BIND_RETRY_HINT,
    )


# ============================================================
# YouTube OAuth Routes
# ============================================================


@app.route("/youtube/auth_callback", methods=["GET"])
def youtube_auth_callback():
    """Handle Google OAuth callback — exchange code for refresh token and store it."""
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        logger.error("YouTube Callback: Missing code or state parameter")
        return jsonify({"error": "Missing code or state."}), 400

    unbound = _unbound_browser_response(state, "YouTube Callback")
    if unbound is not None:
        return unbound

    logger.info(f"YouTube Callback: Token exchange started for state '{state[:8]}...'")

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Load shared client credentials
    try:
        credentials = load_google_oauth_client()
        client_id = credentials["client_id"]
        client_secret = credentials["client_secret"]
    except Exception as e:
        logger.error(f"YouTube Callback: Failed to load client credentials: {e}")
        return jsonify({"error": "Failed to load credentials."}), 500

    # Build redirect_uri (must match exactly what was sent in the initiate step)
    hostname = get_fboauth_hostname()
    if not hostname:
        return jsonify({"error": "Hostname not configured."}), 500
    redirect_uri = (
        f"https://{hostname}/youtube/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
    )

    try:
        # Look up the account document by state — BEFORE the token exchange, so a
        # forged or expired state never spends the provider code (; the
        # Google Ads leg has ordered it this way).
        # _dual_term, not a bare.keyword term: the
        # de_asset_youtube_accounts mapping differs per environment (staging is
        # bare keyword earlier reindex; prod is text+keyword), so a
        # .keyword-only term matches nothing on staging and this callback would
        # fail to find the account it just authorized. Same trap the initiate
        # leg hit (see youtube_auth_initiate).
        state_query = {"query": _dual_term("youtube_account_auth_state", state)}
        state_result = es.search(index="de_asset_youtube_accounts", body=state_query)
        state_hits = state_result.get("hits", {}).get("hits", [])

        if not state_hits:
            logger.error(f"YouTube Callback: No account found for state '{state[:8]}...'")
            return jsonify({"error": "No matching account found."}), 404

        account_doc = state_hits[0]
        account_id = account_doc["_id"]
        account_source = account_doc["_source"]

        # time-boxed + single-use state.
        expired = _expired_state_response(
            es, "de_asset_youtube_accounts", account_id, account_source, "youtube_account_auth_state", "YouTube Callback"
        )
        if expired is not None:
            return expired
        _consume_oauth_state(account_source, "youtube_account_auth_state")

        # Exchange code for tokens
        token_data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        response = requests.post("https://oauth2.googleapis.com/token", data=token_data)
        response_data = response.json()

        if "refresh_token" not in response_data:
            # status + error code only, in the log and never in the body.
            logger.error(f"YouTube Callback: No refresh_token in response ({_token_error_summary(response, response_data)})")
            return (
                jsonify({"error": "No refresh token received. Try re-authorizing — the consent screen must be shown."}),
                400,
            )

        refresh_token = response_data["refresh_token"]

        # consume the reauth marker set by connections.start_reauthorize.
        # Popped from the in-memory source so the success paths (which re-index
        # this dict) don't re-persist it. The refusal paths return WITHOUT
        # re-indexing, so they call _clear_youtube_reauth_flag explicitly:
        # auto_generate only stamps auth_state at doc CREATION, so a reused doc
        # (the reuse path) would otherwise carry a stale flag into a
        # later first-time setup and mislabel it as a reauthorization.
        is_reauth = bool(account_source.pop("youtube_account_reauth_pending", False))

        # probe WHO was just authorized before touching the token
        # slot (see the helper block above for the full rationale). Fail-soft:
        # owned is None when the probe couldn't tell -> save as before.
        sub, owned = _probe_youtube_token_identity(response_data)
        if owned is not None:
            if not owned:
                # The identity owns no channel at all — its token is
                # structurally useless for YouTube Analytics (the
                # manager-not-owner case). Saving it would only overwrite
                # whatever the slot holds and mislead diagnosis.
                logger.warning(
                    f"YouTube Callback: REFUSED tokenless-identity save for account "
                    f"'{account_source.get('youtube_account_title')}' (sub={sub or 'unknown'}: owns no channels)"
                )
                _clear_youtube_reauth_flag(es, account_id, account_source, is_reauth)
                return _oauth_error_page(
                    "The Google identity you selected owns no YouTube channels, so its "
                    "token cannot read any channel's analytics."
                )

            owned_ids = {c["id"] for c in owned}
            if account_source.get("youtube_account_token"):
                # Ping-pong guard: never let a re-auth swap this doc's token to
                # an identity that does not own the channels already linked to
                # it — that silently 403-breaks their imports.
                linked = _youtube_linked_channel_ids(es, account_id)
                broken = {cid: title for cid, title in linked.items() if cid not in owned_ids}
                if broken:
                    broken_names = ", ".join(sorted(broken.values()))
                    logger.warning(
                        f"YouTube Callback: REFUSED identity swap on account "
                        f"'{account_source.get('youtube_account_title')}' — new identity (sub={sub or 'unknown'}) "
                        f"does not own linked channel(s): {broken_names}"
                    )
                    _clear_youtube_reauth_flag(es, account_id, account_source, is_reauth)
                    return _oauth_error_page(
                        "This account's saved authorization serves channels that the identity "
                        f"you just selected does not own ({broken_names}). Saving it would break "
                        "those channels' imports. To connect a different channel, add a new "
                        "account in the Data Connectors wizard instead of re-authorizing this one."
                    )

            # Post-callback identity dedup: if this identity already has a doc,
            # land the token THERE and point the fresh placeholder at it, so a
            # same-email retry can't mint one doc per attempt. Only a tokenless
            # placeholder is redirected — a tokened doc that passed the guard
            # above is a deliberate in-place re-auth.
            if not account_source.get("youtube_account_token"):
                canonical = None
                dedup_kind = ""
                if sub:
                    canonical = _find_youtube_account_by_identity(es, sub, exclude_id=account_id)
                    dedup_kind = "identity"
                if canonical is None:
                    # sub-miss fallback. Legacy grants come back
                    # sub-less, so the identity lookup above can't find the
                    # existing doc; match it by owned-channel overlap instead
                    # (identity-less docs only — see the helper). The ping-pong
                    # guard already ran above on the placeholder, but re-run it
                    # against the overlap TARGET's linked channels before
                    # landing the token there — the target may serve channels
                    # the incoming identity does not own even though their
                    # owned sets intersect.
                    overlap = _find_youtube_account_by_owned_overlap(es, owned_ids, exclude_id=account_id)
                    if overlap is not None:
                        overlap_id, overlap_source = overlap
                        linked = _youtube_linked_channel_ids(es, overlap_id)
                        broken = {cid: t for cid, t in linked.items() if cid not in owned_ids}
                        if broken:
                            logger.warning(
                                f"YouTube Callback:  owned-overlap match on account "
                                f"'{overlap_source.get('youtube_account_title')}' ({overlap_id}) "
                                f"skipped — incoming identity does not own linked channel(s): "
                                f"{', '.join(sorted(broken.values()))}"
                            )
                        else:
                            canonical = overlap
                            dedup_kind = "owned-overlap"
                if canonical is not None:
                    canonical_id, canonical_source = canonical
                    canonical_source["youtube_account_token"] = encrypt(refresh_token)
                    # Stamp the now-known sub (may be "" for a legacy grant —
                    # the doc stays sub-less and keeps matching via overlap until
                    # a modern grant lands a sub) and refresh the owned set from
                    # this authorization (accept the incoming set as truth).
                    _stamp_youtube_identity(canonical_source, sub, owned)
                    es.index(index="de_asset_youtube_accounts", id=canonical_id, document=canonical_source)
                    account_source["youtube_account_redirect_esid"] = canonical_id
                    es.index(index="de_asset_youtube_accounts", id=account_id, document=account_source)
                    logger.info(
                        f"YouTube Callback: {dedup_kind} dedup — token saved to existing account "
                        f"'{canonical_source.get('youtube_account_title')}' ({canonical_id}); "
                        f"placeholder {account_id} redirected"
                    )
                    return _oauth_success_page(reauth=is_reauth)

            _stamp_youtube_identity(account_source, sub, owned)

        # Encrypt and store the refresh token
        account_source["youtube_account_token"] = encrypt(refresh_token)
        es.index(index="de_asset_youtube_accounts", id=account_id, document=account_source)
        logger.info(
            f"YouTube Callback: Token saved for account '{account_source.get('youtube_account_title')}'"
            f"{' (reauthorize)' if is_reauth else ''}"
        )

        # Return the self-closing success page (not raw JSON) so the wizard
        # popup closes itself and the wizard tab advances. The popup is opened
        # by the data-connectors wizard and polls oauth-status for the token;
        # the user never needs to read this response. (made GA4 + this
        # YouTube callback consistent — both previously returned naked JSON.)
        # a reauth gets different copy: its channel selection is
        # already saved, so "return to the wizard and confirm" would be wrong.
        return _oauth_success_page(reauth=is_reauth)

    except HTTPException:
        raise
    except Exception as e:
        # don't echo exception text in the response body. Log the
        # full detail (with traceback) on the server side for ops debugging.
        from lib.errors import safe_error_response

        return safe_error_response(
            "Token exchange failed.",
            log_detail="YouTube Callback error",
            exc=e,
            body={},
        )


@app.route("/youtube/<youtube_account_auth_url>", methods=["GET"])
def youtube_auth_initiate(youtube_account_auth_url):
    """Generate and display the Google OAuth authorization URL for a YouTube account."""
    if len(youtube_account_auth_url) != 15:
        abort(404)

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Look up the YouTube account by auth_url.
    # _dual_term, not a bare.keyword term: staging maps
    # youtube_account_auth_url as bare keyword (earlier reindex) while prod
    # is text+keyword, so a .keyword-only term 404'd EVERY YouTube OAuth
    # initiate on staging (the account doc exists but the sub-field doesn't).
    query = {"query": _dual_term("youtube_account_auth_url", youtube_account_auth_url)}
    try:
        result = es.search(index="de_asset_youtube_accounts", body=query)
        hits = result.get("hits", {}).get("hits", [])

        if not hits:
            logger.warning(f"YouTube OAuth: No account found for auth_url '{youtube_account_auth_url}'")
            abort(404)

        doc = hits[0]["_source"]
        email = doc.get("youtube_account_email", "")

        # a fresh, time-boxed state for THIS attempt (the stored one,
        # if any, is superseded; the callback finds the doc by this value).
        state = _issue_oauth_state(es, "de_asset_youtube_accounts", hits[0]["_id"], "youtube_account_auth_state")

        logger.info(f"YouTube OAuth: Initiating for account '{doc.get('youtube_account_title')}' (email: {email})")

        # Load shared client credentials
        try:
            client_id = load_google_oauth_client()["client_id"]
        except Exception as e:
            logger.error(f"YouTube OAuth: Failed to load client credentials: {e}")
            return jsonify({"error": "Invalid client credentials format."}), 500

        # Build redirect_uri
        hostname = get_fboauth_hostname()
        if not hostname:
            return jsonify({"error": "Hostname not configured."}), 500
        redirect_uri = (
            f"https://{hostname}/youtube/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
        )

        # Construct Google OAuth URL
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": email,
        }
        oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        logger.info(f"YouTube OAuth: Generated auth URL for '{doc.get('youtube_account_title')}'")
        # bind this attempt to the browser that opened it.
        return _bound_initiate_response(
            f"<html><body><p><a href='{oauth_url}'>Authorize YouTube Account</a></p></body></html>",  # nosemgrep: raw-html-format - oauth_url built from hardcoded Google endpoint + config params
            state,
        )

    except HTTPException:
        raise
    except Exception as e:
        # don't echo exception text in the response body.
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to initiate OAuth.",
            log_detail="YouTube OAuth initiate error",
            exc=e,
            body={},
        )


# ============================================================
# GA4 OAuth Routes
# ============================================================
#
# Mirror of the YouTube OAuth pair above. A de_asset_ga4_accounts doc carries
# the per-account auth_url (15-char slug) + auth_state; the callback finds the
# doc by state, exchanges the code for a refresh token, and stores it encrypted
# on ga4_account_token. The wizard then enumerates GA4 properties live from the
# granted account and links them to a team.


@app.route("/ga4/auth_callback", methods=["GET"])
def ga4_auth_callback():
    """Handle Google OAuth callback for GA4 — exchange code for refresh token and store it."""
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        logger.error("GA4 Callback: Missing code or state parameter")
        return jsonify({"error": "Missing code or state."}), 400

    unbound = _unbound_browser_response(state, "GA4 Callback")
    if unbound is not None:
        return unbound

    logger.info(f"GA4 Callback: Token exchange started for state '{state[:8]}...'")

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Load shared client credentials (same Google client as YouTube)
    try:
        credentials = load_google_oauth_client()
        client_id = credentials["client_id"]
        client_secret = credentials["client_secret"]
    except Exception as e:
        logger.error(f"GA4 Callback: Failed to load client credentials: {e}")
        return jsonify({"error": "Failed to load credentials."}), 500

    # Build redirect_uri (must match exactly what was sent in the initiate step)
    hostname = get_fboauth_hostname()
    if not hostname:
        return jsonify({"error": "Hostname not configured."}), 500
    redirect_uri = (
        f"https://{hostname}/ga4/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
    )

    try:
        # Look up the account document by state — BEFORE the token exchange
        # (; see the YouTube callback for the ordering rationale).
        # _dual_term, not a bare.keyword term (same mapping trap as
        # the YouTube legs: de_asset_ga4_accounts maps ga4_account_auth_state as
        # bare keyword on prod+staging, so a .keyword-only term matches nothing
        # and this callback can't find the account it just authorized).
        state_query = {"query": _dual_term("ga4_account_auth_state", state)}
        state_result = es.search(index="de_asset_ga4_accounts", body=state_query)
        state_hits = state_result.get("hits", {}).get("hits", [])

        if not state_hits:
            logger.error(f"GA4 Callback: No account found for state '{state[:8]}...'")
            return jsonify({"error": "No matching account found."}), 404

        account_doc = state_hits[0]
        account_id = account_doc["_id"]
        account_source = account_doc["_source"]

        # time-boxed + single-use state.
        expired = _expired_state_response(
            es, "de_asset_ga4_accounts", account_id, account_source, "ga4_account_auth_state", "GA4 Callback"
        )
        if expired is not None:
            return expired
        _consume_oauth_state(account_source, "ga4_account_auth_state")

        # Exchange code for tokens
        token_data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        response = requests.post("https://oauth2.googleapis.com/token", data=token_data)
        response_data = response.json()

        if "refresh_token" not in response_data:
            # status + error code only, in the log and never in the body.
            logger.error(f"GA4 Callback: No refresh_token in response ({_token_error_summary(response, response_data)})")
            return (
                jsonify({"error": "No refresh token received. Try re-authorizing — the consent screen must be shown."}),
                400,
            )

        refresh_token = response_data["refresh_token"]

        # Encrypt and store the refresh token
        account_source["ga4_account_token"] = encrypt(refresh_token)
        es.index(index="de_asset_ga4_accounts", id=account_id, document=account_source)
        logger.info(f"GA4 Callback: Token saved for account '{account_source.get('ga4_account_title')}'")

        # Self-closing success page (not raw JSON) so the wizard popup closes
        # itself and the wizard tab advances — see _oauth_success_page.
        return _oauth_success_page()

    except HTTPException:
        raise
    except Exception as e:
        # don't echo exception text in the response body. Log the
        # full detail (with traceback) on the server side for ops debugging.
        from lib.errors import safe_error_response

        return safe_error_response(
            "Token exchange failed.",
            log_detail="GA4 Callback error",
            exc=e,
            body={},
        )


@app.route("/ga4/<ga4_account_auth_url>", methods=["GET"])
def ga4_auth_initiate(ga4_account_auth_url):
    """Generate and display the Google OAuth authorization URL for a GA4 account."""
    if len(ga4_account_auth_url) != 15:
        abort(404)

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Look up the GA4 account by auth_url.
    # _dual_term, not a bare.keyword term: ga4_account_auth_url is
    # bare keyword on prod+staging, so a .keyword-only term 404'd every GA4
    # OAuth initiate (same trap as the YouTube initiate leg).
    query = {"query": _dual_term("ga4_account_auth_url", ga4_account_auth_url)}
    try:
        result = es.search(index="de_asset_ga4_accounts", body=query)
        hits = result.get("hits", {}).get("hits", [])

        if not hits:
            logger.warning(f"GA4 OAuth: No account found for auth_url '{ga4_account_auth_url}'")
            abort(404)

        doc = hits[0]["_source"]
        email = doc.get("ga4_account_email", "")

        # fresh, time-boxed state for this attempt (see the YouTube leg).
        state = _issue_oauth_state(es, "de_asset_ga4_accounts", hits[0]["_id"], "ga4_account_auth_state")

        logger.info(f"GA4 OAuth: Initiating for account '{doc.get('ga4_account_title')}' (email: {email})")

        # Load shared client credentials
        try:
            client_id = load_google_oauth_client()["client_id"]
        except Exception as e:
            logger.error(f"GA4 OAuth: Failed to load client credentials: {e}")
            return jsonify({"error": "Invalid client credentials format."}), 500

        # Build redirect_uri
        hostname = get_fboauth_hostname()
        if not hostname:
            return jsonify({"error": "Hostname not configured."}), 500
        redirect_uri = (
            f"https://{hostname}/ga4/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
        )

        # Construct Google OAuth URL
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GA4_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "login_hint": email,
        }
        oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        logger.info(f"GA4 OAuth: Generated auth URL for '{doc.get('ga4_account_title')}'")
        # bind this attempt to the browser that opened it.
        return _bound_initiate_response(
            f"<html><body><p><a href='{oauth_url}'>Authorize Google Analytics Account</a></p></body></html>",  # nosemgrep: raw-html-format - oauth_url built from hardcoded Google endpoint + config params
            state,
        )

    except HTTPException:
        raise
    except Exception as e:
        # don't echo exception text in the response body.
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to initiate OAuth.",
            log_detail="GA4 OAuth initiate error",
            exc=e,
            body={},
        )


# ============================================================
# Google Ads OAuth Routes
# ============================================================
#
# Per-user OAuth for the adwords scope. Structurally like the GA4 leg above
# (same Google client, same authorize/exchange mechanics) with one difference:
# the refresh token lands on a de_connection row via ConnectionService, not a
# token-carrier asset doc. The wizard (app host) creates a pending connection
# and seeds oauth_state on it; this host (no shared Flask session) correlates
# Google's redirect by looking the connection up by that state.


def _load_google_ads_oauth_client():
    """Return (client_id, client_secret) from the shared encrypted Google
    client, or raise on a malformed file. Mirrors the GA4 leg.
    """
    credentials = load_google_oauth_client()
    return credentials["client_id"], credentials["client_secret"]


@app.route("/google_ads/<google_ads_auth_url>", methods=["GET"])
def google_ads_auth_initiate(google_ads_auth_url):
    """Generate and display the Google OAuth authorization URL for a Google Ads
    connection (adwords scope). The popup opens this URL; the connection was
    pre-created (status=pending_token) and seeded with oauth_state by the wizard.
    """
    if len(google_ads_auth_url) != 15:
        abort(404)

    try:
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as e:
        logger.error(f"Google Ads OAuth: ConnectionService import failed: {e}")
        return jsonify({"error": "Connection service unavailable."}), 500

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Look up the pending connection by its 15-char auth_url path segment.
    # dual-term so the lookup survives an oauth_auth_url mapping
    # flip (bare keyword <-> text+.keyword) without a repeat of the
    # add-then-revert-.keyword saga on the sibling fb_app_user fields.
    try:
        result = es.search(
            index="de_connection",
            body={"query": _dual_term("oauth_auth_url", google_ads_auth_url), "size": 1},
        )
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            logger.warning(f"Google Ads OAuth: no connection for auth_url '{google_ads_auth_url}'")
            abort(404)

        doc = hits[0]["_source"]
        state = doc.get("oauth_state")
        if not state:
            return jsonify({"error": "Missing state on connection. Restart the connection."}), 400

        client_id, _ = _load_google_ads_oauth_client()

        hostname = get_fboauth_hostname()
        if not hostname:
            return jsonify({"error": "Hostname not configured."}), 500
        redirect_uri = f"https://{hostname}/google_ads/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request

        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_ADS_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        oauth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        logger.info(f"Google Ads OAuth: generated auth URL for connection '{doc.get('label', '')}'")
        # bind this attempt to the browser that opened it.
        return _bound_initiate_response(
            f"<html><body><p><a href='{oauth_url}'>Authorize Google Ads Account</a></p></body></html>",  # nosemgrep: raw-html-format - oauth_url built from hardcoded Google endpoint + config params
            state,
        )

    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to initiate OAuth.",
            log_detail="Google Ads OAuth initiate error",
            exc=e,
            body={},
        )


@app.route("/google_ads/auth_callback", methods=["GET"])
def google_ads_auth_callback():
    """Handle Google OAuth callback for Google Ads — exchange code for a refresh
    token and store it encrypted on the de_connection row, flipping it active.
    """
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        logger.error("Google Ads Callback: Missing code or state parameter")
        return jsonify({"error": "Missing code or state."}), 400

    unbound = _unbound_browser_response(state, "Google Ads Callback")
    if unbound is not None:
        return unbound

    logger.info(f"Google Ads Callback: token exchange started for state '{state[:8]}...'")

    try:
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as e:
        logger.error(f"Google Ads Callback: ConnectionService import failed: {e}")
        return jsonify({"error": "Connection service unavailable."}), 500

    # Correlate the redirect back to the pending connection BEFORE the
    # exchange, so a forged/expired state never triggers a token request.
    conn = ConnectionService.find_by_oauth_state(state)
    if not conn:
        logger.error(f"Google Ads Callback: no connection for state '{state[:8]}...'")
        return jsonify({"error": "No matching connection found."}), 404

    connection_esid = conn.get("_id")

    # SECURITY: the fb_oauth host has no shared
    # session with the wizard, so the callback cannot itself re-assert that the
    # browser completing OAuth is the user who started it. The binding is the
    # `principal` stamped on the pending connection — which P3's wizard endpoint
    # MUST set from the authenticated flask.session, never from a request param.
    # Here we refuse to commit a token to a connection with no principal: a row
    # that was seeded without an authenticated owner can never receive a token.
    # This does not replace the per-user check the wizard owns; it is the
    # last-line guard on this session-less host.
    # P3 BLOCKING REQUIREMENT: seed_oauth_state's caller sets principal =
    # session['username']; add a callback→wizard-host handoff (signed) if the
    # threat model needs the authorizing browser re-verified against principal.
    conn_principal = (conn.get("principal") or "").strip()
    if not conn_principal:
        ConnectionService.clear_oauth_state(connection_esid)
        logger.error(
            "Google Ads Callback: refusing token commit — connection %s has no principal",
            connection_esid,
        )
        return jsonify({"error": "Connection is not bound to a user."}), 403

    # Defensive expiry check (the state is single-use + time-boxed).
    expires_at = conn.get("oauth_state_expires_at")
    if expires_at:
        try:
            from datetime import datetime, timezone

            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                ConnectionService.clear_oauth_state(connection_esid)
                logger.warning(f"Google Ads Callback: state expired for connection {connection_esid}")
                return jsonify({"error": "Authorization expired. Restart the connection."}), 400
        except ValueError:
            pass  # unparseable expiry → fall through, the upsert is still gated on a valid code

    try:
        client_id, client_secret = _load_google_ads_oauth_client()
    except Exception as e:
        logger.error(f"Google Ads Callback: failed to load client credentials: {e}")
        return jsonify({"error": "Failed to load credentials."}), 500

    hostname = get_fboauth_hostname()
    if not hostname:
        return jsonify({"error": "Hostname not configured."}), 500
    redirect_uri = (
        f"https://{hostname}/google_ads/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
    )

    try:
        token_data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        response = requests.post("https://oauth2.googleapis.com/token", data=token_data)
        response_data = response.json()

        if "refresh_token" not in response_data:
            # Security: never log or echo the raw token response — it can carry
            # access_token / id_token material. Surface only the OAuth error
            # fields, which are safe diagnostics.
            logger.error(
                "Google Ads Callback: no refresh_token in response (keys=%s, error=%s, error_description=%s)",
                list(response_data.keys()),
                response_data.get("error"),
                response_data.get("error_description"),
            )
            return (
                jsonify({"error": "No refresh token received. Try re-authorizing — the consent screen must be shown."}),
                400,
            )

        refresh_token = response_data["refresh_token"]

        # Persist on the de_connection row by its natural key. provider_account_id
        # is the google_ads_customer_id the wizard stamped on the pending row.
        upsert = ConnectionService.upsert_connection(
            principal=conn_principal,
            provider="google_ads",
            provider_account_id=conn.get("provider_account_id") or "",
            oauth_token=refresh_token,
            label=conn.get("label") or "",
            scopes=list(GOOGLE_ADS_SCOPES),
            org_shared=bool(conn.get("org_shared")),
        )
        if not upsert.get("success"):
            logger.error(f"Google Ads Callback: upsert failed for {connection_esid}: {upsert.get('error')}")
            return jsonify({"error": "Failed to store token."}), 500

        # upsert_connection replaces the whole doc via es.index and does NOT
        # carry the transient oauth_state fields forward, so the successful
        # upsert is itself the single-use state-clear — no extra write needed.
        # (The expired-state branch above, which never reaches an upsert, uses
        # clear_oauth_state explicitly.)
        logger.info(f"Google Ads Callback: token saved for connection '{conn.get('label', '')}'")

        return _oauth_success_page()

    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Token exchange failed.",
            log_detail="Google Ads Callback error",
            exc=e,
            body={},
        )


# ============================================================
# TikTok OAuth Routes
# ============================================================
#
# Login Kit for Web, Google Ads shape: the wizard (app host) mints a
# ``wizard-pending-*`` de_connection owned by the session user and seeds
# oauth_state on it; this host finds the row by that state. TikTok's account id
# (open_id) only arrives with the token, so the callback swaps the placeholder
# for it via set_token_for_pending_connection (which also merges into an
# existing row for the same principal + account). That call is a
# partial update, so the state is cleared explicitly afterwards.

_TIKTOK_RETRY_HINT = (
    "Close this window and start again from the TikTok connector in Data Connectors. "
    "On TikTok's consent screen, leave every permission ticked."
)


def _tiktok_redirect_uri():
    """The registered callback URI, or "" when fboauth.hostname is unset."""
    hostname = get_fboauth_hostname()
    if not hostname:
        return ""
    return f"https://{hostname}{TIKTOK_CALLBACK_PATH}"  # nosemgrep: tainted-url-host - hostname from config file, not request


def _tiktok_account_label(user, open_id):
    """``TikTok @username`` when known, else the display name, else a short open_id."""
    username = str((user or {}).get("username") or "").strip()
    if username:
        return f"TikTok @{username}"
    display_name = str((user or {}).get("display_name") or "").strip()
    if display_name:
        return f"TikTok {display_name}"
    return f"TikTok {open_id[:8]}"


@app.route("/tiktok/<tiktok_auth_url>", methods=["GET"])
def tiktok_auth_initiate(tiktok_auth_url):
    """Show the TikTok authorize link for a pending TikTok connection. The popup
    opens this URL; the wizard created the row and seeded its oauth_state."""
    # the connector is switched off on this deployment.
    if not tiktok_enabled():
        abort(404)
    if len(tiktok_auth_url) != 15:
        abort(404)

    try:
        from asset_management.web.services.connection_service import PROVIDER_TIKTOK
    except Exception as e:
        logger.error(f"TikTok OAuth: ConnectionService import failed: {e}")
        return jsonify({"error": "Connection service unavailable."}), 500

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    try:
        result = es.search(
            index="de_connection",
            body={"query": _dual_term("oauth_auth_url", tiktok_auth_url), "size": 1},
        )
        hits = result.get("hits", {}).get("hits", [])
        if not hits or (hits[0].get("_source") or {}).get("provider") != PROVIDER_TIKTOK:
            logger.warning(f"TikTok OAuth: no TikTok connection for auth_url '{tiktok_auth_url}'")
            abort(404)

        doc = hits[0]["_source"]
        state = doc.get("oauth_state")
        if not state:
            return jsonify({"error": "Missing state on connection. Restart the connection."}), 400
        if oauth_state_expired(doc.get("oauth_state_expires_at")):
            return jsonify({"error": "This authorization link has expired. Restart the connection."}), 400

        try:
            client_key, _ = tiktok_load_client_credentials()
        except TikTokError as e:
            logger.error(f"TikTok OAuth: {e}")
            return jsonify({"error": "TikTok is not configured on this server."}), 500

        redirect_uri = _tiktok_redirect_uri()
        if not redirect_uri:
            return jsonify({"error": "Hostname not configured."}), 500

        from urllib.parse import urlencode

        params = {
            "client_key": client_key,
            "scope": ",".join(TIKTOK_SCOPES),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            # Always show TikTok's consent page, so a browser already signed in
            # to another TikTok account can't authorize it silently.
            "disable_auto_auth": "1",
        }
        oauth_url = f"{TIKTOK_AUTHORIZE_URL}?{urlencode(params)}"

        logger.info(f"TikTok OAuth: generated auth URL for connection '{hits[0]['_id']}'")
        # bind this attempt to the browser that opened it.
        return _bound_initiate_response(
            f"<html><body><p><a href='{html_escape(oauth_url)}'>Authorize TikTok Account</a></p></body></html>",  # nosemgrep: raw-html-format - oauth_url escaped; built from a hardcoded TikTok endpoint + config params
            state,
        )

    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to initiate OAuth.",
            log_detail="TikTok OAuth initiate error",
            exc=e,
            body={},
        )


@app.route("/tiktok/auth_callback", methods=["GET"])
def tiktok_auth_callback():
    """Handle TikTok's redirect: exchange the code, bind the token to the pending
    connection's principal, and record the authorized account."""
    # the connector is switched off on this deployment.
    if not tiktok_enabled():
        abort(404)
    code = request.args.get("code")
    state = request.args.get("state")
    tiktok_error = request.args.get("error")

    try:
        from asset_management.web.services.connection_service import (
            ASSET_TYPE_TIKTOK_ACCOUNT,
            PENDING_ACCOUNT_ID_PREFIX,
            PROVIDER_TIKTOK,
            ConnectionService,
        )
    except Exception as e:
        logger.error(f"TikTok Callback: ConnectionService import failed: {e}")
        return jsonify({"error": "Connection service unavailable."}), 500

    # The user declined (or TikTok refused) at the consent screen. Burn the
    # state so the next attempt starts clean, and say why.
    if tiktok_error:
        logger.warning(
            "TikTok Callback: authorization not granted (error=%s description=%s)",
            tiktok_error,
            request.args.get("error_description"),
        )
        if state:
            declined = ConnectionService.find_by_oauth_state(state)
            if declined and declined.get("provider") == PROVIDER_TIKTOK:
                ConnectionService.clear_oauth_state(declined.get("_id"))
        return _oauth_error_page("TikTok did not grant access to the account.", status=400, hint=_TIKTOK_RETRY_HINT)

    if not code or not state:
        logger.error("TikTok Callback: missing code or state parameter")
        return jsonify({"error": "Missing code or state."}), 400

    unbound = _unbound_browser_response(state, "TikTok Callback")
    if unbound is not None:
        return unbound

    logger.info(f"TikTok Callback: token exchange started for state '{state[:8]}...'")

    # Correlate BEFORE the exchange, so a forged or expired state never spends
    # the code.
    conn = ConnectionService.find_by_oauth_state(state)
    if not conn or conn.get("provider") != PROVIDER_TIKTOK:
        logger.error(f"TikTok Callback: no TikTok connection for state '{state[:8]}...'")
        return jsonify({"error": "No matching connection found."}), 404

    connection_esid = conn.get("_id")

    # Session-less host: the only binding to the user who started the flow is
    # the principal the wizard stamped from flask.session.
    conn_principal = (conn.get("principal") or "").strip()
    if not conn_principal:
        ConnectionService.clear_oauth_state(connection_esid)
        logger.error("TikTok Callback: refusing token commit — connection %s has no principal", connection_esid)
        return jsonify({"error": "Connection is not bound to a user."}), 403

    if oauth_state_expired(conn.get("oauth_state_expires_at")):
        ConnectionService.clear_oauth_state(connection_esid)
        logger.warning(f"TikTok Callback: state expired for connection {connection_esid}")
        return jsonify({"error": "Authorization expired. Restart the connection."}), 400

    redirect_uri = _tiktok_redirect_uri()
    if not redirect_uri:
        return jsonify({"error": "Hostname not configured."}), 500

    try:
        # TikTokError messages carry TikTok's error code + log_id, never a token.
        try:
            token_body = tiktok_exchange_code(code, redirect_uri)
        except TikTokError as e:
            ConnectionService.clear_oauth_state(connection_esid)
            logger.error(f"TikTok Callback: {e}")
            return _oauth_error_page("TikTok rejected the authorization.", status=400, hint=_TIKTOK_RETRY_HINT)

        granted = tiktok_granted_scopes(token_body)
        missing = sorted(TIKTOK_REQUIRED_SCOPES - set(granted))
        if missing:
            ConnectionService.clear_oauth_state(connection_esid)
            logger.warning(f"TikTok Callback: grant for {connection_esid} is missing scopes {missing}")
            return _oauth_error_page(
                "The authorization is missing permissions DEAM needs to import: " + ", ".join(missing) + ".",
                status=400,
                hint=_TIKTOK_RETRY_HINT,
            )

        open_id = str(token_body.get("open_id") or "").strip()
        refresh_token = str(token_body.get("refresh_token") or "")
        if not open_id or not refresh_token:
            ConnectionService.clear_oauth_state(connection_esid)
            logger.error(
                "TikTok Callback: token response lacks open_id or refresh_token (keys=%s)",
                sorted(token_body.keys()),
            )
            return _oauth_error_page("TikTok returned an incomplete authorization.", status=400, hint=_TIKTOK_RETRY_HINT)

        # A row that already names a real account may only take a token for THAT
        # account. Swapping the account under an existing connection would move
        # every team linked to it onto someone else's TikTok data.
        current_account = str(conn.get("provider_account_id") or "")
        if not current_account.startswith(PENDING_ACCOUNT_ID_PREFIX) and current_account != open_id:
            ConnectionService.clear_oauth_state(connection_esid)
            logger.warning(
                "TikTok Callback: REFUSED account swap on connection %s (%s -> %s)",
                connection_esid,
                current_account,
                open_id,
            )
            return _oauth_error_page(
                "You authorized a different TikTok account than the one this connection belongs to.",
                status=400,
                hint=_TIKTOK_RETRY_HINT,
            )

        # Name the connection after the account. Fail-soft: the token is good
        # either way, so a failed profile call only costs the nicer label.
        fields = ["open_id", "display_name"] + (["username"] if "user.info.profile" in granted else [])
        user = {}
        try:
            user = tiktok_get_user_info(str(token_body.get("access_token") or ""), fields)
        except TikTokError as e:
            logger.warning(f"TikTok Callback: profile lookup failed for {connection_esid}: {e}")
        label = _tiktok_account_label(user, open_id)

        canonical_esid = ConnectionService.set_token_for_pending_connection(
            connection_esid,
            provider_account_id=open_id,
            oauth_token=refresh_token,
            token_expires_at=tiktok_refresh_expiry_iso(token_body),
            scopes=granted,
            label=label,
        )
        if not canonical_esid:
            logger.error(f"TikTok Callback: token write failed for {connection_esid}")
            return jsonify({"error": "Failed to store token."}), 500
        ConnectionService.clear_oauth_state(connection_esid)

        asset = ConnectionService.upsert_connection_asset(
            connection_esid=canonical_esid,
            asset_type=ASSET_TYPE_TIKTOK_ACCOUNT,
            provider_asset_id=open_id,
            label=label,
            metadata={
                "username": str(user.get("username") or ""),
                "display_name": str(user.get("display_name") or ""),
            },
        )
        if not asset.get("success"):
            logger.warning(f"TikTok Callback: account row write failed for {canonical_esid}: {asset.get('error')}")

        # The Asset Explorer / team-page view of this account (fail-soft).
        from asset_management.web.services import tiktok_account_projection

        tiktok_account_projection.sync(canonical_esid)

        # A reconnect (from the banner or the Manage page) is good news for the
        # credential: clear its health row now rather than at the next import.
        try:
            from lib import credential_health as ch

            ch.record_healthy(
                source_index="de_connection",
                source_esid=canonical_esid,
                provider=PROVIDER_TIKTOK,
                label=label,
                reason="Reauthorized",
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping; the token is already saved
            logger.warning(f"TikTok Callback: credential-health write failed for {canonical_esid}: {e}")

        # A row that already named an account is a reauthorize (Manage page /
        # banner): nothing is left to confirm in the wizard, so say so.
        is_reauth = not current_account.startswith(PENDING_ACCOUNT_ID_PREFIX)
        logger.info(
            f"TikTok Callback: token saved for connection {canonical_esid} ({label}){' (reauthorize)' if is_reauth else ''}"
        )
        return _oauth_success_page(reauth=is_reauth)

    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Token exchange failed.",
            log_detail="TikTok Callback error",
            exc=e,
            body={},
        )


# ============================================================
# Facebook OAuth Routes
# ============================================================


# Route to handle dynamic app and user segments
@app.route("/<auth_url_app>/<auth_url_user>", methods=["GET"])
def handle_auth_url(auth_url_app, auth_url_user):
    if len(auth_url_user) != 15:
        abort(404)  # Return a 404 Not Found if the length is not 15

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection error."}), 500

    # Lookup the Facebook App details using auth_url_app
    try:
        fb_app_result = es.get(index="de_asset_fb_apps", id=auth_url_app)
        fb_app_data = fb_app_result["_source"]
        client_id = fb_app_data.get("fb_app_id")
        logger.info(f"OAuth: App '{fb_app_data.get('fb_app_title')}' (ID: {client_id})")
    except Exception as e:
        logger.error(f"OAuth: Failed to lookup FB App '{auth_url_app}': {e}")
        return jsonify({"error": "Resource not found."}), 404

    # If client_id is missing, return an error
    if not client_id:
        return jsonify({"error": "Resource 02  not found."}), 400

    # Now look up the user document using the auth_url_user.
    # de_asset_fb_app_users.fb_app_user_auth_url is explicit `keyword` today
    # (per update_v4.py), so a bare-field term matches. was the exact
    # symptom this class causes: a prod-only drift to text+.keyword forced a
    # `.keyword` patch, then a revert after prod was reindexed back to bare.
    # use dual-term instead so the query is correct on BOTH shapes
    # and no future reindex re-opens that add-then-revert cycle.
    query = {"query": _dual_term("fb_app_user_auth_url", auth_url_user)}

    try:
        result = es.search(index="de_asset_fb_app_users", body=query)
        hits = result.get("hits", {}).get("hits", [])

        if hits:
            doc = hits[0]["_source"]
            logger.info(f"OAuth: User '{doc.get('fb_app_user_title')}', Role: '{doc.get('fb_app_user_role')}'")

            fb_app_esid = doc.get("fb_app_esid")

            # If fb_app_esid is missing, return an error
            if not fb_app_esid:
                return jsonify({"error": "Missing required parameters."}), 400

            # a fresh, time-boxed state for THIS attempt; the callback
            # resolves the user (and thereby the FB App) by this value.
            state = _issue_oauth_state(es, "de_asset_fb_app_users", hits[0]["_id"], "fb_app_user_auth_state")

            # canonical redirect_uri — one URL per environment, no
            # per-user/per-app segments. Facebook caps Valid OAuth Redirect URIs
            # at ~10 per app, and a published unified app must support unlimited
            # customers. The state parameter (which we already generate per
            # app-user) carries identity through Facebook; the callback resolves
            # both the user AND the FB App from the state lookup.
            hostname = get_fboauth_hostname()
            if not hostname:
                return jsonify({"error": "Resource 03 not found."}), 500
            redirect_uri = (
                f"https://{hostname}/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request
            )

            # Determine the scopes based on fb_app_user_role.
            # Ads role intentionally does NOT request ads_management:
            # DEAM is read-only on Facebook Ads — every Graph call is api_get
            # or get_insights — and Meta App Review rejects ads_management
            # when no write surface exists.
            fb_app_user_role = doc.get("fb_app_user_role", "")
            if fb_app_user_role == "Ads":
                scope = "ads_read,business_management"
            else:
                # Pages role — include Instagram scopes for insights access
                scope = "pages_show_list,pages_read_engagement,instagram_basic,instagram_manage_insights,business_management"
            logger.info(f"OAuth: Using scope='{scope}' for role '{fb_app_user_role}'")

            # Construct the OAuth URL
            oauth_url = (
                f"{FB_OAUTH_DIALOG_URL}?"
                f"client_id={client_id}&"
                f"redirect_uri={redirect_uri}&"
                f"state={state}&"
                f"scope={scope}&"
                "response_type=code"
            )

            # Return the constructed URL in HTML format. escape
            # the URL before interpolating — auth_url_app and (transitively)
            # the ES doc fields can contain values that originated from a
            # request, and the response's content-type is HTML. CodeQL
            # py/reflective-xss.
            logger.info(f"OAuth: Generated URL for '{doc.get('fb_app_user_title')}'")
            # nosemgrep: raw-html-format - oauth_url is escaped via markupsafe.escape, and the only request-derived input is auth_url_app which is the URL path segment.
            oauth_url_html = f"<html><body><p>" f"<a href='{html_escape(oauth_url)}'>Authorize FB</a>" f"</p></body></html>"
            # bind this attempt to the browser that opened it.
            return _bound_initiate_response(oauth_url_html, state)

        else:
            logger.warning(f"OAuth: No user found for auth_url '{auth_url_user}'")
            abort(404)
    except Exception as e:
        print(f"Error querying Elasticsearch or constructing OAuth URL: {e}")
        return jsonify({"error": "Connection 02 Error."}), 500


# canonical callback route — one URL per environment, identity carried
# by the `state` parameter. The user is found by state, the FB App by reading
# fb_app_esid off the user doc. This replaces the prior per-app URL pattern
# (/<auth_url_app>/auth_callback) which forced every new FB App to be
# whitelisted at developers.facebook.com — incompatible with a published
# multi-tenant unified app.
@app.route("/auth_callback", methods=["GET"])
def facebook_callback():
    # Get the code and state parameters from the request
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        logger.error("Callback: Missing code or state parameter")
        return jsonify({"error": "Resource 04 not found."}), 400

    unbound = _unbound_browser_response(state, "Callback")
    if unbound is not None:
        return unbound

    logger.info(f"Callback: Token exchange started for state '{state[:8]}...'")

    # Get the Elasticsearch client
    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"error": "Connection 03 failed."}), 500

    # Resolve the user FIRST by state — the user doc tells us which FB App
    # this callback belongs to (fb_app_esid). State is per-user and was
    # generated when /<auth_url_app>/<auth_url_user> was first hit.
    # dual-term — a mapping flip on fb_app_user_auth_state would
    # otherwise silently drop every FB OAuth callback (no user found → 404).
    user_query = {"query": _dual_term("fb_app_user_auth_state", state)}
    try:
        user_result = es.search(index="de_asset_fb_app_users", body=user_query)
    except Exception as e:
        logger.error(f"Callback: User lookup failed for state '{state[:8]}...': {e}")
        return jsonify({"error": "Resource 11 not found."}), 500
    user_hits = user_result.get("hits", {}).get("hits", [])
    if not user_hits:
        logger.error(f"Callback: No user found for state '{state[:8]}...'")
        return jsonify({"error": "Resource 11 not found."}), 404

    user_doc = user_hits[0]
    user_doc_id = user_doc["_id"]
    user_source = user_doc["_source"]

    # time-boxed + single-use state, checked before the code exchange.
    expired = _expired_state_response(
        es, "de_asset_fb_app_users", user_doc_id, user_source, "fb_app_user_auth_state", "Callback"
    )
    if expired is not None:
        return expired
    _consume_oauth_state(user_source, "fb_app_user_auth_state")

    auth_url_app = user_source.get("fb_app_esid")
    if not auth_url_app:
        logger.error(f"Callback: User doc {user_doc_id} missing fb_app_esid")
        return jsonify({"error": "Resource 11 not found."}), 500

    # Lookup the Facebook App details using fb_app_esid from the user doc
    try:
        fb_app_result = es.get(index="de_asset_fb_apps", id=auth_url_app)
        fb_app_data = fb_app_result["_source"]
        client_id = fb_app_data.get("fb_app_id")
        encrypted_client_secret = fb_app_data.get("fb_app_secret")
        if not encrypted_client_secret:
            return jsonify({"error": "Resource 05 not found."}), 400
        client_secret = decrypt(encrypted_client_secret)
    except Exception as e:
        return jsonify({"error": "Resource 06 not found."}), 404

    # If client_id is missing, return an error
    if not client_id:
        return jsonify({"error": "Resource 07 not found."}), 400

    # Get the canonical redirect_uri — must match handle_auth_url byte-for-byte
    # because Facebook compares the token-exchange redirect_uri against the
    # one passed to the dialog.
    hostname = get_fboauth_hostname()
    if not hostname:
        return jsonify({"error": "Resource 08 not found."}), 500
    redirect_uri = f"https://{hostname}/auth_callback"  # nosemgrep: tainted-url-host - hostname from config file, not request

    # Exchange code for access token
    token_url = f"{FB_GRAPH_URL}/oauth/access_token"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "client_secret": client_secret,
        "code": code,
    }

    try:
        response = requests.get(token_url, params=params)
        response_data = response.json()

        if "access_token" in response_data:
            access_token = response_data["access_token"]

            # Encrypt the access_token
            encrypted_access_token = encrypt(access_token)

            # Save the encrypted access token back into the user document
            user_source["fb_app_user_token"] = encrypted_access_token

            # Update the document in Elasticsearch
            es.index(index="de_asset_fb_app_users", id=user_doc_id, document=user_source)
            logger.info(f"Callback: Token saved for user '{user_source.get('fb_app_user_title')}'")

            # Use the saved access token to call the debug_token endpoint
            debug_token_url = "https://graph.facebook.com/debug_token"
            app_access_token = f"{client_id}|{client_secret}"  # The app token is constructed as {app_id}|{app_secret}
            params = {
                "input_token": access_token,  # The user access token you just obtained
                "access_token": app_access_token,  # The app access token for validation
            }

            try:
                # Make the request to Facebook's debug_token endpoint
                debug_response = requests.get(debug_token_url, params=params)
                debug_data = debug_response.json()

                # Check if the debug response contains an error
                if "error" in debug_data:
                    # error code fields only; the body stays generic.
                    logger.error(f"Callback: debug_token error ({_token_error_summary(debug_response, debug_data)})")
                    return jsonify({"error": "Resource 09 not found."}), 400

                # Extract 'expires_at' and 'user_id' from the debug data
                debug_info = debug_data.get("data", {})
                expires_at = debug_info.get("expires_at")
                user_id = debug_info.get("user_id")

                # Update the user document with the new fields
                user_source["fb_app_user_token_exp"] = expires_at
                user_source["fb_app_user_fbid"] = user_id

                # Save the updated user document back to Elasticsearch
                es.index(
                    index="de_asset_fb_app_users",
                    id=user_doc_id,
                    document=user_source,
                )
                logger.info(f"Callback: Token debug complete - expires_at: {expires_at}, fb_user_id: {user_id}")

                # self-closing popup. The data-connectors wizard
                # opens this URL in a popup window and polls the user-status
                # endpoint to know when the token has landed; we just need
                # the popup to close so the wizard tab can take over. The
                # Asset Explorer flow ends up back on the edit page it
                # opened from. window.close() is a no-op in non-popup
                # contexts, so the visible fallback message covers the
                # case where the user opened the OAuth URL in a regular tab.
                return _oauth_success_page()

            except Exception as e:
                # don't echo exception text in the response body.
                from lib.errors import safe_error_response

                return safe_error_response(
                    "Resource 10 not found.",
                    log_detail="Error during access token debugging",
                    exc=e,
                    body={},
                )

        else:
            # Handle error response from Facebook
            # status + error code only, in the log and never in the body.
            logger.error(f"Callback: Token exchange failed ({_token_error_summary(response, response_data)})")
            return jsonify({"error": "Resource 12 not found."}), 400

    except Exception as e:
        # don't echo exception text in the response body.
        from lib.errors import safe_error_response

        return safe_error_response(
            "Resource 13 not updated.",
            log_detail="Error during token exchange or saving access token",
            exc=e,
            body={},
        )


# Helper functions to manage the Gunicorn server
def stop_gunicorn():
    """Stop any running gunicorn processes."""
    try:
        print("Stopping any running Gunicorn processes...")
        # Use pkill to stop gunicorn processes directly.
        subprocess.run(["pkill", "-f", "gunicorn"], check=True)
        time.sleep(2)  # Give some time for processes to shut down
        print("Gunicorn processes stopped.")
    except subprocess.CalledProcessError as e:
        print(f"No running Gunicorn processes found or error stopping: {e}")


def start_gunicorn():
    """Start the Gunicorn server."""
    try:
        print("Starting FB OAuth web server with Gunicorn...")

        # Change to the directory containing fb_oauth_server.py
        os.chdir("/app/python")

        # Add the directory containing 'lib' to the PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = "/app/python"

        subprocess.run(
            ["gunicorn", "-b", "0.0.0.0:8083", "fb_oauth_server:app"],
            env=env,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error starting gunicorn process: {e}")
    except Exception as e:
        print(f"Unexpected error starting gunicorn process: {e}")


if __name__ == "__main__":
    try:
        stop_gunicorn()
        start_gunicorn()
    except KeyboardInterrupt:
        print("\nGracefully shutting down the server due to keyboard interrupt...")
        stop_gunicorn()
        sys.exit(0)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
