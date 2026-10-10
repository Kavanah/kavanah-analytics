"""Shared cron entitlement gate for the nightly importers.

The on-demand import kickoff (analytics routes) already refuses an
import-blocked user. This module brings the SAME entitlement to the 07:05
cron sweep for every connector, so a lapsed / canceled / comp-revoked owner's
scheduled imports are skipped too — not just Facebook's (which had its own
copy of this logic first; this is the generalized version the other importers
call).

Contract: each importer enumerates its teams as ``{"_id", "_source"}`` docs;
right after that it calls :func:`filter_entitled_teams(teams_docs)` and pulls
only the survivors. A team is dropped only when its owner is *definitively*
import-blocked.

FAIL-OPEN everywhere: a team with no ``connection_esid``, an unresolvable
principal, a missing service, or any error is KEPT (not blocked). A transient
lookup failure must never silently stop a paying team's nightly pull — the
page display may show lapsed, but the data is theirs and the next run catches
up. (Mirrors the on-demand gate's fail-open posture.)
"""

import logging
import sys

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client  # noqa: E402


# billing-block reason codes returned by ``source_billing_block``.
# "" == not blocked. The two non-empty values map 1:1 to the two classifier
# categories (lib.import_error_classifier) so the Report Scheduler shows distinct,
# actionable copy for each.
BLOCK_NONE = ""
BLOCK_NO_SUBSCRIPTION = "no_subscription"  # lapsed / canceled / comp_revoked (Condition A)
BLOCK_OVER_CAP = "over_cap"  # source is over the paid connection cap (Condition B)

# The raw strings ``record_billing_skip`` feeds the classifier. Kept distinctive so
# the classifier rules can't collide with unrelated importer errors (the
# lesson). Keep in sync with lib/import_error_classifier._RULES.
_BLOCK_RAW = {
    BLOCK_NO_SUBSCRIPTION: "billing: subscription is inactive — not imported",
    BLOCK_OVER_CAP: "billing: source is over the connection cap — not imported",
}


def resolve_team_principal(team_src):
    """Resolve a team doc's owning principal via its ``connection_esid`` →
    ``de_connection.principal``. Returns "" when unresolvable (legacy/operator
    team with no connection, missing principal, or any error). Fail-soft.
    """
    connection_esid = (team_src or {}).get("connection_esid")
    if not connection_esid:
        return ""
    try:
        es = get_elasticsearch_client()
        if es is None:
            return ""
        conn = es.get(index="de_connection", id=connection_esid, ignore=[404])
        return ((conn or {}).get("_source") or {}).get("principal") or ""
    except Exception as exc:  # noqa: BLE001 — boundary; fail soft
        logging.warning(
            "entitlement: principal lookup failed for connection_esid=%s: %s",
            connection_esid,
            exc,
        )
        return ""


def team_owner_import_blocked(team_src) -> bool:
    """Return True ONLY when the team's owner is definitively import-blocked.

    Resolves the team doc → its ``connection_esid`` → ``de_connection``'s
    ``principal`` → ``SubscriptionStateService.is_import_blocked``. Fail-open
    (returns False) on any missing/unresolvable/erroring step.
    """
    principal = resolve_team_principal(team_src)
    if not principal:
        # Legacy / operator team with no connection → can't resolve an owner; keep it.
        return False
    try:
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )

        return SubscriptionStateService.is_import_blocked(principal)
    except Exception as exc:  # noqa: BLE001 — boundary; fail OPEN
        logging.warning(
            "cron entitlement: owner check failed for principal=%s: %s — keeping team",
            principal,
            exc,
        )
        return False


def source_billing_block(
    *,
    principal: str = "",
    team_esid: str = "",
    asset_esid: str = "",
    provider_id: str = "",
) -> str:
    """the SHARED importer billing pre-check. Returns the reason a
    specific data source must NOT import, or "" when it may import.

    One source of truth for both billing-inactive conditions, callable from every
    connector importer (no Flask request context):

    * Condition A — ``BLOCK_NO_SUBSCRIPTION``: the owning principal is definitively
      import-blocked (lapsed / canceled / comp_revoked). ``past_due`` is a grace
      period and does NOT block (``is_import_blocked`` already excludes it).
    * Condition B — ``BLOCK_OVER_CAP``: the source is over the paid connection cap.
      Matches the source against ``inactive_source_keys_for_user`` (the SAME
      partition behind the Current Plan "Not imported" badge) by ANY
      identifier the importer has in scope — its real ``provider_id`` (GA4 property
      id, Google Ads customer id, YouTube channel id, IG profile id, FB ad-account
      id), its ``asset_esid`` (de_asset_* doc id), or its ``team_esid`` (for
      team-scoped connectors like echo that have no per-asset id).

    Resolution: pass ``principal`` directly when known; otherwise pass ``team_esid``
    (or the team's ``connection_esid`` via a team doc) and the owner is resolved the
    same way ``team_owner_import_blocked`` does. Condition A is checked first
    (whole-subscription block subsumes the per-source cap).

    FAILS OPEN: any unresolved owner / lookup error returns "" (the source imports)
    — a transient blip must never wrongly stop a paying source. Mirrors the existing
    cron-gate posture.
    """
    try:
        owner = (principal or "").strip()
        if not owner and team_esid:
            owner = resolve_team_principal(_team_src_for_esid(team_esid))
        if not owner:
            return BLOCK_NONE  # can't resolve an owner → fail open

        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )

        # Condition A — no active subscription blocks ALL of the owner's sources.
        if SubscriptionStateService.is_import_blocked(owner):
            return BLOCK_NO_SUBSCRIPTION

        # Condition B — over the connection cap (per-source, matches the badge).
        keys = SubscriptionStateService.inactive_source_keys_for_user(owner)
        pid = (provider_id or "").strip()
        aid = (asset_esid or "").strip()
        tid = (team_esid or "").strip()
        if (
            (pid and pid in keys["provider_ids"])
            or (aid and aid in keys["asset_esids"])
            or (tid and tid in keys["team_esids"])
        ):
            return BLOCK_OVER_CAP
        return BLOCK_NONE
    except Exception as exc:  # noqa: BLE001 — boundary; fail OPEN
        logging.warning(
            "source_billing_block failed (principal=%r team=%r asset=%r pid=%r): %s — allowing import",
            principal,
            team_esid,
            asset_esid,
            provider_id,
            exc,
        )
        return BLOCK_NONE


