"""
Feature Flag Service

Minimal feature-flag system (born in the New User Onboarding
epic; its founding new-onboarding flag was retired and the
platform now serves any registered flag).
Backed by the ``de_feature_flags`` ES index, one doc per flag (doc id =
flag key). Mirrors the WizardSessionService / RolePermissionsService
pattern: ``ensure_index_exists`` bootstraps the template + index +
mapping, ``seed_defaults`` upserts the registered flag set at startup,
and ``is_enabled(flag, user_id)`` is the per-user evaluation hook
callers use to gate behavior.

A flag has three modes:

* ``mode="off"`` — disabled for everyone (default).
* ``mode="on"`` — enabled for everyone.
* ``mode="cohort"`` — enabled for a deterministic ``rollout_percent`` of
  users via SHA-256 hashing of ``{flag}:{user_id}`` mod 100. The same
  user lands in the same bucket across requests, processes, and
  restarts — no per-user cohort table needed.

Storage decision and rollout shape are recorded in
``the private docs`` (entry: ``2026-05-11 — ``).
"""

import hashlib
import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

INDEX = "de_feature_flags"

INDEX_MAPPING = {
    "properties": {
        "flag": {"type": "keyword"},
        "mode": {"type": "keyword"},
        "rollout_percent": {"type": "integer"},
        "description": {"type": "text"},
        "updated_by": {"type": "keyword"},
        "updated_at": {"type": "date"},
        "created_at": {"type": "date"},
    }
}

MODE_OFF = "off"
MODE_ON = "on"
MODE_COHORT = "cohort"
_VALID_MODES = (MODE_OFF, MODE_ON, MODE_COHORT)

FLAG_ORG_BILLING_PER_TEAM = "ORG_BILLING_PER_TEAM"

