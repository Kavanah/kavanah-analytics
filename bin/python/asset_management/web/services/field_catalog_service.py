"""Field Catalog Service.

Per-workspace field-picker selections for a catalog source (fb_ads first).
One sparse-override doc per (workspace, source) in ``de_asset_field_catalog``,
deterministic ``_id = "<workspace_esid>__<source>"``:

    {
      "workspace_esid": "<the workspace>",
      "source":         "fb_ads",
      "fields": {                      # SPARSE — only fields the owner touched
        "fb_ads.cost_per_inline_link_click": {
          "enabled":    false,
          "changed_at": "...",         # go-forward marker shown in the UI
          "changed_by": "<username>"
        }
      },
      "created_at": "...", "updated_at": "...", "updated_by": "..."
    }

No doc (or any read error) means "no overrides" — the catalog's default
``requested: true`` set, i.e. today's behavior. The importers consume
``get_overrides_for_team`` and MUST keep importing on any failure here
(fail-soft to the default set); the picker is config, never an import
dependency.

Write authz: callers (the /field-catalog routes) gate on workspace/org OWNER
(``Permission.WORKSPACE_DELETE``). This service does NOT consult the session —
same convention as WorkspaceMenuService.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client
from migration.catalog_loader import PICKER_KINDS, load_catalog
from lib.errors import UserFacingError

try:
    from web.services.permissions_index_service import FIELD_CATALOG_INDEX
except ImportError:
    from asset_management.web.services.permissions_index_service import FIELD_CATALOG_INDEX


TEAMS_INDEX = "de_asset_teams"
ORGANIZATION_INDEX = "de_asset_organization"

# per-team source on/off toggles. One toggle per Meta importer; a
# source here does NOT need a catalog/<source>.json (the toggle gates the whole
# importer run, not individual fields).
SOURCE_TOGGLE_SOURCES = ("fb_ads", "fb_pages", "instagram")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FieldCatalogError(UserFacingError):
    """Base for field-catalog errors callers may want to distinguish."""


class FieldNotToggleableError(FieldCatalogError):
    """The field is unknown, outside the picker families, core, deprecated
    (enable attempt), or unmapped (enable attempt)."""


class FieldCatalogService:
    """Reads/writes ``de_asset_field_catalog`` and lists the picker catalog."""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @staticmethod
    def get_overrides(workspace_esid: str, source: str = "fb_ads") -> dict:
        """The workspace's sparse override map ``{field_id: {enabled, ...}}``.

        ``{}`` on missing doc, missing workspace, or ANY error — the caller
        then resolves the catalog default set (fail-soft; imports never break
        on the picker).
        """
        if not workspace_esid:
            return {}
        es = get_elasticsearch_client()
        if es is None:
            return {}
        try:
            doc = es.get(index=FIELD_CATALOG_INDEX, id=f"{workspace_esid}__{source}")
        except Exception as e:  # noqa: BLE001 — fail-soft read boundary (see module docstring)
            if "404" not in str(e) and "not_found" not in str(e).lower():
                logging.warning("FieldCatalogService.get_overrides(%s, %s): %s", workspace_esid, source, e)
            return {}
        fields = (doc.get("_source") or {}).get("fields") or {}
        return fields if isinstance(fields, dict) else {}

    @staticmethod
    def get_overrides_for_team(team_esid: str, source: str = "fb_ads") -> dict:
        """Importer entry point: resolve the team's workspace, return its
        overrides. ``{}`` (default set) when the team has no workspace or on
        any error — an unattributable team imports today's set."""
        workspace_esid = FieldCatalogService._workspace_for_team(team_esid)
        if not workspace_esid:
            return {}
        return FieldCatalogService.get_overrides(workspace_esid, source)

    @staticmethod
    def _workspace_for_team(team_esid: str) -> str | None:
        """Team → workspace: the team doc's own ``workspace_esid`` (
        keyword reindex), falling back to its org's ``workspace_esid``."""
        if not team_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            team = (es.get(index=TEAMS_INDEX, id=team_esid).get("_source")) or {}
        except Exception as e:  # noqa: BLE001 — fail-soft read boundary
            if "404" not in str(e) and "not_found" not in str(e).lower():
                logging.warning("FieldCatalogService._workspace_for_team(%s): %s", team_esid, e)
            return None
        direct = team.get("workspace_esid")
        if direct:
            return direct
        org_esid = team.get("organization_esid")
        if not org_esid:
            return None
        try:
            org = (es.get(index=ORGANIZATION_INDEX, id=org_esid).get("_source")) or {}
        except Exception as e:  # noqa: BLE001 — fail-soft read boundary
            if "404" not in str(e) and "not_found" not in str(e).lower():
                logging.warning("FieldCatalogService._workspace_for_team org(%s): %s", org_esid, e)
            return None
        return org.get("workspace_esid") or None

    @staticmethod
    def picker_fields(workspace_esid: str, source: str = "fb_ads") -> list[dict]:
        """The picker listing: every metric/action_metric catalog entry with
        its per-workspace state. Sorted by (kind, display_name)."""
        catalog = load_catalog(source)
        overrides = FieldCatalogService.get_overrides(workspace_esid, source)
        out = []
        for entry in catalog["fields"]:
            if entry.get("kind") not in PICKER_KINDS:
                continue
            field_id = entry["field_id"]
            override = overrides.get(field_id) or {}
            core = bool(entry.get("core"))
            mapped = bool(entry.get("mapped"))
            # action_metric children carry requested: false in the catalog (the
            # parent "actions"/"cost_per_action_type" request covers them) but
            # their values ARE collected and stored today — so their honest
            # default state is enabled. Same for breakdown toggles:
            # every tenant gets the demographics pull unless they opt out.
            default_on = bool(entry.get("requested")) or entry.get("kind") in ("action_metric", "breakdown")
            enabled = core or (override.get("enabled") if "enabled" in override else default_on)
            out.append(
                {
                    "field_id": field_id,
                    "display_name": entry.get("display_name") or field_id,
                    "description": entry.get("description") or "",
                    "kind": entry.get("kind"),
                    "enabled": bool(enabled),
                    "core": core,
                    "mapped": mapped,
                    "deprecated": bool(entry.get("deprecated")),
                    "window_only": (entry.get("aggregation") == "none") and "daily" not in (entry.get("mapped_in") or []),
                    "changed_at": override.get("changed_at"),
                    "changed_by": override.get("changed_by"),
                }
            )
        out.sort(key=lambda f: (f["kind"], f["display_name"].lower()))
        return out

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @staticmethod
    def set_field(
        workspace_esid: str,
        source: str,
        field_id: str,
        enabled: bool,
        username: str,
    ) -> dict:
        """Persist one toggle. Returns the field's new override object.

        Raises :class:`FieldNotToggleableError` for: unknown field_id, a field
        outside the picker families, a core field, enabling a deprecated field,
        or enabling an unmapped field (no ES home for the data yet).
        """
        if not workspace_esid:
            raise ValueError("workspace_esid must be non-empty")
        catalog = load_catalog(source)
        entry = next((e for e in catalog["fields"] if e.get("field_id") == field_id), None)
        if entry is None or entry.get("kind") not in PICKER_KINDS:
            raise FieldNotToggleableError(f"{field_id} is not a picker field")
        if entry.get("core"):
            raise FieldNotToggleableError(f"{field_id} is a core field and cannot be disabled")
        if enabled and entry.get("deprecated"):
            raise FieldNotToggleableError(f"{field_id} is deprecated and cannot be newly enabled")
        if enabled and not entry.get("mapped"):
            raise FieldNotToggleableError(f"{field_id} has no index mapping yet and cannot be enabled")

        es = get_elasticsearch_client()
        if es is None:
            raise FieldCatalogError("Elasticsearch unavailable")

        now = _now_iso()
        override = {"enabled": bool(enabled), "changed_at": now, "changed_by": username}
        doc_id = f"{workspace_esid}__{source}"
        # Partial-doc merge: ES merges the nested ``fields`` object, so one
        # toggle never clobbers another field's override.
        es.update(
            index=FIELD_CATALOG_INDEX,
            id=doc_id,
            doc={
                "workspace_esid": workspace_esid,
                "source": source,
                "fields": {field_id: override},
                "updated_at": now,
                "updated_by": username,
            },
            doc_as_upsert=True,
            refresh="wait_for",
        )
        return override

    # ------------------------------------------------------------------
    # Source toggles — per-team, per-source on/off
    # ------------------------------------------------------------------
    #
    # Per-team only (user decision 2026-07-16): a source is either explicitly
    # switched off for a team or ON. Default ON is behavior-preserving
    # (nothing silently stops importing) and the fail-soft answer on ANY error
    # — the toggle is config, never an import dependency. Toggle docs live in
    # de_asset_field_catalog under id "team__<team_esid>__<source>", disjoint
    # from the workspace field-picker docs' "<workspace_esid>__<source>" space.

    @staticmethod
    def _team_toggle_doc_id(team_esid: str, source: str) -> str:
        return f"team__{team_esid}__{source}"

    @staticmethod
    def _source_enabled_in_doc(doc_id: str) -> bool | None:
        """The ``source_enabled.enabled`` value in one toggle doc, or ``None``
        when the doc/key is absent or unreadable."""
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            doc = es.get(index=FIELD_CATALOG_INDEX, id=doc_id)
        except Exception as e:  # noqa: BLE001 — fail-soft read boundary (see module docstring)
            if "404" not in str(e) and "not_found" not in str(e).lower():
                logging.warning("FieldCatalogService._source_enabled_in_doc(%s): %s", doc_id, e)
            return None
        toggle = (doc.get("_source") or {}).get("source_enabled")
        if not isinstance(toggle, dict) or "enabled" not in toggle:
            return None
        return bool(toggle["enabled"])

    @staticmethod
    def source_enabled_for_team(team_esid: str, source: str) -> bool:
        """Importer entry point: is ``source`` on for this team?

        Explicit team override → its value; missing override / unknown team /
        ANY error → True (fail-soft, behavior-preserving)."""
        if not team_esid or source not in SOURCE_TOGGLE_SOURCES:
            return True
        state = FieldCatalogService._source_enabled_in_doc(FieldCatalogService._team_toggle_doc_id(team_esid, source))
        return True if state is None else state

    @staticmethod
    def get_team_source_states(team_esid: str) -> dict:
        """Effective on/off state for one team across every toggleable source
        (the picker dropdown reads this for the selected team). Missing
        override = ON."""
        return {s: FieldCatalogService.source_enabled_for_team(team_esid, s) for s in SOURCE_TOGGLE_SOURCES}

    @staticmethod
    def set_source_enabled(
        team_esid: str,
        source: str,
        enabled: bool,
        username: str,
        allowed_workspaces: list[str] | None = None,
    ) -> dict:
        """Persist one team's source toggle.

        ``allowed_workspaces`` (the caller's effective workspace allow-list;
        ``None`` = see-all superuser) bounds which teams the caller may edit —
        a team resolving to a workspace outside the list is rejected, matching
        the Data Connectors 404-on-foreign-team contract. Raises
        :class:`FieldCatalogError` for unknown sources, a team the caller can't
        edit, or ES unavailability.
        """
        if not team_esid:
            raise ValueError("team_esid must be non-empty")
        if source not in SOURCE_TOGGLE_SOURCES:
            raise FieldCatalogError(f"'{source}' is not a toggleable import source")

        workspace_esid = FieldCatalogService._workspace_for_team(team_esid)
        if allowed_workspaces is not None and workspace_esid not in allowed_workspaces:
            raise FieldCatalogError(f"Team {team_esid} is outside your accessible workspaces")

        es = get_elasticsearch_client()
        if es is None:
            raise FieldCatalogError("Elasticsearch unavailable")

        now = _now_iso()
        toggle = {"enabled": bool(enabled), "changed_at": now, "changed_by": username}
        es.update(
            index=FIELD_CATALOG_INDEX,
            id=FieldCatalogService._team_toggle_doc_id(team_esid, source),
            doc={
                "team_esid": team_esid,
                "workspace_esid": workspace_esid,
                "source": source,
                "scope": "team",
                "source_enabled": toggle,
                "updated_at": now,
                "updated_by": username,
            },
            doc_as_upsert=True,
            refresh="wait_for",
        )
        return toggle
