"""Chatwoot server address — normalize and vet an account's base URL.

A Chatwoot account lives either on Chatwoot Cloud (app.chatwoot.com) or on a
self-hosted server. ``de_asset_chatwoot_accounts.chatwoot_base_url`` says which;
blank means the cloud, which covers every account created before the field
existed.

The importers send the account's API token to this host on every request, so the
value is user input that aims a credentialed server-side fetch (SSRF). It is held
to a public HTTPS server:

* ``https`` only. A bare host gets ``https://``; ``http://`` is refused, because
  the token would travel in cleartext.
* Reduced to its origin (scheme, host, port). A pasted dashboard URL
  (``https://chat.example.org/app/accounts/1/dashboard``) yields the server, not
  an error — Chatwoot is served from the root of its host.
* No credentials in the URL, no IP literals, no single-label names (``es01``,
  ``localhost``) and no internal-only suffixes.
* Every address the host resolves to must be globally routable, including the
  IPv4 address inside a NAT64 or IPv4-compatible IPv6 answer (6to4, 2002::/16, is
  never global to ``ipaddress`` on Python 3.13, so it is refused outright).
  Checked when the account is saved and again once per account per import run,
  so a name later repointed at a private address is refused at the next import.
  A resolver that
  answers differently between that check and the requests that follow is accepted
  residual risk (re-checking per request would not close it — the HTTP client does
  its own lookup); the importers also refuse redirects, so a public server cannot
  bounce the request inward.
* The lookup has a deadline (:data:`_RESOLVE_TIMEOUT_SECONDS`), so a stalled
  nameserver cannot hang a form save or the import batch.

"Not found" and "private address" are distinct messages on purpose — the first
is the typo hint real users need. The resulting probe (does name X resolve, and
to a private address?) is accepted: single-label and internal-suffix names are
refused before any lookup.

Error messages are fixed strings and never echo the submitted value (
reflective-XSS posture).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from lib.public_host import (
    REASON_FORMAT,
    REASON_IP,
    REASON_NOT_FOUND,
    REASON_PRIVATE,
    REASON_PUBLIC_NAME,
    PublicHostError,
    vet_public_host,
)
from lib.user_errors import UserFacingError

DEFAULT_CHATWOOT_BASE_URL = "https://app.chatwoot.com"

_MSG_FORMAT = "Enter the Chatwoot server as a web address, for example https://chat.example.org."
_MSG_HTTPS = "The Chatwoot server address must use https://."
_MSG_USERINFO = "The Chatwoot server address must not contain a username or password."
_MSG_IP = "Use the Chatwoot server's domain name, not an IP address."
_MSG_PUBLIC_NAME = "The Chatwoot server needs a public domain name, for example chat.example.org."
_MSG_NOT_FOUND = "The Chatwoot server's domain name could not be found. Check the spelling."
_MSG_PRIVATE = "The Chatwoot server's domain name points at a private network address. Only public servers are supported."
_MSG_TOKEN_PINNED = "To move this account to a different Chatwoot server, re-enter its API access token."
_REASON_MESSAGES = {
    REASON_FORMAT: _MSG_FORMAT,
    REASON_IP: _MSG_IP,
    REASON_PUBLIC_NAME: _MSG_PUBLIC_NAME,
    REASON_NOT_FOUND: _MSG_NOT_FOUND,
    REASON_PRIVATE: _MSG_PRIVATE,
}


class ChatwootBaseUrlError(UserFacingError, ValueError):
    """The URL is not an acceptable Chatwoot server. The message is safe to show."""


def normalize_chatwoot_base_url(raw: str | None, *, resolve: bool = True) -> str:
    """Return the vetted origin (``https://host[:port]``) for a submitted or stored URL.

    Blank means Chatwoot Cloud and returns :data:`DEFAULT_CHATWOOT_BASE_URL`
    without a DNS lookup. ``resolve=False`` skips the public-address check; use it
    only to compare stored values, never before a request.

    Raises:
        ChatwootBaseUrlError: the value is not a public HTTPS server.
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT_CHATWOOT_BASE_URL
    if "://" not in value:
        value = "https://" + value

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as e:
        raise ChatwootBaseUrlError(_MSG_FORMAT) from e

    if parts.scheme.lower() != "https":
        raise ChatwootBaseUrlError(_MSG_HTTPS)
    if parts.username is not None or parts.password is not None:
        raise ChatwootBaseUrlError(_MSG_USERINFO)

    try:
        host = vet_public_host(parts.hostname or "", port, resolve=resolve)
    except PublicHostError as e:
        raise ChatwootBaseUrlError(_REASON_MESSAGES[e.reason]) from e

    return f"https://{host}" if port in (None, 443) else f"https://{host}:{port}"


def vet_account_base_url(
    submitted: str | None,
    *,
    existing_doc: dict[str, Any] | None = None,
    token_reentered: bool = True,
) -> str:
    """Vet the server URL for saving an account; return the value to store.

    On an edit (``existing_doc`` given) that moves the account to a different
    server, the API token must be re-entered: replaying the saved token to a new
    host would hand the credential to whoever runs that host. Same posture as the
    Disciple.Tools wizard's domain-pinned token.

    Raises:
        ChatwootBaseUrlError: the URL is refused, or the token was not re-entered.
    """
    new_url = normalize_chatwoot_base_url(submitted)
    if existing_doc is not None and not token_reentered:
        try:
            old_url = normalize_chatwoot_base_url(existing_doc.get("chatwoot_base_url"), resolve=False)
        except ChatwootBaseUrlError:
            old_url = None  # an unusable stored value never matches, so the token is required
        if new_url != old_url:
            raise ChatwootBaseUrlError(_MSG_TOKEN_PINNED)
    return new_url
