"""
User Management Service
Handles suspend/reinstate, role reassignment, user detail view,
and bulk operations via the Elasticsearch Security API.
"""

import logging
from datetime import datetime, timezone


class UserManagementService:

    @staticmethod
    def _get_user(es, username):
        """Fetch a single ES Security user. Returns (user_data, error_msg)."""
        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if user_data is None:
                return None, f"User '{username}' not found"
            return user_data, None
        except Exception as e:
            logging.error("UserManagementService: failed to fetch user '%s': %s", username, e)
            return None, "Could not fetch user."

    @staticmethod
    def _put_user(es, username, user_data, **overrides):
        """PUT user preserving existing fields. Omits password to keep it unchanged.

        ``es`` is the caller's shared client; the write itself goes through the
         admin client (which IS ``es`` until ES_ADMIN_API_KEY is set)."""
        from lib.es_helpers import get_es_admin_client

        metadata = dict(user_data.get("metadata", {}))
        metadata.update(overrides.pop("metadata_updates", {}))

        get_es_admin_client(es).security.put_user(
            username=username,
            roles=overrides.get("roles", user_data.get("roles", [])),
            full_name=user_data.get("full_name", ""),
            email=user_data.get("email", ""),
            enabled=overrides.get("enabled", user_data.get("enabled", True)),
            metadata=metadata,
        )

    @staticmethod
    def suspend_user(username: str) -> dict:
        """Suspend a user by setting enabled=False in Elasticsearch Security."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserManagementService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        if not user_data.get("enabled", True):
            # a re-suspend is the operator's manual retry for a
            # revoke that failed the first time (a suspended user never reaches
            # /logout, the only automatic retry). Fail-soft, and silent when
            # there is no key row. The return value is unchanged on purpose:
            # the caller still hears "already suspended" and bulk_action still
            # counts the user as an error.
            UserManagementService._revoke_kibana_gate_key(username, trigger="user_disabled")
            return {"success": False, "error": f"User '{username}' is already suspended"}

        try:
            now = datetime.now(timezone.utc).isoformat()
            UserManagementService._put_user(
                es,
                username,
                user_data,
                enabled=False,
                metadata_updates={"suspended_at": now},
            )
        except Exception as e:
            logging.error("UserManagementService: failed to suspend '%s': %s", username, e)
            return {"success": False, "error": "Could not suspend the user."}

        UserManagementService._flush_account_state_cache(username)
        UserManagementService._revoke_kibana_gate_key(username, trigger="user_disabled")
        return {"success": True}

    @staticmethod
    def _flush_account_state_cache(username: str) -> None:
        """drop ``username`` from ``verify_auth``'s account-state
        cache right after the account is suspended, reinstated or deleted,
        so the next request served by THIS process re-checks ES Security
        instead of riding the cached state for up to
        ``_USER_EXISTS_CACHE_TTL_SECONDS`` (30 s).

        The cache is per process. DEAM runs two gunicorn workers
        (``start_web_apps.sh``), so a worker other than the one that served
        the admin's request keeps its entry until the TTL expires: the
        guarantee is "denied within 30 s", and usually sooner — not "the very
        next request". Cross-process invalidation would need shared state and
        is not worth it for a 30 s bound.

        Lives here rather than in the routes for the the
        single-user endpoints and ``bulk_action`` both call these service
        methods, so one call site covers all of them. The single-user delete
        route and ``bulk_action``'s delete branch call it directly.

        Best-effort and never raises: the write to ES Security has already
        happened, and a failure here only means the TTL bounds the window.
        """
        try:
            try:
                from web.auth import _invalidate_user_exists_cache
            except ImportError:  # pragma: no cover — alternate import root
                from asset_management.web.auth import _invalidate_user_exists_cache

            _invalidate_user_exists_cache(username)
        except Exception as e:  # noqa: BLE001 — never block the write path
            logging.warning("account-state cache flush failed for %r: %s", username, e)

    @staticmethod
    def _revoke_kibana_gate_key(username: str, *, trigger: str) -> None:
        """kill this user's Kibana gate API key after a non-logout
        session end.

        Lives here rather than in the routes so the single-user endpoint and
        ``bulk_action`` (which calls ``suspend_user``) are both covered by one
        call site, and a future suspend caller cannot forget it.

        Only ``suspend`` needs this. Since the DEAM session itself is
        also denied within 30 s (``session_denial_reason`` /
        ``verify_auth`` check ``enabled``), but that runs through the 30 s
        account-state cache and only on requests that reach Flask — the
        Kibana key is presented to Kibana directly, so it has to be killed
        here. Elasticsearch refuses ``run_as`` for a disabled user, so the
        gate cannot mint a replacement; killing the existing key therefore
        ends Kibana access outright instead of leaving it live for up to
        ``KIBANA_GATE_KEY_TTL`` (8h). The other triggers from a teammate's
        review are either unreachable or futile; the reasoning is in
        ``kibana_gate_service``'s "Session ends that are not a logout".

        Fail-soft and never raises: ``revoke_for`` swallows its own failures
        and keeps the row stamped ``revoke_failed_at``, and a suspension must
        not fail because ES security is unreachable.

        On THIS trigger there is no AUTOMATIC retry: a suspended user never
        reaches ``/logout``, the only path that retries on its own. The retry
        is manual — ``suspend_user`` also attempts this revoke on its "already
        suspended" path, so an operator who sees ``revoke_for``'s ERROR log
        (key id and expiry) re-suspends the account to retry. Until they do,
        the key stays live to ``KIBANA_GATE_KEY_TTL`` (8h) — the exposure this
        ticket set out to close, bounded and exactly the earlier
        behaviour, so the hook is an improvement rather than a guarantee. A
        scheduled sweep over ``revoke_failed_at`` rows was considered and
        rejected: a job that quietly stops running is the defect
        class (a teammate's review, 2026-09-22/23).
        """
        try:
            try:
                from web.services.kibana_gate_service import KibanaGateService
            except ImportError:  # pragma: no cover — alternate import root
                from asset_management.web.services.kibana_gate_service import KibanaGateService

            KibanaGateService.revoke_for(username, trigger=trigger)
        except Exception as e:  # noqa: BLE001 — never block the write path
            logging.warning(
                "kibana gate revoke (trigger=%s) failed for %r: %s",
                trigger,
                username,
                e,
            )

    @staticmethod
    def reinstate_user(username: str) -> dict:
        """Reinstate a suspended user by setting enabled=True."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserManagementService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        if user_data.get("enabled", True):
            return {"success": False, "error": f"User '{username}' is not suspended"}

        try:
            now = datetime.now(timezone.utc).isoformat()
            UserManagementService._put_user(
                es,
                username,
                user_data,
                enabled=True,
                metadata_updates={
                    "suspended_at": False,
                    "reinstated_at": now,
                },
            )
        except Exception as e:
            logging.error("UserManagementService: failed to reinstate '%s': %s", username, e)
            return {"success": False, "error": "Could not reinstate the user."}

        UserManagementService._flush_account_state_cache(username)
        return {"success": True}

    @staticmethod
    def update_user_roles(username: str, roles: list) -> dict:
        """Update a user's ES Security roles, preserving all other fields.

        ``roles`` is the system-role list only — ``org_*``/``team_*`` membership
        strings have been retired from ES Security as of v3 follow-up;
        scope membership lives in ``de_authz_user_team_role`` /
        ``de_authz_user_org_role``. The route writes the legacy roles array
        here and the authz indices separately so the two stores never carry
        duplicate state.

        a role change used to reach a user's LIVE session only at
        their next login — ``verify_auth`` wrote ``session["roles"]`` once
        at login and never revisited it. On a successful write this now (a)
        flushes this process's account-state cache entry for ``username``
        (``UserManagementService._flush_account_state_cache``, the same
         flush ``suspend_user``/``reinstate_user`` already
        call) so the next request THIS worker serves re-checks ES instead of
        riding a stale cached role list to the 30 s TTL, and ``verify_auth``
        / the Kibana gate route (``auth.refresh_session_roles``) pick the
        new roles up from there; and (b) when ``superuser`` was just dropped,
        revokes the target's Kibana gate API key
        (``_revoke_kibana_gate_key(..., trigger="roles_changed")``) the same
        way ``suspend_user`` does for ``trigger="user_disabled"`` — Kibana
        never sees the DEAM session, so a demoted superuser's existing key
        would otherwise keep working to its ``KIBANA_GATE_KEY_TTL`` (8h). Not
        revoked when superuser is added or the role set is otherwise
        unchanged. Both calls are best-effort and never raise — the ES write
        has already succeeded, and a failure here only widens the window to
        the pre-existing TTL bound (see ``_flush_account_state_cache`` and
        ``_revoke_kibana_gate_key`` for their own fail-soft contracts).
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserManagementService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        old_roles = user_data.get("roles", [])

        try:
            UserManagementService._put_user(es, username, user_data, roles=roles)
            UserManagementService._flush_account_state_cache(username)
            if "superuser" in old_roles and "superuser" not in roles:
                UserManagementService._revoke_kibana_gate_key(username, trigger="roles_changed")
            return {"success": True, "old_roles": old_roles, "new_roles": roles}
        except Exception as e:
            logging.error("UserManagementService: failed to update roles for '%s': %s", username, e)
            return {"success": False, "error": "Could not update roles."}

    @staticmethod
    def get_user_detail(username: str) -> dict:
        """Get full user detail including MFA status and recent login history."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserManagementService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        # MFA record
        mfa_record = {}
        try:
            result = es.search(
                index="de_asset_user_mfa",
                body={"query": {"term": {"username": username}}},
                size=1,
            )
            hits = result.get("hits", {}).get("hits", [])
            if hits:
                mfa_record = hits[0].get("_source", {})
        except Exception:
            pass

        # An admin can pre-enable MFA before the user has logged in
        # to pick a method. In that state the record stores mfa_method=None and
        # neither *_confirmed flag is set. Coerce method to "" so the sidebar
        # never crashes on null.toUpperCase, and expose
        # setup_complete so the UI can render a "Pending" badge.
        method = mfa_record.get("mfa_method") or ""
        if method == "totp":
            setup_complete = bool(mfa_record.get("totp_confirmed"))
        elif method == "yubikey":
            setup_complete = bool(mfa_record.get("yubikey_confirmed"))
        else:
            setup_complete = False

        # Entra-linked account — factor enforced upstream; derived
        # from metadata, never stored (mirrors MFAService.is_mfa_required
        # rule 2 and list_all_users_with_mfa).
        if (user_data.get("metadata") or {}).get("microsoft_sub"):
            method = "entra_sso"
            setup_complete = True

        # Recent audit events for this user (last 10 — the sidebar panel
        # only renders the most recent few, so a wider fetch is wasted work).
        login_history = []
        try:
            result = es.search(
                index="de_audit_log",
                body={
                    "query": {"term": {"username": username}},
                    "sort": [{"@timestamp": {"order": "desc"}}],
                    "size": 10,
                },
            )
            for hit in result.get("hits", {}).get("hits", []):
                login_history.append(hit.get("_source", {}))
        except Exception:
            pass

        return {
            "success": True,
            "username": username,
            "full_name": user_data.get("full_name", ""),
            "email": user_data.get("email", ""),
            "roles": user_data.get("roles", []),
            "enabled": user_data.get("enabled", True),
            "metadata": user_data.get("metadata", {}),
            "mfa": {
                "enabled": bool(mfa_record.get("mfa_enabled", False)),
                "method": method,
                "last_used": mfa_record.get("last_used") or "",
                "setup_complete": setup_complete,
                "bypass_mfa": bool(mfa_record.get("bypass_mfa", False)),
            },
            "login_history": login_history,
        }

    @staticmethod
    def bulk_action(action: str, usernames: list, admin_username: str = "") -> dict:
        """Execute a bulk action (suspend, reinstate, delete) on multiple users.

        Returns dict with success_count, fail_count, and errors list.
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            return {
                "success_count": 0,
                "fail_count": len(usernames),
                "errors": [{"username": u, "error": "Elasticsearch unavailable"} for u in usernames],
            }

        # Filter out protected usernames
        protected = {"elastic"}
        if admin_username:
            protected.add(admin_username)

        success_count = 0
        fail_count = 0
        errors = []

        for uname in usernames:
            if uname in protected:
                fail_count += 1
                errors.append({"username": uname, "error": "Protected user — cannot perform this action"})
                continue

            try:
                if action == "suspend":
                    result = UserManagementService.suspend_user(uname)
                elif action == "reinstate":
                    result = UserManagementService.reinstate_user(uname)
                elif action == "delete":
                    # Prevent deletion of superuser accounts
                    target = dict(es.security.get_user(username=uname)).get(uname, {})
                    if "superuser" in (target.get("roles") or []):
                        result = {"success": False, "error": "Superuser accounts cannot be deleted from the UI"}
                    else:
                        # 2026-06-05 — cancel any Stripe subscription
                        # BEFORE the delete (needs the user's stamped
                        # stripe_customer_id). Best-effort; never blocks delete.
                        try:
                            from asset_management.web.services.subscription_state_service import (
                                SubscriptionStateService,
                            )

                            _c = SubscriptionStateService.cancel_stripe_for_user(uname)
                            if _c.get("mode") == "error":
                                logging.warning(
                                    "bulk delete: ORPHANED Stripe sub for %r " "(customer=%s) — cancel manually: %s",
                                    uname,
                                    _c.get("customer_id"),
                                    _c.get("detail"),
                                )
                        except Exception as _e:  # noqa: BLE001 — never block delete
                            logging.warning("bulk cancel-on-delete failed for %r: %s", uname, _e)
                        get_es_admin_client(es).security.delete_user(username=uname)
                        # cascade-purge the 7 username-keyed authz
                        # indices so a future re-creation of the same
                        # username doesn't inherit the deleted user's grants.
                        # Best-effort — failures log but don't abort.
                        from asset_management.web.services.permissions_index_service import (
                            cascade_purge_authz_for_user,
                        )

                        cascade_counts = cascade_purge_authz_for_user(es, uname)
                        # same flush as the single-user delete
                        # route; without it a bulk-deleted user rode a
                        # cached "active" until the TTL expired.
                        UserManagementService._flush_account_state_cache(uname)
                        result = {"success": True, "authz_cascade": cascade_counts}
                else:
                    result = {"success": False, "error": f"Unknown action '{action}'"}

                if result.get("success"):
                    success_count += 1
                else:
                    fail_count += 1
                    errors.append({"username": uname, "error": result.get("error", "Unknown error")})
            except Exception:  # noqa: BLE001 — one user failing must not abort the batch
                logging.exception("UserManagementService.bulk_action: %s failed for '%s'", action, uname)
                fail_count += 1
                errors.append({"username": uname, "error": "Action failed for this user."})

        return {"success_count": success_count, "fail_count": fail_count, "errors": errors}
