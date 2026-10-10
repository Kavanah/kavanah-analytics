"""
Provider Identity Service

Source-of-truth for the "Logged in as … — Switch account?" surface
rendered on wizard step 5. The screen sits *before* the OAuth handoff,
so we have no provider-side access token to query a real
``me?fields=name,email`` endpoint with. Instead, this service builds
an honest identity surface from what we *do* know — the DEAM
session's ``username`` (email) and ``full_name`` — paired with a
per-connector "Switch account" URL that logs the user out of the
provider and returns to step-5 so they can re-pick on the next OAuth.

Why a service rather than a one-line dict lookup in the route:

1. The AC names three providers (Meta, Google, Microsoft) and asks
   for the same pattern across all of them. Today only ``meta_ads``
   has a step-5 manifest, but the registry
   pattern means a future Google Ads / Microsoft Ads connector
   plugs in by adding an entry, not by editing the route.
2. The provider-side switch URL contract differs per connector
   (Facebook's ``logout.php?next=…``, Google's ``accounts.google.com/Logout``
   with continuation, Microsoft's ``login.microsoftonline.com/common/oauth2/v2.0/logout``).
   Keeping the URL builder per-connector here means the route
   stays connector-agnostic.
3. A future ticket can swap the "we don't have a token" stub for a
   real ``me?fields=…`` call on step-5 *re-entry* (i.e. after the
   user has completed OAuth and is reviewing) without changing the
   route or the template — the service grows a richer resolver and
   the route's `resolve(...)` callsite stays unchanged.

The "Switch account" URL is built so that clicking it lands the
user back at ``/onboarding/step-7`` after the provider acknowledges
the logout. Combined with the next OAuth (which on step-5 continue
will use ``prompt=select_account`` / ``auth_type=reauthenticate``
on the eventual OAuth ticket), the user is forced through a fresh
account picker rather than re-using the cookie that bound them to
the wrong personal account.

This module performs no Elasticsearch, network, or filesystem I/O
and so is import-time safe in the test harness without mocking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote


@dataclass(frozen=True)
class IdentitySurface:
    """The data the step-5 template needs to render the
    "Logged in as … — Switch account?" row.

    ``provider_label`` — the provider name as shown to the user
    ("Meta", "Google", "Microsoft").

    ``display_name`` — the user's display name (full name from the
    DEAM session if available, else the username/email).

    ``display_email`` — the user's email as held by the DEAM
    session. This is what the user signed up with; we surface it
    so they can compare against the provider account they're about
    to authorise.

    ``initials`` — two-letter initials derived from ``display_name``
    for the avatar circle.

    ``switch_url`` — the URL the "Switch account" anchor points at.
    Per-provider; logs the user out of the provider and returns to
    step-5 so the user can re-OAuth with the right account.

    ``is_provider_verified`` — whether ``display_name`` / ``display_email``
    came from a live provider session check (``True``) or from the
    DEAM session as a stand-in (``False``). Today this is always
    ``False`` because step-5 is pre-OAuth — but the field is on the
    dataclass so a future ticket can flip it once we have a real
    Meta/Google/Microsoft ``me`` call to make.
    """

    provider_label: str
    display_name: str
    display_email: str
    initials: str
    switch_url: str
    is_provider_verified: bool = False


# Per-connector switch-account URL builder. Keeps the "what does a
# switch account link look like" knowledge per-provider so the route
# stays connector-agnostic. Each builder takes the absolute wizard
# re-OAuth URL (``reauth_url``, e.g.
# ``https://app/onboarding/step-7/start-oauth?reauth=1``) and decides
# whether to point the user at that URL directly or through a
# provider-side logout step first.
#
# (2026-05-21): the pre-fix Meta builder pointed at
# ``facebook.com/logout.php?next=<return_to>``. Facebook stopped
# honoring the ``next`` parameter for non-facebook.com destinations
# years ago (around 2018-2019, phishing defense) and silently dropped
# users on facebook.com home page still logged in. The reliable path
# is to skip the provider-side logout entirely and pass
# ``auth_type=reauthenticate`` on the OAuth dialog — Meta then shows
# the account picker even with an active FB session. The route
# handler appends that param when ``reauth=1`` is on the query string.
def _meta_switch_url(reauth_url: str) -> str:
    """Return the wizard's re-OAuth URL verbatim.

    earlier this built a facebook.com/logout.php URL with a
    ``?next=`` redirect. Facebook silently broke that pattern years
    ago. The new path uses Meta's ``auth_type=reauthenticate`` OAuth
    parameter (applied by the route handler when ``?reauth=1`` is on
    the request) to force the FB account picker without any
    provider-side logout step.
    """
    return reauth_url


def _google_switch_url(reauth_url: str) -> str:
    """Build the Google "log out then return" URL.

    Google's ``accounts.google.com/Logout`` accepts a ``continue``
    parameter to redirect post-logout. We pair this with the
    eventual OAuth call's ``prompt=select_account`` (set on the
    OAuth ticket, not here) so the user lands on the account
    picker even if they had a single Google identity cached.

    this still uses the provider-side logout pattern
    because Google honors ``continue=`` for arbitrary destinations,
    unlike Facebook's logout.php. If Google ever drops that support
    (parallel to Meta's 2018-19 change), flip this to the same
    reauth-via-OAuth pattern Meta now uses.
    """
    return f"https://accounts.google.com/Logout?continue={quote(reauth_url, safe='')}"


def _microsoft_switch_url(reauth_url: str) -> str:
    """Build the Microsoft "log out then return" URL.

    Microsoft's logout endpoint uses ``post_logout_redirect_uri``
    on the v2.0 OAuth surface. The redirect URI must match one
    registered against the app (configured in Azure AD on the
    eventual Microsoft Ads ticket); for the step-5 stub we name
    step-5 as the post-logout target and accept that the redirect
    will only round-trip cleanly once the Azure app config lists it.
    """
    return (
        "https://login.microsoftonline.com/common/oauth2/v2.0/logout" f"?post_logout_redirect_uri={quote(reauth_url, safe='')}"
    )


# Registry of providers that have a step-5 identity surface today.
# Keep this narrower than the connector catalog: a connector without
# a manifest never reaches step-5 (the narrower-catalog-than-
# tiles invariant), and a connector without an identity surface here
# renders step-5 without the "Logged in as" row (graceful degradation
# — the rest of the screen still works).
@dataclass(frozen=True)
class _ProviderSpec:
    label: str
    switch_url_builder: Callable[[str], str]


_PROVIDERS: dict[str, _ProviderSpec] = {
    "meta_ads": _ProviderSpec(
        label="Meta",
        switch_url_builder=_meta_switch_url,
    ),
    # Google Ads / GA4 are placeholder=True on the connector catalog
    # today and never reach step-5. Listed here so the moment a
    # future ticket flips one of them to selectable, the identity
    # surface is already wired.
    "google_ads": _ProviderSpec(
        label="Google",
        switch_url_builder=_google_switch_url,
    ),
    "google_analytics_4": _ProviderSpec(
        label="Google",
        switch_url_builder=_google_switch_url,
    ),
    "microsoft_ads": _ProviderSpec(
        label="Microsoft",
        switch_url_builder=_microsoft_switch_url,
    ),
}


def _derive_initials(display_name: str, fallback_email: str) -> str:
    """Two-letter avatar initials from a display name, falling back
    to the first two letters of the email's local part if the name
    is empty or a single character. Upper-case for visual weight.
    Always returns at least one character — never empty.
    """
    name = (display_name or "").strip()
    if name:
        parts = [p for p in name.split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        if len(parts) == 1 and len(parts[0]) >= 2:
            return parts[0][:2].upper()
        if len(parts) == 1:
            return parts[0][0].upper()
    local = (fallback_email or "").split("@", 1)[0]
    if len(local) >= 2:
        return local[:2].upper()
    if local:
        return local[0].upper()
    return "?"


class ProviderIdentityService:
    """Read-only resolver for the step-5 "Logged in as" surface."""

    @staticmethod
    def supported_connector_ids() -> frozenset[str]:
        """Set of connector_ids that have an identity surface today.

        Wider than ``ConnectorScopeManifestService.supported_connector_ids()``
        on purpose: providers that aren't reachable on step-5 yet
        (Google, Microsoft) still register their switch-URL builder
        here so the moment one becomes selectable, the surface is
        wired without a service edit.
        """
        return frozenset(_PROVIDERS.keys())

    @staticmethod
    def resolve(
        connector_id: str,
        session_username: str,
        session_full_name: str,
        return_to_url: str,
    ) -> IdentitySurface | None:
        """Build the step-5 identity surface for ``connector_id``.

        Returns ``None`` for any connector without a registered
        provider — the route then renders step-5 without the
        "Logged in as" row (graceful degradation; the rest of the
        scope-review screen is unchanged).

        ``session_username`` is the email the user signed up with
        (DEAM ``session['username']``). ``session_full_name`` is the
        display name (DEAM ``session['full_name']``, falls back to
        ``session_username`` upstream).

        ``return_to_url`` is the absolute URL the "Switch account"
        link points the user at. earlier the route handler
        passes the wizard's re-OAuth URL here (``/onboarding/step-7/
        start-oauth?reauth=1``) instead of the step-5 page itself —
        Meta's switch path now skips provider-side logout entirely
        and forces the account picker via ``auth_type=reauthenticate``
        on the OAuth dialog. Google + Microsoft builders still
        provider-logout first (those providers honor logout-with-
        redirect, unlike Facebook). The parameter name is unchanged
        for backward compatibility with existing test mocks.
        """
        if not connector_id:
            return None
        spec = _PROVIDERS.get(connector_id)
        if spec is None:
            return None
        if not return_to_url:
            return None

        display_name = (session_full_name or session_username or "").strip()
        display_email = (session_username or "").strip()
        initials = _derive_initials(display_name, display_email)
        switch_url = spec.switch_url_builder(return_to_url)

        return IdentitySurface(
            provider_label=spec.label,
            display_name=display_name,
            display_email=display_email,
            initials=initials,
            switch_url=switch_url,
            is_provider_verified=False,
        )
