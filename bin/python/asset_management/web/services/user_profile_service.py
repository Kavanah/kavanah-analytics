"""
User Profile Service

Reads and writes the ``account_type`` field on an Elasticsearch Security
user's metadata dict — the same dict that already stores
``force_password_change``, ``suspended_at``, ``reinstated_at``, and the
ES-managed ``_reserved`` marker. reframes the original
"add ``users.account_type`` column" design as "store on the user-
metadata index used by UserManagementService / UserProvisioningService"
because there is no relational users table — identity lives in the ES
Security API.

The four supported values describe the entry point a user signed up
under and drive downstream defaults plus entry-point cohort analytics:

* ``individual``  — solo user (no org)
* ``org_owner``   — created the workspace for an organisation
* ``invited``     — joined via an invitation from an existing workspace
* ``agency``      — manages multiple client workspaces

Storage decision is recorded in ``the private docs``
(entry: ``2026-05-11 — ``).
"""

import logging
import sys

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover — Python 3.9+ ships zoneinfo
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

sys.path.insert(0, "/app/python")
try:
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client
except ImportError:
    # The pytest environment doesn't have /app/python on disk — fall
    # back to the in-repo location so the module imports under both the
    # imports_container runtime and a host-side pytest run.
    from pathlib import Path

    _repo_python = Path(__file__).resolve().parents[3]
    if str(_repo_python) not in sys.path:
        sys.path.insert(0, str(_repo_python))
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402

ACCOUNT_TYPE_INDIVIDUAL = "individual"
ACCOUNT_TYPE_ORG_OWNER = "org_owner"
ACCOUNT_TYPE_INVITED = "invited"
ACCOUNT_TYPE_AGENCY = "agency"

VALID_ACCOUNT_TYPES = (
    ACCOUNT_TYPE_INDIVIDUAL,
    ACCOUNT_TYPE_ORG_OWNER,
    ACCOUNT_TYPE_INVITED,
    ACCOUNT_TYPE_AGENCY,
)

# Default for users who pre-exist the most early users
# are org owners. Individuals can flip the value from Settings later.
DEFAULT_ACCOUNT_TYPE = ACCOUNT_TYPE_ORG_OWNER

METADATA_KEY = "account_type"

# per-user time zone, stored on the same user.metadata dict.
# Independent of the workspace-level ``timezone`` field on the
# de_asset_workspace doc — a workspace's reporting zone is set by the
# admin at workspace-create time (step-5 onboarding); a user's display
# zone follows them across workspaces and is editable from /profile.
METADATA_KEY_TIME_ZONE = "time_zone"
DEFAULT_TIME_ZONE = "UTC"


