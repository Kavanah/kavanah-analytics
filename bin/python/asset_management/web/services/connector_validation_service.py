"""Connector ID validation.

GA4 and Google Ads are tokenless service-account connectors: the setup wizard
links a customer/property ID to a team and the card shows "Configured" as soon
as the asset row resolves — nothing ever asks Google whether the ID is real or
reachable by our service account. So garbage IDs produced a false "Configured".

This module validates a candidate ID in two stages before the wizard creates
the asset row:

1. **Format** — a cheap regex (numeric GA4 property, 10-digit Google Ads
   customer). Catches obvious garbage with no API call.
2. **Existence** — a live probe with the SAME credentials the importer uses, so
   "reachable here" == "reachable at import time". For GA4 that means
   the connected GA4 OAuth accounts (the service account is retired). For Google
   Ads the service account is also retired: the standalone probe here
   fails open and the real OAuth-token probe runs at the wizard Authorize step.
   Classified:

   * definitive denial (not-found / not-authorized / permission-denied) →
     ``ProbeResult(ok=False, definitive=True)`` → caller REJECTS (fail-closed).
   * infra failure (timeout / network / quota / missing-creds / lib-unavailable)
     → ``ProbeResult(ok=False, definitive=False)`` → caller ALLOWS with a logged
     warning (fail-open) so a Google outage doesn't block legitimate setup.
   * success → ``ProbeResult(ok=True)``.

The probe is timeout-wrapped. The wizard service calls
:func:`validate_google_ads_customer` / :func:`validate_ga4_property`; tests
inject a fake ``_probe`` to exercise each branch without hitting Google.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from typing import Callable
from lib.email_format import is_plausible_email

sys.path.insert(0, "/app/python")

logger = logging.getLogger(__name__)

# Google Ads customer ID: 10 digits, optionally dash-grouped NNN-NNN-NNNN.
_GOOGLE_ADS_ID_RE = re.compile(r"^\d{3}-?\d{3}-?\d{4}$")
# GA4 property ID: all digits (Google calls it the numeric Property ID).
_GA4_PROPERTY_ID_RE = re.compile(r"^\d{6,15}$")
# RFC-pragmatic email: not a full RFC 5322 parser, just rejects obvious non-emails.

# How long a single existence probe may run before we treat it as an infra
# failure (fail-open). Google client calls are normally sub-second.
_PROBE_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of an existence probe.

    ``ok``          — the ID is real and reachable by our SA.
    ``definitive``  — only meaningful when ``ok`` is False: True means Google
                      gave a definitive denial (reject / fail-closed); False
                      means an infra failure we can't distinguish from a real
                      ID (allow / fail-open).
    ``message``     — operator-facing reason (already safe to show the user).
    """

    ok: bool
    definitive: bool = False
    message: str = ""


def normalize_google_ads_id(raw: str) -> str:
    """Strip dashes/whitespace to the bare 10-digit Google Ads customer id."""
    return re.sub(r"[\s-]", "", raw or "")


def is_valid_google_ads_id_format(raw: str) -> bool:
    return bool(_GOOGLE_ADS_ID_RE.match((raw or "").strip()))


def is_valid_ga4_property_id_format(raw: str) -> bool:
    return bool(_GA4_PROPERTY_ID_RE.match((raw or "").strip()))


def is_valid_email_format(raw: str) -> bool:
    # Length first: the loose regex is quadratic on long
    # '@'/'.'-heavy input; 254 is the longest valid address.
    email = (raw or "").strip()
    return len(email) <= 254 and is_plausible_email(email)


def _run_with_timeout(fn: Callable[[], ProbeResult], timeout: int) -> ProbeResult:
    """Run ``fn`` with a hard timeout. A timeout is an INFRA failure
    (fail-open). Uses a thread + join so it works regardless of signal context
    (the Flask request thread may not be the main thread, so signal.alarm is
    not safe here)."""
    import threading

    box: dict[str, ProbeResult] = {}

    def _target() -> None:
        try:
            box["result"] = fn()
        except Exception as e:  # noqa: BLE001 — any probe error is infra (fail-open)
            box["result"] = ProbeResult(ok=False, definitive=False, message=f"probe error: {e}")

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # Still running past the timeout — treat as infra (fail-open).
        return ProbeResult(ok=False, definitive=False, message="probe timed out")
    return box.get("result", ProbeResult(ok=False, definitive=False, message="probe produced no result"))


# ── GA4 ───────────────────────────────────────────────────────────────────────


