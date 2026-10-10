"""Canonical public base URL for links the app hands out of band.

Links that leave the request/response cycle — verification emails, invites,
anything a user opens later from another client — must not be built from the
inbound request's host. ``ProxyFix`` rewrites ``request.host_url`` from
``X-Forwarded-Host``, and any header nginx does not explicitly set is passed
through from the client, so a request-derived host is attacker-influenced.

Resolution order, first non-empty wins:

1. ``APP_PUBLIC_HOSTNAME`` (compose sets it from ``SERVER_NAME``;) →
   ``https://<hostname>``.
2. The SMTP settings' ``app_url`` (scheme + host only), the same source the
   invite / billing / feedback emails already use.
3. ``request.host_url`` — the earlier behaviour, kept only so a
   deployment with neither setting still sends mail. Logged at WARNING so it
   is visible, never silent.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from flask import has_request_context, request

logger = logging.getLogger(__name__)

_ENV_HOSTNAME = "APP_PUBLIC_HOSTNAME"


def _from_env() -> str:
    hostname = (os.environ.get(_ENV_HOSTNAME) or "").strip().strip("/")
    if not hostname:
        return ""
    if "://" in hostname:
        parsed = urlparse(hostname)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    return f"https://{hostname}"


def _from_smtp_settings() -> str:
    # Local import: the settings reader touches the filesystem and is mocked in tests.
    from asset_management.web.services.onboarding_email_verification_service import _get_app_base_url

    try:
        return _get_app_base_url() or ""
    except Exception:  # noqa: BLE001 — a missing/invalid settings file must not break signup
        return ""


def public_base_url() -> str:
    """Return ``scheme://host`` (no trailing slash) for out-of-band links."""
    base = _from_env() or _from_smtp_settings()
    if base:
        return base.rstrip("/")
    if has_request_context():
        logger.warning(
            "public_base_url: neither %s nor SMTP app_url is configured; falling back to the "
            "request host %s — set %s so emailed links cannot be steered by X-Forwarded-Host",
            _ENV_HOSTNAME,
            request.host,
            _ENV_HOSTNAME,
        )
        return request.host_url.rstrip("/")
    return ""
