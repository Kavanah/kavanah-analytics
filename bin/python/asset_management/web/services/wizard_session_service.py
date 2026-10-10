"""
Wizard Session Service

Persists in-flight wizard state to ES so the wizard is resumable across
browser refresh, OAuth roundtrip, and crashes. Backed by the
``de_wizard_session`` index; doc ID is ``{user_id}:{wizard_type}``, so a
user has at most one active doc per wizard at any time.

Storage decision and schema are recorded in
``the private docs`` (entry: ``2026-05-11 — ``).
"""

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

INDEX = "de_wizard_session"

INDEX_MAPPING = {
    "properties": {
        "user_id": {"type": "keyword"},
        "wizard_type": {"type": "keyword"},
        "status": {"type": "keyword"},
        "current_step": {"type": "keyword"},
        # ``state`` is an opaque per-step JSON blob (selected ESIDs, partial
        # form payloads, OAuth roundtrip markers). enabled=false stops ES
        # from indexing every nested field — we only ever read the whole
        # blob back by doc id.
        "state": {"type": "object", "enabled": False},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
        "completed_at": {"type": "date"},
    }
}

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"
# "skipped" is a third terminal status, distinct from "completed".
# A skipped session is re-entrable from the dashboard's "Connect your first
# source" checklist — the GET handler reactivates it (status -> active),
# preserving the prior state blob. ``get_active`` deliberately does NOT
# return skipped sessions: resume is an explicit checklist click, not an
# auto-resume banner.
STATUS_SKIPPED = "skipped"