def _ga4_probe(property_id: str) -> ProbeResult:
    """Live GA4 existence probe.

    Asks the Data API for the property's metadata using each CONNECTED GA4 OAuth
    account's credentials (``iter_connected_ga4_accounts``), since there is no
    single service-account identity any more. Classification:

      * any connected account can read the metadata → ``ok=True``.
      * at least one account gets a definitive denial (403/404) AND no account
        succeeds → ``definitive=True`` (reject / fail-closed).
      * no connected accounts, libs unavailable, or only infra errors (quota /
        timeout / network) → ``definitive=False`` (fail-open), matching the
        prior SA-probe's fail-open-on-infra contract.

    In the wizard the user authorizes the GA4 account before picking a property,
    so the connecting account is among the connected set this iterates.
    """
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.api_core.exceptions import PermissionDenied, NotFound, GoogleAPIError
        from import_scripts.google_analytics.lib.ga_config import iter_connected_ga4_accounts
    except ImportError as e:
        # Libs unavailable in this env — infra, fail-open.
        return ProbeResult(ok=False, definitive=False, message=f"GA client unavailable: {e}")

    saw_account = False
    definitive_denial = None  # remember a 403/404 to report if nothing succeeds
    last_infra = None
    try:
        for _account_doc, creds in iter_connected_ga4_accounts():
            saw_account = True
            try:
                client = BetaAnalyticsDataClient(credentials=creds)
                # get_metadata is the cheapest property-scoped read; it 403s for
                # an unauthorized property and 404s for a non-existent one.
                client.get_metadata(name=f"properties/{property_id}/metadata")
                return ProbeResult(ok=True)
            except (PermissionDenied, NotFound) as e:
                # This account can't see it — another connected account might.
                definitive_denial = str(e)
                continue
            except GoogleAPIError as e:
                last_infra = f"GA API error: {e}"
                continue
            except Exception as e:  # noqa: BLE001 — per-account infra
                last_infra = f"GA probe error: {e}"
                continue
    except Exception as e:  # noqa: BLE001 — account iteration failed → infra
        return ProbeResult(ok=False, definitive=False, message=f"GA account iteration error: {e}")

    if not saw_account:
        # No connected GA4 OAuth accounts to probe with → can't prove anything;
        # fail-open so setup isn't blocked (the import will simply skip an
        # unconnected property anyway).
        return ProbeResult(ok=False, definitive=False, message="no connected GA4 OAuth accounts to probe with")
    if definitive_denial is not None:
        # Some account definitively denied and none succeeded → reject.
        return ProbeResult(ok=False, definitive=True, message=definitive_denial)
    # Only infra errors across all accounts → fail-open.
    return ProbeResult(ok=False, definitive=False, message=last_infra or "GA probe inconclusive")


def validate_ga4_property(
    property_id: str,
    *,
    _probe: Callable[[str], ProbeResult] | None = None,
) -> None:
    """Raise ``WizardError`` if the GA4 property id is malformed or definitively
    unreachable. No-op (allow) on a clean probe OR an infra failure (fail-open).

    ``_probe`` is injectable for tests; defaults to the live GA4 probe.
    """
    try:
        from web.services.connector_wizard_service import WizardError  # local: avoid cycle
    except ImportError:
        from asset_management.web.services.connector_wizard_service import WizardError

    pid = (property_id or "").strip()
    if not is_valid_ga4_property_id_format(pid):
        raise WizardError("GA4 property ID must be numeric (the Property ID from Admin → Property Settings).")

    probe = _probe or _ga4_probe
    result = _run_with_timeout(lambda: probe(pid), _PROBE_TIMEOUT_SECONDS)
    if result.ok:
        return
    if result.definitive:
        # OAuth-only. The actionable fix is that the authorized Google
        # account needs access to the property (not an SA Viewer grant).
        raise WizardError(
            "We can't access that GA4 property. Make sure the Google account you "
            "authorized has access to this property in Google Analytics, then try again."
        )
    # Infra failure → fail-open.
    logging.warning("GA4 existence probe non-definitive for %s; allowing (fail-open): %s", pid, result.message)


# ── Google Ads ─────────────────────────────────────────────────────────────────


def _google_ads_probe(customer_id: str) -> ProbeResult:
    """Google Ads existence probe.

    There is no longer a shared service-account / MCC identity to probe a bare
    customer id with, so this standalone probe always **fails open**
    (non-definitive) — the candidate id is allowed through on format alone here.

    The real, OAuth-appropriate validation happens at the Authorize step via
    ``get_campaign_data.probe_google_ads_customer_with_connection``, which uses
    the user's just-granted token (so the error message is about the user's
    Google login, not "link it under our MCC"). Mirrors the GA4 probe's
    "no identity to probe with → fail-open" contract (``_ga4_probe``).
    """
    return ProbeResult(
        ok=False,
        definitive=False,
        message="Google Ads is OAuth-only; customer id validated at the Authorize step (no service-account probe).",
    )


def validate_google_ads_customer(
    customer_id: str,
    *,
    _probe: Callable[[str], ProbeResult] | None = None,
) -> None:
    """Raise ``WizardError`` if the Google Ads customer id is malformed or
    definitively unreachable. Allow on a clean probe or infra failure.

    ``_probe`` is injectable for tests; defaults to the live Ads probe.
    """
    try:
        from web.services.connector_wizard_service import WizardError  # local: avoid cycle
    except ImportError:
        from asset_management.web.services.connector_wizard_service import WizardError

    if not is_valid_google_ads_id_format(customer_id):
        raise WizardError("Google Ads Customer ID must be 10 digits (e.g. 123-456-7890).")

    bare = normalize_google_ads_id(customer_id)
    probe = _probe or _google_ads_probe
    result = _run_with_timeout(lambda: probe(bare), _PROBE_TIMEOUT_SECONDS)
    if result.ok:
        return
    if result.definitive:
        # OAuth-only. With the service account retired the actionable
        # fix is that the authorized Google account needs access to the customer
        # (not an MCC link). The live OAuth-token probe at Authorize is what
        # produces a definitive reject; this standalone probe fails open.
        raise WizardError(
            "We can't access that Google Ads account with the Google login you authorized. "
            "Make sure that account has access to it, then try again."
        )
    logging.warning(
        "Google Ads existence probe non-definitive for %s; allowing (fail-open): %s",
        bare,
        result.message,
    )
