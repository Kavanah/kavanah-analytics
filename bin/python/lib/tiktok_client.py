"""TikTok Login Kit + Display API calls shared by the OAuth host and the importer
.

Every function raises :class:`TikTokError` on failure with a message that is
safe to log and to show an operator: it carries TikTok's ``error`` code,
``error_description`` / ``message`` and ``log_id`` (their request id), and never
a token. Callers must not log the raw response bodies either — a token response
carries both the access and the refresh token.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from lib.tiktok_constants import (
    ENV_CLIENT_KEY,
    ENV_CLIENT_SECRET,
    REVOKE_URL,
    TOKEN_URL,
    USER_INFO_URL,
    VIDEO_LIST_URL,
)

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20

# Waits before each retry (so up to len+1 attempts). TikTok's limit is a
# one-minute sliding window, so the last wait gives it room to slide.
RETRY_WAITS_SECONDS = (2, 8, 30)
_sleep = time.sleep  # patched in tests

# Read calls may be repeated safely. The token endpoint may not: an
# authorization code is single-use and a refresh may rotate the refresh token,
# so a retry after a 5xx the server actually processed would spend it twice.
# Only a 429 — refused before processing — is retried there.
_READ_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TOKEN_RETRY_STATUSES = frozenset({429})


def _send(method: str, url: str, what: str, *, retry_statuses: frozenset[int], retry_network: bool, **kwargs: Any):
    """One HTTP call with bounded retries; returns the final ``Response``.

    Raises TikTokError when the call cannot reach TikTok at all.
    """
    kwargs.setdefault("timeout", _TIMEOUT_SECONDS)
    for attempt in range(len(RETRY_WAITS_SECONDS) + 1):
        last = attempt == len(RETRY_WAITS_SECONDS)
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            if not retry_network or last:
                raise TikTokError(f"{what} unreachable: {type(e).__name__}") from e
            logger.warning("%s unreachable (%s) — retrying", what, type(e).__name__)
        else:
            if resp.status_code not in retry_statuses or last:
                return resp
            logger.warning("%s answered HTTP %s — retrying", what, resp.status_code)
        _sleep(RETRY_WAITS_SECONDS[attempt])
    raise TikTokError(f"{what} failed after retries")  # unreachable: the last attempt returns or raises


class TikTokError(Exception):
    """A TikTok credential, OAuth or API call failed."""


def load_client_credentials() -> tuple[str, str]:
    """Return ``(client_key, client_secret)`` from the environment.

    Raises TikTokError when either is missing. There is no on-disk fallback:
    the TikTok credentials were SSM-first from the start.
    """
    client_key = (os.environ.get(ENV_CLIENT_KEY) or "").strip()
    client_secret = (os.environ.get(ENV_CLIENT_SECRET) or "").strip()
    if not client_key or not client_secret:
        missing = [name for name, value in ((ENV_CLIENT_KEY, client_key), (ENV_CLIENT_SECRET, client_secret)) if not value]
        raise TikTokError(f"TikTok app credentials are not configured ({', '.join(missing)} unset)")
    return client_key, client_secret


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint and return the parsed success body."""
    resp = _send(
        "POST",
        TOKEN_URL,
        "TikTok token endpoint",
        retry_statuses=_TOKEN_RETRY_STATUSES,
        retry_network=False,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        body = resp.json()
    except ValueError as e:
        raise TikTokError(f"TikTok token endpoint returned HTTP {resp.status_code} with a non-JSON body") from e
    if not isinstance(body, dict):
        raise TikTokError(f"TikTok token endpoint returned HTTP {resp.status_code} with an unexpected body")
    if body.get("error") or not body.get("access_token"):
        raise TikTokError(
            "TikTok token request failed: "
            f"HTTP {resp.status_code} error={body.get('error')!r} "
            f"description={body.get('error_description')!r} log_id={body.get('log_id')!r}"
        )
    return body


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Swap an authorization code for tokens (``grant_type=authorization_code``).

    Returns TikTok's body: ``access_token``, ``expires_in``, ``open_id``,
    ``refresh_token``, ``refresh_expires_in``, ``scope`` (comma-separated),
    ``token_type``.
    """
    client_key, client_secret = load_client_credentials()
    return _token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Mint a fresh access token (``grant_type=refresh_token``).

    TikTok may return a DIFFERENT ``refresh_token`` than the one sent; the
    caller must store whatever comes back.
    """
    client_key, client_secret = load_client_credentials()
    return _token_request(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


def granted_scopes(token_body: dict[str, Any]) -> list[str]:
    """The scopes a token response says were granted, as a sorted list."""
    raw = str(token_body.get("scope") or "")
    return sorted({s.strip() for s in raw.split(",") if s.strip()})


def refresh_expiry_iso(token_body: dict[str, Any], now: datetime | None = None) -> str | None:
    """ISO-8601 UTC instant the refresh token expires, or None when TikTok
    did not say. This is when the account must re-authorize."""
    try:
        seconds = int(token_body.get("refresh_expires_in") or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def _api_error(resp: requests.Response, body: Any) -> TikTokError:
    err = (body.get("error") if isinstance(body, dict) else None) or {}
    return TikTokError(
        f"TikTok API call failed: HTTP {resp.status_code} code={err.get('code')!r} "
        f"message={err.get('message')!r} log_id={err.get('log_id')!r}"
    )


def get_user_info(access_token: str, fields: list[str]) -> dict[str, Any]:
    """``GET /v2/user/info/`` — return the ``data.user`` object.

    A field whose scope was not granted makes TikTok fail the whole call, so
    request only fields the grant covers.
    """
    resp = _send(
        "GET",
        USER_INFO_URL,
        "TikTok user info",
        retry_statuses=_READ_RETRY_STATUSES,
        retry_network=True,
        params={"fields": ",".join(fields)},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        body = resp.json()
    except ValueError as e:
        raise TikTokError(f"TikTok user info returned HTTP {resp.status_code} with a non-JSON body") from e
    error_code = ((body.get("error") or {}).get("code") if isinstance(body, dict) else None) or ""
    if resp.status_code != 200 or error_code not in ("", "ok"):
        raise _api_error(resp, body)
    user = ((body.get("data") or {}).get("user")) or {}
    if not isinstance(user, dict):
        raise TikTokError("TikTok user info returned no user object")
    return user


# TikTok caps a video.list page at 20.
VIDEO_PAGE_SIZE = 20


def list_videos(access_token: str, fields: list[str], max_pages: int) -> tuple[list[dict[str, Any]], bool]:
    """``POST /v2/video/list/`` — every video of the authorizing account, newest
    first, following ``cursor`` / ``has_more``.

    Returns ``(videos, truncated)``; ``truncated`` is True when ``max_pages``
    stopped the walk before TikTok said there was nothing more, so the caller
    can report the account as partially imported instead of silently short.
    """
    videos: list[dict[str, Any]] = []
    cursor: int | None = None
    for _ in range(max_pages):
        payload: dict[str, Any] = {"max_count": VIDEO_PAGE_SIZE}
        if cursor is not None:
            payload["cursor"] = cursor
        resp = _send(
            "POST",
            VIDEO_LIST_URL,
            "TikTok video list",
            retry_statuses=_READ_RETRY_STATUSES,
            retry_network=True,
            params={"fields": ",".join(fields)},
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            body = resp.json()
        except ValueError as e:
            raise TikTokError(f"TikTok video list returned HTTP {resp.status_code} with a non-JSON body") from e
        error_code = ((body.get("error") or {}).get("code") if isinstance(body, dict) else None) or ""
        if resp.status_code != 200 or error_code not in ("", "ok"):
            raise _api_error(resp, body)
        data = body.get("data") or {}
        videos.extend(v for v in (data.get("videos") or []) if isinstance(v, dict))
        if not data.get("has_more"):
            return videos, False
        next_cursor = data.get("cursor")
        if next_cursor is None or next_cursor == cursor:
            # A cursor that doesn't move would loop forever; stop and say so.
            logger.warning("TikTok video list: has_more with a stuck cursor (%r) — stopping", next_cursor)
            return videos, True
        cursor = next_cursor
    return videos, True


def revoke(refresh_token: str) -> None:
    """End the grant at TikTok (``POST /v2/oauth/revoke/``).

    The revoke endpoint takes an ACCESS token and DEAM stores only the refresh
    token, so this refreshes first. Raises TikTokError on any failure; the
    caller decides whether that matters (a local disconnect stands either way).
    """
    access_token = str(refresh_access_token(refresh_token).get("access_token") or "")
    client_key, client_secret = load_client_credentials()
    resp = _send(
        "POST",
        REVOKE_URL,
        "TikTok revoke",
        retry_statuses=_READ_RETRY_STATUSES,  # revoking twice is harmless
        retry_network=True,
        data={"client_key": client_key, "client_secret": client_secret, "token": access_token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code == 200:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    raise TikTokError(
        f"TikTok revoke failed: HTTP {resp.status_code} error={body.get('error')!r} log_id={body.get('log_id')!r}"
    )
