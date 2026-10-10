"""Daily validity check for the legacy Facebook credential chain.

 (2026-08-27) — this job used to read ``de_asset_fb_apps.fb_app_token``.
**Nothing imports with that field.** Both importers resolve their credential via
``de_asset_fb_app_users`` → ``get_fb_app_user_token()``:

* pages — ``get_page_insights.py`` builds ``fb_app_map`` from the app_users rows;
* ads   — ``get_insights.py`` calls ``get_fb_app_user_token(fb_app_user_esid)``.

Prod audit on 2026-08-27 measured the gap directly. All 12 ``de_asset_fb_app_users``
tokens are ``type=SYSTEM_USER``, ``is_valid=True``, with no ``expires_at`` and no
``data_access_expires_at`` — i.e. every credential the importers actually use is
healthy and non-expiring. Meanwhile ``fb_app_token`` held junk on all 8 app rows:
six carried a 32-char app *secret* (Meta: "Cannot parse access token" — never a
token at all), one agency app row carried a USER token that died 2026-03-20,
and 'Kavanah Analytics Unified' carried an APP token with ``scopes: []``.

The consequence was a daily FALSE ALARM: this job read that dead value, got
OAuth code 190, and recorded a ``connection_expired`` row naming an app whose real
credentials were fine. Every scheduled Meta run, since March.

So the check is repointed at the credentials that matter. A code-190 surfaced from
here now means an import really is broken. The vestigial ``fb_app_token`` field is
left in the mapping and the asset-management UI (``handlers/facebook_apps.py``,
``asset_configs.py`` still render it); ``tools/clear_stale_fb_app_tokens.py``
clears its dead values as a separate, reviewable step.
"""

import requests
import logging
import lib.logging
from datetime import datetime
from lib.encryption import decrypt, encrypt
from lib.es_helpers import get_all_documents, edit_document, get_document_by_id
from lib.fb_constants import FB_GRAPH_URL as _FB_GRAPH_BASE
from lib.fb_constants import FB_GRAPH_HTTP_TIMEOUT

FB_GRAPH_URL = f"{_FB_GRAPH_BASE}/"

# the index/field the importers actually authenticate with. Kept as
# constants so a future move (e.g. onto de_connection) is one edit, not a grep.
_APP_USER_INDEX = "de_asset_fb_app_users"
_APP_USER_TOKEN_FIELD = "fb_app_user_token"


# FB OAuth reconnect-class codes. 190 = access-token error
# (expired session / invalid / revoked). A dead legacy de_asset_fb_apps token
# raises one of these on the debug-token check or the exchange, and CANNOT
# auto-refresh — it needs a manual reconnect. We surface it once per affected
# app per run (de-duped) so the operator sees an actionable Report-Scheduler row.
_FB_RECONNECT_ERROR_CODES = {190}
# Apps whose expired-token error we've already surfaced this run — dedups per
# app so a multi-app run with two dead logins surfaces both, once each.
_expired_apps_recorded: set[str] = set()


def _fb_app_detail(app_id, app_title, user_title=None) -> str:
    """follow-up — operator-facing name of the dead FB credential:
    ``'<title> (app <id>)'``, degrading to whichever part is known.

    ``user_title`` names the specific ``de_asset_fb_app_users`` row
    whose token failed. An app carries a Pages credential and an Ads credential
    that can die independently, so "reconnect <app>" without saying WHICH of the
    two is dead sends the operator to re-mint both. Rendered as
    ``'<app title> — <user title> (app <id>)'``.
    """
    title = str(app_title).strip() if app_title else ""
    app = str(app_id).strip() if app_id else ""
    user = str(user_title).strip() if user_title else ""
    if title and user:
        title = f"{title} — {user}"
    elif user:
        title = user
    if title and app:
        return f"{title} (app {app})"
    if app:
        return f"app {app}"
    return title


def _record_credential_unhealthy(user_esid, app_title, user_title, category, detail):
    """persist durable health when a REAL run finds a dead credential.

    Deliberately one-directional: this writes UNHEALTHY only, never healthy.
    ``check_fb_token`` does not look at ``data_access_expires_at``, so if it were
    allowed to write ``healthy`` it would clobber a ``renewal_required`` verdict
    that the scheduled probe had correctly set — reporting a credential as fine
    on the strength of a check that never examined the clock that kills it.
    Self-clearing belongs to the probe, which does examine it.

    Fail-soft: health bookkeeping must never abort a token check.
    """
    if not user_esid:
        return
    try:
        from lib import credential_health as ch

        ch.record_classified_error(
            source_index=ch_source_index(),
            source_esid=user_esid,
            provider="facebook",
            label=f"{app_title or ''} — {user_title or ''}".strip(" —"),
            category=category,
            detail=detail or "",
        )
    except Exception as exc:  # noqa: BLE001 — never break the check
        logging.warning("could not record credential health: %s", exc)


