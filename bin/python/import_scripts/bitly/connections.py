"""Phase 3 — resolve Bitly pull credentials from de_connection.

Enumerates the ``bitly_account`` connection_assets, joins each to its parent
``de_connection`` for the encrypted API token, and yields ready configs the
importer can pull with. The decrypted-token surface lives here (importer-only),
mirroring import_scripts/disciple_tools/connections.py.

Replaces the Phase 1 spike config (/app/reporting_configs/bitly.yaml) — team
attribution now comes from each connection's stored tag filter, and the token
is per-connection, decrypted at read time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from asset_management.web.services.connection_service import (
    ASSET_TYPE_BITLY_ACCOUNT,
    STATUS_ACTIVE,
    ConnectionService,
)


@dataclass
class BitlyConnectionConfig:
    team_esid: str
    team_title: str
    tags: set = field(default_factory=set)
    token: str = ""
    connection_esid: str = ""


def _resolve_team_title(team_esid: str) -> str:
    """The team's current display title, or "Unknown" when unresolvable.

    Read live from de_asset_teams (not stored on the connection) so a team
    rename doesn't leave stale titles on new click docs. Fail-soft.
    """
    try:
        from lib.es_helpers import get_document_by_id

        team = get_document_by_id("de_asset_teams", team_esid) or {}
        title = team.get("team_title")
        if isinstance(title, list):
            title = title[0] if title else ""
        return str(title or "").strip() or "Unknown"
    except Exception as e:  # noqa: BLE001 — fail-soft; the esid still routes the doc
        logging.warning("bitly: team title lookup failed for %s: %s", team_esid, e)
        return "Unknown"


def list_bitly_connection_configs() -> list[BitlyConnectionConfig]:
    """Return one config per configured, ACTIVE Bitly connection.

    Rows are skipped (with a warning) when the parent connection is missing,
    not active (revoked/expired — the Disconnect path), has no decryptable
    token, or carries no tags — a misconfigured connection must not abort the
    whole import, and a revoked connection must stop its team's import cleanly.
    """
    configs: list[BitlyConnectionConfig] = []
    for hit in ConnectionService.list_connection_assets_by_type(ASSET_TYPE_BITLY_ACCOUNT):
        src = hit.get("_source") or {}
        team_esid = src.get("provider_asset_id") or ""
        if not team_esid:
            # malformed data, not a routine skip: silently
            # unreachable by every import until someone notices.
            logging.warning("bitly: connection asset %s has no provider_asset_id (team) — skipping", hit.get("_id"))
            continue
        connection_esid = src.get("connection_esid") or ""
        tags = {str(t).strip() for t in ((src.get("metadata") or {}).get("tags") or []) if str(t).strip()}
        if not connection_esid or not tags:
            logging.warning("bitly: connection asset %s missing connection/tags — skipping", hit.get("_id"))
            continue

        conn = ConnectionService.get_connection(connection_esid)
        if not conn:
            logging.warning("bitly: team %s: parent connection %s missing — skipping", team_esid, connection_esid)
            continue
        if (conn.get("status") or "") != STATUS_ACTIVE:
            logging.warning("bitly: team %s: connection status=%s (not active) — skipping", team_esid, conn.get("status"))
            continue
        token = ConnectionService.get_decrypted_token(connection_esid)
        if not token:
            logging.warning("bitly: team %s: no decryptable token — skipping", team_esid)
            continue

        configs.append(
            BitlyConnectionConfig(
                team_esid=team_esid,
                team_title=_resolve_team_title(team_esid),
                tags=tags,
                token=token,
                connection_esid=connection_esid,
            )
        )
    logging.info("bitly: %d active connection(s)", len(configs))
    return configs
