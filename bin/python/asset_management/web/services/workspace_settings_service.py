"""Workspace settings sanitisation for the onboarding wizard (,
narrative-retargeted / F6).

The step-3 "More options" disclosure surfaces three power-user inputs —
``timezone``, ``currency``, ``fiscal_year_start_month`` — that default
from the browser's ``Intl`` locale and are persisted to
``wizard_session.state`` for the wizard's downstream commit step to
read.

Final write target: the ``de_asset_workspace`` doc (fields declared by
F4 / ``WORKSPACE_MAPPING``). Pre-F6 docstrings called these
"workspace defaults that end up on the org doc"; the spike
overturned that — these were always tenant-level conceptually, and
the workspace doc is the canonical home. The commit-step wiring is
F7's responsibility; F6 only retargets the narrative so
F7's reviewer doesn't have to reconcile a "writes-to-org" docstring
against a "writes-to-workspace" implementation.

The wizard contract is **lenient**: an unknown timezone or currency
silently falls back to the server defaults rather than blocking the
user with a 400. These surfaces are power-user opt-ins inside a closed
disclosure; the cost of a stray garbage value reaching state is low
(the commit step will resolve it against whatever the workspace doc
schema allows), while the cost of blocking a wizard step on a
non-blocking AC bullet is high.

The validation rules:

* ``timezone`` — a string that ``zoneinfo.ZoneInfo`` can resolve.
  Anything else → ``DEFAULT_TIMEZONE`` (UTC). We accept the ``Intl``
  output verbatim including ``Etc/`` zones and continent-prefixed
  zones; the only filter is "Python can load it".
* ``currency`` — a three-letter alphabetic code, upper-cased.
  Anything else → ``DEFAULT_CURRENCY`` (USD). We deliberately don't
  validate against an ISO-4217 list — the cost of rejecting a real
  but obscure currency code is worse than the cost of storing a typo
  ("USS"); the downstream surface owns the canonicalisation.
* ``fiscal_year_start_month`` — an integer 1..12. Anything else →
  ``DEFAULT_FISCAL_YEAR_START_MONTH`` (1, January).

The service is a pure function with no Flask, no ES, and no
side-effects so it can be unit-tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — Python 3.9+ ships zoneinfo
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]


DEFAULT_TIMEZONE = "UTC"
DEFAULT_CURRENCY = "USD"
DEFAULT_FISCAL_YEAR_START_MONTH = 1  # January

# follow-up — curated ISO-4217 currency list for the wizard +
# /profile <select>. Kept short on purpose: tax-reporting hard currencies
# the dashboard's ad-platform exporters actually return (USD, EUR, GBP,
# CAD, AUD, etc.) plus the next tier of high-volume regions. Adding a
# code here is cheap; the sanitize() rule is "any 3 alphabetic chars"
# so a tenant who needs an unlisted code can still submit it via API.
# UI surface stays opinionated to keep the picker scannable.
CURRENCY_OPTIONS = (
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "NZD",
    "JPY",
    "CNY",
    "HKD",
    "SGD",
    "INR",
    "KRW",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
    "BRL",
    "MXN",
    "ZAR",
    "AED",
)

# State-dict keys persisted onto wizard_session.state. The wizard's
# downstream commit step (F7 /) reads these and writes them to
# the new workspace doc (de_asset_workspace) rather than the org doc.
STATE_KEY_TIMEZONE = "workspace_timezone"
STATE_KEY_CURRENCY = "workspace_currency"
STATE_KEY_FISCAL_YEAR_START = "workspace_fiscal_year_start_month"


@dataclass(frozen=True)
class WorkspaceSettings:
    """Sanitised triple of workspace defaults for step-3 persistence."""

    timezone: str
    currency: str
    fiscal_year_start_month: int


class WorkspaceSettingsService:
    """Sanitise step-3 "More options" inputs before persistence."""

    @staticmethod
    def sanitize(
        timezone: Any,
        currency: Any,
        fiscal_year_start_month: Any,
    ) -> WorkspaceSettings:
        """Coerce three raw form values into a validated ``WorkspaceSettings``.

        Each value is independent — an invalid timezone does not cause a
        valid currency to be discarded. Unknown / malformed values fall
        back to the module-level defaults silently (the wizard never
        blocks on this surface).
        """
        return WorkspaceSettings(
            timezone=WorkspaceSettingsService._coerce_timezone(timezone),
            currency=WorkspaceSettingsService._coerce_currency(currency),
            fiscal_year_start_month=WorkspaceSettingsService._coerce_month(
                fiscal_year_start_month,
            ),
        )

    @staticmethod
    def _coerce_timezone(raw: Any) -> str:
        """Validate an IANA timezone string. Returns the validated zone
        or an empty string on any failure.

         follow-up (2026-05-21): pre-fix this returned
        ``DEFAULT_TIMEZONE`` ("UTC") on every failure path — invalid
        input, empty input, ZoneInfo rejection. That created a silent
        round-trip: a browser-detected zone the validator rejected
        would land on ``state.workspace_timezone="UTC"``, the next GET
        rendered "UTC", and the user saw their timezone "change" to
        UTC between visits. Now we return "" on failure so the state
        stays empty and the next GET lets the browser's Intl detection
        re-fill the field consistently.

        The "UTC" fallback that callers used to receive from this
        method still lives on at the final ES write site
        (``_upsert_workspace`` hardcodes ``timezone: DEFAULT_TIMEZONE``
        in the workspace doc — a separate ticket should thread the
        wizard's collected zone through to the doc create call).
        """
        if not isinstance(raw, str):
            return ""
        candidate = raw.strip()
        if not candidate:
            return ""
        if ZoneInfo is None:  # pragma: no cover
            # zoneinfo unavailable — accept the candidate as-is (best
            # effort) since we can't validate. Caller's downstream
            # surface will reject if it must.
            return candidate
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return ""
        return candidate

    @staticmethod
    def _coerce_currency(raw: Any) -> str:
        if not isinstance(raw, str):
            return DEFAULT_CURRENCY
        candidate = raw.strip().upper()
        # ISO-4217 codes are three alphabetic characters. Anything else
        # is a typo / forged value.
        if len(candidate) != 3 or not candidate.isalpha():
            return DEFAULT_CURRENCY
        return candidate

    @staticmethod
    def _coerce_month(raw: Any) -> int:
        if isinstance(raw, bool):
            # bool is a subclass of int — exclude before the int branch.
            return DEFAULT_FISCAL_YEAR_START_MONTH
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return DEFAULT_FISCAL_YEAR_START_MONTH
            try:
                value = int(stripped)
            except ValueError:
                return DEFAULT_FISCAL_YEAR_START_MONTH
        else:
            return DEFAULT_FISCAL_YEAR_START_MONTH
        if 1 <= value <= 12:
            return value
        return DEFAULT_FISCAL_YEAR_START_MONTH
