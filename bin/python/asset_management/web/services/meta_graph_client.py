"""
Meta Graph API client for wizard step-6.

Single-purpose client that enumerates a user's ad accounts via
``/me/adaccounts`` and computes a per-account ``last_activity_at``
timestamp so recency-driven default-select has a real
signal to rank on. The wizard's
``WizardAssetCatalogService.list_for_session`` is the only caller.

Why a separate module rather than inlining in the catalog service:
the catalog service is shape + mapping logic (``WizardAsset``
dataclass, default-select, friendly-name validation); the Graph API
work is network I/O with pagination, status mapping, and 401 handling.
Tests mock this module wholesale rather than threading ``requests``
patches into the catalog service's namespace.

 deliberately keeps the v1 fetch narrow:

* ``/me/adaccounts`` with the six fields the AC names — ``id``, ``name``,
  ``account_status``, ``currency``, ``business``, ``timezone_name``.
* Per-account ``/insights?date_preset=last_30d&fields=date_stop&limit=1``
  to source ``last_activity_at``. Insights failures are non-fatal
  (the row still renders, just without a recency hint — sorts last
  in the default-select ranker, which is the right behaviour for
  accounts with no recent spend).
* No Pages / IG / Marketing-API extras. Those land once
  Meta App Review approves the additional scopes on app
  1858829444798788.
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable

sys.path.insert(0, "/app/python")

import requests  # noqa: E402 — top-level so tests can patch the module path

from lib.fb_constants import FB_GRAPH_URL  # noqa: E402

logger = logging.getLogger(__name__)


def _graph_error_summary(resp) -> str:
    """One-line summary of a non-200 Graph response for logging.

    Extracts Meta's structured error object (message / type / code /
    error_subcode / fbtrace_id) rather than dumping the raw body, so the
    log line is bounded and can never echo a token. A status code alone
    is not diagnosable — a permissions refusal, a rate limit and a bad id
    all logged identically, which cost three diagnosis rounds on.
    """
    try:
        err = (resp.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001 — non-JSON body; report that fact only
        return f"status={resp.status_code} (non-JSON body)"
    if not isinstance(err, dict) or not err:
        return f"status={resp.status_code} (no error object)"
    parts = [f"status={resp.status_code}"]
    for key in ("code", "error_subcode", "type", "fbtrace_id"):
        if err.get(key):
            parts.append(f"{key}={err[key]}")
    message = str(err.get("message") or "")[:300]
    if message:
        parts.append(f"message={message!r}")
    return " ".join(parts)


# Meta's documented account_status codes. Captured here as constants
# rather than magic numbers so the mapping (active vs disabled vs
# grace-period) is readable at the call site. Source: Marketing API
# AdAccount.account_status enum.
_STATUS_ACTIVE = 1
_STATUS_DISABLED = 2
_STATUS_UNSETTLED = 3
_STATUS_PENDING_RISK_REVIEW = 7
_STATUS_PENDING_SETTLEMENT = 8
_STATUS_IN_GRACE_PERIOD = 9
_STATUS_PENDING_CLOSURE = 100
_STATUS_CLOSED = 101
_STATUS_ANY_ACTIVE = 102
_STATUS_ANY_CLOSED = 201


# Default page size for /me/adaccounts. Meta's default is 25; bumping
# to 100 reduces round-trips for users on Business Managers with many
# ad accounts. Still walks pagination via ``paging.next``.
_PAGE_SIZE = 100

# Pagination safety cap — if Meta returns a runaway cursor chain
# (defensive; not observed in practice), stop walking after this many
# pages so a single wizard step-6 GET can't hang forever. 50 pages at
# 100 rows/page = 5000 accounts, far above any realistic wizard user.
_MAX_PAGES = 50

# Per-request timeout for every Graph call. Wizard step-6 blocks on
# this; long timeouts make the page feel broken. 10s matches
# WizardOAuthService.
_REQUEST_TIMEOUT_SECONDS = 10


class TokenExpiredError(Exception):
    """Provider returned 401 / token-expired on a Graph API call.

    Re-exposed under this name so callers in
    ``wizard_asset_catalog_service`` can catch a single exception type
    regardless of which inner Graph call (``/me/adaccounts`` or
    ``/<act_id>/insights``) raised. The catalog service re-exports its
    own ``TokenExpiredError`` for the route's existing handler — the two
    classes are deliberately distinct so a test that mocks one of them
    doesn't accidentally suppress the other.
    """


def list_ad_accounts(access_token: str) -> list[dict]:
    """Return the normalised list of ad accounts under the token's user.

    Each item is a dict with keys ``id``, ``name``, ``account_status``,
    ``currency``, ``business_manager_hint``, ``timezone_name``, and
    ``last_activity_at``. Callers map these onto ``WizardAsset`` rows.

    Pagination walks ``paging.next`` until exhausted or
    ``_MAX_PAGES``. A missing or malformed cursor stops the walk and
    returns what we have so far — partial data beats no data on a
    transient Graph hiccup.

    Raises ``TokenExpiredError`` on a 401 from ``/me/adaccounts``.
    Other non-200 responses log + return an empty list (the route
    surfaces the existing zero-accounts empty state in that case).

    Per-account insights failures are logged + treated as "no
    activity" (``last_activity_at=None``). ranker sorts None
    last so this degrades gracefully — the row still renders, just
    not auto-selected.
    """
    if not access_token:
        return []

    accounts: list[dict] = []
    next_url: str | None = (
        f"{FB_GRAPH_URL}/me/adaccounts"
        f"?fields=id,name,account_status,currency,business,timezone_name"
        f"&limit={_PAGE_SIZE}"
        f"&access_token={access_token}"
    )
    pages_walked = 0

    while next_url and pages_walked < _MAX_PAGES:
        pages_walked += 1
        try:
            resp = requests.get(next_url, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 — top-level network boundary
            # nosemgrep: python-logger-credential-disclosure - logs requests exception, not token
            logger.error("meta_graph_client.list_ad_accounts: GET failed: %s", e)
            break

        if resp.status_code == 401:
            raise TokenExpiredError("Meta /me/adaccounts returned 401")
        if resp.status_code != 200:
            # nosemgrep: python-logger-credential-disclosure - logs Meta's structured error, not token
            logger.warning("meta_graph_client.list_ad_accounts: %s", _graph_error_summary(resp))
            break

        try:
            body = resp.json() or {}
        except Exception as e:  # noqa: BLE001
            logger.error("meta_graph_client.list_ad_accounts: body not JSON: %s", e)
            break

        data = body.get("data")
        if not isinstance(data, list):
            break
        for raw in data:
            if not isinstance(raw, dict):
                continue
            normalised = _normalise_account(raw, access_token=access_token)
            if normalised is not None:
                accounts.append(normalised)

        # Walk pagination via paging.next. Meta returns a fully-qualified
        # URL with the cursor + access_token already embedded.
        paging = body.get("paging") or {}
        candidate = paging.get("next")
        if isinstance(candidate, str) and candidate:
            next_url = candidate
        else:
            next_url = None

    return accounts


# Phase 3 — Graph error codes that positively mean "this token may
# not read this ad account's insights" (permission denied / not visible to
# the token). Anything else is treated as unverified (fail-open) so a Meta
# hiccup never blocks a legitimate link.
_PERMISSION_ERROR_CODES = {10, 200, 272}


def can_read_ad_account_insights(account_id: str, access_token: str) -> tuple[bool, str]:
    """Phase 3 — probe whether ``access_token`` has ads_read on the
    ad account (the cross-BM case, one team: an ad account under a different
    Business Manager than the page owner may not be readable by the authing
    login even though it was typed/selected).

    One ``/act_<id>/insights?limit=1`` GET — the same ads_read-gated endpoint
    the importer will hit daily, so a pass here means the daily import will
    work with this token.

    Returns ``(allowed, detail)``:

    * ``(True, "")`` — probe succeeded.
    * ``(False, <reason>)`` — Meta POSITIVELY denied access (error code in
      ``_PERMISSION_ERROR_CODES``, or code 100 — "object does not exist or
      no permission", Meta's deliberate existence-hiding response).
    * ``(True, "unverified: <reason>")`` — transient/unknown failure —
      fail-open, callers proceed but may log the detail.
    """
    if not account_id or not access_token:
        return True, "unverified: missing account_id or token"
    bare = str(account_id).strip()
    if bare.startswith("act_"):
        bare = bare[4:]
    url = f"{FB_GRAPH_URL}/act_{bare}/insights"
    try:
        resp = requests.get(
            url,
            params={"limit": 1, "fields": "date_stop", "access_token": access_token},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — network boundary; fail-open
        # nosemgrep: python-logger-credential-disclosure - logs requests exception, not token
        return True, f"unverified: probe request failed: {e}"

    if resp.status_code == 200:
        return True, ""

    try:
        error = (resp.json() or {}).get("error") or {}
    except Exception:  # noqa: BLE001
        error = {}
    code = error.get("code")
    message = error.get("message") or f"HTTP {resp.status_code}"
    if code in _PERMISSION_ERROR_CODES or code == 100:
        return False, f"Graph error code={code}: {message}"
    return True, f"unverified: Graph error code={code}: {message}"


def _normalise_account(raw: dict, *, access_token: str) -> dict | None:
    """Map one /me/adaccounts row onto the wizard's normalised shape.

    Returns None if the row is missing a usable ``id`` — those can't
    be rendered (the wizard's selection allowlist keys off the id).

    Per-account ``last_activity_at`` is fetched via
    ``/<act_id>/insights`` with ``date_preset=last_30d``, ``limit=1``,
    ``fields=date_stop``. Failures are non-fatal; we return the row
    with ``last_activity_at=None`` so ranker can still
    process it.
    """
    account_id = (raw.get("id") or "").strip()
    if not account_id:
        return None
    name = (raw.get("name") or "").strip() or account_id
    status_int = _coerce_int(raw.get("account_status"))
    currency = (raw.get("currency") or "").strip()
    timezone_name = (raw.get("timezone_name") or "").strip()
    business = raw.get("business") or {}
    bm_name = (business.get("name") or "").strip() if isinstance(business, dict) else ""

    return {
        "id": account_id,
        "name": name,
        "account_status": status_int,
        "currency": currency,
        "business_manager_hint": bm_name,
        "timezone_name": timezone_name,
        "last_activity_at": _fetch_last_activity_at(account_id, access_token),
    }


def _fetch_last_activity_at(account_id: str, access_token: str) -> str | None:
    """Fetch the most recent ``date_stop`` from /<act_id>/insights.

    Returns an ISO-8601 date string (e.g. ``"2026-05-19"``) on
    success or None on any failure. Failures are deliberately
    swallowed — an account with no insights data is the common case
    for newly-created ad accounts, and surfacing "we couldn't read
    activity for this account" as an error would be noisier than
    useful at the wizard surface.

    A token-expired surface inside this call still raises
    ``TokenExpiredError`` so the outer list_ad_accounts loop can
    propagate it. Only the recoverable 4xx/5xx/parse paths fall
    through to None.
    """
    if not account_id or not access_token:
        return None
    url = (
        f"{FB_GRAPH_URL}/{account_id}/insights"
        f"?date_preset=last_30d&fields=date_stop&limit=1"
        f"&access_token={access_token}"
    )
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001
        # nosemgrep: python-logger-credential-disclosure - logs exception, not token
        logger.info(
            "meta_graph_client._fetch_last_activity_at(%s): GET failed: %s",
            account_id,
            e,
        )
        return None
    if resp.status_code == 401:
        raise TokenExpiredError("Meta /insights returned 401")
    if resp.status_code != 200:
        return None
    try:
        body = resp.json() or {}
    except Exception:
        return None
    data = body.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    date_stop = (first.get("date_stop") or "").strip()
    return date_stop or None


def map_account_status(account_status: int | None) -> tuple[str, str]:
    """Map a Meta account_status code to (status_code, status_label).

    The wizard surfaces three status buckets — active, paused,
    disabled — collapsing Meta's 10-code enum onto the prototype's
    three-color UX. Mapping rules:

    * ``1`` (active) → ASSET_STATUS_ACTIVE / "Active"
    * ``9`` (in_grace_period) → ASSET_STATUS_ACTIVE / "Active" with
      a grace-period hint surfaced in business_manager_hint by the
      caller.
    * Anything else (including None) → ASSET_STATUS_DISABLED /
      "Disabled".

    Returning a tuple keeps the catalog service's mapping site
    obvious — no caller has to remember which enum maps to which copy.
    """
    # Local import keeps this module's import surface lean; the
    # catalog service is the only consumer that needs the constants.
    from asset_management.web.services.wizard_asset_catalog_service import (
        ASSET_STATUS_ACTIVE,
        ASSET_STATUS_DISABLED,
    )

    if account_status == _STATUS_ACTIVE:
        return (ASSET_STATUS_ACTIVE, "Active")
    if account_status == _STATUS_IN_GRACE_PERIOD:
        return (ASSET_STATUS_ACTIVE, "Active")
    return (ASSET_STATUS_DISABLED, "Disabled")


def _coerce_int(value) -> int | None:
    """Return ``value`` as an int, or None on any conversion failure.

    Meta returns ``account_status`` as a JSON number, but the test
    surface (and a defensive prod path) may see it as a string. Falling
    back to None routes through the default-disabled branch of
    map_account_status, which is the safest default for an
    unparseable status.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_grace_period(account_status: int | None) -> bool:
    """Return True iff this account is in Meta's billing grace period.

    Surfaced separately from map_account_status so the catalog
    service can append "Grace period" to ``business_manager_hint``
    without having to re-parse the status integer.
    """
    return account_status == _STATUS_IN_GRACE_PERIOD


def list_pages_on_ad_account(
    access_token: str,
    ad_account_id: str,
) -> list[dict]:
    """List Facebook Pages associated with an ad account.

    Returns ``[{"id": "<page_id>", "name": "<page_title>"}, ...]``.
    Walks ``/<act_id>/promote_pages?fields=id,name`` with paging.next
    until exhausted or ``_MAX_PAGES``. Caller is the wizard
    bridge, which uses this list as the seed for the
    ``/<page_id>?fields=instagram_business_account`` walk.

    Raises ``TokenExpiredError`` on a 401 so the route handler can
    bounce the user back to step-5 for re-OAuth (same posture as
    ``list_ad_accounts``). Other non-200 responses log + return what
    was collected before the failure — partial data beats no data on
    a transient hiccup.

    ``ad_account_id`` may be supplied with or without the ``act_``
    prefix; the helper accepts both shapes since callers store the
    bare id in some places and
    the prefixed id in others (the wizard's ``accepted_ids``). The
    Graph API itself requires the prefix, so this normalises before
    the GET.
    """
    if not access_token or not ad_account_id:
        return []
    aid = ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"

    pages: list[dict] = []
    next_url: str | None = (
        f"{FB_GRAPH_URL}/{aid}/promote_pages" f"?fields=id,name" f"&limit={_PAGE_SIZE}" f"&access_token={access_token}"
    )
    pages_walked = 0
    while next_url and pages_walked < _MAX_PAGES:
        pages_walked += 1
        try:
            resp = requests.get(next_url, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001 — top-level network boundary
            # nosemgrep: python-logger-credential-disclosure - logs requests exception, not token
            logger.error("meta_graph_client.list_pages_on_ad_account(%s): GET failed: %s", aid, e)
            break

        if resp.status_code == 401:
            raise TokenExpiredError("Meta /promote_pages returned 401")
        if resp.status_code != 200:
            # nosemgrep: python-logger-credential-disclosure - logs Meta's structured error, not token
            logger.warning(
                "meta_graph_client.list_pages_on_ad_account(%s): %s",
                aid,
                _graph_error_summary(resp),
            )
            break

        try:
            body = resp.json() or {}
        except Exception as e:  # noqa: BLE001
            logger.error("meta_graph_client.list_pages_on_ad_account(%s): body not JSON: %s", aid, e)
            break

        data = body.get("data")
        if not isinstance(data, list):
            break
        for raw in data:
            if not isinstance(raw, dict):
                continue
            pid = (raw.get("id") or "").strip()
            if not pid:
                continue
            pages.append({"id": pid, "name": (raw.get("name") or "").strip() or pid})

        paging = body.get("paging") or {}
        candidate = paging.get("next")
        next_url = candidate if isinstance(candidate, str) and candidate else None

    return pages


def get_instagram_business_account(
    access_token: str,
    page_id: str,
) -> dict | None:
    """Resolve the Instagram Business account linked to a Page, or None.

    Calls ``/<page_id>?fields=instagram_business_account{id,username,name}``.
    Returns ``{"id": "<ig_account_id>", "username": "<handle>",
    "name": "<display_name>"}`` when the Page has a linked IG Business
    account; ``None`` when no link is present.

    Meta returns "no linked IG account" in three observed shapes:
    the ``instagram_business_account`` field is omitted entirely, set
    to ``null``, or set to ``{}``. All three collapse to ``None``
    here so callers don't have to defend against the variance.

    Raises ``TokenExpiredError`` on a 401 so the bridge can
    surface re-auth uniformly with the ad-account list call. Any
    other non-200 logs + returns None (silent skip — many Pages
    legitimately have no linked IG, and a transient 5xx shouldn't
    abort the bridge for the remaining Pages).
    """
    if not access_token or not page_id:
        return None
    url = (
        f"{FB_GRAPH_URL}/{page_id}"
        f"?fields=instagram_business_account%7Bid%2Cusername%2Cname%7D"
        f"&access_token={access_token}"
    )
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001
        # nosemgrep: python-logger-credential-disclosure - logs exception, not token
        logger.warning(
            "meta_graph_client.get_instagram_business_account(%s): GET failed: %s",
            page_id,
            e,
        )
        return None

    if resp.status_code == 401:
        raise TokenExpiredError("Meta /<page_id>?fields=instagram_business_account returned 401")
    if resp.status_code != 200:
        # WARNING, not INFO — a refused page lookup is a page that silently
        # yields no Instagram; at INFO with only a status code the
        # failure was invisible in a log grep for warnings, and undiagnosable
        # when found (permissions vs rate limit vs bad id all looked alike).
        # nosemgrep: python-logger-credential-disclosure - logs Meta's structured error, not token
        logger.warning(
            "meta_graph_client.get_instagram_business_account(%s): %s",
            page_id,
            _graph_error_summary(resp),
        )
        return None

    try:
        body = resp.json() or {}
    except Exception:
        return None

    ig = body.get("instagram_business_account")
    if not isinstance(ig, dict) or not ig:
        return None
    ig_id = (ig.get("id") or "").strip()
    if not ig_id:
        return None
    return {
        "id": ig_id,
        "username": (ig.get("username") or "").strip(),
        "name": (ig.get("name") or "").strip(),
    }


def status_codes() -> Iterable[int]:
    """Return the set of account_status codes the client recognises.

    Used by tests to assert exhaustive coverage. The mapping
    deliberately collapses everything outside (1, 9) onto "disabled"
    so callers don't need to know about codes 2/3/7/8/100/101/102/201,
    but the exhaustive list documents the underlying enum.
    """
    return (
        _STATUS_ACTIVE,
        _STATUS_DISABLED,
        _STATUS_UNSETTLED,
        _STATUS_PENDING_RISK_REVIEW,
        _STATUS_PENDING_SETTLEMENT,
        _STATUS_IN_GRACE_PERIOD,
        _STATUS_PENDING_CLOSURE,
        _STATUS_CLOSED,
        _STATUS_ANY_ACTIVE,
        _STATUS_ANY_CLOSED,
    )
