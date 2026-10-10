"""
Onboarding OAuth Error Service

Resolves a provider-side OAuth callback error code (``access_denied``,
``server_error``, ``timeout`` and friends) into the inline error banner
the user sees when they're bounced back to step-5 from a failed Meta /
Google / Microsoft OAuth roundtrip.

Why a service rather than an inline lookup in the route:

1. Each failure mode has its own copy. ``access_denied`` is "you clicked
   Cancel" — friendly, no scary red. ``server_error`` is "the provider
   had a hiccup" — apologetic, retry-suggested. ``timeout`` is "we lost
   the round-trip" — neutral, retry-suggested. Lumping them into one
   banner reads as if every failure is the user's fault.

2. The provider-side error code vocabulary is not standardised across
   Facebook / Google / Microsoft (Facebook uses ``error=access_denied``
   with the OAuth 2.0 RFC vocabulary, Google adds non-RFC values like
   ``user_cancel``, Microsoft adds ``consent_required`` and
   ``interaction_required``). A central resolver narrows that vocabulary
   to four buckets the user can act on, so a new provider's quirky code
   doesn't drop the user onto a blank banner.

3. The end-to-end Meta callback wiring is a deferred follow-up (the
   fb_oauth_server.py app isn't wizard_session-aware yet — open
   carry-over). Until that lands, the
   step-5 GET still needs to handle a ``?error=…`` query string so the
   moment the callback ticket ships, the receiving side already exists.
   Keeping the resolver as a standalone service means the future
   callback handler can call ``resolve(...)`` directly without depending
   on a route module.

This module performs no Elasticsearch, network, or filesystem I/O and
so is import-time safe in the test harness without mocking.
"""

from __future__ import annotations

from dataclasses import dataclass


# Error code buckets the UI can render distinct copy for. The query-string
# vocabulary from each provider is normalised onto these five codes by the
# alias table below; everything else falls through to ``unknown``.
CODE_ACCESS_DENIED = "access_denied"
CODE_SERVER_ERROR = "server_error"
CODE_TIMEOUT = "timeout"
# the user's OAuth token expired between the callback and the
# step-6 asset fetch. Distinct from access_denied (user-cancel) because
# the user already granted consent once; the copy nudges them to re-OAuth
# rather than explaining what scopes do.
CODE_TOKEN_EXPIRED = "token_expired"
CODE_UNKNOWN = "unknown"


# Provider-side OAuth error vocabulary → bucket. Lower-cased keys; the
# resolver lower-cases the input before lookup. Keys here are the union
# of (a) RFC 6749 standard errors and (b) provider-specific extensions
# we've actually seen in prod logs.
_ALIASES: dict[str, str] = {
    # RFC 6749 — the user denied consent
    "access_denied": CODE_ACCESS_DENIED,
    # Google extension — "Continue with another account" / Cancel
    "user_cancel": CODE_ACCESS_DENIED,
    "user_cancelled_authorize": CODE_ACCESS_DENIED,
    "user_cancelled_login": CODE_ACCESS_DENIED,
    # Microsoft extension — user backed out of the consent screen
    "consent_required": CODE_ACCESS_DENIED,
    "interaction_required": CODE_ACCESS_DENIED,
    # RFC 6749 — provider-side 5xx / config issue
    "server_error": CODE_SERVER_ERROR,
    "temporarily_unavailable": CODE_SERVER_ERROR,
    "invalid_client": CODE_SERVER_ERROR,
    "invalid_request": CODE_SERVER_ERROR,
    "invalid_scope": CODE_SERVER_ERROR,
    "unauthorized_client": CODE_SERVER_ERROR,
    "unsupported_response_type": CODE_SERVER_ERROR,
    # Our own — the exchange step exceeded the configured deadline
    "timeout": CODE_TIMEOUT,
    "request_timeout": CODE_TIMEOUT,
    # our own synthetic code, emitted by the step-6 GET when
    # the provider rejects the asset fetch with 401 after the user has
    # already completed OAuth. ``invalid_token`` is the RFC 6750 Bearer
    # token rejection vocabulary; ``expired_token`` is the OAuth 2.0
    # token-introspection extension. Both map to the same bucket so a
    # future provider's variant resolves coherently.
    "token_expired": CODE_TOKEN_EXPIRED,
    "invalid_token": CODE_TOKEN_EXPIRED,
    "expired_token": CODE_TOKEN_EXPIRED,
}


