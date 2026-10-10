"""Shared, IP-aware login / MFA lockout.

Before the brute-force lockout was ``auth.failed_attempts`` — a
``defaultdict(list)`` in ONE gunicorn worker, keyed by username only. That
meant:

* a distributed slow guess spread across the workers never tripped it (each
  worker saw a fraction of the failures);
* a worker restart (deploy, SIGHUP, OOM) reset every counter to zero;
* an attacker rotating usernames from one address never hit any threshold,
  because nothing counted by IP.

The store is now an Elasticsearch index, ``de_auth_lockout`` — one small
document per failed attempt ``{username, ip, ts}``. Every worker and every
restart sees the same rows; the window is a range query on ``ts``; nothing is
ever mutated in place, so concurrent workers cannot lose each other's writes.
Two thresholds are evaluated on every check:

* **username** — ``USERNAME_MAX`` (5) failures in ``WINDOW_SECONDS`` (15 min):
  unchanged from the original contract, so the tests and the
  operator expectations hold;
* **IP** — ``IP_MAX`` (default 20, ``LOGIN_LOCKOUT_IP_MAX``) failures from one
  ``remote_addr`` in the same window, *regardless of username*. High enough
  that a shared office NAT with a few people mistyping never trips it; low
  enough that a username-rotating attacker does.

Both the lock and the unlock are visible: the first failure that crosses a
threshold writes an ``account_locked`` / ``ip_locked`` event to
``de_audit_log``; a successful login that clears a locked username writes
``lockout_cleared``. Expiry by window is silent (there is no event to hang it
on).

**Degraded mode.** If Elasticsearch is unreachable the service counts in the
in-process dictionaries below — exactly the earlier behaviour, no
weaker. It never fails *open*: an ES outage cannot disable the lockout. (ES
is also the authentication backend, so a login cannot succeed during an
outage anyway; the fallback matters for the counters it keeps, not the
decisions it makes.) ``auth.failed_attempts`` IS ``local_username_failures``
— the same object — so the existing test suites that seed or clear it keep
their meaning.

Call sites keep the historical names in ``web/auth.py``
(``check_rate_limit`` / ``record_failed_attempt``), which now delegate here
and add the request's ``remote_addr``; ``clear_failed_attempts`` replaces the
inline ``del failed_attempts[username]``.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

INDEX = "de_auth_lockout"
MAPPING: dict[str, Any] = {
    "properties": {
        # Lower-cased username the failure was recorded against. Keyword so a
        # term query is exact; never analysed.
        "username": {"type": "keyword"},
        # request.remote_addr as seen by Flask. Keyword, not ``ip``: behind a
        # proxy misconfiguration the value could be a comma-list or blank, and
        # a mapping rejection here would drop the audit trail of an attack.
        "ip": {"type": "keyword"},
        # When the failure happened (UTC ISO-8601). The window is a range on it.
        "ts": {"type": "date"},
        # Which surface failed — informational, for the operator reading rows.
        "surface": {"type": "keyword"},
    }
}

WINDOW_SECONDS = 900  # 15 minutes — unchanged from the earlier contract
USERNAME_MAX = 5  # unchanged from the earlier contract


def _ip_max() -> int:
    """``LOGIN_LOCKOUT_IP_MAX`` (default 20). Read at call time so a test or an
    operator can change it without a restart."""
    raw = os.environ.get("LOGIN_LOCKOUT_IP_MAX", "")
    try:
        value = int(raw) if raw.strip() else 20
    except ValueError:
        value = 20
    return max(1, value)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _window_start_iso() -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(seconds=WINDOW_SECONDS)).isoformat()


class LoginLockoutService:
    """Shared failed-login counters with username AND IP thresholds."""

    # In-process fallback stores (degraded mode) — see module docstring.
    # ``web.auth.failed_attempts`` is bound to ``local_username_failures``.
    local_username_failures: dict[str, list[float]] = defaultdict(list)
    local_ip_failures: dict[str, list[float]] = defaultdict(list)

    _index_ready = False
    _degraded_logged = False

    # ── infrastructure ────────────────────────────────────────────────────────

    @classmethod
    def _es(cls):
        try:
            from lib.es_helpers import get_elasticsearch_client
        except ImportError:  # pragma: no cover - import boundary
            return None
        try:
            return get_elasticsearch_client()
        except Exception:  # noqa: BLE001 — an ES client failure means degraded mode, never fail-open
            return None

    @classmethod
    def ensure_index(cls, es=None) -> bool:
        """Create ``de_auth_lockout`` with its mapping if missing. Called at app
        start (``app.py``) and lazily on first use."""
        es = es if es is not None else cls._es()
        if es is None:
            return False
        if cls._index_ready:
            return True
        try:
            from lib.es_helpers import create_index_if_not_exists

            cls._index_ready = bool(create_index_if_not_exists(es, INDEX, MAPPING))
        except Exception as exc:  # noqa: BLE001 — boundary; degraded mode below
            logging.warning("LoginLockoutService: could not ensure %s: %s", INDEX, exc)
            cls._index_ready = False
        return cls._index_ready

    @classmethod
    def _degraded(cls, why: str) -> None:
        if not cls._degraded_logged:
            logging.warning(
                "LoginLockoutService: Elasticsearch unavailable (%s) — counting failed logins in-process "
                "(per-worker, reset on restart) until it returns",
                why,
            )
            cls._degraded_logged = True

    @staticmethod
    def _norm_user(username: str) -> str:
        return (username or "").strip().lower()

    @staticmethod
    def _norm_ip(ip: str | None) -> str:
        return (ip or "").strip()

    # ── ES-backed counting ────────────────────────────────────────────────────

    @staticmethod
    def _count(es, field: str, value: str) -> int:
        body = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {field: value}},
                        {"range": {"ts": {"gte": _window_start_iso()}}},
                    ]
                }
            }
        }
        resp = es.count(index=INDEX, body=body)
        return int((resp or {}).get("count", 0))

    # ── local fallback counting ───────────────────────────────────────────────

    @classmethod
    def _local_prune(cls, store: dict[str, list[float]], key: str) -> list[float]:
        now = time.time()
        kept = [t for t in store.get(key, []) if now - t < WINDOW_SECONDS]
        if kept:
            store[key] = kept
        else:
            store.pop(key, None)
        return kept

    # ── public API ────────────────────────────────────────────────────────────

    @classmethod
    def is_allowed(cls, username: str, ip: str | None = None) -> bool:
        """True while BOTH the username and the IP are under their thresholds."""
        user = cls._norm_user(username)
        addr = cls._norm_ip(ip)
        es = cls._es()
        if es is not None and cls.ensure_index(es):
            try:
                if user and cls._count(es, "username", user) >= USERNAME_MAX:
                    return False
                if addr and cls._count(es, "ip", addr) >= _ip_max():
                    return False
                return True
            except Exception as exc:  # noqa: BLE001 — fall through to degraded counting, never fail-open
                cls._degraded(f"count failed: {type(exc).__name__}")
        else:
            cls._degraded("no client")
        if user and len(cls._local_prune(cls.local_username_failures, user)) >= USERNAME_MAX:
            return False
        if addr and len(cls._local_prune(cls.local_ip_failures, addr)) >= _ip_max():
            return False
        return True

    @classmethod
    def record_failure(cls, username: str, ip: str | None = None, *, surface: str = "login") -> None:
        """Record one failed attempt and emit the lock event if this failure
        crosses a threshold (exactly once per crossing)."""
        user = cls._norm_user(username)
        addr = cls._norm_ip(ip)
        if not user and not addr:
            return
        es = cls._es()
        user_count = ip_count = None
        if es is not None and cls.ensure_index(es):
            try:
                es.index(
                    index=INDEX,
                    document={"username": user, "ip": addr, "ts": _now_iso(), "surface": surface},
                    refresh="wait_for",
                )
                user_count = cls._count(es, "username", user) if user else 0
                ip_count = cls._count(es, "ip", addr) if addr else 0
            except Exception as exc:  # noqa: BLE001 — degrade, never drop the failure silently
                cls._degraded(f"index/count failed: {type(exc).__name__}")
                user_count = ip_count = None
        else:
            cls._degraded("no client")
        if user_count is None:
            now = time.time()
            if user:
                cls._local_prune(cls.local_username_failures, user)
                cls.local_username_failures[user].append(now)
                user_count = len(cls.local_username_failures[user])
            else:
                user_count = 0
            if addr:
                cls._local_prune(cls.local_ip_failures, addr)
                cls.local_ip_failures[addr].append(now)
                ip_count = len(cls.local_ip_failures[addr])
            else:
                ip_count = 0

        if user and user_count == USERNAME_MAX:
            cls._audit(user, "account_locked", addr, f"{USERNAME_MAX} failed {surface} attempts in {WINDOW_SECONDS // 60} min")
        if addr and ip_count == _ip_max():
            cls._audit(
                user,
                "ip_locked",
                addr,
                f"{_ip_max()} failed {surface} attempts from one address in {WINDOW_SECONDS // 60} min",
            )

    @classmethod
    def clear(cls, username: str, ip: str | None = None) -> None:
        """Forget a username's failures after a successful login. Emits
        ``lockout_cleared`` when the username was actually locked."""
        user = cls._norm_user(username)
        if not user:
            return
        was_locked = False
        es = cls._es()
        if es is not None and cls.ensure_index(es):
            try:
                was_locked = cls._count(es, "username", user) >= USERNAME_MAX
                es.delete_by_query(
                    index=INDEX,
                    body={"query": {"term": {"username": user}}},
                    refresh=True,
                    conflicts="proceed",
                )
            except Exception as exc:  # noqa: BLE001 — degrade; the local store is cleared below regardless
                cls._degraded(f"clear failed: {type(exc).__name__}")
        local = cls._local_prune(cls.local_username_failures, user)
        was_locked = was_locked or len(local) >= USERNAME_MAX
        cls.local_username_failures.pop(user, None)
        if was_locked:
            cls._audit(user, "lockout_cleared", cls._norm_ip(ip), "successful login cleared the lockout")

    # ── audit ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _audit(username: str, action: str, ip: str, detail: str) -> None:
        logging.warning("AUDIT: user=%s action=%s ip=%s detail=%s", username or "-", action, ip or "-", detail)
        try:
            try:
                from web.services.audit_log_service import AuditLogService
            except ImportError:
                from .audit_log_service import AuditLogService
            AuditLogService.log_event(
                username or "-",
                action,
                action == "lockout_cleared",
                ip_address=ip,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 — audit is best-effort; the lock decision already stands
            logging.warning("LoginLockoutService: audit write failed for %s: %s", action, exc)
