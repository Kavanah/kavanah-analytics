"""Login Gate Service.

a teammate asked for a hard block at login: a new user must verify their
email AND complete the 6-step wizard before they can reach /dashboard.
This service is the single source of truth for that policy.

Three gate states:

* ``LoginGate.NONE``            — allow login to proceed to /dashboard.
* ``LoginGate.VERIFY_EMAIL``    — user has not clicked the verify link
                                  in their inbox. Redirect to a holding
                                  page (``/onboarding/verify-required``)
                                  that surfaces a re-send button and
                                  refresh affordance.
* ``LoginGate.WIZARD_INCOMPLETE`` — user has an in-flight wizard session
                                  that is not ``status="completed"``.
                                  Redirect to their resume step
                                  (``WizardResumeService.get_resume_state``).

Evaluation order (important for the admin-bypass + grandfathered-user
posture):

1. Empty username → NONE (defensive; should never happen post-auth).
2. Superuser / asset_admin / configured admin role → NONE.
3. ES Security ``get_user`` fails / ES outage → NONE (fail OPEN — a
   transient blip MUST NOT lock everyone out of the product). This is
   a deliberate inversion of ``OnboardingEmailVerificationService.
   is_email_verified``'s fail-closed posture, which is correct for the
   step-7 surface but wrong for login. The login surface is the
   recovery path for everything else; failing closed there means
   nobody can come and fix the underlying problem.
4. ``metadata.onboarding_required`` is missing or falsy → NONE
   (grandfathered: existing users that pre-date this gate land here
   automatically because the signup service that stamps the flag is
   a no-op for them).
5. ``metadata.email_verified`` is not True → VERIFY_EMAIL.
6. Wizard session lookup fails or session status != "completed" →
   WIZARD_INCOMPLETE.
7. Otherwise → return NONE / ``detail="passed_both_gates"``.

 (2026-09-25): step 7 above is a pure read — ``evaluate`` never
writes. The lazy clear of ``metadata.onboarding_required`` (so future logins
skip this work) now lives in :meth:`LoginGateService.evaluate_for_login`,
which calls ``evaluate()`` and, only on the ``passed_both_gates`` outcome,
performs the write. ``evaluate()`` reads as a query and is safe to call as a
diagnostic against any user without side effects; login callers MUST use
``evaluate_for_login`` or the lazy clear never happens.

The lazy-clear write is best-effort — a failure here does NOT block the
user from logging in; they just take the slower gate path on the next
login. The audit log records both the gate evaluation result and the
lazy-clear attempt.

The service is pure-function (no global state); the route handler is
the consumer. Test surface mocks out the two ES dependencies
(``security.get_user`` + ``WizardSessionService.get_active``) to
exercise every branch without an ES cluster.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum


_log = logging.getLogger(__name__)


# Role names that bypass the gate entirely. Mirrors the
# ``session["is_admin"]`` computation in ``login_post``: superuser is
# the cluster-level escape hatch, and ``config.REQUIRED_ROLE``
# (typically ``asset_admin``) is the platform-level admin. Hardcoded
# here because the gate runs BEFORE the Flask session is populated.
_BYPASS_ROLES = frozenset({"superuser", "asset_admin"})


class LoginGate(Enum):
    """Tri-state result of :meth:`LoginGateService.evaluate`.

    The route handler dispatches on this value: ``NONE`` proceeds with
    the existing login flow, the other two redirect to dedicated
    landing surfaces.
    """

    NONE = "none"
    VERIFY_EMAIL = "verify_email"
    WIZARD_INCOMPLETE = "wizard_incomplete"


@dataclass(frozen=True)
class GateDecision:
    """Result envelope. ``gate`` is the policy outcome; ``detail`` is
    an audit-log hint surfaced into the login audit event.
    """

    gate: LoginGate
    detail: str = ""


class LoginGateService:
    """Stateless evaluator. Public API: :meth:`evaluate` (pure read, never
    writes — safe to call as a diagnostic) and :meth:`evaluate_for_login`
    (the one that writes: performs the lazy clear on the
    ``passed_both_gates`` outcome; this is what the login path must call).
    """

    @staticmethod
    def evaluate(username: str, roles: list[str] | None = None) -> GateDecision:
        """Compute the gate decision for ``username``. Pure read — never
        writes to ES, even on the ``passed_both_gates`` outcome.

        Caller is the login POST handler — the username + roles come
        from the ES Security authentication result. See module docstring
        for the evaluation order. a login caller must use
        :meth:`evaluate_for_login` instead, which performs the lazy clear
        this method used to do inline.
        """
        if not username:
            return GateDecision(gate=LoginGate.NONE, detail="no_username")

        if roles:
            for r in roles:
                if r in _BYPASS_ROLES:
                    return GateDecision(gate=LoginGate.NONE, detail=f"admin_bypass:{r}")

        # Lazy ES import so a unit test importing this module doesn't
        # incur the lib.es_helpers import cost. The login route is
        # already in the ES dependency graph.
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            _log.warning("LoginGateService: ES unreachable for %r — fail open", username)
            return GateDecision(gate=LoginGate.NONE, detail="es_unreachable_fail_open")

        # Single get_user call surfaces both metadata.onboarding_required
        # and metadata.email_verified, so the gate evaluation is one
        # ES round-trip (plus the wizard_session lookup below when we
        # get that far).
        try:
            raw = dict(es.security.get_user(username=username))
        except Exception as e:  # noqa: BLE001 — top-level boundary
            _log.warning("LoginGateService.get_user(%r) failed: %s — fail open", username, e)
            return GateDecision(gate=LoginGate.NONE, detail="get_user_failed_fail_open")

        current = raw.get(username)
        if not current:
            # User exists in ES Security (auth just succeeded) but
            # get_user returned nothing — most likely an ES Security
            # config quirk. Fail open: don't lock the user out.
            _log.warning("LoginGateService: get_user('%s') returned no record — fail open", username)
            return GateDecision(gate=LoginGate.NONE, detail="get_user_empty_fail_open")

        metadata = dict(current.get("metadata") or {})

        # Grandfathered: any user without the flag predates this gate
        # (or is a superuser-provisioned account that bypassed the
        # wizard signup service). Let them in.
        if not metadata.get("onboarding_required"):
            return GateDecision(gate=LoginGate.NONE, detail="not_onboarding_required")

        # Gate 1: email verification.
        if not metadata.get("email_verified"):
            return GateDecision(gate=LoginGate.VERIFY_EMAIL, detail="email_not_verified")

        # Gate 2: wizard completion. Anything other than status="completed"
        # blocks. WizardSessionService.get_active filters on status="active"
        # so we read the raw doc from ES directly. Falls back open on
        # lookup failure (don't trap a user who completed the wizard but
        # hit ES weather on login).
        from asset_management.web.services.wizard_session_service import (
            INDEX as _WS_INDEX,
            STATUS_COMPLETED,
        )

        try:
            ws_raw = es.get(index=_WS_INDEX, id=f"{username}:onboarding")
            ws_status = (ws_raw.get("_source") or {}).get("status")
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # Could be NotFoundError (user has no session row at all) OR
            # a real ES outage. Treat differently: NotFoundError means
            # the user has signed up but never opened step-2; that IS
            # the wizard-incomplete branch. Other errors fail open.
            err_name = type(e).__name__
            if "NotFound" in err_name:
                return GateDecision(gate=LoginGate.WIZARD_INCOMPLETE, detail="no_wizard_session")
            _log.warning("LoginGateService.wizard_session(%r) failed: %s — fail open", username, e)
            return GateDecision(gate=LoginGate.NONE, detail="wizard_session_failed_fail_open")

        if ws_status != STATUS_COMPLETED:
            return GateDecision(gate=LoginGate.WIZARD_INCOMPLETE, detail=f"wizard_status={ws_status}")

        # Both gates passed. the lazy clear used to happen here
        # inline, which made this "pure query" method a silent writer. It
        # now lives in evaluate_for_login — see the module docstring.
        return GateDecision(gate=LoginGate.NONE, detail="passed_both_gates")

    @staticmethod
    def _lazy_clear_onboarding_required(username: str) -> bool:
        """Best-effort write: clear ``metadata.onboarding_required`` so future
        logins skip the gate work.

        Re-reads the user (the decision passed to :meth:`evaluate_for_login`
        does not carry the raw ES record) and issues the same ``put_user``
        body shape the old inline lazy-clear used. This only runs once per
        user lifetime: after the write succeeds, ``onboarding_required`` is
        False and :meth:`evaluate` short-circuits at the grandfathered check
        on every later login, so the extra ``get_user`` round trip here is
        negligible over the account's lifetime.

        Returns True on a successful write, False otherwise (ES unreachable,
        user record missing, or the write itself failed) — never raises. A
        failure here does NOT block the user from logging in; they just pay
        the gate cost again on the next login.
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        try:
            es = get_elasticsearch_client()
            if es is None:
                return False

            raw = dict(es.security.get_user(username=username))
            current = raw.get(username)
            if not current:
                return False

            metadata = dict(current.get("metadata") or {})
            metadata["onboarding_required"] = False
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "roles": current.get("roles", []),
                    "full_name": current.get("full_name", ""),
                    "email": current.get("email", ""),
                    # ES writes enabled=true when it is omitted; a suspended
                    # user signing in through SSO must stay suspended.
                    "enabled": current.get("enabled", True),
                    "metadata": metadata,
                },
            )
            _log.info("AUDIT: action=login_gate_lazy_clear username=%s", username)
            return True
        except Exception as e:  # noqa: BLE001 — top-level boundary; best-effort
            _log.warning("LoginGateService.lazy_clear(%r) failed: %s", username, e)
            return False

    @staticmethod
    def evaluate_for_login(username: str, roles: list[str] | None = None) -> GateDecision:
        """Compute the gate decision AND perform the lazy clear.

        This is the one that writes — it is what every login call site must
        use. Anything diagnostic (an operator checking a user's gate state)
        must call :meth:`evaluate` instead, which never mutates the user.

        Calls ``LoginGateService.evaluate`` through the class attribute (not
        a private helper) so tests that patch
        ``LoginGateService.evaluate`` keep intercepting this path too.
        """
        decision = LoginGateService.evaluate(username, roles)
        if decision.gate is LoginGate.NONE and decision.detail == "passed_both_gates":
            LoginGateService._lazy_clear_onboarding_required(username)
        return decision
