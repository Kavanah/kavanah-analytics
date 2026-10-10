"""Read-only Meta credential prober.

One implementation, three consumers:

* scheduled sweep — is every stored credential still usable?
* Tier 2's coverage check — after a re-auth, which assets does the NEW
  token actually reach? (Same call, different token.)
* token→asset binding, if that lands.

Everything here is READ-ONLY: ``/debug_token`` and GET probes against individual
assets. Nothing mutates Meta state and nothing writes to Elasticsearch — callers
own persistence. Never returns or logs a token value (CLAUDE.md #26/#37); only
lengths, types, ids and verdicts.

Why an asset-level probe and not just ``/debug_token``
------------------------------------------------------
A token can be perfectly valid and still reach nothing. Meta's consent dialog has
a separate ASSET-SELECTION step, so a user can grant every scope while ticking
zero pages; the resulting token is structurally valid and returns empty lists
forever. ``/debug_token`` cannot see that — only touching each asset can. This is
the concrete mechanism behind "it says connected but isn't".

Dependency order matters
------------------------
An Instagram Business account is reached THROUGH its Facebook Page's
``instagram_business_account`` edge. If the Page is not granted, the IG failure is
a CONSEQUENCE, not an independent second fault. ``probe_assets`` therefore records
``caused_by`` on a dependent failure so callers can render
"Page X not granted → Instagram Y unreachable" instead of two unrelated red rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from lib.fb_constants import FB_GRAPH_URL

# versioned. An unversioned Marketing API call (act_<id>) is now
# rejected by Meta with "(#2635) You are calling a deprecated version of the Ads
# API", which made every ad-account coverage probe fail and staged otherwise
# good re-auth tokens as "partial". Pages / IG tolerated the unversioned form,
# which is why only ad accounts broke.
GRAPH = FB_GRAPH_URL
_TIMEOUT = 20

# Meta OAuth codes that mean "this credential is dead, a human must re-auth".
RECONNECT_CODES = {190}
# Codes that mean "the credential lives, but it cannot see THIS asset".
PERMISSION_CODES = {10, 200, 272, 298, 803}
# Meta's transient codes: rate limits (4, 17, 32) and the temporary
# "please retry" (613). They say nothing about permissions, so the per-asset cause
# classifier assigns no cause to them; derive_status still counts them as a
# denial so the credential is looked at, but the banner must not send a reader
# to a Business Manager admin for a throttle.
TRANSIENT_CODES = {4, 17, 32, 613}


@dataclass
class AssetProbe:
    """Result of touching one asset with one token."""

    asset_type: str  # 'page' | 'ig' | 'ad_account'
    asset_id: str
    asset_name: str = ""
    ok: bool = False
    error: str = ""
    error_code: int | None = None
    # Set when this failure is downstream of another asset's failure (IG via Page).
    caused_by: str = ""
    # set when THIS token cannot reach the asset but another credential
    # in the same sweep whose scope includes it can: the importers try every
    # in-scope credential and stop at the first that works, so the asset still
    # imports and this failure must not drive permission_lost. Holds the label of
    # the credential that reached it.
    covered_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_type": self.asset_type,
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "ok": self.ok,
            "error": self.error,
            "error_code": self.error_code,
            "caused_by": self.caused_by,
            "covered_by": self.covered_by,
        }


@dataclass
class TokenProbe:
    """Result of ``/debug_token`` on one credential."""

    reachable: bool = False  # did we get an answer from Meta at all?
    is_valid: bool = False
    token_type: str = ""
    app_id: str = ""
    user_id: str = ""
    scopes: list[str] = field(default_factory=list)
    expires_at: int | None = None
    data_access_expires_at: int | None = None
    error: str = ""
    error_code: int | None = None

    @property
    def days_until_data_access_expiry(self) -> int | None:
        """Days until Meta's 90-day data-access window closes, or None.

        This is the ONLY live clock on our Meta credentials. Prod audit
        2026-08-27: every stored token reports no ``expires_at`` at all, so an
        expiry check built on that field can never fire.
        """
        return _days_until(self.data_access_expires_at)

    @property
    def days_until_token_expiry(self) -> int | None:
        return _days_until(self.expires_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "is_valid": self.is_valid,
            "token_type": self.token_type,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at,
            "data_access_expires_at": self.data_access_expires_at,
            "error": self.error,
            "error_code": self.error_code,
        }


def _days_until(ts: int | None) -> int | None:
    if not ts:
        return None
    try:
        when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return (when - datetime.now(timezone.utc)).days


def _get(path: str, token: str, **params) -> tuple[dict | None, str]:
    """GET a Graph path. Returns ``(body, transport_error)``.

    ``body`` is None only on a transport/parse failure — which is explicitly NOT
    the same as "the credential is bad". Callers must not downgrade a credential's
    health on a transport error, or a network blip would mark the whole fleet dead.
    """
    params["access_token"] = token
    try:
        resp = requests.get(f"{GRAPH}/{path}", params=params, timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — network
        return None, f"{type(exc).__name__}"
    try:
        return resp.json() or {}, ""
    except Exception:  # noqa: BLE001 — non-JSON body
        return None, f"HTTP {resp.status_code} non-JSON"


def probe_token(token: str, app_id: str, app_secret: str) -> TokenProbe:
    """``/debug_token`` a credential. Read-only; never raises."""
    out = TokenProbe()
    if not token or not app_id or not app_secret:
        out.error = "missing token or app credentials"
        return out

    body, transport_err = _get("debug_token", f"{app_id}|{app_secret}", input_token=token)
    if body is None:
        out.error = f"unreachable: {transport_err}"
        return out

    out.reachable = True

    # A TOP-LEVEL error means our app token was rejected — that says nothing
    # definitive about the credential under test, so leave is_valid False but
    # record why. Callers treat "reachable but not conclusive" as unknown.
    if "error" in body:
        err = body["error"] or {}
        out.error = str(err.get("message") or "app-token rejected")
        out.error_code = err.get("code")
        return out

    data = body.get("data")
    if not isinstance(data, dict):
        out.error = "malformed /debug_token response (no data object)"
        return out

    out.is_valid = bool(data.get("is_valid"))
    out.token_type = str(data.get("type") or "")
    out.app_id = str(data.get("app_id") or "")
    out.user_id = str(data.get("user_id") or "")
    out.scopes = list(data.get("scopes") or [])
    out.expires_at = data.get("expires_at") or None
    out.data_access_expires_at = data.get("data_access_expires_at") or None

    # The NESTED failure shape: 200 + data.is_valid=false + data.error. This is
    # how Meta reports a dead INPUT token, and reading only the top-level "error"
    # key misses it entirely.
    nested = data.get("error")
    if nested:
        out.error = str(nested.get("message") or "token reported invalid")
        out.error_code = nested.get("code")
    elif not out.is_valid:
        out.error = "token reported is_valid=false"

    return out


def probe_assets(token: str, assets: list[dict]) -> list[AssetProbe]:
    """Touch each asset with ``token`` and report per-asset reachability.

    ``assets`` is a list of ``{"asset_type", "asset_id", "asset_name"}`` dicts.
    Probes run in dependency order — pages first, then the Instagram accounts that
    hang off them — so a dependent failure can name its cause instead of
    presenting as a second independent fault.

    Read-only: every probe is a GET of the asset's own id.
    """
    results: list[AssetProbe] = []
    failed_pages: dict[str, str] = {}  # page_id -> error

    def _probe_one(asset: dict) -> AssetProbe:
        a_type = str(asset.get("asset_type") or "")
        a_id = str(asset.get("asset_id") or "")
        probe = AssetProbe(a_type, a_id, str(asset.get("asset_name") or ""))
        if not a_id:
            probe.error = "asset has no id"
            return probe

        # Ad accounts need the act_ prefix; pages and IG accounts are bare ids.
        path = a_id if a_type != "ad_account" or a_id.startswith("act_") else f"act_{a_id}"
        body, transport_err = _get(path, token, fields="id,name")
        if body is None:
            probe.error = f"unreachable: {transport_err}"
            return probe
        if "error" in body:
            err = body["error"] or {}
            probe.error = str(err.get("message") or "unknown error")
            probe.error_code = err.get("code")
            return probe

        probe.ok = True
        if body.get("name"):
            probe.asset_name = str(body["name"])
        return probe

    pages = [a for a in assets if a.get("asset_type") == "page"]
    others = [a for a in assets if a.get("asset_type") != "page"]

    for asset in pages:
        probe = _probe_one(asset)
        if not probe.ok:
            failed_pages[probe.asset_id] = probe.error
        results.append(probe)

    for asset in others:
        probe = _probe_one(asset)
        # An IG account is reached through its parent Page. If that Page already
        # failed, say so rather than reporting an unexplained second failure.
        parent = str(asset.get("parent_page_id") or "")
        if not probe.ok and asset.get("asset_type") == "ig" and parent in failed_pages:
            probe.caused_by = parent
        results.append(probe)

    return results


def summarize(token_probe: TokenProbe, asset_probes: list[AssetProbe]) -> dict[str, Any]:
    """Condense a probe into the shape callers persist or render.

    ``covered``/``total`` is what coverage check gates on: a re-auth
    that reaches 2 of 7 assets must not be committed silently.
    """
    total = len(asset_probes)
    covered = sum(1 for p in asset_probes if p.ok)
    return {
        "token": token_probe.as_dict(),
        "assets": [p.as_dict() for p in asset_probes],
        "covered": covered,
        "total": total,
        "fully_covered": total > 0 and covered == total,
        "days_until_data_access_expiry": token_probe.days_until_data_access_expiry,
    }
