"""Wizard Progress Service.

Visible "Step N of M · ~X min left" progress indicator surfaced on every
onboarding wizard screen. The route handlers call :meth:`get_progress`
and pass the returned :class:`WizardProgress` into ``render_template``;
the ``_onboarding_progress.html`` partial reads it.

Three things the indicator carries (matching the acceptance
criteria):

* **Step number + total** — "Step 3 of 6", computed from a fixed step
  order. ``invite-team-picker`` is the invited-user variant of step-2
   and shares step-2's slot in the numbering.
* **Time-left estimate** — sum of static medians for the remaining
  steps, scaled by an elapsed-time pace multiplier derived from
  ``wizard_session.created_at``. The medians are calibrated from the
  user-research timings the ticket cites; the multiplier kicks in once
  the user has spent at least the expected time on prior steps
  (otherwise the signal is noise — autofill, replay) and is capped at
  3.0× so a multi-hour pause doesn't blow the estimate past the real
  horizon. The estimate updates per step (smaller base on each
  successive screen) AND per user (multiplier reflects their actual
  pace). Once the analytics events accumulate enough
  population data to derive real medians, a follow-up ticket can swap
  the constant ``_MEDIAN_MINS_BY_STEP`` table for a query-backed
  source without changing the public service shape.
* **Progress dots** — one entry per step, each ``"done"``,
  ``"current"``, or ``"pending"``. The template renders these as small
  colored circles per the design prototype.

The service is intentionally pure (no ES reads, no I/O) so it's trivially
unit-testable and adds zero render latency. The route handlers already
know which step they're rendering, so all the service needs is the step
slug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Canonical step order — matches WizardResumeService._STEP_ROUTE_NAMES
# but expressed as a sequence so step number / position derives from
# the index. ``invite-team-picker`` is NOT in this list: it's the
# invited-user alternative to step-4 (Account type) and renders with
# step-4's progress slot.
#
# Phase 1 (2026-05-20): ``verify-email`` inserted as the new
# logical step 2.
# Phase 2 (2026-05-20): ``mfa-setup`` inserted as the new
# logical step 3.
#
# follow-up (2026-05-21): URL paths renumbered from
# step-2..step-6 to step-4..step-8 so the URL and UI numbers match.
# The slug strings used as keys below are ALSO the URL slugs (the
# wizard_session.current_step value matches the URL path's final
# segment). The mapping table lives in
# ``blueprints/analytics/routes.py`` above the ``onboarding_step4_page``
# route. Adding a new wizard step is a four-file change: route decl +
# fn in routes.py, plus this _STEP_ORDER, plus
# ``WizardResumeService._STEP_ROUTE_NAMES``, plus the two allowlists
# in ``auth.py``.
_STEP_ORDER: tuple[str, ...] = (
    "verify-email",  # logical step 2 — confirm inbox link
    "mfa-setup",  # logical step 3 (new) — TOTP or yubikey
    "step-4",  # logical step 4 — Account type
    "step-5",  # logical step 5 — Workspace
    "step-6",  # logical step 6 — Connect a source
    "step-7",  # logical step 7 — Review permissions
    "step-8",  # logical step 8 — Pick your accounts
)
TOTAL_STEPS = len(_STEP_ORDER) + 1  # +1 for the implicit step-1 (sign-up)

# Static median minutes per step. Calibrated from the user-research
# numbers behind the ticket scope ("~3 min left" hardcoded on every
# template before this service existed); refined per step so the total
# starts at ~10 min (matches the marketing copy "Takes ~10 min") and
# decrements per screen. A follow-up ticket will swap this for a query
# against the wizard.step.completed events once enough data
# has accumulated to derive real medians.
#
# Phase 1: ``verify-email`` is a near-zero-effort step
# (click the link in your inbox) so 1 minute is generous; the time
# is dominated by waiting for SES, not user effort.
# Phase 2: ``mfa-setup`` is 2 minutes — install an
# authenticator app or insert a yubikey + confirm one code.
_MEDIAN_MINS_BY_STEP: dict[str, int] = {
    "verify-email": 1,  # Phase 1 — click inbox link
    "mfa-setup": 2,  # Phase 2 — install authenticator app + 1 code
    "step-4": 1,  # Account type — one click
    "step-5": 2,  # Workspace name / org / team
    "step-6": 3,  # Pick a connector (slowest — browse + decision)
    "step-7": 2,  # OAuth consent
    "step-8": 2,  # Pick ad accounts to import
}

# User-facing labels for the progress rail. These are the wizard-step
# captions that appear next to "Step N of 8"; the wizard h1 inside the
# card is owned by the template and stays distinct.
_STEP_LABELS: dict[str, str] = {
    "verify-email": "Verify your email",  # Phase 1
    "mfa-setup": "Set up two-factor",  # Phase 2
    "step-4": "Account type",
    "step-5": "Workspace",
    "step-6": "Connect a source",
    "step-7": "Review permissions",
    "step-8": "Pick your accounts",
    # invited users land here instead of step-2 (logical
    # step 4 now). Same slot in the numbering, different caption.
    "invite-team-picker": "Pick a team",
}

# Steps that resolve to another slug's position in the numbering.
# Keeps the step-N-of-M math in one place so a variant is a one-line
# addition rather than a branch in every consumer.
_STEP_NUMBER_OVERRIDES: dict[str, str] = {
    "invite-team-picker": "step-4",
}

# Upper pace-multiplier cap. A user who's burned 3× the expected time
# probably hit a real problem (paused for support, lost network);
# capping prevents the visible "X min left" from racing to absurd
# values like "~47 min left" on step-4. There's no lower clamp because
# the evidence gate below (actual >= expected) means the multiplier
# can only be ≥ 1.0 — adapting downward would require fewer minutes
# elapsed than expected, which is also the signal we explicitly do
# NOT trust (it's noise, not a fast user).
_PACE_MULTIPLIER_MAX = 3.0

# Pace adapts only once the user has spent at least the expected
# minutes on the prior steps. Adapting earlier means "I have less
# evidence than a single median's worth of work" — autofill, replay,
# or test-traffic territory. By requiring elapsed >= expected_so_far,
# the multiplier always starts at 1.0 (no adaptation) and only kicks
# in once the user has demonstrably spent more time than expected.


@dataclass(frozen=True)
class WizardProgress:
    """Snapshot of where the user is in the wizard, for the progress rail.

    All values are pre-computed so the template renders without any
    Python in the Jinja expressions (the template stays a dumb consumer).
    Frozen so a route handler can't accidentally mutate it after passing
    it into ``render_template``.
    """

    current_step: str
    step_number: int  # 1-based; step-1 is the sign-up page (outside the wizard)
    total_steps: int
    step_label: str
    minutes_left: int
    # Per-step dot states. Each entry is one of "done", "current",
    # "pending". follow-up (2026-05-20): length now equals
    # total_steps (6), with the leading dot pinned to "done" — it
    # represents the sign-up step the user has already completed by
    # the time they see the progress rail. a teammate flagged the prior
    # 5-dot count read as inconsistent with the "Step N of 6" copy.
    dots: tuple[str, ...] = field(default_factory=tuple)


class WizardProgressService:
    """Pure-function progress computation. Stateless, ES-free."""

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_progress(
        current_step: str,
        wizard_session: dict | None = None,
    ) -> WizardProgress | None:
        """Compute the progress snapshot for ``current_step``.

        ``wizard_session`` is the active row from
        :meth:`WizardSessionService.get_active`. When present the elapsed
        time since ``created_at`` is used to derive a pace multiplier so
        a user who's running slower than the population sees the
        remaining-time estimate scale up (and vice-versa for a faster
        user). When absent or unparseable, the estimate falls back to
        the static medians.

        Returns None for unrecognised step slugs so a forged value can't
        500 the wizard render — the template guards with ``{% if
        progress %}`` and skips the rail in that case.
        """
        if not current_step:
            return None
        # Resolve invite-team-picker (and any future variant) to its
        # canonical position in the numbering.
        position_slug = _STEP_NUMBER_OVERRIDES.get(current_step, current_step)
        if position_slug not in _STEP_ORDER:
            return None

        index = _STEP_ORDER.index(position_slug)  # 0-based within _STEP_ORDER
        step_number = index + 2  # +2 because step-1 (sign-up) is implicit

        remaining_slugs = _STEP_ORDER[index:]
        # "X min left" includes the current step (the user is
        # actively spending that time right now). The number ticks down
        # as they advance.
        base_remaining_mins = sum(_MEDIAN_MINS_BY_STEP.get(s, 0) for s in remaining_slugs)
        pace_multiplier = _compute_pace_multiplier(position_slug, wizard_session)
        # Round up so we never under-promise; a "~3 min left" that
        # silently becomes 4 in practice is worse than "~4 min left"
        # that finishes in 3.
        minutes_left = max(1, _ceil_int(base_remaining_mins * pace_multiplier))

        # follow-up (2026-05-20): emit one dot per step in the
        # "Step N of 6" copy (so 6 dots, not 5). The leading dot is
        # always "done" — it represents the sign-up step the user
        # completed before reaching the wizard. The remaining 5 dots
        # map to steps 2-6 with done/current/pending as before.
        dots = ("done",) + tuple(
            "done" if i < index else "current" if i == index else "pending" for i in range(len(_STEP_ORDER))
        )

        return WizardProgress(
            current_step=current_step,
            step_number=step_number,
            total_steps=TOTAL_STEPS,
            step_label=_STEP_LABELS.get(current_step, ""),
            minutes_left=minutes_left,
            dots=dots,
        )


def _compute_pace_multiplier(
    position_slug: str,
    wizard_session: dict | None,
) -> float:
    """Return the pace multiplier in [_PACE_MULTIPLIER_MIN, _PACE_MULTIPLIER_MAX].

    The signal is elapsed-wall-clock-time divided by the population
    median for the steps the user has already finished. Step-2 (the
    first wizard screen, index 0) has no prior steps, so it always
    returns 1.0 — there's no pace history to read.

    Returns 1.0 (the no-adaptation default) on every short-circuit so
    the visible estimate matches the static-median behaviour when the
    session data is missing or unparseable.
    """
    if wizard_session is None:
        return 1.0

    index = _STEP_ORDER.index(position_slug)
    if index == 0:
        # First wizard screen — no prior-step pace history.
        return 1.0

    expected_minutes_so_far = sum(_MEDIAN_MINS_BY_STEP.get(s, 0) for s in _STEP_ORDER[:index])
    if expected_minutes_so_far <= 0:
        return 1.0

    started_at = _parse_iso(wizard_session.get("created_at"))
    if started_at is None:
        return 1.0

    elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    # Snap elapsed time to whole-minute resolution before computing the
    # multiplier. Median minutes are integer-valued, so a 60.001-second
    # elapsed time vs a 60-second expected resolves to 1.0× (not
    # 1.000016×), which kept the visible "X min left" from twitching
    # up by a minute on every render due to floating-point fuzz.
    actual_minutes_so_far = round(elapsed_seconds / 60.0)
    if actual_minutes_so_far < expected_minutes_so_far:
        # Not enough evidence to claim the user is slower or faster
        # than the population median: they haven't spent even the
        # expected-time on the prior steps yet. Most common cause is
        # back-button replay or autofill — both of which would
        # otherwise yield a misleadingly-low multiplier.
        return 1.0
    raw_multiplier = actual_minutes_so_far / expected_minutes_so_far
    return min(_PACE_MULTIPLIER_MAX, raw_multiplier)


def _parse_iso(value: object) -> datetime | None:
    """Parse an ES-stored ISO 8601 timestamp into a UTC-aware datetime.

    Mirrors :func:`WizardResumeService._parse_iso` so a wizard_session
    doc passed through either service yields the same start-time
    interpretation. ES sometimes round-trips UTC as ``Z`` instead of
    ``+00:00``; both shapes are handled.
    """
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logging.warning("WizardProgressService: unparseable created_at %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ceil_int(value: float) -> int:
    """Integer ceiling without importing :mod:`math`.

    ``int(value)`` truncates toward zero; ``int(value) + 1`` is wrong
    when value is already an integer. The branch covers both.
    """
    truncated = int(value)
    return truncated if value == truncated else truncated + 1
