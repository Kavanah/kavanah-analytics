"""
Source Limit Service

Per-user free-tier gate on the step-4 connector tile grid.

The wizard's step-4 page shows ~10 connector tiles. A free-tier user
can pick ``config.FREE_TIER_SOURCE_LIMIT`` of them (default 1) during a
``config.FREE_TIER_TRIAL_DAYS`` window (default 14). After the window
expires the limit drops to 0 — every tile is locked behind the
"Upgrade to add more sources" tooltip. A paid user (metadata flag
``paid_tier=true``) gets the unlimited path: every selectable tile is
enabled, no tooltip.

Trial start timestamp lives on the ES Security user's ``metadata``
dict under the key ``trial_started_at`` (ISO-8601, UTC). The service
stamps it lazily on the user's first hit so a user who pre-exists the
 rollout gets a fresh 14-day window starting at their next
step-4 visit. Re-stamping is a no-op — once the timestamp is in place
it never moves. The metadata merge mirrors UserProfileService's
``set_account_type`` pattern (read-modify-write the whole metadata
dict; preserve every other key).

The service intentionally lives next to ``UserProfileService`` rather
than inside it because the "limit" surface is a computed view of two
metadata fields plus config — it composes them, it doesn't store new
state of its own. The only mutation the service performs is the
lazy ``trial_started_at`` stamp.

The "per-org" wording on the spec is preserved at the API
shape — callers pass ``username`` and we resolve the user's tier
flag. Today the tier flag is per-user (no paid orgs exist), so the
"org" framing is read here as "the org this user belongs to," which
in the current data model is one-to-one with the user. When
or a billing ticket adds per-org tier flags, this service grows a
``de_org_billing`` lookup; the public API shape ``get_limit(username)``
stays the same.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/python")
try:
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client
except ImportError:
    from pathlib import Path

    _repo_python = Path(__file__).resolve().parents[3]
    if str(_repo_python) not in sys.path:
        sys.path.insert(0, str(_repo_python))
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402

PAID_TIER_KEY = "paid_tier"
TRIAL_STARTED_AT_KEY = "trial_started_at"


class SourceLimitService:
    """Per-user free-tier source-limit lookup for wizard step-4."""

    @staticmethod
    def _get_user(es, username: str):
        """Fetch the ES Security user record. Returns (user_data, err).

        Mirrors ``UserProfileService._get_user`` — same read-then-merge
        contract so the two services play nicely under a shared mock.
        """
        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if user_data is None:
                return None, f"User '{username}' not found"
            return user_data, None
        except Exception as e:  # noqa: BLE001 — surface as a soft error, not a 500
            logging.error("SourceLimitService: failed to fetch user '%s': %s", username, e)
            return None, str(e)

    @staticmethod
    def _now() -> datetime:
        """Indirection so tests can monkeypatch the clock at the service."""
        return datetime.now(timezone.utc)

    @staticmethod
    def _stamp_trial_started_at(es, username: str, user_data: dict, now: datetime) -> str:
        """Write ``trial_started_at`` into the user's metadata. Read-modify-write
        the whole metadata dict so other keys (``account_type``,
        ``force_password_change``, etc.) are preserved.
        """
        metadata = dict(user_data.get("metadata", {}))
        iso = now.isoformat()
        metadata[TRIAL_STARTED_AT_KEY] = iso
        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "SourceLimitService: failed to stamp trial_started_at for '%s': %s",
                username,
                e,
            )
        return iso

    @staticmethod
    def get_limit(username: str) -> dict:
        """Return the source-limit decision for ``username``.

        Shape::

            {
                "limit": int,            # tiles enabled in the grid order
                "is_paid": bool,         # paid_tier flag from user metadata
                "trial_active": bool,    # within FREE_TIER_TRIAL_DAYS
                "trial_started_at": str, # ISO-8601 UTC, may be ""
                "trial_ends_at": str,    # ISO-8601 UTC, may be ""
            }

        * ``is_paid=True`` returns ``limit=-1`` (sentinel for unlimited),
          ``trial_active=True`` (we still return True so the template can
          read a single "no tooltip" branch), and empty trial dates.
        * Free + trial active returns ``limit=FREE_TIER_SOURCE_LIMIT``.
        * Free + trial expired returns ``limit=0`` — every tile locked.
        * ES unavailable or user-not-found returns ``limit=0`` (fail-closed
          — refusing access on a missing limit decision is safer than
          accidentally granting an unlimited surface). The caller can
          still render the grid; every tile just shows the upgrade
          tooltip.

        Stamps ``trial_started_at`` on the user's metadata the first
        time the service is asked about a free-tier user. Idempotent
        after that: a present timestamp is never overwritten. Paid
        users do NOT get a trial stamp — the field is meaningless once
        the user has upgraded.
        """
        # Import inline so the module imports cleanly under a pytest
        # path that has not yet loaded the Flask app's config singleton.
        from asset_management.web.config import config

        empty = {
            "limit": 0,
            "is_paid": False,
            "trial_active": False,
            "trial_started_at": "",
            "trial_ends_at": "",
        }
        if not username:
            return empty

        es = get_elasticsearch_client()
        if es is None:
            return empty

        user_data, err = SourceLimitService._get_user(es, username)
        if err or user_data is None:
            return empty

        metadata = user_data.get("metadata") or {}
        is_paid = bool(metadata.get(PAID_TIER_KEY))

        if is_paid:
            return {
                "limit": -1,
                "is_paid": True,
                "trial_active": True,
                "trial_started_at": "",
                "trial_ends_at": "",
            }

        # Free tier — compute the trial window from the stamped start
        # timestamp. If the stamp is missing or unparseable we stamp it
        # at "now" and treat the trial as freshly started. The lazy
        # stamp lets pre-existing users (who never went through the
        # codepath at signup) get a clean 14-day window starting
        # at their first step-4 visit.
        now = SourceLimitService._now()
        stored = metadata.get(TRIAL_STARTED_AT_KEY) or ""
        started_at: datetime | None = None
        if stored:
            try:
                parsed = datetime.fromisoformat(stored)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                started_at = parsed
            except ValueError:
                started_at = None

        if started_at is None:
            stamped = SourceLimitService._stamp_trial_started_at(es, username, user_data, now)
            started_at = now
            stored = stamped

        ends_at = started_at + timedelta(days=config.FREE_TIER_TRIAL_DAYS)
        trial_active = now < ends_at

        if trial_active:
            limit = config.FREE_TIER_SOURCE_LIMIT
        else:
            limit = 0

        return {
            "limit": limit,
            "is_paid": False,
            "trial_active": trial_active,
            "trial_started_at": stored,
            "trial_ends_at": ends_at.isoformat(),
        }

    @staticmethod
    def is_tile_index_allowed(tile_index_in_selectable_order: int, limit: int) -> bool:
        """Position-based allowlist: tile-index 0..N-1 are enabled when
        the limit is N; -1 means unlimited. The template renders the
        ``popular`` section first, then ``all``, so the position is
        computed by walking the union of ``popular_tiles`` and
        ``all_tiles`` in render order and counting selectable tiles.

        Used by both the GET (to mark tiles ``data-locked``) AND the
        POST (to refuse a selection beyond the limit). Keeping the
        decision in one place means the visual gate and the
        server-side gate can never disagree.
        """
        if limit < 0:
            return True
        return tile_index_in_selectable_order < limit
