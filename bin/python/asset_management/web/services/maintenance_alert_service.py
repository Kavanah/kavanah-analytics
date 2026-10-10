"""Maintenance Alert Service.

Stores a single dashboard-wide maintenance notification in the existing
``de_system_settings`` index under doc id ``maintenance_alert`` (kept
separate from ``global_settings`` so it does not collide with feedback
settings — different docs in the same index, different field sets).

Conceptual shape of the stored doc::

    {
        "@timestamp": "2026-04-29T...",
        "maintenance_alert_message": "Scheduled maintenance Saturday 8 PM.",
        "maintenance_alert_published": true,
        "maintenance_alert_version": 7,
        "updated_by": "superuser_username"
    }

``maintenance_alert_version`` is bumped on every Publish so that the
client banner re-shows for users who had dismissed the prior message.
Hide leaves the message intact and flips ``published`` to false.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

INDEX = "de_system_settings"
DOC_ID = "maintenance_alert"

_INDEX_MAPPING_PROPS = {
    "@timestamp": {"type": "date"},
    "maintenance_alert_message": {"type": "text"},
    "maintenance_alert_published": {"type": "boolean"},
    "maintenance_alert_version": {"type": "long"},
    "updated_by": {"type": "keyword"},
}


def _empty() -> dict[str, Any]:
    return {"published": False, "message": "", "version": 0, "updated_by": "", "updated_at": ""}


class MaintenanceAlertService:
    """CRUD for the dashboard's header maintenance alert."""

    @staticmethod
    def ensure_index_exists() -> None:
        """Merge the maintenance-alert mapping into ``de_system_settings``.

        Safe to call alongside ``FeedbackService.ensure_index_exists`` —
        ``put_mapping`` is additive, so the two services contribute their
        respective fields to the shared index without conflict.
        """
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("MaintenanceAlertService: could not connect to ES")
            return

        mapping = {"properties": _INDEX_MAPPING_PROPS}
        put_regular_index_template(es, INDEX, mapping, priority=500)
        create_index_if_not_exists(es, INDEX, mapping)
        try:
            es.indices.put_mapping(index=INDEX, body=mapping)
        except Exception as e:
            logging.warning("MaintenanceAlertService: put_mapping update: %s", e)

    @staticmethod
    def get_alert(es) -> dict[str, Any]:
        """Return the current alert state.

        Always returns a dict with the keys ``published``, ``message``,
        ``version``, ``updated_by``, ``updated_at``. Missing doc / ES
        unavailable both yield the empty default — callers don't need to
        branch on ``None``.
        """
        if es is None:
            return _empty()
        try:
            doc = es.get(index=INDEX, id=DOC_ID)
        except Exception:
            return _empty()
        src = doc.get("_source", {}) or {}
        return {
            "published": bool(src.get("maintenance_alert_published")),
            "message": src.get("maintenance_alert_message") or "",
            "version": int(src.get("maintenance_alert_version") or 0),
            "updated_by": src.get("updated_by") or "",
            "updated_at": src.get("@timestamp") or "",
        }

    @staticmethod
    def publish(es, message: str, updated_by: str) -> dict[str, Any]:
        """Publish ``message`` and bump the version so prior dismissals reset."""
        text = (message or "").strip()
        if not text:
            return {"success": False, "error": "Message text is required."}
        current = MaintenanceAlertService.get_alert(es)
        new_version = int(current.get("version") or 0) + 1
        try:
            es.index(
                index=INDEX,
                id=DOC_ID,
                body={
                    "@timestamp": datetime.now(timezone.utc).isoformat(),
                    "maintenance_alert_message": text,
                    "maintenance_alert_published": True,
                    "maintenance_alert_version": new_version,
                    "updated_by": updated_by,
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error("MaintenanceAlertService: publish failed: %s", e)
            return {"success": False, "error": "Failed to save alert."}
        return {"success": True, "version": new_version, "message": text}

    @staticmethod
    def hide(es, updated_by: str) -> dict[str, Any]:
        """Mark the alert as not published. The message text is retained
        so a superuser can flip it back on without re-typing."""
        current = MaintenanceAlertService.get_alert(es)
        try:
            es.index(
                index=INDEX,
                id=DOC_ID,
                body={
                    "@timestamp": datetime.now(timezone.utc).isoformat(),
                    "maintenance_alert_message": current.get("message", ""),
                    "maintenance_alert_published": False,
                    "maintenance_alert_version": int(current.get("version") or 0),
                    "updated_by": updated_by,
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error("MaintenanceAlertService: hide failed: %s", e)
            return {"success": False, "error": "Failed to hide alert."}
        return {"success": True}
