"""One way to call the Chatwoot API from the importers.

Before this, every fetch helper turned any HTTP error into ``None`` or an empty
list. A self-hosted server rate-limits a backfill with 429 after about 23 days,
so those days were saved as all-null reports and the run still logged success.
A wrong token (401) went the same way. Now:

* 429 and the transient 502 / 503 / 504, plus connection errors and timeouts,
  are retried — honouring ``Retry-After`` when the server sends one, otherwise
  exponential backoff — up to ``MAX_ATTEMPTS``;
* 401 / 403 raise :class:`ChatwootAuthError` at once (retrying cannot fix a
  refused token);
* anything still failing raises :class:`ChatwootApiError`, so the caller skips
  the day instead of saving nulls, and the run exits non-zero.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from lib.user_errors import UserFacingError

RETRY_STATUSES = frozenset({429, 502, 503, 504})
AUTH_STATUSES = frozenset({401, 403})
MAX_ATTEMPTS = 6
MAX_WAIT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30


class ChatwootApiError(RuntimeError):
    """A Chatwoot request failed after every retry, or answered something that
    is not usable data. The caller must not save a report built from it."""


class ChatwootAuthError(ChatwootApiError):
    """Chatwoot refused the account's token (401 / 403): a credential problem
    for the whole account, not a transient one."""


def _retry_after_seconds(response: requests.Response) -> float | None:
    """``Retry-After`` as seconds, or None when absent or given as an HTTP date
    (backoff is used then)."""
    raw = response.headers.get("Retry-After")
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    return float(raw.strip())


def _backoff_seconds(attempt: int) -> float:
    return float(min(2**attempt, MAX_WAIT_SECONDS))


def chatwoot_request(
    method: str,
    url: str,
    *,
    api_token: str,
    what: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:  # Any: the parsed JSON body, whose shape differs per endpoint
    """Send one Chatwoot API request and return its parsed JSON body.

    ``what`` names the request in logs and errors (never the token). Redirects
    are refused (the base URL is vetted, a redirect target is not).

    Raises :class:`ChatwootAuthError` on 401 / 403 and :class:`ChatwootApiError`
    when the request still fails after ``MAX_ATTEMPTS`` or the body is not JSON.
    """
    send = requests.post if method == "POST" else requests.get
    last_problem = "no attempt made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = send(
                url,
                headers={"api_access_token": api_token},
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            last_problem = f"{type(e).__name__}: {e}"
            wait = _backoff_seconds(attempt)
        else:
            status = response.status_code
            if status in AUTH_STATUSES:
                raise ChatwootAuthError(f"{what}: Chatwoot refused the API token (HTTP {status}).")
            if status not in RETRY_STATUSES:
                try:
                    response.raise_for_status()
                    return response.json()
                except requests.HTTPError as e:
                    raise ChatwootApiError(f"{what}: {e}") from e
                except ValueError as e:
                    raise ChatwootApiError(f"{what}: the response was not JSON ({e}).") from e
            last_problem = f"HTTP {status}"
            retry_after = _retry_after_seconds(response)
            wait = min(retry_after, MAX_WAIT_SECONDS) if retry_after is not None else _backoff_seconds(attempt)

        if attempt == MAX_ATTEMPTS:
            break
        logging.warning(f"{what}: {last_problem}; retrying in {wait:.0f}s (attempt {attempt} of {MAX_ATTEMPTS}).")
        time.sleep(wait)

    raise ChatwootApiError(f"{what}: still failing after {MAX_ATTEMPTS} attempts ({last_problem}).")


# ── verify an account's credentials when it is saved ───────────────
#
# Two bad credentials saved without complaint on 2026-09-25 and only failed at
# import (a self-hosted token aimed at Chatwoot Cloud, then an Agent Bot token).
# The save paths call verify_account_access before writing. Messages are fixed
# strings: the token, the server address and the account id are never echoed
#. One attempt per call, no retries — a save must not sit through the
# importer's backoff.

MSG_UNREACHABLE = "Could not reach the Chatwoot server. Check its address and that it is online."
MSG_NOT_CHATWOOT = "That server did not answer like a Chatwoot server. Check its address."
MSG_BOT_TOKEN = (
    "Chatwoot refused this token because it is an Agent Bot token. Use a user Access Token "
    "(Profile settings) from an administrator of this account, not a bot token."
)
MSG_TOKEN_REFUSED = "Chatwoot refused the API token. Check the token, and that it belongs to this server."
MSG_NOT_MEMBER = "The token's user is not a member of this Chatwoot account. Check the account ID."
MSG_NOT_ADMIN = (
    "The token's user is not an administrator of this Chatwoot account. Reports need an " "administrator's Access Token."
)
MSG_NO_REPORTS = "Chatwoot did not return reports for this account with this token."
VERIFY_TIMEOUT_SECONDS = 15


class ChatwootCredentialError(UserFacingError, ChatwootApiError):
    """The saved server, account id or token cannot be used; ``str(e)`` is a
    user-safe message."""


def _verify_get(url: str, api_token: str, params: dict[str, Any] | None = None) -> requests.Response:
    try:
        return requests.get(
            url,
            headers={"api_access_token": api_token},
            params=params,
            timeout=VERIFY_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        logging.warning("Chatwoot verify: request failed: %s", type(e).__name__)
        raise ChatwootCredentialError(MSG_UNREACHABLE) from e


def verify_account_access(base_url: str, account_id: Any, api_token: str) -> None:  # Any: form value, str or int
    """Raise :class:`ChatwootCredentialError` unless ``api_token`` belongs to an
    administrator of ``account_id`` on ``base_url`` and can read its reports.

    ``base_url`` must already be vetted (lib.chatwoot_base_url).
    """
    account = str(account_id or "").strip()
    token = str(api_token or "").strip()
    if not account.isdigit():
        raise ChatwootCredentialError(MSG_NOT_MEMBER)
    if not token:
        raise ChatwootCredentialError(MSG_TOKEN_REFUSED)

    profile = _verify_get(f"{base_url}/api/v1/profile", token)
    if profile.status_code in AUTH_STATUSES:
        raise ChatwootCredentialError(MSG_BOT_TOKEN if "not authorized for bots" in profile.text else MSG_TOKEN_REFUSED)
    if profile.status_code != 200:
        raise ChatwootCredentialError(MSG_NOT_CHATWOOT)
    try:
        body = profile.json()
    except ValueError as e:
        raise ChatwootCredentialError(MSG_NOT_CHATWOOT) from e
    accounts = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(accounts, list):
        raise ChatwootCredentialError(MSG_NOT_CHATWOOT)
    membership = next((a for a in accounts if isinstance(a, dict) and str(a.get("id")) == account), None)
    if membership is None:
        raise ChatwootCredentialError(MSG_NOT_MEMBER)
    if membership.get("role") != "administrator":
        raise ChatwootCredentialError(MSG_NOT_ADMIN)

    now = int(time.time())
    summary = _verify_get(
        f"{base_url}/api/v2/accounts/{account}/reports/summary",
        token,
        params={"type": "account", "since": now - 86400, "until": now},
    )
    if summary.status_code in AUTH_STATUSES:
        raise ChatwootCredentialError(MSG_BOT_TOKEN if "not authorized for bots" in summary.text else MSG_NOT_ADMIN)
    if summary.status_code != 200:
        raise ChatwootCredentialError(MSG_NO_REPORTS)
