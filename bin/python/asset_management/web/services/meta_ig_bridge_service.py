"""Meta Instagram-asset bridge.

Shared helper that walks a set of Facebook Pages to discover their linked
Instagram Business accounts, writes one ``de_asset_ig_accounts`` doc per
discovered account, and returns the new asset ESIDs so the caller can
stamp them onto the team's ``team_instagram_profile_esids`` back-link.

Two call sites share this helper:

* **Onboarding wizard step-7 finish handler** (``_bridge_instagram_assets``
  in ``blueprints/analytics/routes.py``) — discovers the user's Pages by
  walking the accepted ad-account IDs first, then calls
  :func:`bridge_ig_chain_for_pages`. The wrapper still owns the
  ad-account → page walk because the wizard's input shape is
  ``accepted_ids = [<ad_account_id>, ...]``, not page IDs.
* **Data Connectors page FB wizard** (see ``connector_wizard_service.fb_link_pages_to_team``)
  — calls this helper directly because the wizard's input shape already
  carries page IDs (the user picked Pages explicitly, no ad-account walk
  needed).

The helper is fail-soft: per-Page Graph errors and per-doc write errors
log at WARNING and the bridge continues processing remaining Pages. The
caller decides what to do with the returned ``ig_account_esids`` —
typically pair it with a ``team_instagram_profile_esids`` update.

Scope-gating is the **caller's** responsibility. The onboarding
wrapper reads ``instagram_basic`` off the de_connection doc and
short-circuits. The data-connectors path doesn't have a de_connection
row (legacy fb_app_user token chain) — it relies on this helper's
fail-soft per-Page handling: a partial-grant user hits ``None`` from
``get_instagram_business_account`` on every Page (the Graph response
omits the field) and the helper returns an empty list cleanly with no
401/403 noise.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app/python")

try:
    from web.services import meta_graph_client
except ImportError:  # pragma: no cover — production import path
    from . import meta_graph_client


def bridge_ig_chain_for_pages(
    *,
    access_token: str,
    pages: list[dict[str, str]],
    team_esid: str,
    workspace_esid: str | None = None,
    connection_esid: str | None = None,
    wizard_principal: str | None = None,
) -> dict[str, Any]:
    """Walk Pages → Instagram Business accounts and write IG asset docs.

    Parameters
    ----------
    access_token:
        Decrypted Meta OAuth access token with at least
        ``instagram_basic`` scope. Callers without scope visibility can
        still call — the helper fail-softs per-Page when the scope is
        missing (the Graph response omits the IG field rather than
        returning 401 for the typical legacy fb_app_user token).
    pages:
        ``[{"fb_page_id": "<graph_id>", "fb_page_name": "<title>"}]``.
        Either field can be empty / missing; an entry with no
        ``fb_page_id`` is silently skipped (nothing to look up).
    team_esid:
        ``de_asset_teams._id`` to stamp on each IG asset doc so the IG
        importer's stage-2 team filter can resolve back.
    workspace_esid:
        Optional ``de_asset_workspace._id``. Stamped on each IG asset doc
        when supplied so the workspace-tier resolver picks it up. None
        for legacy non-workspace teams.
    connection_esid:
        Optional ``de_connection._id``. Stamped on each IG asset doc for
        wizard-cohort users so the Manage Connection page can
        join from connection → IG assets. None for the data-connectors
        legacy fb_app_user token path.
    wizard_principal:
        Optional username for audit logging. None for system-initiated
        bridges.

    Returns
    -------
    ``{"ig_account_esids": list[str], "discovered_count": int}``.

    Notes
    -----
    Dedupes by Instagram account id so a page → IG link discovered
    twice (e.g. the same Page surfaced under two ad-accounts in the
    onboarding wizard) only produces one asset doc. The dedup is
    in-memory per call; callers running multiple bridges against
    the same team get separate dedup windows.
    """
    if not pages or not team_esid or not access_token:
        return {"ig_account_esids": [], "discovered_count": 0}

    try:
        from lib.es_helpers import get_elasticsearch_client
    except ImportError:  # pragma: no cover — production import path
        from ....lib.es_helpers import get_elasticsearch_client  # type: ignore[no-redef]

    es = get_elasticsearch_client()
    if es is None:
        logging.warning(
            "meta_ig_bridge: ES client unavailable for team=%s; skipping IG provisioning",
            team_esid,
        )
        return {"ig_account_esids": [], "discovered_count": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    discovered_esids: list[str] = []
    seen_ig_ids: set[str] = set()

    for page in pages:
        page_id = (page or {}).get("fb_page_id")
        page_name = (page or {}).get("fb_page_name") or page_id
        if not page_id:
            continue

        try:
            ig_account = meta_graph_client.get_instagram_business_account(access_token, str(page_id))
        except meta_graph_client.TokenExpiredError:
            # Re-raise so the caller can surface re-auth uniformly.
            raise
        except Exception as e:  # noqa: BLE001 — per-page fail-soft
            logging.warning(
                "meta_ig_bridge: get_instagram_business_account(page=%s) raised: %s",
                page_id,
                e,
            )
            continue

        if not ig_account:
            # Page has no linked IG Business account (common) OR token
            # lacks instagram_basic scope (partial-grant). Either way,
            # nothing to provision for this Page; move on.
            continue

        ig_id = ig_account.get("id")
        if not ig_id or ig_id in seen_ig_ids:
            continue
        seen_ig_ids.add(ig_id)

        ig_username = ig_account.get("username") or ""
        ig_name = ig_account.get("name") or ig_username or ig_id
        # point 4 — no team_esid on IG asset docs. The stamp was
        # write-only and misled ownership resolution (both contested
        # docs named the wrong team). Ownership authority is linked_page_id →
        # team_facebook_page_id; the cascade purges via workspace_esid.
        doc: dict[str, Any] = {
            "ig_account_id": ig_id,
            "ig_account_username": ig_username,
            "ig_account_title": ig_name,
            "linked_page_id": page_id,
            "linked_page_title": page_name,
            "@timestamp": now_iso,
        }
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        if connection_esid:
            doc["wizard_connection_esid"] = connection_esid
            doc["created_by_wizard"] = True
        if wizard_principal:
            doc["wizard_principal"] = wizard_principal

        try:
            resp = es.index(
                index="de_asset_ig_accounts",
                document=doc,
                refresh="wait_for",
            )
            esid = resp.get("_id")
            if esid:
                discovered_esids.append(esid)
            logging.info(
                "meta_ig_bridge: wrote de_asset_ig_accounts esid=%s team=%s ig=@%s (page=%s)",
                esid,
                team_esid,
                ig_username,
                page_id,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "meta_ig_bridge: de_asset_ig_accounts write failed team=%s ig=%s: %s",
                team_esid,
                ig_id,
                e,
            )

    return {
        "ig_account_esids": discovered_esids,
        "discovered_count": len(discovered_esids),
    }
