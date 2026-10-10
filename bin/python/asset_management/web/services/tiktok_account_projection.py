"""Phase 6 — the Asset Explorer's view of TikTok accounts.

``de_connection`` is the source of truth for a TikTok account (token, status,
open_id). The Asset Explorer and the team page's "TikTok Accounts" chips read
``de_asset_tiktok_accounts`` instead: one doc per TikTok connection whose doc id
IS the connection esid. Because the ids match, a team's
``team_tiktok_connection_esids`` list resolves straight to these docs — the
team-page chips and the Explorer's reference cleanup work with no extra mapping.

Only code writes this index (the Explorer type is read-only): :func:`sync` is
called after every change to a TikTok connection and recomputes the doc from
``de_connection`` + its ``tiktok_account`` asset row, so a missed call is fixed
by the next one. ``tools/rebuild_tiktok_account_projection.py`` rebuilds every
doc and drops orphans. Nothing here holds a token.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

INDEX = "de_asset_tiktok_accounts"


def _account_names(connection_esid: str) -> tuple[str, str]:
    """``(username, display_name)`` from the connection's tiktok_account row."""
    from asset_management.web.services.connection_service import ASSET_TYPE_TIKTOK_ACCOUNT, ConnectionService

    for hit in ConnectionService.list_assets_for_connection(connection_esid):
        src = hit.get("_source") or {}
        if src.get("asset_type") == ASSET_TYPE_TIKTOK_ACCOUNT:
            meta = src.get("metadata") or {}
            return str(meta.get("username") or ""), str(meta.get("display_name") or "")
    return "", ""


def build_doc(connection_esid: str, conn: dict, username: str, display_name: str) -> dict:
    """The projected fields for one TikTok connection (pure)."""
    open_id = str(conn.get("provider_account_id") or "")
    if username:
        title = f"@{username}"
    elif display_name:
        title = display_name
    else:
        title = conn.get("label") or open_id or connection_esid
    return {
        "tiktok_account_title": title,
        "tiktok_username": username,
        "tiktok_display_name": display_name,
        "tiktok_open_id": open_id,
        "tiktok_connection_status": conn.get("status") or "",
        "tiktok_authorized_by": conn.get("principal") or "",
        "tiktok_refresh_expires_at": conn.get("token_expires_at") or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _ensure_index(es) -> None:
    from lib.es_helpers import create_index_if_not_exists
    from migration.schema_loader import get_schema_for_asset_index

    create_index_if_not_exists(es, INDEX, get_schema_for_asset_index(INDEX))


def sync(connection_esid: str, *, workspace_esid: str | None = None, last_snapshot_date: str | None = None) -> bool:
    """Recompute the projection doc for one TikTok connection.

    ``workspace_esid`` / ``last_snapshot_date`` are only known to some callers
    (configure / the importer); None keeps whatever the doc already has. A
    connection that no longer exists — or is still an unauthorized wizard
    placeholder — has no doc. Never raises: the caller's own write has already
    happened and a stale projection is fixed by the next sync or the rebuild tool.
    """
    if not connection_esid:
        return False
    try:
        from asset_management.web.services.connection_service import (
            PENDING_ACCOUNT_ID_PREFIX,
            PROVIDER_TIKTOK,
            ConnectionService,
        )
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return False
        conn = ConnectionService.get_connection(connection_esid)
        if not conn or str(conn.get("provider_account_id") or "").startswith(PENDING_ACCOUNT_ID_PREFIX):
            if es.indices.exists(index=INDEX) and es.exists(index=INDEX, id=connection_esid):
                es.delete(index=INDEX, id=connection_esid, refresh="wait_for")
            return True
        if conn.get("provider") != PROVIDER_TIKTOK:
            logging.warning("tiktok projection: %s is not a TikTok connection — not projected", connection_esid)
            return False

        _ensure_index(es)
        username, display_name = _account_names(connection_esid)
        doc = build_doc(connection_esid, conn, username, display_name)
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        if last_snapshot_date:
            doc["tiktok_last_snapshot_date"] = last_snapshot_date
        es.update(index=INDEX, id=connection_esid, doc=doc, doc_as_upsert=True, refresh="wait_for")
        return True
    except Exception as e:  # noqa: BLE001 — see docstring
        logging.warning("tiktok projection: sync failed for %s: %s", connection_esid, e)
        return False