class UserProfileService:
    """Service for reading/writing per-user profile fields stored on the
    ES Security user's metadata dict.

    Today only ``account_type`` lives here. If this grows past a handful
    of fields we should split into a dedicated ``de_user_profile`` index
    (see the DECISIONS.md entry's "Revisit if" clause).
    """

    @staticmethod
    def _get_user(es, username: str):
        """Fetch a single ES Security user. Returns (user_data, error_msg).

        Mirrors UserManagementService._get_user so the two services have
        identical "fetch then merge metadata then put" semantics.
        """
        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if user_data is None:
                return None, f"User '{username}' not found"
            return user_data, None
        except Exception as e:
            logging.error("UserProfileService: failed to fetch user '%s': %s", username, e)
            return None, "Could not fetch user."

    @staticmethod
    def get_account_type(username: str) -> str:
        """Return the stored ``account_type`` for a user, or the default.

        Falls back to ``DEFAULT_ACCOUNT_TYPE`` when:

        * the metadata dict has no ``account_type`` key (user predates
          the rollout and has not been backfilled yet), or
        * ES is unreachable / the user is not found (the caller can
          decide whether to surface that separately by calling
          ``get_account_type_detailed``).

        Use this when the caller wants a value to render in a UI or
        stamp on an event and does not need to distinguish "absent" from
        "default".
        """
        result = UserProfileService.get_account_type_detailed(username)
        return result.get("account_type") or DEFAULT_ACCOUNT_TYPE

    @staticmethod
    def get_account_type_detailed(username: str) -> dict:
        """Return ``{success, account_type, is_default, error}``.

        ``is_default`` is True when the value came from
        ``DEFAULT_ACCOUNT_TYPE`` because no key was stored, so a caller
        that needs to know "this user has never picked a value" can
        tell. ``account_type`` is always populated (even on error) so
        callers do not have to guard against None.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "account_type": DEFAULT_ACCOUNT_TYPE,
                "is_default": True,
                "error": "Elasticsearch unavailable",
            }

        user_data, err = UserProfileService._get_user(es, username)
        if err:
            return {
                "success": False,
                "account_type": DEFAULT_ACCOUNT_TYPE,
                "is_default": True,
                "error": err,
            }

        metadata = user_data.get("metadata") or {}
        stored = metadata.get(METADATA_KEY)
        if stored in VALID_ACCOUNT_TYPES:
            return {
                "success": True,
                "account_type": stored,
                "is_default": False,
                "error": None,
            }
        # Either absent or an unrecognised value. Treat both as "default"
        # rather than surfacing the bad value — a bad value can only have
        # come from a hand-edit and we'd rather render something usable.
        return {
            "success": True,
            "account_type": DEFAULT_ACCOUNT_TYPE,
            "is_default": True,
            "error": None,
        }

    @staticmethod
    def set_account_type(username: str, account_type: str) -> dict:
        """Persist ``account_type`` on the user's metadata dict.

        Validates against ``VALID_ACCOUNT_TYPES`` before touching ES so
        a bad value surfaces as a clean error rather than corrupting the
        record. Preserves every other metadata key
        (``force_password_change``, ``suspended_at``, etc.) by merging
        on the existing dict.

        Returns ``{success, error, old_value, new_value}``. ``old_value``
        is the prior stored value or None if absent — useful for the
        audit log entry the caller writes after a successful flip.
        """
        if account_type not in VALID_ACCOUNT_TYPES:
            return {
                "success": False,
                "error": (f"Invalid account_type '{account_type}'. Must be one of: " f"{', '.join(VALID_ACCOUNT_TYPES)}"),
            }

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserProfileService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        metadata = dict(user_data.get("metadata", {}))
        old_value = metadata.get(METADATA_KEY)
        metadata[METADATA_KEY] = account_type

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:
            logging.error(
                "UserProfileService: failed to set account_type for '%s': %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not update account type."}

        logging.info(
            "AUDIT: action=set_account_type username=%s old=%s new=%s",
            username,
            old_value or "",
            account_type,
        )
        return {
            "success": True,
            "error": None,
            "old_value": old_value,
            "new_value": account_type,
        }

    @staticmethod
    def _coerce_time_zone(raw: object) -> str | None:
        """Validate an IANA zone string. Returns the validated zone or
        None if the input is empty/invalid.

        The validator is the same shape as
        ``WorkspaceSettingsService._coerce_timezone``: accept
        any zone that ``zoneinfo.ZoneInfo`` can resolve, including
        ``Etc/`` and continent-prefixed names. We deliberately don't
        ship our own IANA-name allowlist — the OS's tzdata is the
        source of truth and stays current automatically.
        """
        if not isinstance(raw, str):
            return None
        candidate = raw.strip()
        if not candidate:
            return None
        if len(candidate) > 64:
            # Matches the wizard's maxlength on the same field; rejects
            # obviously-forged payloads without burning a ZoneInfo lookup.
            return None
        if ZoneInfo is None:  # pragma: no cover
            return candidate
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None
        return candidate

    @staticmethod
    def get_time_zone(username: str) -> str:
        """Return the stored ``time_zone`` for a user, or ``DEFAULT_TIME_ZONE``.

        Falls back to UTC when the metadata dict has no ``time_zone``
        key (user predates the rollout) or ES is unreachable.
        """
        return UserProfileService.get_time_zone_detailed(username)["time_zone"]

    @staticmethod
    def get_time_zone_detailed(username: str) -> dict:
        """Return ``{time_zone, is_default}`` for the user.

        ``is_default`` is True when no value is stored on user.metadata —
        the caller (profile_page) uses this to decide whether to let the
        browser's Intl-detected zone pre-select the <option>. Once the
        user explicitly saves a value, ``is_default`` flips to False and
        the browser-detection JS leaves the stored value alone.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"time_zone": DEFAULT_TIME_ZONE, "is_default": True}
        user_data, err = UserProfileService._get_user(es, username)
        if err or user_data is None:
            return {"time_zone": DEFAULT_TIME_ZONE, "is_default": True}
        metadata = user_data.get("metadata") or {}
        stored = metadata.get(METADATA_KEY_TIME_ZONE)
        if isinstance(stored, str) and stored.strip():
            return {"time_zone": stored.strip(), "is_default": False}
        return {"time_zone": DEFAULT_TIME_ZONE, "is_default": True}

    @staticmethod
    def set_time_zone(username: str, time_zone: str) -> dict:
        """Persist ``time_zone`` on the user's metadata dict.

        Validates against ``zoneinfo`` before touching ES so a typo or
        forged value surfaces as a clean error rather than corrupting
        the record. Preserves every other metadata key (account_type,
        force_password_change, etc.) by merging on the existing dict.

        Returns ``{success, error, old_value, new_value}``. ``old_value``
        is the prior stored value or None if absent.
        """
        validated = UserProfileService._coerce_time_zone(time_zone)
        if validated is None:
            return {
                "success": False,
                "error": "Invalid time zone. Use an IANA name like 'America/New_York'.",
            }

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        user_data, err = UserProfileService._get_user(es, username)
        if err:
            return {"success": False, "error": err}

        metadata = dict(user_data.get("metadata", {}))
        old_value = metadata.get(METADATA_KEY_TIME_ZONE)
        if old_value == validated:
            # No-op write — short-circuit before the ES round-trip and
            # before the audit-log row. Matches the form-side gating
            # that disables the Save button when the field is unchanged.
            return {
                "success": True,
                "error": None,
                "old_value": old_value,
                "new_value": validated,
                "noop": True,
            }
        metadata[METADATA_KEY_TIME_ZONE] = validated

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:
            logging.error(
                "UserProfileService: failed to set time_zone for '%s': %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not update time zone."}

        logging.info(
            "AUDIT: action=set_time_zone username=%s old=%s new=%s",
            username,
            old_value or "",
            validated,
        )
        return {
            "success": True,
            "error": None,
            "old_value": old_value,
            "new_value": validated,
        }

    @staticmethod
    def extract_account_type_from_user_record(user_record: dict) -> str | None:
        """Read ``account_type`` straight out of an ES Security user
        record without making a fresh ES call.

        Used by AuditLogService.log_event on login events: that path
        already calls ``es.security.get_user()`` (to compute
        ``suspicious``), so the metadata is available for free in the
        same response. Returns the stored value when it is a valid enum
        member, else None — callers stamp the field only when it is
        meaningful (avoids polluting the audit doc with stale/default
        sentinel values that would muddy entry-point cohort queries).
        """
        if not isinstance(user_record, dict):
            return None
        metadata = user_record.get("metadata") or {}
        value = metadata.get(METADATA_KEY)
        if value in VALID_ACCOUNT_TYPES:
            return value
        return None
