"""Echo tenant subdomain — vet it and build the API base URL.

An Echo tenant is reached at ``https://<subdomain>.echoglobal.org``. The
subdomain is entered by a workspace owner/admin, and the importers used to
splice it into that f-string unchecked. A value such as ``attacker.example/``
(or one ending ``#``, ``?``, ``user@host/``, ``host:port/``) moves the request
host off echoglobal.org entirely, so whoever runs that host answers the cron
importer — and the importers turn its ids into file paths.

The subdomain is held to a single lowercase DNS label, checked when the tenant
is saved (Data Connectors wizard, Asset Explorer, CLI) and again when an
importer builds the URL. :func:`echo_base_url` additionally asserts the parsed
host is ``<label>.echoglobal.org``, so the URL cannot drift even if the label
rule is loosened later.

Normalization is limited to stripping whitespace and lowercasing (DNS names are
case-insensitive). Nothing else is "fixed up": a pasted full host or URL is
refused with a message that says what to enter.

Error messages are fixed strings and never echo the submitted value (
reflective-XSS posture).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from lib.user_errors import UserFacingError

ECHO_DOMAIN = "echoglobal.org"

_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

_MSG_FORMAT = "Enter only the Echo subdomain — letters, digits and hyphens — " "for example acme for acme.echoglobal.org."


class EchoSubdomainError(UserFacingError, ValueError):
    """The value is not a usable Echo subdomain. The message is safe to show."""


def normalize_echo_subdomain(raw: str | None) -> str:
    """Return the subdomain as a single lowercase DNS label.

    Raises:
        EchoSubdomainError: the value is empty or not exactly one DNS label.
    """
    value = (raw or "").strip().lower()
    if not _LABEL_RE.fullmatch(value):
        raise EchoSubdomainError(_MSG_FORMAT)
    return value


def echo_base_url(subdomain: str | None) -> str:
    """Return ``https://<subdomain>.echoglobal.org`` for a stored subdomain.

    Raises:
        EchoSubdomainError: the subdomain is invalid, or the built URL's host is
            not ``<subdomain>.echoglobal.org``.
    """
    label = normalize_echo_subdomain(subdomain)
    url = f"https://{label}.{ECHO_DOMAIN}"
    if urlsplit(url).hostname != f"{label}.{ECHO_DOMAIN}":
        raise EchoSubdomainError(_MSG_FORMAT)
    return url