def ch_source_index():
    """The index the health row is keyed on — kept as a function so the constant
    stays in one place alongside _APP_USER_INDEX."""
    return _APP_USER_INDEX


def _maybe_record_expired_fb_token(error_obj, app_id, app_title, user_title=None):
    """surface a reconnect-class FB token failure (code 190, expired
    session) from check_fb_token to the Report Scheduler, once per affected app.

    This is the real surfacing point for the legacy de_asset_fb_apps token: the
    dead token fails HERE (the [fb-check-token] job), not downstream in
    get_insights — account_wide teams authenticate with the healthy wizard token
    via WizardOAuthService, so get_insights never throws 190 (the original
     catch point was dead code in an all-account_wide deployment).

     follow-up (2026-06-29): the app name is passed as the classifier
    ``detail`` (woven into the user-facing message) rather than baked into the
    raw string — the classifier maps ``raw`` to a fixed category message and
    discards its text, so the app name baked into ``raw`` never reached the
    operator ("...for this account..." with no app). ``detail`` is the only
    channel that surfaces.

    ``error_obj`` is the FB Graph error dict (``{"message","code",...}``) or a
    string. No-op unless a progress_run_id is bound (main set it from
    --progress_run_id); a cron run with no run_id records nothing. Never raises.
    """
    code = None
    msg = ""
    if isinstance(error_obj, dict):
        code = error_obj.get("code")
        msg = str(error_obj.get("message") or "")
    else:
        msg = str(error_obj or "")
    is_reconnect = code in _FB_RECONNECT_ERROR_CODES or "code 190" in msg.lower() or "session has expired" in msg.lower()
    if not is_reconnect:
        return
    detail = _fb_app_detail(app_id, app_title, user_title)
    dedup_key = detail or "<unknown-fb-app>"
    if dedup_key in _expired_apps_recorded:
        return
    _expired_apps_recorded.add(dedup_key)
    try:
        from lib.import_run_context import record_user_error

        record_user_error(
            "Facebook login expired (OAuth code 190) — reconnect Facebook to resume imports.",
            connector_id="meta_ads",
            detail=detail or None,
        )
    except Exception as rec_err:  # noqa: BLE001 — surfacing must never break the token check
        logging.warning("failed to record expired-token error: %s", rec_err)


