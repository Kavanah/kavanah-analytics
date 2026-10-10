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

import logging
import secrets
import string
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