def _team_src_for_esid(team_esid):
    """Fetch a team doc's ``_source`` by esid (for resolving its owner). Returns
    {} on any miss/error. Internal helper for ``source_billing_block``."""
    if not team_esid:
        return {}
    try:
        es = get_elasticsearch_client()
        if es is None:
            return {}
        team = es.get(index="de_asset_teams", id=team_esid, ignore=[404])
        return (team or {}).get("_source") or {}
    except Exception as exc:  # noqa: BLE001 — boundary; fail soft
        logging.warning("team lookup failed for team_esid=%s: %s", team_esid, exc)
        return {}


def record_billing_skip(reason: str, *, connector_id: str | None = None, detail: str | None = None) -> None:
    """surface a billing-inactive source skip as a classified,
    user-actionable error on the active run (Report Scheduler "Errors" button).

    ``reason`` is one of ``BLOCK_NO_SUBSCRIPTION`` / ``BLOCK_OVER_CAP`` from
    :func:`source_billing_block`. Maps it to the distinctive raw string the
    classifier resolves to the matching billing category, then defers to
    ``lib.import_run_context.record_user_error`` — which is a NO-OP on the cron
    path (no ``progress_run_id`` bound), so the cron sweep stays silent exactly as
    before while a scheduled run shows the error. ``detail`` names the specific
    asset/team so the user knows which source to act on. Fail-soft.
    """
    raw = _BLOCK_RAW.get(reason)
    if not raw:
        return
    try:
        from lib.import_run_context import record_user_error

        record_user_error(raw, connector_id=connector_id, detail=detail)
    except Exception as exc:  # noqa: BLE001 — reporting must never abort the import
        logging.warning("record_billing_skip failed (reason=%s): %s", reason, exc)


def team_esid_import_blocked(team_esid) -> bool:
    """Like :func:`team_owner_import_blocked` but takes a team ESID (for
    importers that carry only the esid at their enumeration point — GA4,
    Google Ads, YouTube — not the full team doc). Fetches the team doc to read
    its ``connection_esid``, then defers to the same owner check. Fail-open on
    any missing/erroring step.
    """
    if not team_esid:
        return False
    try:
        es = get_elasticsearch_client()
        if es is None:
            return False
        team = es.get(index="de_asset_teams", id=team_esid, ignore=[404])
        return team_owner_import_blocked((team or {}).get("_source") or {})
    except Exception as exc:  # noqa: BLE001 — boundary; fail OPEN
        logging.warning(
            "cron entitlement: team lookup failed for team_esid=%s: %s — keeping team",
            team_esid,
            exc,
        )
        return False


def filter_entitled_teams(teams_docs, *, connector_id: str | None = None):
    """Drop teams whose owner is definitively import-blocked OR (for team-scoped
    connectors) whose team is over the paid connection cap. Returns the surviving
    ``{"_id", "_source"}`` docs. Logs how many were skipped so a gated nightly pull
    is visible. Fail-open per team.

    uses the shared :func:`source_billing_block` so the verdict matches
    the Current Plan "Not imported" badge, and surfaces each drop as a classified,
    user-actionable error via :func:`record_billing_skip` (no-op on the cron path).
    For team-scoped connectors (echo) the team IS the billing source, so passing
    ``team_esid`` catches BOTH the no-subscription (A) and over-cap (B) conditions
    here. For per-asset connectors (facebook / instagram) only the no-subscription
    condition resolves at team grain — their over-cap (per-source) check lives at
    the per-asset loop, since the over-cap partition matches FB/IG sources by asset
    id, never by team. ``connector_id`` labels the surfaced error.
    """
    if not teams_docs:
        return teams_docs
    kept = []
    skipped = 0
    for team_doc in teams_docs:
        team_esid = (team_doc or {}).get("_id")
        src = (team_doc or {}).get("_source") or {}
        block = source_billing_block(team_esid=team_esid)
        if block:
            skipped += 1
            logging.info(
                "billing pre-check: skipping team esid=%s (reason=%s).",
                team_esid,
                block,
            )
            record_billing_skip(
                block,
                connector_id=connector_id,
                detail=src.get("team_title") or team_esid,
            )
            continue
        kept.append(team_doc)
    if skipped:
        logging.info(
            "billing pre-check: %d team(s) skipped this run (billing-inactive); %d kept.",
            skipped,
            len(kept),
        )
    return kept
