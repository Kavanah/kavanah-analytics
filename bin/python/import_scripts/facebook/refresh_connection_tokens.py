"""Refresh wizard-cohort de_connection OAuth tokens.

Meta long-lived access tokens last ~60 days. Wizard signups mint a
long-lived token at OAuth callback time (``WizardOAuthService.
exchange_code_for_granted_scopes``) and persist it encrypted on
``de_connection.oauth_token_enc``. Without a scheduled refresh, the
first wizard cohort starts hitting 401s on every importer run ~60 days
after signup.

This job mirrors what ``check_fb_token.refresh_fb_token`` does for the
legacy ``de_asset_fb_apps`` index, but reads/writes ``de_connection``
instead. The two are deliberately kept as separate scripts:

* the legacy job is per-app and chained from the FB importer's
  team-scoped path (with strictness);
* this job is per-tenant-connection and intentionally fleet-wide —
  ``de_connection`` is one row per tenant by construction, so there is
  no cross-tenant fan-out concern.

Wizard Meta-app credentials (``client_id`` + ``client_secret``) come
from ``WizardOAuthService.get_meta_app()``. We resolve them ONCE at
job start; if the wizard meta-app row is missing (config gap), the job
logs at WARNING and exits cleanly — refusing to refresh against a
mis-configured / stale credential is safer than guessing.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/python")

import requests  # noqa: E402 — patched by tests via this module path

import lib.logging  # noqa: E402
from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import search_index  # noqa: E402
from lib.fb_constants import FB_GRAPH_URL  # noqa: E402

from asset_management.web.services.connection_service import (  # noqa: E402
    ConnectionService,
    PROVIDER_FACEBOOK,
)
from asset_management.web.services.wizard_oauth_service import (  # noqa: E402
    WizardOAuthService,
)

# Refresh tokens that expire within this many days. Matches the legacy
# refresh_fb_token threshold so operators only have one number to
# remember.
REFRESH_THRESHOLD_DAYS = 5

# retry budget for the ambiguous "counted but not retrieved" read.
# Elasticsearch intermittently returned hits.total.value > 0 with an EMPTY
# hits[] for the de_connection scan at the 02:30 cron (a read-after-refresh /
# segment-visibility blip on a single healthy shard — no shard failure, no
# alias). The pre-fix code read hits[] only, saw empty, and logged "no rows
# found" + exited — silently skipping the refresh of every real facebook
# connection (14 on prod) two mornings running. We now retry the read a few
# times with a short backoff before deciding; a genuine zero (total == 0)
# short-circuits without retrying.
CONNECTION_READ_RETRIES = 3
CONNECTION_READ_RETRY_DELAY_SECONDS = 2


def _debug_token(access_token: str, app_id: str, app_secret: str) -> dict | None:
    """Call Meta /debug_token. Returns the parsed ``data`` dict or None
    on any failure (network, non-200, JSON parse, missing data field).

    Errors are logged but never raised — the per-connection loop in
    ``main`` traps exceptions defensively, but keeping the boundary
    soft here too makes the call site simpler.
    """
    debug_url = "https://graph.facebook.com/debug_token"
    app_token = f"{app_id}|{app_secret}"
    response = requests.get(
        debug_url,
        params={"input_token": access_token, "access_token": app_token},
        timeout=10,
    )
    if response.status_code != 200:
        logging.warning("refresh_connection_tokens: /debug_token returned %s", response.status_code)
        return None
    body = response.json() or {}
    if "error" in body:
        logging.warning(
            "refresh_connection_tokens: /debug_token error: %s",
            body["error"].get("message"),
        )
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        logging.warning("refresh_connection_tokens: /debug_token missing data field")
        return None
    return data


def _exchange_for_long_lived(access_token: str, app_id: str, app_secret: str) -> tuple[str | None, int | None]:
    """Call ``oauth/access_token?grant_type=fb_exchange_token``. Returns
    ``(new_access_token, expires_in_seconds)`` on success or
    ``(None, None)`` on any failure. ``expires_in`` may be None even on
    success — Meta's contract doesn't guarantee the field.
    """
    exchange_url = f"{FB_GRAPH_URL}/oauth/access_token"
    response = requests.get(
        exchange_url,
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": access_token,
        },
        timeout=10,
    )
    if response.status_code != 200:
        logging.warning(
            "refresh_connection_tokens: fb_exchange_token returned %s",
            response.status_code,
        )
        return None, None
    body = response.json() or {}
    new_token = body.get("access_token")
    if not new_token:
        logging.warning("refresh_connection_tokens: fb_exchange_token missing access_token")
        return None, None
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return str(new_token), int(expires_in)
    return str(new_token), None


def _describe_connection(doc: dict) -> str:
    """Build a human-traceable identifier for a connection log line.

    The bare ES ``_id`` (e.g. ``Lclhy54BTHJFkQFQwjmm``) is opaque to an
    operator reading the cron log — it forces an ES lookup to find out
    *which* connector a line refers to. We prefix it with the two fields
    that actually identify the connector to a human: the wizard/grid
    ``label`` (what the user named it) and the owning ``principal``. The
    ESID stays in the line so an exact ES lookup is still one copy-paste
    away.

    Format: ``'<label>' [principal=<principal>, esid=<id>]``. Missing
    fields degrade gracefully — a blank label renders as ``''`` and a
    blank principal as ``principal=?`` so the line is never misleading.
    """
    source = doc.get("_source") or {}
    esid = doc.get("_id") or "<unknown>"
    label = source.get("label") or ""
    principal = source.get("principal") or "?"
    return f"'{label}' [principal={principal}, esid={esid}]"


def _process_connection(doc: dict, app_id: str, app_secret: str) -> str:
    """Refresh a single connection if its token expires soon.

    Returns one of ``refreshed``, ``skipped``, ``no_token``,
    ``debug_failed``, ``exchange_failed`` — used for the summary log
    line in ``main``. Any unexpected exception bubbles up so the
    caller's per-connection try/except can absorb + log it.
    """
    source = doc.get("_source") or {}
    connection_desc = _describe_connection(doc)
    encrypted_token = source.get("oauth_token_enc")
    if not encrypted_token:
        logging.info(
            "refresh_connection_tokens: connection %s has no oauth_token_enc — skipping",
            connection_desc,
        )
        return "no_token"

    access_token = decrypt(encrypted_token)
    if not access_token:
        logging.warning(
            "refresh_connection_tokens: connection %s decrypt produced empty string — skipping",
            connection_desc,
        )
        return "no_token"

    data = _debug_token(access_token, app_id, app_secret)
    if data is None:
        return "debug_failed"

    expires_at_unix = data.get("expires_at")
    if not expires_at_unix:
        # Non-expiring token (e.g. system user). Log and treat as skipped.
        logging.info(
            "refresh_connection_tokens: connection %s reports non-expiring token (expires_at=%r)",
            connection_desc,
            expires_at_unix,
        )
        return "skipped"

    expires_at = datetime.fromtimestamp(int(expires_at_unix))
    days_remaining = (expires_at - datetime.now()).days
    if days_remaining >= REFRESH_THRESHOLD_DAYS:
        logging.info(
            "refresh_connection_tokens: connection %s has %d days remaining — skipping",
            connection_desc,
            days_remaining,
        )
        return "skipped"

    logging.info(
        "refresh_connection_tokens: connection %s expires in %d days — exchanging for fresh long-lived token",
        connection_desc,
        days_remaining,
    )
    new_token, expires_in = _exchange_for_long_lived(access_token, app_id, app_secret)
    if not new_token:
        return "exchange_failed"

    # Compute the new ISO-8601 expires_at from expires_in seconds if Meta
    # returned it; otherwise leave it None and let upsert_connection
    # preserve whatever was there. Note: ``upsert_connection`` writes a
    # fresh ``token_expires_at`` from the kwarg, so None means "no
    # expiry recorded" rather than "preserve prior" — that's an honest
    # signal when Meta didn't tell us.
    new_token_expires_at: str | None = None
    if expires_in is not None:
        new_token_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # upsert_connection re-encrypts at rest and preserves created_at +
    # org_shared. The natural key (principal, provider, provider_account_id)
    # routes to the SAME doc we just read — see _find_by_natural_key.
    principal = source.get("principal") or ""
    provider = source.get("provider") or ""
    provider_account_id = source.get("provider_account_id") or ""
    label = source.get("label") or ""
    scopes = source.get("scopes") or []
    legacy_asset_index = source.get("legacy_asset_index")
    legacy_asset_esid = source.get("legacy_asset_esid")

    result = ConnectionService.upsert_connection(
        principal=principal,
        provider=provider,
        provider_account_id=provider_account_id,
        oauth_token=new_token,
        label=label,
        token_expires_at=new_token_expires_at,
        scopes=list(scopes),
        legacy_asset_index=legacy_asset_index,
        legacy_asset_esid=legacy_asset_esid,
    )
    if not result.get("success"):
        logging.error(
            "refresh_connection_tokens: upsert_connection failed for %s: %s",
            connection_desc,
            result.get("error"),
        )
        return "exchange_failed"

    logging.info(
        "refresh_connection_tokens: connection %s token refreshed successfully (esid=%s)",
        connection_desc,
        result.get("esid"),
    )
    return "refreshed"


def _parse_search_hits(result: object) -> tuple[list, int]:
    """pull (hits_list, total_value) out of an ES search response.

    Returns the retrieved ``hits.hits`` list and the reported ``hits.total.value``
    (ES 7+ shape ``{"value": N, "relation": ...}``; also tolerates a bare int for
    older shapes). A response without a ``.get`` accessor / missing key yields
    ``([], 0)``. The two are read SEPARATELY on purpose: an empty hits list with a
    non-zero total is the ambiguous "counted but not retrieved" read the caller
    must retry, not a genuine zero.
    """
    # search_index returns an elasticsearch ObjectApiResponse, NOT a plain dict
    # (isinstance(..., dict) is False), so duck-type on .get — the wrapper proxies
    # mapping access to its body. The original isinstance(dict) guard rejected
    # EVERY real response and made the job always see "0 rows".
    if not hasattr(result, "get"):
        return [], 0
    hits_obj = result.get("hits", {}) or {}
    hits_list = hits_obj.get("hits", []) or []
    total = hits_obj.get("total", 0)
    if isinstance(total, dict):
        total_value = total.get("value", 0) or 0
    elif isinstance(total, int):
        total_value = total
    else:
        total_value = 0
    return hits_list, total_value


def _load_facebook_connections() -> list | None:
    """read the provider=facebook de_connection rows, resilient to the
    transient "total>0 but empty hits" read.

    Returns the hits list (possibly empty for a GENUINE zero), or ``None`` when the
    read was AMBIGUOUS — ES reported rows exist (total>0) but returned none even
    after retries. ``None`` tells ``main`` to skip-and-retry-next-cycle with a
    WARNING rather than log the misleading "no rows found" and silently skip
    refreshing every real connection.
    """
    last_total = 0
    for attempt in range(CONNECTION_READ_RETRIES):
        result = search_index(
            index_name="de_connection",
            query={"term": {"provider": PROVIDER_FACEBOOK}},
            size=10000,
        )
        hits, total = _parse_search_hits(result)
        if hits:
            return hits
        if total == 0:
            # Genuine zero: no facebook connections exist. Clean, not a failure.
            return []
        # Ambiguous: total>0 but no hits retrieved. Retry after a short backoff.
        last_total = total
        if attempt < CONNECTION_READ_RETRIES - 1:
            logging.info(
                "refresh_connection_tokens: de_connection read returned 0 of %d reported rows " "(attempt %d/%d) — retrying.",
                total,
                attempt + 1,
                CONNECTION_READ_RETRIES,
            )
            time.sleep(CONNECTION_READ_RETRY_DELAY_SECONDS)
    # Exhausted retries with rows reported but never retrieved.
    logging.warning(
        "refresh_connection_tokens: de_connection reports %d facebook rows but returned NONE "
        "after %d attempts — transient read failure; skipping this run, will retry next cycle. "
        "(NOT 'no rows': the connections exist; this run refreshed nothing.)",
        last_total,
        CONNECTION_READ_RETRIES,
    )
    return None


def main(params: dict | None = None) -> None:
    lib.logging.define("fb-refresh-connection-tokens")
    del params  # accepted for import.py's main(params) contract; unused

    # Resolve wizard Meta-app credentials ONCE. If the wizard meta-app
    # row is missing, refuse to fall back to fleet-wide — same
    # philosophy as strict-mode abort. An operator who hasn't
    # configured the wizard app yet has no business running this job.
    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        logging.warning(
            "refresh_connection_tokens: WizardOAuthService.get_meta_app() returned None — "
            "no wizard Meta app configured; nothing to refresh. Exiting cleanly."
        )
        return

    # hard-delete abandoned wizard OAuth placeholders before the
    # refresh read. The data-connectors FB wizard mints a pending_token row
    # at oauth-START; an abandoned consent popup leaves it tokenless forever
    # (prod 2026-06-27: 5 orphans across 3 users). They are provably inert
    # (no token, no asset/team links, excluded from billing), so we delete
    # rather than carry a 5th status. Fail-soft — a sweep error must not
    # block the refresh below.
    swept = ConnectionService.sweep_abandoned_pending_connections()
    if swept:
        logging.info(
            "refresh_connection_tokens: swept %d abandoned wizard placeholder(s) before refresh",
            swept,
        )

    # Scan de_connection for provider=facebook. IG tokens are NOT
    # written as separate connection rows — the wizard's IG importer
    # reads the parent FB connection's token (Meta OAuth grants both
    # FB Ads and IG via instagram_basic + instagram_manage_insights
    # scopes from the same access_token). When a future ticket introduces
    # standalone IG / GA4 / etc. connections that DO carry their own
    # refreshable tokens, file a separate job per ticket scope-note.
    #
    # _load_facebook_connections distinguishes a genuine zero
    # (returns []) from a transient "total>0 but empty hits" read (returns
    # None after retrying). The pre-fix code conflated the two and silently
    # skipped refreshing every real connection on a flaky read.
    hits = _load_facebook_connections()
    if hits is None:
        # Ambiguous read already WARNED inside the helper. Skip this run.
        return
    if not hits:
        logging.info("refresh_connection_tokens: no facebook de_connection rows found.")
        return

    counts: dict[str, int] = {}
    for doc in hits:
        connection_desc = _describe_connection(doc)
        try:
            outcome = _process_connection(doc, meta_app.client_id, meta_app.client_secret)
        except Exception as e:  # noqa: BLE001 — top-level boundary; per-connection isolation
            logging.warning(
                "refresh_connection_tokens: connection %s raised %s: %s — continuing loop",
                connection_desc,
                type(e).__name__,
                e,
            )
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1

    logging.info("refresh_connection_tokens: completed run — outcomes=%s", counts)


if __name__ == "__main__":
    main({})