@dataclass(frozen=True)
class OAuthError:
    """The data the step-5 template needs to render the error banner.

    ``code`` — one of the four normalised buckets. Useful for analytics
    slicing and for the audit-event ``detail`` field; the template
    keys off ``title`` / ``body`` / ``cta_label`` rather than ``code``
    so a future copy edit doesn't have to know about the bucket name.

    ``title`` — short headline (e.g. "Connection cancelled").

    ``body`` — one-or-two-sentence explanation in plain English. Does
    not name a specific provider; the template wraps it with the
    connector name so the same copy serves Meta / Google / Microsoft.

    ``cta_label`` — the retry button text. ``"Try again"`` for every
    failure mode today; the field exists so a future "Contact support"
    variant can swap it for a server-side incident.
    """

    code: str
    title: str
    body: str
    cta_label: str


class OnboardingOAuthErrorService:
    """Resolves a provider error-code string to an ``OAuthError`` banner.

    The resolver is a pure function: takes a string (or None), returns
    an ``OAuthError`` (or None). No ES, no Flask, no I/O.

    Truthy-but-unrecognised codes resolve to the ``unknown`` bucket
    rather than ``None`` so a future provider's quirky error string
    still surfaces a banner — silent fall-through would be worse than
    a generic message because the user is staring at a wizard that
    appears to have done nothing.
    """

    # Copy is module-level so a marketing edit is a literal string diff.
    # Keep one banner per bucket; the four buckets are the public surface
    # the AC asks for ("denied vs. server error vs. timeout").
    _COPY: dict[str, OAuthError] = {
        CODE_ACCESS_DENIED: OAuthError(
            code=CODE_ACCESS_DENIED,
            title="Connection cancelled",
            body=(
                "You cancelled before granting access, so we couldn't "
                "connect the source. Your selections are saved — try "
                "again when you're ready."
            ),
            cta_label="Try again",
        ),
        CODE_SERVER_ERROR: OAuthError(
            code=CODE_SERVER_ERROR,
            title="The provider had a problem",
            body=(
                "The connector's sign-in service returned an error and "
                "we couldn't complete the connection. Your selections "
                "are saved — try again in a moment."
            ),
            cta_label="Try again",
        ),
        CODE_TIMEOUT: OAuthError(
            code=CODE_TIMEOUT,
            title="The connection timed out",
            body=(
                "We didn't hear back from the provider in time, so the "
                "connection didn't go through. Your selections are "
                "saved — try again."
            ),
            cta_label="Try again",
        ),
        CODE_TOKEN_EXPIRED: OAuthError(
            code=CODE_TOKEN_EXPIRED,
            # copy comes straight from the ticket's Scope
            # bullet ("Your Meta connection expired — please reconnect").
            # The template wraps the title with the connector name
            # ({{ connector_name }}) where it surfaces in the banner.
            title="Your connection expired — please reconnect",
            body=(
                "Your sign-in to the source timed out while you were "
                "choosing accounts. Your selections are saved — reconnect "
                "to pick up where you left off."
            ),
            cta_label="Reconnect",
        ),
        CODE_UNKNOWN: OAuthError(
            code=CODE_UNKNOWN,
            title="We couldn't complete the connection",
            body=(
                "Something went wrong during sign-in and we couldn't "
                "connect the source. Your selections are saved — try "
                "again, or contact support if this keeps happening."
            ),
            cta_label="Try again",
        ),
    }

    @classmethod
    def resolve(cls, code: str | None) -> OAuthError | None:
        """Map a provider-side error code (or None) to an ``OAuthError``.

        ``None``, empty string, and whitespace-only strings return
        ``None`` so the route can pass the result straight through to
        the template's ``{% if oauth_error %}`` gate without an extra
        truthy check.

        Any truthy code that isn't in the alias table falls through to
        the ``unknown`` bucket so the user always sees *some* banner —
        silent fall-through is the worst UX.
        """
        if code is None:
            return None
        normalised = code.strip().lower()
        if not normalised:
            return None
        bucket = _ALIASES.get(normalised, CODE_UNKNOWN)
        return cls._COPY[bucket]

    @classmethod
    def known_codes(cls) -> tuple[str, ...]:
        """The normalised bucket codes, in declaration order.

        Exposed for tests + analytics dashboards so the bucket
        vocabulary has a single source of truth.
        """
        return (
            CODE_ACCESS_DENIED,
            CODE_SERVER_ERROR,
            CODE_TIMEOUT,
            CODE_TOKEN_EXPIRED,
            CODE_UNKNOWN,
        )
