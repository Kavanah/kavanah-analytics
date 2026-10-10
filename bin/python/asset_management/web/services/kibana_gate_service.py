"""
Kibana gate API-key bridge.

Why this exists
---------------
``/kibana`` is already gated by nginx ``auth_request`` → ``GET
/api/internal/kibana-gate``: only an authenticated DEAM superuser
session reaches Kibana at all, and that session may itself have been
established with Google/Microsoft SSO. But Kibana then shows its OWN native
login form, because kibana.yml configures only the ``basic`` provider and the
OIDC/SAML providers that would let Kibana consume the SSO directly are
Platinum-tier features. On the Basic license the free, documented path is HTTP
authentication: Kibana authenticates any request carrying an
``Authorization: ApiKey <encoded>`` header against Elasticsearch, with no
login page and no Kibana session.

This service mints that key. The gate route calls
:meth:`KibanaGateService.authorization_header_for` on a 204 and returns the
value in an ``X-Kibana-Authorization`` response header; nginx copies it into
the upstream ``Authorization`` header (``auth_request_set`` +
``proxy_set_header``, see ``deployment/nginx/conf.d/default.conf.template``).
The browser never sees the key.

Identity model
--------------
The key is OWNED BY THE SESSION USER, not by a shared service identity. The
gate authenticates to ES as a dedicated low-privilege account
(``KIBANA_GATE_USER`` / ``KIBANA_GATE_PASSWORD``, ES role ``de_kibana_gate``)
whose only cluster privilege is ``grant_api_key`` and whose ``run_as`` list is
the explicit set of superuser usernames. ``POST /_security/api_key/grant`` with
``run_as=<session user>`` therefore yields a key that Elasticsearch attributes
to that user in its audit log, carrying exactly that user's privileges — and
Elasticsearch refuses the grant for any username outside the ``run_as`` list,
so a compromised gate credential cannot impersonate ``elastic`` or anyone else
who is not already a superuser. Verified on the dev cluster (ES 9.3.2, Basic
license) 2026-09-04.

Storage
-------
Flask sessions here are signed cookies (see DECISIONS.md 2026-06-17),
so the key can NOT live in ``session`` — it would travel to the browser.
Instead the encrypted key lives server-side in the ``de_kibana_gate_keys``
index, one doc per user (``_id = username``), Fernet-encrypted with the
project ``ENC_KEY`` (same as connector tokens and pending-registration
passwords). The gate request never writes to the session at all: nginx
``auth_request`` discards the subrequest's ``Set-Cookie``, so anything stored
there would be lost — keying the doc on the username (already in the signed
session) needs no new cookie state.

Failure model
-------------
Every method is fail-SOFT and never raises into the gate: a failed mint or
lookup logs, writes a ``kibana_gate.mint_failed`` audit event, and returns
``None`` — the gate then answers a bare 204 and Kibana falls back to its own
login form, i.e. exactly the pre-feature behavior. That direction is safe: the
only thing a failure can withhold is convenience, never authorization (the
gate's 401/403 decisions run BEFORE this service and fail closed,
KIBANA-004). When the two env vars are unset the feature is off by absence
 and the gate is byte-identical to before.

Revocation
----------
``/logout`` calls :meth:`revoke_for`, which invalidates the key in
Elasticsearch (authenticating AS the key — the gate account deliberately lacks
``manage_api_key``) and deletes the doc. Keys also carry a hard expiration
(``KIBANA_GATE_KEY_TTL``, default 8h) so an orphaned key — crashed logout,
abandoned browser — dies on its own; a revoked or expired key is refused by
Kibana immediately (verified: 401 on the next request).

Session ends that are not a logout
---------------------------------------------
a teammate's review asked for a revoke hook on every non-logout session end,
naming five triggers. Measured against this codebase (2026-09-22, dev ES 9.3.2)
only ONE of them is both reachable and worth wiring:

* **user disabled (suspend)** — wired. ``UserManagementService.suspend_user``
  calls ``revoke_for(..., trigger="user_disabled")``. This is the case the
  ticket was really about, and the revoke fully closes it: ES refuses
  ``run_as`` for a disabled user (401, verified on dev), so once the existing
  key is dead the gate cannot mint a replacement. Without the revoke the key
  stayed live to its TTL — up to 8h of Kibana for a suspended account.
* **user deleted** — unreachable. Both delete paths (``api_delete_user`` and
  ``bulk_action``) refuse any account holding ``superuser``, and only
  superusers ever get a key. A delete performed straight in ES/Kibana has no
  application hook at all, so there is nothing to call from.
* **superuser role removed** — the
  ``/api/user-management/update-roles`` route still cannot cause this:
  ``superuser`` is in BOTH ``_PICKER_HIDDEN_SYSTEM_ROLES`` and
  ``_SUPERUSER_ONLY_SYSTEM_ROLES``, so it re-adds the role for every caller,
  superuser included (, reconfirmed 2026-09-25 — still accurate).
  But ``UserManagementService.update_user_roles`` itself now DOES revoke
  when a write drops ``superuser`` from a target's role list — a successful
  ``_put_user`` calls ``revoke_for(..., trigger="roles_changed")`` whenever
  ``"superuser" in old_roles and "superuser" not in roles``, regardless of
  which caller reaches the service — so a future or direct caller that
  bypasses the route's re-add logic is covered the same way ``suspend_user``
  covers ``user_disabled`` above. Not fired when superuser is added or the
  role set is otherwise unchanged.

  Separately also closes the DEAM-session half of every role
  change, not just this one: ``auth.refresh_session_roles`` re-checks a
  live session's roles against the same cached ``_user_account_record`` on
  every request and rewrites ``session["roles"]`` / ``session["is_admin"]``
  within the 30 s account-state cache window — called from ``verify_auth``
  for ordinary routes and from this route's own gate
  (``/api/internal/kibana-gate``, which ``verify_auth`` skips by design).
  So a session that kept ``superuser`` in its signed cookie loses the gate's
  204 within that window too, without waiting for logout — the key revoke
  above and the session refresh close the same gap from two different
  angles (the key is presented straight to Kibana; the session is what the
  gate itself reads).
* **password reset / change** — now wired, but only
  where it matters. ``UserProvisioningService.reset_password`` and
  ``admin_set_password`` (the admin-triggered writers) both stamp
  ``metadata.credential_changed_at`` AND call
  ``revoke_for(..., trigger="credential_changed")``: ``session_denial_reason``
  / ``verify_auth`` now deny any DEAM session whose ``auth_timestamp``
  predates the stamp (within the 30 s account-state cache TTL, same
  mechanism as the ``user_disabled`` check above), and the key revoke closes
  the same gap this class exists for — Kibana never sees Flask's session,
  so it needed its own kill switch. Self-service ``profile_change_password``
  and the onboarding/SSO provisioning writers also stamp
  ``credential_changed_at`` (so a stray other session is still denied) but do
  **not** revoke: the actor is changing their own credential, so their own
  key should keep working, matching the pre-existing "who is the caller?"
  distinction this whole section draws. The standalone
  ``password_reset/app.py`` flow stamps the same metadata key but has no path
  to this service or to Flask's process cache — that key rides its own
  ``KIBANA_GATE_KEY_TTL`` expiry, same as before this ticket.
* **admin session revocation** — no such endpoint exists.

The sessions themselves: ``session_denial_reason`` also checks
``enabled`` (reason ``user_disabled``), so a suspended user's DEAM session is
denied on their next request through the same 30 s account-state cache the
 deleted-user check uses. The revoke above is still needed — Kibana
presents the key to Kibana directly, never to Flask — so the two together
close suspension end to end.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken
from elasticsearch import ConflictError
from lib.encryption import decrypt, encrypt

if TYPE_CHECKING:  # pragma: no cover — typing only; the runtime imports stay lazy
    from elasticsearch import Elasticsearch

INDEX = "de_kibana_gate_keys"

INDEX_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "api_key_id": {"type": "keyword"},
        # Fernet ciphertext. Never indexed — there is nothing to search on and
        # an indexed copy would just be a second place the ciphertext lives.
        "api_key_enc": {"type": "keyword", "index": False, "doc_values": False},
        "expires_at": {"type": "date"},
        "created_at": {"type": "date"},
        "ip_address": {"type": "keyword"},
        # Set when a revoke could not invalidate the key in ES (review item 4):
        # the row is KEPT so the key id is still on hand. Retried automatically
        # only on the `logout` trigger; on `user_disabled` the retry is manual
        # (re-suspend the account) and the key's own expiration is the backstop
        # — see revoke_for.
        "revoke_failed_at": {"type": "date"},
    }
}

#: Response header the gate sets on a 204; nginx maps it to ``Authorization``.
HEADER_NAME = "X-Kibana-Authorization"

#: Re-mint when the cached key has less than this many seconds left, so a key
#: never expires mid-page-load. Kibana pages fan out dozens of requests.
REMINT_MARGIN_SECONDS = 120

#: A persistent mint failure (gate account misconfigured, user missing from
#: the role's run_as list, ES security API down) recurs on EVERY gated request
#: — dozens per Kibana page load. Audit + error-log it at most once per user
#: per this window; the rest go to debug.
FAILURE_AUDIT_INTERVAL_SECONDS = 300
_failure_audit_last: dict[str, float] = {}
_failure_audit_lock = threading.Lock()

_DEFAULT_TTL_SECONDS = 8 * 3600
_MIN_TTL_SECONDS = 300
_MAX_TTL_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# Dependency seams (module-level so tests can patch them on THIS module object,
# whichever import path — ``web.services`` or ``asset_management.web.services``
# — the app happened to bind).
# ---------------------------------------------------------------------------
def _app_es() -> "Elasticsearch | None":
    """The shared app ES client (owns the de_* indices). Late import: the
    project convention to avoid import cycles."""
    from lib.es_helpers import get_elasticsearch_client

    return get_elasticsearch_client()


def _gate_es(username: str, password: str) -> "Elasticsearch | None":
    """A client authenticated as the gate service account (basic auth)."""
    from lib.es_auth import get_elasticsearch_client_for_auth

    return get_elasticsearch_client_for_auth(username, password)


def _key_es(encoded_key: str) -> "Elasticsearch | None":
    """A client authenticated AS a minted key — used only to invalidate it.

    Reaches into es_helpers' underscore helpers on purpose: the public
    ``get_elasticsearch_client()`` is cached and bound to the app credential,
    and there is no public builder for an uncached, differently-authenticated
    client. If those helpers change shape, this is the one call site to fix."""
    from lib.es_helpers import _build_elasticsearch_client, _es_connection_config

    es_ip, use_ssl, _mode, _secret = _es_connection_config()
    return _build_elasticsearch_client(es_ip, use_ssl, "api_key", encoded_key)


def _should_report_failure(username: str, now: float) -> bool:
    """True at most once per FAILURE_AUDIT_INTERVAL_SECONDS per username."""
    with _failure_audit_lock:
        last = _failure_audit_last.get(username)
        if last is not None and now - last < FAILURE_AUDIT_INTERVAL_SECONDS:
            return False
        _failure_audit_last[username] = now
        # Keep the map bounded: drop entries older than two windows.
        for u in [u for u, t in _failure_audit_last.items() if now - t > 2 * FAILURE_AUDIT_INTERVAL_SECONDS]:
            _failure_audit_last.pop(u, None)
        return True


def _audit(username: str, action: str, success: bool, **kwargs) -> None:
    """Audit write. ``AuditLogService.log_event`` is fire-and-forget by its own
    contract (failures logged, never raised), so no extra guard here."""
    from .audit_log_service import AuditLogService

    AuditLogService.log_event(username, action, success, **kwargs)


class KibanaGateService:
    """Mint, cache, and revoke per-user Kibana API keys for the /kibana gate."""

    # -------------------------------------------------------------------------
    # Configuration
    # -------------------------------------------------------------------------
    @staticmethod
    def gate_credentials() -> tuple[str, str] | None:
        """``(user, password)`` of the gate service account, or ``None`` when
        either env var is blank — the feature-off signal."""
        user = (os.environ.get("KIBANA_GATE_USER") or "").strip()
        password = os.environ.get("KIBANA_GATE_PASSWORD") or ""
        if not user or not password:
            return None
        return user, password

    @classmethod
    def enabled(cls) -> bool:
        return cls.gate_credentials() is not None

    @staticmethod
    def key_ttl_seconds() -> int:
        """Hard expiration for minted keys. Env ``KIBANA_GATE_KEY_TTL``
        (seconds), default 8h, clamped to [5min, 24h] so a typo can neither
        mint a key that dies mid-session nor one that outlives a day."""
        raw = (os.environ.get("KIBANA_GATE_KEY_TTL") or "").strip()
        try:
            ttl = int(raw) if raw else _DEFAULT_TTL_SECONDS
        except ValueError:
            logging.warning("KibanaGateService: KIBANA_GATE_KEY_TTL=%r is not an integer; using default", raw)
            ttl = _DEFAULT_TTL_SECONDS
        return max(_MIN_TTL_SECONDS, min(_MAX_TTL_SECONDS, ttl))

    # -------------------------------------------------------------------------
    # Index bootstrap / housekeeping
    # -------------------------------------------------------------------------
    @classmethod
    def startup(cls) -> bool:
        """App-start hook: ensure the index and purge expired rows — but only
        when the feature is on. With the env vars unset the app must be
        byte-identical to before, including at startup (no new index, no
        delete_by_query). Returns whether anything ran."""
        if not cls.enabled():
            return False
        cls.ensure_index_exists()
        cls.purge_expired()
        return True

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the key index (regular index, not a data stream — mirrors
        PresenceService/AuditLogService). Fail-soft."""
        from lib.es_helpers import (
            create_index_if_not_exists,
            put_regular_index_template,
        )

        es = _app_es()
        if es is None:
            logging.warning("KibanaGateService: could not connect to ES; index not ensured")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("KibanaGateService: put_mapping update: %s", e)

    @staticmethod
    def purge_expired() -> int:
        """Delete cache rows whose key has already expired in ES (the key
        itself is dead; only the row lingers). Returns rows deleted, 0 on
        any failure. Fail-soft — called at startup."""
        try:
            es = _app_es()
            if es is None:
                return 0
            resp = es.delete_by_query(
                index=INDEX,
                body={"query": {"range": {"expires_at": {"lt": "now"}}}},
                conflicts="proceed",
                refresh=True,
            )
            deleted = int((resp or {}).get("deleted", 0) or 0)
            if deleted:
                logging.info("KibanaGateService: purged %d expired key rows", deleted)
            return deleted
        except Exception as e:
            logging.warning("KibanaGateService: purge_expired failed: %s", e)
            return 0

    # -------------------------------------------------------------------------
    # Read path (every gated /kibana request)
    # -------------------------------------------------------------------------
    @classmethod
    def authorization_header_for(cls, username: str | None, *, ip_address: str = "") -> str | None:
        """Return ``"ApiKey <encoded>"`` for ``username``, minting when there
        is no live cached key. NEVER raises: any failure logs + audits and
        returns ``None`` (gate degrades to Kibana's own login).

        The caller has already decided authorization (authenticated AND
        superuser). This method must not be reached for anyone else — the
        ``run_as`` list on the gate role is the server-side backstop if it is.
        """
        creds = cls.gate_credentials()
        if creds is None:
            return None
        if not isinstance(username, str) or not username.strip():
            logging.warning("KibanaGateService: refusing to mint for an empty username")
            return None
        username = username.strip()

        try:
            cached, prev = cls._load_cached(username)
            if cached is not None:
                return "ApiKey " + cached
            encoded = cls._mint_and_store(username, creds, ip_address=ip_address, prev=prev)
            return "ApiKey " + encoded
        except Exception as e:
            # Deliberately broad: this is the one place a mint/lookup problem
            # is converted into "no header". The gate's authz above is not
            # inside this try, so fail-closed there is preserved.
            if _should_report_failure(username, time.time()):
                logging.error("KibanaGateService: mint/lookup failed for user=%s: %s", username, e)
                _audit(username, "kibana_gate.mint_failed", False, ip_address=ip_address, detail=str(e)[:200])
            else:
                logging.debug("KibanaGateService: mint/lookup failed for user=%s (suppressed repeat): %s", username, e)
            return None

    @classmethod
    def _load_cached(cls, username: str) -> tuple[str | None, dict | None]:
        """Return ``(decrypted_key, raw_get_response)``. ``decrypted_key`` is
        set only when the cached row is bound to ``username`` and has more than
        REMINT_MARGIN_SECONDS of life left. The raw GET response (or None when
        no row exists) carries ``_source`` plus ``_seq_no``/``_primary_term``
        so the mint path can supersede the row with optimistic concurrency and
        without a second read."""
        es = _app_es()
        if es is None:
            raise RuntimeError("app ES client unavailable")
        resp = es.get(index=INDEX, id=username, ignore=[404])
        if not resp or not resp.get("found"):
            return None, None
        resp = dict(resp)
        src = resp.get("_source") or {}
        # Belt and braces: the doc id IS the username, but never hand out a
        # key whose recorded owner differs from the session user.
        if src.get("username") != username:
            logging.error("KibanaGateService: cached row for %s carries owner %r — ignoring", username, src.get("username"))
            return None, resp
        expires_at = cls._parse_ts(src.get("expires_at"))
        if expires_at is None or expires_at - time.time() < REMINT_MARGIN_SECONDS:
            return None, resp
        enc = src.get("api_key_enc")
        if not enc:
            return None, resp
        try:
            return decrypt(enc), resp
        except InvalidToken:
            # Written under a different ENC_KEY (rotation, staging→prod restore)
            # or corrupted. Treat as a cache miss so the mint path SUPERSEDES
            # the row — a permanent "None forever" here would lock the user out
            # of the bridge until the row happened to expire (review item 2).
            logging.error("KibanaGateService: cached key for %s is undecryptable — re-minting", username)
            return None, resp

    @classmethod
    def _mint_and_store(cls, username: str, creds: tuple[str, str], *, ip_address: str = "", prev: dict | None = None) -> str:
        """Mint a key for ``username`` and store it as THE row for that user.

        Kibana fans a page load out into dozens of requests; on a cold cache
        several of them reach here at once and each mints its own key. Only one
        row can win, and every LOSER must invalidate the key it just minted —
        otherwise fully privileged, untracked keys stay live until their TTL.
        The write is therefore conditional: ``op_type=create`` when no row
        existed, ``if_seq_no``/``if_primary_term`` from the read when one did.
        A 409 means another request won; we kill our key and serve the
        winner's."""
        gate_user, gate_password = creds
        ttl = cls.key_ttl_seconds()
        gate = _gate_es(gate_user, gate_password)
        if gate is None:
            raise RuntimeError("gate ES client unavailable")

        # Grant API key: the CALLER (gate account, needs cluster grant_api_key)
        # supplies its own credentials in the body and names the user to
        # impersonate. ES enforces the caller's run_as list here.
        resp = gate.security.grant_api_key(
            grant_type="password",
            username=gate_user,
            password=gate_password,
            run_as=username,
            api_key={"name": f"kibana-gate:{username}", "expiration": f"{ttl}s"},
        )
        resp = dict(resp) if resp is not None else {}
        encoded = resp.get("encoded")
        key_id = resp.get("id")
        if not encoded or not key_id:
            raise RuntimeError("grant_api_key returned no key")

        # ES returns expiration as epoch millis; fall back to our own clock.
        exp_ms = resp.get("expiration")
        expires_at = (float(exp_ms) / 1000.0) if isinstance(exp_ms, (int, float)) else time.time() + ttl

        es = _app_es()
        if es is None:
            # We hold a live key we cannot cache: kill it rather than leak it.
            cls._invalidate_key(encoded, key_id)
            raise RuntimeError("app ES client unavailable after mint")

        doc = {
            "username": username,
            "api_key_id": key_id,
            "api_key_enc": encrypt(encoded),
            "expires_at": cls._iso(expires_at),
            "created_at": cls._iso(time.time()),
            "ip_address": ip_address or "",
        }
        # Conditional write (see docstring). ``prev`` is the GET response from
        # _load_cached — no second read.
        write_kwargs: dict = {"index": INDEX, "id": username, "document": doc, "refresh": "wait_for"}
        if prev is None:
            write_kwargs["op_type"] = "create"
        else:
            write_kwargs["if_seq_no"] = prev.get("_seq_no")
            write_kwargs["if_primary_term"] = prev.get("_primary_term")
        try:
            es.index(**write_kwargs)
        except ConflictError:
            # Lost the race. Our key must not stay live and untracked.
            cls._invalidate_key(encoded, key_id)
            logging.info("KibanaGateService: lost mint race for user=%s; using the winner's key", username)
            winner, _resp = cls._load_cached(username)
            if winner is None:
                raise RuntimeError("lost mint race and the winning row is not usable")
            return winner

        # We won. If an older key is being superseded, invalidate it so exactly
        # one live key per user exists.
        if prev:
            cls._invalidate_prev_row(username, prev.get("_source") or {})

        logging.info("AUDIT: user=%s action=kibana_gate.mint key_id=%s ttl=%ss ip=%s", username, key_id, ttl, ip_address)
        _audit(username, "kibana_gate.mint", True, ip_address=ip_address, detail=f"key_id={key_id} ttl={ttl}s")
        return encoded

    # -------------------------------------------------------------------------
    # Revocation (logout)
    # -------------------------------------------------------------------------
    @classmethod
    def revoke_for(cls, username: str | None, *, ip_address: str = "", trigger: str = "logout") -> bool:
        """Invalidate ``username``'s cached key in ES and drop the row.

        Returns True only when the key is known dead (invalidated now, or
        already past its expiration) and the row was removed. If ES refused
        or failed the invalidation while the key is still live, the row is
        KEPT and stamped ``revoke_failed_at`` — deleting it would throw away
        the only handle (``api_key_id``) a retry or an operator has. Fail-soft:
        never raises — logout must always complete.

        **The automatic retry is trigger-specific, and only ``logout`` has one.**
        On ``logout`` the next ``/logout`` retries (review item 4). On
        ``user_disabled`` the retry is manual: the user cannot log in, so they
        never reach ``/logout``, but ``UserManagementService.suspend_user``
        attempts the revoke on its "already suspended" path too, so
        re-suspending the account retries it. The stamp, paired with the ERROR
        log below (key id and expiry), is what tells an operator to do that;
        until they do, the key's own ``KIBANA_GATE_KEY_TTL`` expiration (8h) is
        the backstop — the earlier exposure, so the failure case is no
        worse than before the hook existed (a teammate's review,
        2026-09-22/23).

        ``trigger`` names WHY the key is being revoked and is carried on the
        audit event. ``logout`` is the original path; the only
        other caller today is ``user_disabled`` (see the class docstring's
        "Session ends that are not a logout" note for why the other triggers
        a teammate listed in the review are not wired up)."""
        if not isinstance(username, str) or not username.strip():
            return False
        username = username.strip()
        try:
            es = _app_es()
            if es is None:
                return False
            resp = es.get(index=INDEX, id=username, ignore=[404])
            if not resp or not resp.get("found"):
                return False
            src = resp.get("_source") or {}
            invalidated = cls._invalidate_prev_row(username, src)
            expires_at = cls._parse_ts(src.get("expires_at"))
            already_expired = expires_at is not None and expires_at <= time.time()
            key_dead = bool(invalidated) or already_expired
            if key_dead:
                es.delete(index=INDEX, id=username, ignore=[404], refresh="wait_for")
            else:
                es.update(index=INDEX, id=username, doc={"revoke_failed_at": cls._iso(time.time())}, refresh="wait_for")
                logging.error(
                    "KibanaGateService: could not invalidate key_id=%s for user=%s — row kept for retry; " "key expires at %s",
                    src.get("api_key_id"),
                    username,
                    src.get("expires_at"),
                )
            logging.info(
                "AUDIT: user=%s action=kibana_gate.revoke trigger=%s key_id=%s invalidated=%s expired=%s row_removed=%s ip=%s",
                username,
                trigger,
                src.get("api_key_id"),
                invalidated,
                already_expired,
                key_dead,
                ip_address,
            )
            _audit(
                username,
                "kibana_gate.revoke",
                key_dead,
                ip_address=ip_address,
                detail=(
                    f"trigger={trigger} key_id={src.get('api_key_id')} invalidated={invalidated} "
                    f"expired={already_expired} row_removed={key_dead}"
                ),
            )
            return key_dead
        except Exception as e:
            logging.error("KibanaGateService: revoke failed for user=%s (trigger=%s): %s", username, trigger, e)
            return False

    @classmethod
    def _invalidate_prev_row(cls, username: str, src: dict) -> bool:
        """Invalidate the key recorded in ``src`` — but ONLY if the row says it
        belongs to ``username``. A row whose recorded owner differs (should be
        impossible; the id IS the username) is left alone: superseding it is
        fine, killing someone else's live key as a side effect is not."""
        if src.get("username") != username:
            logging.error(
                "KibanaGateService: row for %s carries owner %r — not invalidating its key", username, src.get("username")
            )
            return False
        enc = src.get("api_key_enc")
        key_id = src.get("api_key_id")
        if not enc or not key_id:
            return False
        try:
            return cls._invalidate_key(decrypt(enc), key_id)
        except Exception as e:
            logging.warning("KibanaGateService: could not invalidate prior key for %s: %s", username, e)
            return False

    @staticmethod
    def _invalidate_key(encoded: str, key_id: str) -> bool:
        """Invalidate one key by id, authenticating AS that key. A key that is
        already expired/invalid cannot authenticate — that is fine, it is
        already dead; report False and move on."""
        try:
            key_client = _key_es(encoded)
            if key_client is None:
                return False
            resp = key_client.security.invalidate_api_key(ids=[key_id])
            resp = dict(resp) if resp is not None else {}
            return key_id in (resp.get("invalidated_api_keys") or [])
        except Exception as e:
            logging.warning("KibanaGateService: invalidate_api_key(%s) failed: %s", key_id, e)
            return False

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _parse_ts(value) -> float | None:
        """Parse the ISO-8601 UTC stamp written by ``_iso``. Anything else
        (missing, malformed) parses as None → the row is treated as expired
        and re-minted, which is the safe outcome."""
        if not isinstance(value, str) or not value:
            return None
        try:
            s = value[:-1] + "+00:00" if value.endswith("Z") else value
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
