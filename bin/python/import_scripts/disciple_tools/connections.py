"""resolve DT-site pull credentials from de_connection.

Enumerates the ``dt_site`` connection_assets, joins each to its parent
``de_connection`` for the encrypted Site-to-Site Link token, and yields ready
configs the importer can hand to ``client.DTSiteClient``. The decrypted-token
surface lives here (importer-only), mirroring ConnectionService.get_decrypted_token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from asset_management.web.services.connection_service import (
    ASSET_TYPE_DT_SITE,
    STATUS_ACTIVE,
    ConnectionService,
)


@dataclass
class DTSiteConfig:
    team_esid: str
    dt_domain: str
    our_domain: str
    token: str
    connection_esid: str


def list_dt_site_configs(team_filter: set[str] | None = None) -> list[DTSiteConfig]:
    """Return one config per configured, active DT site.

    ``team_filter`` (a set of team_esids) limits the result; None = all sites.
    Rows are skipped (with a warning) when the parent connection is missing,
    revoked, or has no decryptable token, or when the metadata domains are
    absent — a misconfigured site shouldn't abort the whole import.
    """
    configs: list[DTSiteConfig] = []
    for hit in ConnectionService.list_connection_assets_by_type(ASSET_TYPE_DT_SITE):
        src = hit.get("_source") or {}
        team_esid = src.get("provider_asset_id") or ""
        if not team_esid:
            # was an unlogged bare continue. A dt_site asset row with no
            # provider_asset_id is malformed data, not a routine skip: it is silently
            # unreachable by every import until someone notices the missing team.
            logging.warning("DT site asset %s has no provider_asset_id (team) — skipping", hit.get("_id"))
            continue
        # NOTE: the team_filter skip below is deliberately NOT logged — it is how
        # scoping works, and logging it would emit one line per out-of-scope site
        # on every run. The caller raises when the filter matches nothing at all.
        if team_filter and team_esid not in team_filter:
            continue
        connection_esid = src.get("connection_esid") or ""
        meta = src.get("metadata") or {}
        dt_domain = meta.get("dt_domain") or ""
        our_domain = meta.get("our_domain") or ""
        if not (connection_esid and dt_domain and our_domain):
            logging.warning("DT site asset %s missing connection/domains — skipping", hit.get("_id"))
            continue

        conn = ConnectionService.get_connection(connection_esid)
        if not conn:
            logging.warning("DT site %s: parent connection %s missing — skipping", dt_domain, connection_esid)
            continue
        if (conn.get("status") or "") != STATUS_ACTIVE:
            logging.warning("DT site %s: connection status=%s (not active) — skipping", dt_domain, conn.get("status"))
            continue
        token = ConnectionService.get_decrypted_token(connection_esid)
        if not token:
            logging.warning("DT site %s: no decryptable token — skipping", dt_domain)
            continue

        configs.append(
            DTSiteConfig(
                team_esid=team_esid,
                dt_domain=dt_domain,
                our_domain=our_domain,
                token=token,
                connection_esid=connection_esid,
            )
        )
    logging.info("DT pull: %d configured site(s)", len(configs))
    return configs