def _doc_id(user_id: str, wizard_type: str) -> str:
    return f"{user_id}:{wizard_type}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WizardSessionService:
    """Service for persisting and recovering in-flight wizard state."""

    # ─────────────────────────────────────────────────────────────────────────
    # Index bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the wizard session index if it does not exist."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("WizardSessionService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("WizardSessionService: put_mapping update: %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_active(user_id: str, wizard_type: str) -> dict | None:
        """Return the active wizard session for this user+wizard, or None.

        Returns None when no doc exists or the stored doc's status is not
        ``active`` (i.e. completed or abandoned sessions never auto-resume).
        """
        if not user_id or not wizard_type:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.get(index=INDEX, id=_doc_id(user_id, wizard_type))
        except Exception:
            return None
        source = result.get("_source") or {}
        if source.get("status") != STATUS_ACTIVE:
            return None
        return dict(source)

    # ─────────────────────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def save(
        user_id: str,
        wizard_type: str,
        current_step: str,
        state: dict,
    ) -> bool:
        """Upsert the active wizard session for this user+wizard.

        Sets ``status="active"`` and refreshes ``updated_at``. Preserves
        ``created_at`` from a prior doc when one exists, so the lifespan
        of a session is observable across restarts.
        """
        if not user_id or not wizard_type:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False

        doc_id = _doc_id(user_id, wizard_type)
        now = _now_iso()

        # Preserve created_at from the existing doc so save() is an upsert
        # rather than a reset. If no doc exists, this is the first write.
        try:
            existing = es.get(index=INDEX, id=doc_id)
            created_at = (existing.get("_source") or {}).get("created_at") or now
        except Exception:
            created_at = now

        doc = {
            "user_id": user_id,
            "wizard_type": wizard_type,
            "status": STATUS_ACTIVE,
            "current_step": current_step or "",
            "state": state or {},
            "created_at": created_at,
            "updated_at": now,
        }
        try:
            # refresh="wait_for" so the resume read after an OAuth redirect
            # or page refresh sees the latest state. Acceptance criterion
            # ("OAuth bounce never restarts the wizard") depends on this.
            es.index(index=INDEX, id=doc_id, body=doc, refresh="wait_for")
            return True
        except Exception as e:
            logging.error(
                "WizardSessionService: save failed user=%r wizard=%r: %s",
                user_id,
                wizard_type,
                e,
            )
            return False

    @staticmethod
    def complete(user_id: str, wizard_type: str) -> bool:
        """Mark the session as completed (dashboard handoff). Sets
        ``status="completed"`` and ``completed_at``. Idempotent — completing
        a non-existent session is a no-op that returns False.
        """
        return WizardSessionService._set_terminal_status(user_id, wizard_type, STATUS_COMPLETED)

    @staticmethod
    def abandon(user_id: str, wizard_type: str) -> bool:
        """Mark the session as abandoned. Same shape as ``complete`` but a
        different terminal status — retained for observability rather than
        hard-deleted so we can answer "where do users drop off?" later.
        """
        return WizardSessionService._set_terminal_status(user_id, wizard_type, STATUS_ABANDONED)

    @staticmethod
    def skip(user_id: str, wizard_type: str) -> bool:
        """Mark the session as skipped. Re-entry from the dashboard
        checklist reactivates the same doc via ``reactivate``; the state blob
        is preserved so the user lands back at their last picked tile.
        """
        return WizardSessionService._set_terminal_status(user_id, wizard_type, STATUS_SKIPPED)

    @staticmethod
    def get_skipped(user_id: str, wizard_type: str) -> dict | None:
        """Return the skipped wizard session for this user+wizard, or None.

        Distinct from ``get_active`` — skipped sessions are NOT auto-resumed
        by the wizard's normal GET path, but the dashboard's "Connect your
        first source" checklist surfaces only when this returns non-None.
        """
        if not user_id or not wizard_type:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.get(index=INDEX, id=_doc_id(user_id, wizard_type))
        except Exception:
            return None
        source = result.get("_source") or {}
        if source.get("status") != STATUS_SKIPPED:
            return None
        return dict(source)

    @staticmethod
    def get_status(user_id: str, wizard_type: str) -> str | None:
        """follow-up — return the raw status of the wizard
        session doc (``active`` / ``completed`` / ``abandoned`` /
        ``skipped``), or None if no doc exists.

        Distinct from ``get_active`` (which only returns the source
        dict when status==active) and ``get_skipped`` (status==skipped).
        Used by the wizard-URL guard that blocks /onboarding/step-N
        re-entry after the wizard is done — needs to recognise BOTH
        completed and skipped terminal states with a single read.

        Fail-soft: returns None on ES outage so the guard does NOT
        block legit access during a transient blip.
        """
        if not user_id or not wizard_type:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.get(index=INDEX, id=_doc_id(user_id, wizard_type))
        except Exception:
            return None
        source = result.get("_source") or {}
        status = source.get("status")
        return status if isinstance(status, str) and status else None

    @staticmethod
    def reactivate(user_id: str, wizard_type: str) -> bool:
        """Flip a terminal session back to ``active``.

        Called by ``onboarding_step6_page`` when a user clicks "Connect your
        first source" on the dashboard checklist. Preserves ``state``,
        ``current_step``, and ``created_at`` — only flips ``status`` and
        refreshes ``updated_at``. Returns True if a doc was found and
        updated; False if there was nothing to reactivate.
        """
        if not user_id or not wizard_type:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc_id = _doc_id(user_id, wizard_type)
        now = _now_iso()
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                doc={"status": STATUS_ACTIVE, "updated_at": now, "completed_at": None},
                refresh="wait_for",
            )
            return True
        except Exception as e:
            logging.warning(
                "WizardSessionService: reactivate failed user=%r wizard=%r: %s",
                user_id,
                wizard_type,
                e,
            )
            return False

    @staticmethod
    def _set_terminal_status(user_id: str, wizard_type: str, status: str) -> bool:
        if not user_id or not wizard_type:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc_id = _doc_id(user_id, wizard_type)
        now = _now_iso()
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                doc={"status": status, "completed_at": now, "updated_at": now},
                refresh="wait_for",
            )
            return True
        except Exception as e:
            logging.warning(
                "WizardSessionService: %s failed user=%r wizard=%r: %s",
                status,
                user_id,
                wizard_type,
                e,
            )
            return False