# Function to check and refresh a Facebook app-user token if needed
def refresh_fb_app_user_token(app_id, app_secret, access_token, user_esid, app_title=None, user_title=None):
    """Validate a ``de_asset_fb_app_users`` token; refresh if it expires within 5 days.

    repointed from ``de_asset_fb_apps.fb_app_token``, which no importer
    reads (see module docstring). Both the pages and ads importers authenticate
    with the app_user token this function now checks, so a failure here means an
    import really is broken.

    Returns True when the credential is usable, False when it needs a manual
    reconnect. Never raises.
    """
    lib.logging.define("fb-check-token")
    who = _fb_app_detail(app_id, app_title, user_title) or f"app {app_id}"
    debug_url = f"https://graph.facebook.com/debug_token?input_token={access_token}&access_token={app_id}|{app_secret}"

    try:
        response = requests.get(debug_url, timeout=FB_GRAPH_HTTP_TIMEOUT)
        data = response.json() or {}

        # Top-level error = our APP token (app_id|app_secret) is wrong, not the
        # credential under test. Still reconnect-class if Meta says 190.
        if "error" in data:
            logging.error(f"Error debugging token for {who}: {data['error'].get('message')}")
            _maybe_record_expired_fb_token(data["error"], app_id, app_title, user_title)
            return False

        payload = data.get("data") or {}

        # handle the NESTED failure shape. /debug_token answers 200
        # with {"data": {"is_valid": false, "error": {...}}} for a dead INPUT
        # token; the pre-fix code only looked at the top-level "error" key, so
        # this fell through to the expiry math and only surfaced later, as a
        # failed exchange. Reading is_valid directly is the honest signal and
        # skips a pointless exchange attempt against a token Meta already
        # rejected.
        if payload.get("error") or payload.get("is_valid") is False:
            err = payload.get("error") or {"message": "token reported is_valid=false"}
            logging.error(f"Token for {who} is not valid: {err}")
            _maybe_record_expired_fb_token(err, app_id, app_title, user_title)
            _record_credential_unhealthy(user_esid, app_title, user_title, "connection_expired", str(err.get("message") or ""))
            return False

        expires_at_timestamp = payload.get("expires_at")
        if not expires_at_timestamp:
            # Non-expiring credential — every prod app_user token is a SYSTEM_USER
            # token with no expiry, so this is the healthy steady state, not an
            # anomaly. Nothing to refresh.
            logging.info(f"Token for {who} does not expire (expires_at={expires_at_timestamp})")
            return True

        expires_at = datetime.fromtimestamp(expires_at_timestamp)
        days_until_expiration = (expires_at - datetime.now()).days

        if days_until_expiration < 5:
            # Refresh the token if it expires in less than 5 days
            exchange_url = f"{FB_GRAPH_URL}oauth/access_token?grant_type=fb_exchange_token&client_id={app_id}&client_secret={app_secret}&fb_exchange_token={access_token}"
            new_token_response = requests.get(exchange_url, timeout=FB_GRAPH_HTTP_TIMEOUT)
            new_token_data = new_token_response.json() or {}

            if "access_token" in new_token_data:
                new_access_token = new_token_data["access_token"]

                # Encrypt the new token before saving
                encrypted_access_token = encrypt(new_access_token)

                # write back to the app_user row we read from, NOT to
                # de_asset_fb_apps.fb_app_token. Refreshing a field no importer
                # reads was the whole defect.
                current_document = get_document_by_id(_APP_USER_INDEX, user_esid)
                if current_document is None:
                    logging.error(f"Could not retrieve the app_user document for {who} (esid={user_esid})")
                    return False

                current_document[_APP_USER_TOKEN_FIELD] = encrypted_access_token

                if edit_document(_APP_USER_INDEX, user_esid, current_document):
                    logging.info(f"Access token for {who} successfully refreshed and updated in Elasticsearch.")
                    return True
                logging.error(f"Failed to update access token for {who} in Elasticsearch.")
                return False
            else:
                err = new_token_data.get("error", "Unknown error")
                logging.error(f"Failed to refresh token for {who}: {err}")
                # a token that can't be exchanged is a dead login
                # needing a MANUAL reconnect (code 190 / expired session), not a
                # retry. Surface it to the Report Scheduler so the operator sees
                # an actionable row. No-op unless a progress_run_id is bound (set
                # in main from --progress_run_id).
                _maybe_record_expired_fb_token(err, app_id, app_title, user_title)
                _record_credential_unhealthy(
                    user_esid,
                    app_title,
                    user_title,
                    "connection_expired",
                    str(err.get("message") if isinstance(err, dict) else err),
                )
                return False
        else:
            logging.info(f"Access token for {who} is valid for more than 5 days.")
            return True

    except Exception as e:
        logging.error(f"Error refreshing token for {who}: {e}")
        return False


