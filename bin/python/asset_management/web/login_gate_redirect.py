"""Shared post-authentication login gate step.

The login gate decides whether a freshly-authenticated user may enter
the app or must first verify their email / finish the onboarding wizard. It was
wired into exactly one place: the tail of the form-login POST, *after* the
branch that redirects an MFA-enrolled user to their challenge. Every other way
of reaching an authenticated session skipped it:

  * ``_complete_mfa_login`` (app.py) — promotes an ``mfa_pending`` session to
    authenticated and redirects, never consulting the gate. Because the wizard
    makes MFA mandatory at logical step 3, **every wizard-created user takes
    this path**, so the gate never ran for exactly the population it was built
    for.
  * the Google / Microsoft SSO callbacks, on their existing-user sign-in branch
    ("email match → /dashboard"), which is a login entry point in its own right.

Three things were therefore missed on those paths: the ``login_gate_evaluated``
audit event, the ``VERIFY_EMAIL`` / ``WIZARD_INCOMPLETE`` redirects, and — the
consequential one — ``session["wizard_pending"]``. That flag is what
``verify_auth`` uses to confine a user to the wizard whitelist on *every*
subsequent request, and it is set only where the gate runs. An MFA user never
got it, so the confinement never engaged for them.

This module is the single post-auth step all of those call. It lives outside
``blueprints/`` and ``app.py`` deliberately: both import from each other lazily
at function scope, and a helper either of them can import at module scope must
belong to neither. Same reasoning as :mod:`asset_management.web.public_url`.

Not wired here, on purpose: the already-authenticated branch of ``GET /login``
 and the verify-email landing both re-evaluate the gate for a session
that already exists, rather than for a fresh authentication. Routing their audit
through ``login_gate_evaluated`` would turn a per-login event into a per-visit
one. They keep their own inline evaluation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from flask import redirect, session, url_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from werkzeug.wrappers import Response


_log = logging.getLogger(__name__)


def gate_redirect_after_auth(
    username: str,
    roles: list[str] | None,
    *,
    auth_method: str = "",
    ip_address: str | None = None,
    user_agent: str = "",
) -> Response | None:
    """Run the login gate for a just-authenticated user.

    Returns a redirect to the surface the user must visit first, or ``None``
    when the gate is clear and the caller should continue its normal
    post-login redirect.

    The caller must have established the authenticated session already — this
    sets ``session["wizard_pending"]`` on the blocking outcomes and clears it on
    the clear one, so a stale flag from a previous wizard-incomplete session in
    the same browser does not survive.

    Fail-open by construction: ``LoginGateService.evaluate_for_login`` already
    swallows ES failures and returns ``NONE``, and the resume-URL lookup is
    wrapped. A gate that cannot reach Elasticsearch must never lock a user out
    of the product.
    """
    from asset_management.web.services.audit_log_service import AuditLogService
    from asset_management.web.services.login_gate_service import (
        LoginGate,
        LoginGateService,
    )

    decision = LoginGateService.evaluate_for_login(username, roles or [])

    AuditLogService.log_event(
        username,
        "login_gate_evaluated",
        True,
        auth_method=auth_method,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=f"gate={decision.gate.value} reason={decision.detail}",
    )

    if decision.gate == LoginGate.VERIFY_EMAIL:
        # confine the user to the wizard whitelist on every
        # subsequent request, not just this one.
        session["wizard_pending"] = True
        return redirect(url_for("analytics.onboarding_verify_required_page"))

    if decision.gate == LoginGate.WIZARD_INCOMPLETE:
        session["wizard_pending"] = True
        return redirect(_wizard_resume_url(username))

    # Gate clear — drop any flag left over from a prior wizard-incomplete
    # session in this browser.
    session.pop("wizard_pending", None)
    return None


def _wizard_resume_url(username: str) -> str:
    """Where a wizard-incomplete user resumes.

    Uses the same ``WizardResumeService`` URL the banner and
    auto-redirect use, so the gate redirect matches the existing resume
    landing. Falls back to step 4 when no resume state exists yet (no
    ``wizard_session`` row — first arrival) or the lookup fails.
    """
    try:
        from asset_management.web.services.wizard_resume_service import WizardResumeService

        resume_state = WizardResumeService.get_resume_state(username)
        if resume_state is not None and resume_state.resume_url:
            return resume_state.resume_url
    except Exception as e:  # noqa: BLE001 — fail-soft, never block login
        _log.warning("login gate wizard-resume lookup failed for %r: %s", username, e)
    return url_for("analytics.onboarding_step4_page")
