"""OAuth correlation values: CSPRNG generation, TTL stamping, expiry checks.

Before the per-asset ``*_auth_url`` slug and ``*_auth_state`` were
drawn from ``random.choices`` (Mersenne Twister, predictable from its output)
by two duplicate generators, stamped once at asset creation, and honoured by
the fb_oauth callbacks for the life of the document: a leaked state was
replayable forever. This module is the single generator for all of them and
carries the TTL contract the Google Ads flow already had (,
``ConnectionService.OAUTH_STATE_TTL_SECONDS``).

Two kinds of value, deliberately different shapes:

* :func:`random_alnum` — the URL path slug (``/<app>/<auth_url>``,
  ``/youtube/<auth_url>``, ``/ga4/<auth_url>``). Stays alphanumeric so the
  15-char route guards, the ``unique`` auto_generate check and every stored
  slug keep working; only the RNG changes.
* :func:`new_oauth_state` — the OAuth ``state`` parameter.
  ``secrets.token_urlsafe(32)`` (43 url-safe chars, 256 bits). Minted by the
  *initiate* leg on every click and stamped with :func:`oauth_state_expiry`;
  the callback refuses a state past its expiry and clears it on success, so
  each value is single-use and short-lived.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import string
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

# Same window as ConnectionService.OAUTH_STATE_TTL_SECONDS: long
# enough to click through a provider consent screen (incl. a login + 2FA),
# short enough that a leaked authorize URL cannot be replayed later.
OAUTH_STATE_TTL_SECONDS = 600

_ALNUM = string.ascii_letters + string.digits


def random_alnum(length: int) -> str:
    """Cryptographically random ``[A-Za-z0-9]`` string of ``length`` chars."""
    if length <= 0:
        return ""
    return "".join(secrets.choice(_ALNUM) for _ in range(length))


def new_oauth_state() -> str:
    """A fresh OAuth ``state`` value (url-safe, 256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def oauth_state_expiry(now: datetime | None = None) -> str:
    """ISO-8601 UTC instant ``OAUTH_STATE_TTL_SECONDS`` from ``now``."""
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)).isoformat()


def oauth_state_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    """True when ``expires_at`` (ISO-8601) is in the past.

    A missing expiry is NOT expired: docs whose state was minted before
     carry none, and they keep working until the next initiate click
    re-mints with a stamp. An unparseable value logs and is treated the same
    way (the callback's fall-through), since only our own code writes
    this field.
    """
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(str(expires_at))
    except ValueError:
        logging.warning("oauth_state_expired: unparseable expiry %r — treating as unexpired", expires_at)
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    return deadline < current


# ── bind each attempt to the browser that started it ─────────────
#
# The state alone correlates a provider callback to an asset doc / connection,
# but it was not tied to the browser: an attacker could open the initiate leg
# for THEIR asset, forward the provider consent URL (state inside) to a victim,
# and the victim's consent would land the victim's token on the attacker's
# asset (consent phishing). The initiate leg now sets a short-lived cookie
# derived from the state it hands out, and every callback refuses a state the
# presenting browser holds no matching cookie for.
#
# Both legs are served by the same OAuth host (``fboauth.hostname``: the popup
# opens ``https://<host>/<leg>/<auth_url>`` and the provider redirects to
# ``https://<host>/<leg>/auth_callback``), so a host-only cookie spans them with
# no shared domain. ``__Host-`` makes the browser refuse the cookie unless it
# is Secure, Path=/ and carries no Domain — a sibling subdomain cannot plant
# one. The provider redirect is a top-level GET, which SameSite=Lax allows.
# One cookie per state (name keyed by its digest) so two attempts in the same
# browser do not overwrite each other; each expires with its state.

OAUTH_BIND_COOKIE_PREFIX = "__Host-km_oauth_"


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def oauth_bind_cookie_name(state: str) -> str:
    """The binding cookie's name for ``state`` (keyed by a digest prefix)."""
    return OAUTH_BIND_COOKIE_PREFIX + _state_digest(state)[:16]


def oauth_bind_cookie_value(state: str) -> str:
    """The binding cookie's value for ``state`` (its full SHA-256, never the state)."""
    return _state_digest(state)


def oauth_browser_bound(state: str, cookies: Mapping[str, str]) -> bool:
    """True when ``cookies`` carry the binding cookie the initiate leg set for ``state``."""
    if not state:
        return False
    presented = cookies.get(oauth_bind_cookie_name(state)) or ""
    return hmac.compare_digest(presented.encode("utf-8"), oauth_bind_cookie_value(state).encode("utf-8"))