def main(params):
    lib.logging.define("fb-check-token")

    # bind the progress run so a dead-token failure (code 190) can
    # surface to the Report Scheduler via record_user_error. run_daily_imports
    # threads --progress_run_id into this subprocess on a scheduler/on-demand
    # run; the cron path passes nothing, so set_run_id(None) is a clean no-op
    # and the surfacing stays silent (just logs, as before).
    try:
        from lib.import_run_context import set_run_id

        set_run_id(params.get("progress_run_id") if params else None)
    except Exception as exc:  # noqa: BLE001 — never let run-context wiring break the check
        logging.warning("set_run_id failed: %s", exc)

    # when --team_ids is set, only debug tokens for FB apps that are
    # reachable from those teams. Tokens for other apps will be debugged the
    # next time their team's import runs.
    #
    # follow-up: workspaces are isolated. When the scope resolves to
    # an all-account_wide cohort (wizard signups), the legacy fb_app token
    # chain has no business being touched — wizard connection tokens are
    # refreshed via WizardOAuthService at OAuth callback time, not via this
    # job's legacy refresh_fb_token loop. Likewise, when team_ids matches
    # nothing or matches a misconfigured legacy team, refuse to fall back
    # to fleet-wide (strict mode mirrors the get_insights.py fix).
    team_ids = params.get("team_ids", False) if params else False
    scoped_app_esids = None
    if team_ids:
        # Local import — avoids pulling lib/assets into module-load time for
        # the unscoped path, which still has to work without a configured ES.
        from import_scripts.facebook.lib.assets import resolve_team_filter_scope

        scope = resolve_team_filter_scope(team_ids)
        if scope is None or not scope.get("team_esids"):
            logging.warning(
                f"check_fb_token:  strict mode — team_ids={team_ids!r} resolved to ZERO teams; "
                f"refusing to fall back to fleet-wide token refresh."
            )
            return
        if scope.get("all_account_wide"):
            logging.info(
                f"check_fb_token: team_ids={team_ids!r} resolved to "
                f"{len(scope.get('team_esids') or set())} account_wide team(s); skipping legacy "
                f"fb_app token check entirely (wizard tokens live on de_connection, refreshed by "
                f"WizardOAuthService at OAuth callback time)."
            )
            return
        if scope.get("fb_app_esids"):
            scoped_app_esids = list(scope["fb_app_esids"])
            logging.info(f"check_fb_token: scoped to {len(scoped_app_esids)} app(s) reachable from team_ids={team_ids!r}")
        else:
            # Legacy team matched but has no traversable fb_app chain AND is
            # not account_wide. Treating this as "check ALL apps" is exactly
            # the cross-tenant fan-out closes; refuse and abort.
            logging.warning(
                f"check_fb_token:  strict mode — team_ids={team_ids!r} matched team(s) "
                f"{sorted(scope.get('team_esids') or set())} but none have a traversable fb_app "
                f"chain AND none are account_wide; refusing to fall back to fleet-wide."
            )
            return

    # Step 1: Get Facebook apps from Elasticsearch (scoped if a team filter
    # narrowed the set, otherwise all apps).
    logging.info("Fetching Facebook apps from Elasticsearch...")
    if scoped_app_esids:
        from lib.es_helpers import search_index

        result = search_index(
            index_name="de_asset_fb_apps",
            query={"ids": {"values": scoped_app_esids}},
            size=len(scoped_app_esids),
        )
        fb_apps = result.get("hits", {}).get("hits", [])
    else:
        fb_apps = get_all_documents("de_asset_fb_apps")

    if not fb_apps:
        logging.error("No Facebook apps found in Elasticsearch.")
        return

    # Step 2: For each app, check every credential the importers can authenticate
    # with — the de_asset_fb_app_users rows under it. Local import so
    # the unscoped path still module-loads without a configured ES.
    from import_scripts.facebook.lib.assets import get_fb_app_users

    checked = healthy = failed = 0
    apps_without_users = []

    for fb_app_doc in fb_apps:
        fb_app = fb_app_doc["_source"]
        fb_app_esid = fb_app_doc["_id"]

        app_id = fb_app.get("fb_app_id")
        app_title = fb_app.get("fb_app_title")
        app_secret = decrypt(fb_app.get("fb_app_secret", ""))

        if not app_id or not app_secret:
            logging.warning(f"App {fb_app_esid} ({app_title!r}) missing app_id/app_secret. Skipping.")
            continue

        app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid}) or []
        if not app_users:
            # Not an error: an app row with no app_users contributes nothing to
            # the importer's credential ladder. Worth naming so an operator can
            # tell "no credentials configured" apart from "credentials broken".
            apps_without_users.append(f"{app_title!r} (app {app_id})")
            logging.info(f"App {app_title!r} ({app_id}) has no fb_app_users — nothing to check.")
            continue

        for user_doc in app_users:
            user_esid = user_doc["_id"]
            user_src = user_doc["_source"]
            user_title = user_src.get("fb_app_user_title")
            access_token = decrypt(user_src.get(_APP_USER_TOKEN_FIELD, "") or "")
            if not access_token:
                logging.warning(f"App user {user_title!r} ({user_esid}) has no token. Skipping.")
                continue

            checked += 1
            logging.info(f"Checking token for {app_title} | {user_title} | {app_id}...")
            ok = refresh_fb_app_user_token(
                app_id,
                app_secret,
                access_token,
                user_esid,
                app_title=app_title,
                user_title=user_title,
            )
            if ok:
                healthy += 1
            else:
                failed += 1

    # Summary line — a run that checked ZERO credentials is a silent-failure
    # surface (CLAUDE.md #22: an empty success is not success), so say so loudly
    # rather than exiting quietly like the old app-token loop did.
    if checked == 0:
        logging.warning(
            "check_fb_token: checked 0 credentials across %d app(s) — no fb_app_users "
            "with tokens were found. Apps without users: %s",
            len(fb_apps),
            ", ".join(apps_without_users) or "(none)",
        )
    else:
        logging.info(
            "check_fb_token: checked %d credential(s) — %d healthy, %d needing reconnect. " "Apps with no fb_app_users: %s",
            checked,
            healthy,
            failed,
            ", ".join(apps_without_users) or "(none)",
        )


if __name__ == "__main__":
    main({})