# Registered flags. Adding a flag here registers it; ``seed_defaults`` will
# create the ES doc on next startup if one does not already exist. The
# stored doc is the runtime source of truth — the registry only seeds
# initial state and documents intent, so a CLI flip is not clobbered on
# the next restart.
#
# Retired flags are REMOVED from the registry (and their ES doc deleted
# manually on each environment) rather than left at mode='on':
# NEW_ONBOARDING was retired
# after the wizard rollout completed.
_REGISTRY: dict[str, dict] = {
    FLAG_ORG_BILLING_PER_TEAM: {
        "mode": MODE_OFF,
        "rollout_percent": 0,
        "description": (
            "Option B org-level billing. OFF by default while the "
            "per-team subscription model is built behind the flag (Phases O2+). "
            "When ON, checkout licenses the buyer's primary TEAM (one "
            "subscription per team under the org's Stripe customer) and get_state "
            "derives from the team subscription; OFF keeps the existing "
            "per-workspace single-subscription path. Flip to 'on' only AFTER the "
            "O4 migration has associated existing subscriptions with a team."
        ),
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cohort_bucket(flag: str, user_id: str) -> int:
    """Return a deterministic 0–99 bucket for ``(flag, user_id)``.

    SHA-256 of ``f"{flag}:{user_id}"`` mod 100. The same user lands in
    the same bucket across processes, restarts, and rollout-percent
    bumps within the same flag — so a user who saw the wizard at 50%
    keeps seeing it at 60%, but a different flag re-rolls independently
    (because the flag key is part of the hash input).
    """
    digest = hashlib.sha256(f"{flag}:{user_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100


class FeatureFlagService:
    """Service for evaluating and managing feature flags."""

    # ─────────────────────────────────────────────────────────────────────────
    # Index bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the feature flags index if it does not exist."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("FeatureFlagService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("FeatureFlagService: put_mapping update: %s", e)

    @staticmethod
    def seed_defaults() -> None:
        """Create default docs for every registered flag if missing.

        Uses ``op_type=create`` so an admin-set value (via the CLI) is
        never clobbered on the next startup. New flags added to
        ``_REGISTRY`` after the index is populated get seeded on the
        next boot.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("FeatureFlagService: could not connect to ES for seeding")
            return
        now = _now_iso()
        for flag, defaults in _REGISTRY.items():
            try:
                es.index(
                    index=INDEX,
                    id=flag,
                    body={
                        "flag": flag,
                        "mode": defaults["mode"],
                        "rollout_percent": int(defaults.get("rollout_percent", 0)),
                        "description": defaults.get("description", ""),
                        "updated_by": "system",
                        "updated_at": now,
                        "created_at": now,
                    },
                    op_type="create",
                )
                logging.info("FeatureFlagService: seeded defaults for '%s'", flag)
            except Exception as e:
                # Document already exists — leave the runtime value alone.
                if "version_conflict_engine_exception" not in str(e).lower():
                    logging.error("FeatureFlagService: error seeding '%s': %s", flag, e)

    # ─────────────────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_flag(flag: str) -> dict | None:
        """Return the stored config for a flag, or None if missing.

        Falls back to the registry default if ES is unreachable, so the
        evaluation hook still answers deterministically during an ES
        outage instead of throwing.
        """
        es = get_elasticsearch_client()
        if es is None:
            return _registry_default(flag)
        try:
            result = es.get(index=INDEX, id=flag)
        except Exception:
            return _registry_default(flag)
        return dict(result.get("_source") or {}) or _registry_default(flag)

    @staticmethod
    def list_flags() -> list[dict]:
        """Return every stored flag doc (admin/CLI listing)."""
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(index=INDEX, body={"query": {"match_all": {}}, "size": 100})
            return [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]
        except Exception as e:
            logging.error("FeatureFlagService: list_flags error: %s", e)
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Evaluation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_enabled(flag: str, user_id: str | None = None) -> bool:
        """Return whether ``flag`` is enabled for the given user.

        * ``mode="off"`` → always False
        * ``mode="on"`` → always True
        * ``mode="cohort"`` → True iff ``_cohort_bucket(flag, user_id) <
          rollout_percent``. A missing/empty ``user_id`` in cohort mode
          falls back to False — anonymous traffic never enters an A/B
          arm because the cohort assignment would not be stable.

        Unknown flag → False. The caller should fail closed rather than
        accidentally exposing new code paths on a typo.
        """
        config = FeatureFlagService.get_flag(flag) if flag else None
        if not config:
            return False
        mode = config.get("mode", MODE_OFF)
        if mode == MODE_ON:
            return True
        if mode == MODE_OFF:
            return False
        if mode == MODE_COHORT:
            if not user_id:
                return False
            percent = int(config.get("rollout_percent", 0) or 0)
            if percent <= 0:
                return False
            if percent >= 100:
                return True
            return _cohort_bucket(flag, user_id) < percent
        # Unknown mode (corrupted doc) — fail closed.
        logging.warning("FeatureFlagService: unknown mode %r on flag %r", mode, flag)
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Write (admin)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def set_flag(
        flag: str,
        mode: str,
        *,
        rollout_percent: int | None = None,
        updated_by: str = "cli",
    ) -> dict:
        """Set a flag's mode (and rollout percent, when applicable).

        Returns ``{"success": True, "flag": <doc>}`` or
        ``{"success": False, "error": "..."}``. Only flags listed in
        ``_REGISTRY`` may be set — silently creating arbitrary flag docs
        would defeat the point of having a registry.
        """
        if flag not in _REGISTRY:
            return {
                "success": False,
                "error": f"Unknown flag '{flag}'. Register it in _REGISTRY first.",
            }
        if mode not in _VALID_MODES:
            return {
                "success": False,
                "error": f"mode must be one of {_VALID_MODES}; got {mode!r}",
            }
        percent = 0
        if mode == MODE_COHORT:
            if rollout_percent is None:
                return {
                    "success": False,
                    "error": "rollout_percent is required when mode='cohort'",
                }
            try:
                percent = int(rollout_percent)
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "error": f"rollout_percent must be an int 0–100; got {rollout_percent!r}",
                }
            if percent < 0 or percent > 100:
                return {
                    "success": False,
                    "error": f"rollout_percent must be between 0 and 100; got {percent}",
                }

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = _now_iso()
        # Preserve created_at across edits — useful for "when was this
        # flag first registered" when auditing rollouts.
        try:
            existing = es.get(index=INDEX, id=flag)
            created_at = (existing.get("_source") or {}).get("created_at") or now
        except Exception:
            created_at = now

        doc = {
            "flag": flag,
            "mode": mode,
            "rollout_percent": percent,
            "description": _REGISTRY[flag].get("description", ""),
            "updated_by": updated_by or "cli",
            "updated_at": now,
            "created_at": created_at,
        }
        try:
            es.index(index=INDEX, id=flag, body=doc, refresh="wait_for")
        except Exception as e:
            logging.error("FeatureFlagService: set_flag '%s' error: %s", flag, e)
            return {"success": False, "error": str(e)}
        logging.info(
            "FeatureFlagService: set '%s' mode=%s rollout=%s by=%s",
            flag,
            mode,
            percent,
            updated_by,
        )
        return {"success": True, "flag": doc}


def _registry_default(flag: str) -> dict | None:
    """Fallback config from the in-process registry. Returned when ES is
    unreachable or has no doc yet, so the eval hook can still answer.
    """
    if flag not in _REGISTRY:
        return None
    defaults = _REGISTRY[flag]
    return {
        "flag": flag,
        "mode": defaults["mode"],
        "rollout_percent": int(defaults.get("rollout_percent", 0)),
        "description": defaults.get("description", ""),
    }
