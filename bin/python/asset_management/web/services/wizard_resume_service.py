"""Wizard Resume Service.

Resume-on-login surface for the onboarding wizard. Closes the "user closed
the tab mid-flow" gap: an active ``wizard_session`` row is the source of
truth, and this service turns that row into the two affordances the ticket
asks for:

* **Auto-redirect on login** — the form-login POST handler calls
  :meth:`get_resume_state` after authentication succeeds and routes the user
  straight to ``/onboarding/{current_step}`` when an active session exists.
  Skips ``step-6`` (the review screen completes the wizard; if a user got
  there and didn't submit, the review screen itself is the resume point).
* **"Continue setup" banner on /dashboard** — the dashboard handler threads
  the same state into ``dashboards.html`` so a user who navigates away
  from the auto-redirect (or who lands on /dashboard via MFA / FPC flow,
  where the auto-redirect doesn't fire) gets a one-click resume.

The stale-session contract (AC: "If session is older than 30 days: offer to
restart") lives here too: :attr:`STALE_AGE_DAYS` is the threshold, and the
banner copy / button set flips when :attr:`age_days` crosses it. The actual
restart is a one-shot POST to ``/onboarding/resume/restart`` which calls
:meth:`restart` to flip the doc to ``abandoned`` so the next render starts
fresh.

Idempotency: the underlying ``provision_*`` helpers in OnboardingService
all key on deterministic doc IDs (workspace slug, org title, team title +
org esid) and 409-swallow on race, so a duplicate POST on resume can't
create a second workspace / org / team. This service only owns the
*routing* decision; it never touches asset docs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from web.services.wizard_session_service import (
        WizardSessionService,
        STATUS_ACTIVE,
    )
except ImportError:
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
        STATUS_ACTIVE,
    )


WIZARD_TYPE = "onboarding"

# AC: "If session is older than 30 days: offer to restart". Anchored to
# ``created_at`` (the start of the wizard), not ``updated_at`` — a user who
# touched step-2 yesterday but started 45 days ago still gets the restart
# offer because the freshness of the state itself is what's in doubt.
STALE_AGE_DAYS = 30

# Resume target for each persisted ``current_step`` value. The wizard
# saves ``current_step`` AFTER finishing the prior step, so the saved value
# IS the next screen to land on. ``step-6`` is intentionally absent: a user
# who reached the review screen but didn't submit gets a banner that routes
# them BACK to step-6 (the review screen owns its own submit / completion),
# but they never get auto-redirected on login away from /dashboard for the
# same reason ``get_active`` already returns None when the session is
# completed.
_STEP_ROUTE_NAMES = {
    # Phase 1 (2026-05-20) — verify-email as logical step 2.
    "verify-email": "analytics.onboarding_verify_email_step_page",
    # Phase 2 (2026-05-20) — mfa-setup as logical step 3.
    # Resume lands a user mid-MFA-enrollment back on this step.
    "mfa-setup": "analytics.onboarding_mfa_setup_step_page",
    "step-4": "analytics.onboarding_step4_page",
    "step-5": "analytics.onboarding_step5_page",
    # optional "Your plan" screen after Workspace.
    "plan": "analytics.onboarding_plan_page",
    "step-6": "analytics.onboarding_step6_page",
    "step-7": "analytics.onboarding_step7_page",
    "step-8": "analytics.onboarding_step8_page",
    # invited variant detour. A user who started the picker
    # but didn't pick a team yet resumes there, not on step-2.
    "invite-team-picker": "analytics.onboarding_invite_team_picker_page",
    # follow-up (2026-05-21) — partial back-compat shim. The
    # URL paths and the internal wizard_session.current_step slug
    # strings were renumbered together from step-2..step-6 to
    # step-4..step-8 so the URL bar matches the UI eyebrow.
    #
    # Pure pre-rename legacy slugs that no longer collide ("step-2"
    # and "step-3") map cleanly to their post-rename destinations:
    "step-2": "analytics.onboarding_step4_page",  # legacy Account type
    "step-3": "analytics.onboarding_step5_page",  # legacy Workspace
    #
    # The slugs "step-4" / "step-5" / "step-6" CANNOT be shimmed —
    # they're already keys above with their new (post-rename) meaning.
    # A wizard_session doc written PRE-rename with current_step in
    # {"step-4", "step-5", "step-6"} will resume at the NEW endpoint
    # bearing that slug, not the original one:
    #   legacy step-4 (Connect a source) → new step-4 page (Account type)
    #   legacy step-5 (Review perms)     → new step-5 page (Workspace)
    #   legacy step-6 (Pick accounts)    → new step-6 page (Connect a source)
    # This is a regression for any user mid-wizard at deploy time —
    # they re-land one or two screens back. Blast radius on staging
    # is ~0; on prod the alternative would be a one-shot bulk update
    # of de_wizard_session docs (a future ticket can add that script
    # if the rename ships to prod with active sessions in flight).
    #
    # Remove the "step-2"/"step-3" shim entries after 2026-06-20 — by
    # then every active session will have either completed (get_active
    # returns None) or aged past STALE_AGE_DAYS (banner offers restart,
    # not resume).
}

# Auto-redirect on login skips step-6 — see the docstring above. The banner
# still surfaces a Continue button for step-6 so a user who left the review
# screen open can get back to it in one click from the dashboard.
_AUTO_REDIRECT_SKIP_STEPS = {"step-8"}


@dataclass(frozen=True)
class WizardResumeState:
    """Snapshot of the active onboarding wizard for one user.

    Returned by :meth:`WizardResumeService.get_resume_state`. The dashboard
    handler renders the banner iff this is non-None; the login handler
    auto-redirects iff this is non-None AND :attr:`auto_redirect_url` is
    truthy. The two are decoupled (a step-6 session has a banner URL but
    no auto-redirect URL) so the policy can flex without touching the
    underlying service.
    """

    current_step: str
    started_at: datetime
    age_days: int
    resume_url: str
    auto_redirect_url: str | None

    @property
    def is_stale(self) -> bool:
        """True iff the session started more than ``STALE_AGE_DAYS`` ago.

        The banner consumer uses this to flip copy + add a Restart button;
        the auto-redirect path is unaffected (a 35-day-old session still
        gets you back to your last step on login — the user can hit the
        banner's Restart button from /dashboard if they want a clean
        slate).
        """
        return self.age_days >= STALE_AGE_DAYS


class WizardResumeService:
    """Single source of truth for "where should this user resume?".

    The login POST handler and the dashboard render both read through this
    service so the resume target lives in one place. Adding a new wizard
    step is therefore a two-line change here (one entry in
    ``_STEP_ROUTE_NAMES``) instead of a hunt through every consumer.
    """

    # ─────────────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_resume_state(username: str) -> WizardResumeState | None:
        """Return the active onboarding wizard state, or None.

        Returns None when:

        * No username is on the session (anonymous / pre-auth read).
        * No wizard_session doc exists for this user.
        * The doc exists but its status is not ``active`` (a completed,
          skipped, or abandoned session is not a resume candidate;
           owns the skipped-session checklist surface).
        * The stored ``current_step`` is unrecognised (defensive: a future
          step name added to ``WizardSessionService.save`` calls but not
          registered here returns None rather than 500-ing the dashboard).
        """
        if not username:
            return None
        # WizardSessionService.get_active already filters on status="active",
        # so a completed / skipped / abandoned doc surfaces as None here.
        active = WizardSessionService.get_active(username, WIZARD_TYPE)
        if not active:
            return None
        current_step = (active.get("current_step") or "").strip()
        route_name = _STEP_ROUTE_NAMES.get(current_step)
        if not route_name:
            return None
        started_at = _parse_iso(active.get("created_at"))
        if started_at is None:
            return None
        age_days = max(0, (datetime.now(timezone.utc) - started_at).days)

        # url_for must run inside a request / app context, so the route
        # name is the load-bearing handle here; the consumer (route
        # handler) resolves it to a URL. We pre-resolve in the request
        # path below so the dataclass surfaces a string for the template.
        try:
            from flask import url_for

            resume_url = url_for(route_name)
        except Exception:
            # Outside an app context (e.g. a unit test calling this
            # service directly) — surface the route name so the caller
            # can resolve it. Tests that exercise the dashboard / login
            # path do so via the Flask test client and hit the live url_for.
            resume_url = f"/onboarding/{current_step}"

        auto_redirect_url = resume_url if current_step not in _AUTO_REDIRECT_SKIP_STEPS else None

        return WizardResumeState(
            current_step=current_step,
            started_at=started_at,
            age_days=age_days,
            resume_url=resume_url,
            auto_redirect_url=auto_redirect_url,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def restart(username: str) -> bool:
        """Abandon the user's active onboarding wizard so the next start
        is a clean slate.

        Idempotent: a missing-or-already-terminal doc returns False without
        raising. The route handler unconditionally redirects to
        ``/onboarding/start`` after calling this; the user lands on a
        fresh wizard regardless of whether the abandon actually flipped a
        doc (the worst case is a stranded active row whose next-login
        resume reads it — also acceptable, because /onboarding/start is
        the next stop anyway and will write a new row over the top via
        the usual step-2 ``save`` path).

        The doc is flipped to ``abandoned`` (not deleted) so the
        observability story stays intact — "where do users
        drop off?" query reads abandoned + completed terminal docs.
        """
        if not username:
            return False
        return WizardSessionService.abandon(username, WIZARD_TYPE)


def _parse_iso(value: object) -> datetime | None:
    """Parse an ES-stored ISO 8601 timestamp into a UTC-aware datetime.

    ``WizardSessionService._now_iso`` writes UTC-aware ISO strings, but
    ES sometimes round-trips them with ``Z`` instead of ``+00:00``; both
    shapes are handled. Returns None on any parse failure so the caller
    can short-circuit to "no resume state".
    """
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logging.warning("WizardResumeService: unparseable created_at %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
