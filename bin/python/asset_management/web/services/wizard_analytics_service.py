"""
Wizard Analytics Service

Single emitter for the four-action canonical onboarding-wizard funnel
events. Every step transition fires exactly one canonical event whose
``analytics_event`` substring follows the schema::

    wizard.{step}.{entered|completed|skipped|errored}

and whose ``detail`` carries the five context fields the marketing funnel
needs to reconstruct drop-off end-to-end without joining against other
indices at query time::

    user_id            ES Security username (== email).
    account_type       From wizard_session.state or UserProfileService.
                       Stamped on every event so a funnel slice doesn't
                       need to join against the user record.
    connector          wizard_session.state.selected_connector_id (the
                       picked tile id, e.g. ``meta_ads``, ``google_ads``).
                       Empty string on events that fire BEFORE step-4
                       (step-2 / step-3 don't know the connector yet) so
                       the field is always present at the schema level.
    cohort             ISO week of the wizard's ``created_at``
                       (``YYYY-Www``, e.g. ``2026-W20``). Stable per
                       wizard session — the cohort doesn't shift across
                       steps because it's anchored to start, not now.
    wizard_session_id  ``{user_id}:{wizard_type}`` from
                       WizardSessionService's doc id, so the same session
                       can be threaded end-to-end across multiple step
                       events.

The canonical events live ALONGSIDE the pre-existing rich step events
(``wizard.step8.org_shared_decision``, ``wizard.step7.oauth_started``,
etc.) — the canonical envelope is the funnel; the rich events stay for
drill-down. A release-plan test pins this co-existence so a
future cleanup doesn't accidentally remove either layer.

The emitter is intentionally a thin wrapper around
:meth:`AuditLogService.log_event` so events sink to the existing
``de_audit_log`` index and downstream dashboards can grep for the
``analytics_event=`` substring the same way they already do for the
other ``wizard.stepN.*`` events.

The ``log_event`` call is fire-and-forget — any failure (no ES, no
session, malformed state) is logged and swallowed so the wizard's user
journey is never blocked by the funnel emitter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from asset_management.web.services.audit_log_service import AuditLogService

# Four canonical actions per step. The set is closed: a new event type
# should NOT be added here — sub-step granularity belongs on the rich
# step-specific events (e.g. ``wizard.step7.oauth_started``).
ACTION_ENTERED: Final[str] = "entered"
ACTION_COMPLETED: Final[str] = "completed"
ACTION_SKIPPED: Final[str] = "skipped"
ACTION_ERRORED: Final[str] = "errored"

_VALID_ACTIONS: Final[frozenset[str]] = frozenset([ACTION_ENTERED, ACTION_COMPLETED, ACTION_SKIPPED, ACTION_ERRORED])


@dataclass(frozen=True)
class WizardEventContext:
    """The five context fields the canonical event carries.

    Constructed once per step handler and threaded into every emit call
    so the four canonical events for the same step share identical
    context. ``from_session`` is the typical builder — most callers
    pass it a freshly-loaded wizard_session row and never touch the
    fields directly.
    """

    user_id: str
    account_type: str
    connector: str
    cohort: str
    wizard_session_id: str


class WizardAnalyticsService:
    """Single canonical emitter for the onboarding wizard funnel."""

    WIZARD_TYPE: Final[str] = "onboarding"

    # ─────────────────────────────────────────────────────────────────
    # Context construction
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def build_context(
        *,
        username: str,
        wizard_session: dict | None,
        account_type: str = "",
        connector_override: str | None = None,
    ) -> WizardEventContext:
        """Build a context block from a wizard_session row + optional overrides.

        ``wizard_session`` is the raw doc from
        :meth:`WizardSessionService.get_active` (None when no active
        session exists — happens for step-2 GET on a fresh signup,
        before any step has saved state). ``account_type`` may be
        passed in when the caller has already resolved it
        (UserProfileService fallback); otherwise pulled from session
        state. ``connector_override`` lets callers stamp the connector
        even when the wizard_session state hasn't been re-read on the
        current request (e.g. step-4 POST happy path that's about to
        save the selection).

        ``connector_override=None`` (the default) means "fall back to
        wizard_session state". Passing the empty string ``""`` is an
        EXPLICIT override — the step-4 skip path uses it to stamp
        ``connector=`` (empty) even though the session might still
        carry a prior pick from a resumed wizard.
        """
        state: dict = {}
        created_at_iso = ""
        if isinstance(wizard_session, dict):
            raw_state = wizard_session.get("state")
            if isinstance(raw_state, dict):
                state = raw_state
            created_at_iso = str(wizard_session.get("created_at") or "")

        resolved_account_type = account_type or str(state.get("account_type") or "")
        if connector_override is None:
            resolved_connector = str(state.get("selected_connector_id") or "")
        else:
            resolved_connector = str(connector_override)

        return WizardEventContext(
            user_id=str(username or ""),
            account_type=resolved_account_type,
            connector=resolved_connector,
            cohort=_iso_week_from_iso(created_at_iso),
            wizard_session_id=_session_id(username),
        )

    @staticmethod
    def build_context_for_user(
        *,
        username: str,
        account_type: str = "",
    ) -> WizardEventContext:
        """Build context for a post-wizard surface (e.g. dashboard checklist).

        Reads the wizard_session doc by id REGARDLESS of status —
        ``WizardSessionService.get_active`` filters on ``status="active"``
        so it returns None for completed / skipped / abandoned sessions,
        but the checklist analytics on /dashboard fire AFTER the wizard
        has terminated. We want cohort + wizard_session_id stamped on
        those events too so the funnel can answer "users who onboarded in
        week-X — how often do they engage with the checklist?".

        Falls back to the empty-context shape (only user_id +
        wizard_session_id populated) when no doc exists. The fail-soft
        try/except blanket here mirrors :meth:`emit` — never let the
        emitter break the caller. AC3 follow-up.
        """
        wizard_session: dict | None = None
        if username:
            try:
                # Read the underlying doc bypassing the active-status filter.
                from lib.es_helpers import get_elasticsearch_client
                from asset_management.web.services.wizard_session_service import INDEX

                es = get_elasticsearch_client()
                if es is not None:
                    result = es.get(index=INDEX, id=_session_id(username))
                    # es.get returns an ObjectApiResponse (duck-type on .get, not
                    # isinstance dict); the old isinstance(dict) guard left
                    # wizard_session None for every real session.
                    wizard_session = result.get("_source") if hasattr(result, "get") else None
            except Exception:  # noqa: BLE001 — fail soft; cohort will be empty
                wizard_session = None
        return WizardAnalyticsService.build_context(
            username=username,
            wizard_session=wizard_session,
            account_type=account_type,
        )

    # ─────────────────────────────────────────────────────────────────
    # Emit
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def emit(
        *,
        step: str,
        action: str,
        context: WizardEventContext,
        ip_address: str = "",
        user_agent: str = "",
        extra: dict | None = None,
    ) -> None:
        """Emit one canonical ``wizard.{step}.{action}`` event.

        ``step`` is the dotted slug (``step-2``, ``step-3`` … ``step-7``)
        as carried on wizard_session.current_step; the emitted event
        name normalises it to ``stepN`` (no hyphen) to match the
        pre-existing ``wizard.step7.entered`` / ``wizard.step8.finish``
        convention.

        ``action`` MUST be one of the four canonical action constants.
        Anything else logs a warning and refuses to emit — the funnel
        schema is closed.

        ``extra`` is an optional ``{key: value}`` map appended after
        the five canonical fields, used for action-specific detail
        (e.g. ``error_code`` on ``errored``). Keys are serialised in
        sorted order so the audit ``detail`` string is deterministic
        across runs.
        """
        if action not in _VALID_ACTIONS:
            logging.warning(
                "WizardAnalyticsService.emit: refusing unknown action=%r "
                "(must be one of entered/completed/skipped/errored)",
                action,
            )
            return

        normalised_step = _normalise_step(step)
        if not normalised_step:
            logging.warning("WizardAnalyticsService.emit: refusing empty step")
            return

        event_name = f"wizard.{normalised_step}.{action}"
        WizardAnalyticsService.emit_event(
            event_name=event_name,
            audit_action=f"wizard_{normalised_step}_{action}",
            context=context,
            ip_address=ip_address,
            user_agent=user_agent,
            extra=extra,
        )

    @staticmethod
    def emit_event(
        *,
        event_name: str,
        audit_action: str,
        context: WizardEventContext,
        ip_address: str = "",
        user_agent: str = "",
        extra: dict | None = None,
    ) -> None:
        """Emit a canonical analytics event with the five-field envelope.

        Generic surface for events that share the wizard funnel's
        five-field context but don't fit the ``wizard.{step}.{action}``
        shape — notably the AC3 ``checklist.{event}`` events
        (dismissed / undismissed / item.skipped / item.unskipped /
        collapsed / expanded) which live on the post-wizard dashboard
        surface.

        ``event_name`` is the literal ``analytics_event=`` substring the
        marketing funnel greps for (e.g. ``checklist.item.skipped``).
        ``audit_action`` is the AuditLogService ``action`` field
        (snake_case, used as a Kibana facet column). The five-field
        envelope follows in the ``detail`` string per the standard
        schema.

        Same fail-soft posture as :meth:`emit` — any failure logs a
        warning and is swallowed; never blocks the caller.
        """
        if not event_name:
            logging.warning("WizardAnalyticsService.emit_event: refusing empty event_name")
            return
        if not audit_action:
            logging.warning("WizardAnalyticsService.emit_event: refusing empty audit_action")
            return

        detail = _build_detail(event_name, context, extra)

        try:
            AuditLogService.log_event(
                context.user_id,
                audit_action,
                True,
                ip_address=ip_address,
                user_agent=user_agent,
                detail=detail,
            )
        except Exception as e:  # noqa: BLE001 — never let the funnel emitter break the caller
            logging.warning(
                "WizardAnalyticsService.emit_event: log_event raised for %r/%r: %s",
                event_name,
                context.user_id,
                e,
            )


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _normalise_step(step: str) -> str:
    """Normalise ``step-2`` → ``step2`` to match the existing event names.

    The wizard_session row uses the hyphen form for ``current_step``
    (``step-2``, ``step-3`` …) but the pre-existing canonical events
    in the codebase (``wizard.step7.entered``, ``wizard.step8.finish``)
    use the hyphen-less form. Match the existing convention so the
    marketing funnel doesn't have to grep for both.
    """
    if not step:
        return ""
    return str(step).strip().replace("-", "")


def _iso_week_from_iso(iso_timestamp: str) -> str:
    """Return ``YYYY-Www`` from an ISO-8601 timestamp, ``""`` on parse failure.

    Cohort granularity is week-of-wizard-start, anchored to the
    wizard_session's ``created_at`` so it doesn't shift across step
    events. A malformed / empty timestamp returns the empty string
    rather than raising — the funnel field is always present, but
    may be empty when the upstream wizard_session row was created
    before this field was emitted.
    """
    if not iso_timestamp:
        return ""
    try:
        # Python 3.11+ accepts the trailing 'Z'; earlier versions need
        # explicit replacement. Strip whitespace defensively.
        cleaned = iso_timestamp.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _session_id(username: str) -> str:
    """Return the doc id WizardSessionService uses internally.

    Mirrors :func:`asset_management.web.services.wizard_session_service._doc_id`
    without importing it, so the emitter doesn't pull the whole
    wizard_session module just for the id format. The contract is
    deliberately duplicated as a short literal — both sides must move
    together if the id shape ever changes.
    """
    if not username:
        return ""
    return f"{username}:{WizardAnalyticsService.WIZARD_TYPE}"


def _build_detail(
    event_name: str,
    context: WizardEventContext,
    extra: dict | None,
) -> str:
    """Serialise the canonical event to the audit ``detail`` string.

    Shape::

        analytics_event=<name> user_id=<...> account_type=<...> connector=<...>
        cohort=<...> wizard_session_id=<...> [extra_key=extra_value ...]

    The five canonical fields always render in this fixed order so
    marketing's regex slice is stable. ``extra`` keys render in sorted
    order after the canonical block; empty strings are emitted as
    ``key=`` rather than dropped so the schema column always exists.
    """
    parts = [
        f"analytics_event={event_name}",
        f"user_id={context.user_id}",
        f"account_type={context.account_type}",
        f"connector={context.connector}",
        f"cohort={context.cohort}",
        f"wizard_session_id={context.wizard_session_id}",
    ]
    if extra:
        for key in sorted(extra.keys()):
            value = extra[key]
            # Render booleans as lowercase strings ("true"/"false") to
            # match the pre-existing wizard.step8.org_shared_decision
            # event's convention; numbers and strings render via str().
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = "" if value is None else str(value)
            parts.append(f"{key}={rendered}")
    return " ".join(parts)
