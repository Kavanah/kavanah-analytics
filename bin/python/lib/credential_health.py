"""Durable credential/asset health state.

Importers already classify reconnect-class failures per run onto
``de_import_progress`` via ``lib.import_run_context.record_user_error`` — but that
state is per-run and ages out, so nobody can answer "is this connection broken
right now?". This module is the durable answer.

Where the state lives, and why not on the connection
----------------------------------------------------
 describes health "on the connection". Prod recon on 2026-08-27 showed
that would monitor the wrong thing:

* all 38 ``ds_facebook`` teams import via ``de_asset_fb_app_users`` (12 rows, every
  one a valid SYSTEM_USER token with no expiry and no data-access clock);
* the 25 ``de_connection`` facebook rows carry real 67–88 day data-access clocks
  but drive no team imports at all.

Health written only onto ``de_connection`` would therefore watch credentials that
do nothing while staying blind to the ones doing all the work. So state lives in
its own index keyed by ``(source_index, source_esid)`` — credential-agnostic, so
it covers both families today and GA4/YouTube tomorrow, mutates neither existing
schema, and can be dropped wholesale if the design is rejected.

**This is a deliberate departure from the ticket's literal wording and wants a
second opinion at review.**

States
------
``healthy``           — probed and usable.
``renewal_required``  — Meta's 90-day data-access window is closing or closed.
                        Only a human re-auth resets it; no automated path exists.
``token_dead``        — Meta rejects the credential (OAuth 190 / is_valid false).
``permission_lost``   — the credential lives but cannot see one or more assets.
``unknown``           — could not be determined (e.g. Graph unreachable).

``expiring_soon`` is deliberately absent. It would key off ``expires_at``, and
every stored Meta token reports no ``expires_at`` at all — the state could never
fire, and shipping a status that is structurally unreachable is worse than not
having it (CLAUDE.md #22: a check that cannot fail is not a check).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from asset_management.web.services.data_quality_service import _CONTINUITY_STALL_DAYS
from lib.credential_prober import (
    PERMISSION_CODES,
    RECONNECT_CODES,
    TRANSIENT_CODES,
    AssetProbe,
    TokenProbe,
)
from lib.es_helpers import (
    create_index_if_not_exists,
    ensure_mapping_fields,
    get_document_by_id,
    get_elasticsearch_client,
)

INDEX = "de_credential_health"

STATUS_HEALTHY = "healthy"
STATUS_RENEWAL_REQUIRED = "renewal_required"
STATUS_TOKEN_DEAD = "token_dead"
STATUS_PERMISSION_LOST = "permission_lost"
STATUS_UNKNOWN = "unknown"

UNHEALTHY_STATUSES = (STATUS_RENEWAL_REQUIRED, STATUS_TOKEN_DEAD, STATUS_PERMISSION_LOST)

# Days of runway before a closing data-access window becomes actionable.
#
# 21, not the 5 used by the token-refresh job:
# that job is machine-to-machine and self-healing, whereas this needs a HUMAN —
# often a client's admin — to click through an OAuth dialog. Three weeks is what
# it takes to reach someone, get them to the right Business Manager, and have
# them tick the right assets — with the ~36-team November batch landing in one
# fortnight, two weeks left no slack at all.
RENEWAL_THRESHOLD_DAYS = 21

# Classifier category -> health status, for the importer-side hook. Categories
# come from lib.import_error_classifier; anything not listed is not health-bearing
# (rate limits, no_data, lock contention are transient or informational).
# per-asset CAUSE of a failed probe. ``permission_lost`` names the
# credential's state; it does not say what to do about each asset, and the
# three things that put an asset there have three different fixes and three
# different people who can apply them:
#
#   permission_missing — the token is alive but Meta will not let it see THIS
#                        asset (codes 10/200/...). Fix: grant the permission /
#                        assign the asset in Meta Business Manager. Who: a
#                        Business Manager admin; then whoever holds the
#                        connection reconnects so the grant is picked up.
#   asset_gone         — no connection we hold can reach the asset (code 100 on
#                        a token whose scopes could see that asset type, AND
# imports for it have stopped — the IG case).
# a 100 reads identically whether the asset was
#                        deleted or is merely no longer assigned to us, so this
#                        class means UNREACHABLE, not gone. Fix: check it at
#                        Meta (Business Settings → Assigned assets); re-assign
#                        it, or unlink it in DEAM only if it is truly absent.
#                        Who: a workspace administrator. A reconnect cannot help.
#   token_dead         — the token itself is rejected, so every asset behind it
#                        fails as a consequence. Fix: reconnect. Who: the
#                        principal, or a workspace administrator.
#
# The classifier fails TOWARD ``permission_missing``: the reconnect it invites
# is reversible and sometimes works, whereas telling someone to unlink an asset
# that merely lost a scope would throw away a working configuration. Only the
# conjunction "token could see this type" AND "nothing has imported" earns
# ``asset_gone``.
CAUSE_PERMISSION_MISSING = "permission_missing"
CAUSE_ASSET_GONE = "asset_gone"
CAUSE_TOKEN_DEAD = "token_dead"
CAUSES = (CAUSE_PERMISSION_MISSING, CAUSE_ASSET_GONE, CAUSE_TOKEN_DEAD)

# Graph codes that read "no such object / unsupported get" rather than "not
# allowed": Meta answers 100 both for an id that no longer exists and for one
# the token has no path to. Scopes + import recency disambiguate (see above).
ASSET_GONE_CODES = {100}

# "Imports had already stopped" for the asset_gone conjunction: the asset's
# newest imported doc must be at least this many days OLDER than the moment the
# credential first went unhealthy. The same window as the FB continuity check,
# and the same constant — one definition, so the two cannot drift apart.
IMPORT_STALL_DAYS = _CONTINUITY_STALL_DAYS

# ANY of these on the token means it could see assets of that type, so a code
# 100 is not a missing scope. A page's ``id,name`` is public — a live token
# never needs a scope for it — so any RECORDED scope set reaches pages. An
# empty / absent scope list is "unknown" for every type, pages included: it
# means /debug_token told us nothing, not that the token could see the asset.
REACH_SCOPES: dict[str, tuple[str, ...]] = {
    "page": (),
    "ig": ("instagram_basic", "instagram_manage_insights"),
    "ad_account": ("ads_read", "ads_management"),
}

CATEGORY_TO_STATUS = {
    "connection_expired": STATUS_TOKEN_DEAD,
    "connection_revoked": STATUS_TOKEN_DEAD,
    "missing_credentials": STATUS_TOKEN_DEAD,
    "dt_auth": STATUS_TOKEN_DEAD,
    "permission_denied": STATUS_PERMISSION_LOST,
    "youtube_channel_access_denied": STATUS_PERMISSION_LOST,
    "wrong_connection": STATUS_PERMISSION_LOST,
}

MAPPING = {
    "properties": {
        "source_index": {"type": "keyword"},
        "source_esid": {"type": "keyword"},
        "provider": {"type": "keyword"},
        "label": {"type": "keyword"},
        # Resolved at WRITE time, not read time: the banner runs in a Flask
        # context processor on every page render, so it cannot afford a
        # credential->workspace join per request.
        "workspace_esid": {"type": "keyword"},
        "principal": {"type": "keyword"},
        # how the credential authenticates ("system_user" vs OAuth).
        # The banner keys its regrant branch on this, so it must reach the health
        # row: without it, a dead system-user token would render a Reconnect CTA
        # that silently downgrades the connection.
        "token_kind": {"type": "keyword"},
        # what /debug_token said the token may do. The per-asset cause
        # needs it to tell "no scope for IGs" from "this IG is gone"; without it
        # on the row the banner would have to re-probe Meta on every render.
        "scopes": {"type": "keyword"},
        "status": {"type": "keyword"},
        "reason": {"type": "text"},
        "token_type": {"type": "keyword"},
        "app_id": {"type": "keyword"},
        "user_id": {"type": "keyword"},
        "expires_at": {"type": "date", "format": "epoch_second"},
        "data_access_expires_at": {"type": "date", "format": "epoch_second"},
        "days_until_data_access_expiry": {"type": "integer"},
        "covered_assets": {"type": "integer"},
        "total_assets": {"type": "integer"},
        "assets": {
            "type": "nested",
            "properties": {
                "asset_type": {"type": "keyword"},
                "asset_id": {"type": "keyword"},
                "asset_name": {"type": "keyword"},
                "ok": {"type": "boolean"},
                "error": {"type": "text"},
                "error_code": {"type": "integer"},
                "caused_by": {"type": "keyword"},
                "covered_by": {"type": "keyword"},
                # permission_missing / asset_gone / token_dead, or ""
                # for an ok / downstream / covered / transport-blip probe.
                "cause": {"type": "keyword"},
                # yyyy-MM-dd of the newest doc stamped with this asset's id in its
                # family's indices. ABSENT when nothing has ever imported for it
                # or when recency is unknown — never "": a keyword that is
                # sometimes a date and sometimes "" is what ES date-detection
                # turns into a mapper_parsing_exception on a dynamically-mapped
                # index (the review finding on this PR).
                "last_import_at": {"type": "keyword"},
            },
        },
        "last_probed_at": {"type": "date"},
        "last_ok_at": {"type": "date"},
        "unhealthy_since": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def doc_id(source_index: str, source_esid: str) -> str:
    """Stable identity for a credential's health row.

    Keyed on the credential's SLOT, not on any human or label, so a renamed app
    or a re-stamped principal keeps its history.
    """
    return f"{source_index}:{source_esid}"


def _ensure_index(es) -> bool:
    """Create the index if missing, then add-only put_mapping any MAPPING field
    the live index lacks (added ``scopes``, ``assets.cause``,
    ``assets.last_import_at``, ``assets.covered_by`` to an index prod already
    had). ``create_index_if_not_exists`` never touches an existing index, and
    the web app's ``ensure_indices_exist()`` services do not cover this one, so
    without the second step the new fields would be dynamically mapped by the
    first sweep after deploy. Idempotent; a heal failure is logged, not fatal.
    """
    if not create_index_if_not_exists(es, INDEX, MAPPING):
        return False
    ensure_mapping_fields(es, INDEX, MAPPING)
    return True


def derive_status(
    token: TokenProbe,
    assets: list[AssetProbe] | None = None,
    *,
    renewal_threshold_days: int = RENEWAL_THRESHOLD_DAYS,
) -> tuple[str, str]:
    """Map a probe result onto ``(status, reason)``.

    Precedence is deliberate:

    1. unreachable  -> ``unknown``. A network blip must never be read as "dead";
       callers refuse to downgrade a known-good row on ``unknown``.
    2. rejected     -> ``token_dead``. Nothing else matters if Meta says no.
    3. data access  -> ``renewal_required``. Checked BEFORE per-asset failures,
       because once the window closes the assets fail *as a consequence* —
       reporting those as ``permission_lost`` would name the symptom, not the cause.
    4. asset denied -> ``permission_lost``.
    5. otherwise    -> ``healthy``.
    """
    assets = assets or []

    if not token.reachable:
        return STATUS_UNKNOWN, token.error or "Graph API unreachable"

    if token.error_code in RECONNECT_CODES or (token.reachable and not token.is_valid):
        return STATUS_TOKEN_DEAD, token.error or "Meta rejected this credential"

    days = token.days_until_data_access_expiry
    if days is not None and days <= renewal_threshold_days:
        if days < 0:
            return (
                STATUS_RENEWAL_REQUIRED,
                f"Data access expired {abs(days)} day(s) ago — only re-authorising restores it.",
            )
        # say what resets the clock, not just that it is running out:
        # a re-auth from the connection page restarts Meta's 90-day window.
        return (
            STATUS_RENEWAL_REQUIRED,
            f"Data access expires in {days} day(s) — re-authorising from the connection page resets the 90-day window.",
        )

    # Count ROOT-CAUSE denials only:
    #   * ``caused_by`` set  -> downstream of another asset's failure (IG behind a
    #     failed Page). Counting it would double-report one problem as two.
    #   * ``error_code`` None -> a transport blip, not Meta refusing us. Treating a
    #     dropped connection as "permission lost" would invent a fault.
    #   * ``covered_by`` set -> another credential the importers try for this asset
    # reaches it. The importer stops at the first token that works,
    #     so the asset imports; a fallback token that cannot see it is not a fault.
    denied = [
        a for a in assets if not a.ok and not a.caused_by and not getattr(a, "covered_by", "") and a.error_code is not None
    ]
    if denied:
        names = ", ".join((a.asset_name or a.asset_id) for a in denied[:5])
        more = f" (+{len(denied) - 5} more)" if len(denied) > 5 else ""
        return (
            STATUS_PERMISSION_LOST,
            f"Cannot reach {len(denied)} asset(s): {names}{more}",
        )

    covered = [a for a in assets if not a.ok and getattr(a, "covered_by", "")]
    if covered:
        # Healthy, but say why the failures do not count — the operator reading
        # the row should not have to rediscover the importer ladder.
        return (
            STATUS_HEALTHY,
            f"{len(covered)} asset(s) this token cannot reach are imported through another credential",
        )

    return STATUS_HEALTHY, ""


def _token_can_see(asset_type: str, scopes: list[str] | None) -> bool | None:
    """Could a token with ``scopes`` see assets of ``asset_type`` at all?

    ``None`` when we cannot tell: no scope list was recorded (an older health
    row, or /debug_token returned none). The caller treats None as "not
    proven", which keeps the classifier on the permission_missing side.
    """
    need = REACH_SCOPES.get(str(asset_type or ""))
    if need is None or not scopes:
        return None
    if not need:
        return True
    have = {str(x) for x in scopes}
    return any(x in have for x in need)


def _stalled_before_onset(last_import_at: str | None, unhealthy_since: str | None) -> bool | None:
    """Had this asset's imports ALREADY stopped when the credential first failed?

    The asset_gone conjunction needs a signal independent of the permission,
    because losing the assignment is itself what stops the imports: measured
    against today, every code-100 loss reads "stopped" from day 4 on and would
    flip to unlink advice. Measured against ``unhealthy_since`` — fixed at the
    onset, never moving — an asset whose newest doc is IMPORT_STALL_DAYS+ older
    than the onset was dark before the permission problem existed, so the
    problem did not cause the stall: the asset was already gone.

    ``last_import_at`` is yyyy-MM-dd or "" (never imported); ``unhealthy_since``
    is the ISO timestamp the row carries. None for unknown on either side, and
    for "never imported": a brand-new team's asset has no history to place
    before the onset, so it cannot be proven gone. The classifier reads None as
    "not proven" and stays on permission_missing.

    Known limit: an assignment lost BEFORE the probe existed also
    reads as stalled-before-onset, because the onset we know is the first probe.
    """
    if not last_import_at or not unhealthy_since:
        return None
    try:
        last = datetime.strptime(str(last_import_at)[:10], "%Y-%m-%d").date()
        onset = datetime.strptime(str(unhealthy_since)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (onset - last).days >= IMPORT_STALL_DAYS


def classify_asset_failure(
    asset: dict[str, Any],
    *,
    token_dead: bool,
    scopes: list[str] | None,
    last_import_at: str | None,
    unhealthy_since: str | None = None,
) -> str:
    """which of the three causes explains this failed asset probe?

    Returns "" for probes that are not a root-cause loss: ``ok``, downstream of
    another asset (``caused_by``), imported through another credential
    (``covered_by``), a transport blip (``error_code`` None) — the same
    exclusions :func:`derive_status` applies — or one of Meta's transient codes
    (rate limit / retry), which say nothing about who must act.

    Precedence: a dead token explains everything behind it, so it wins; then
    Meta's own permission codes; then the asset_gone conjunction on a code 100
    (token could see the type AND imports had stalled before the credential's
    onset); everything else is permission_missing (the reversible default).
    """
    if asset.get("ok") or asset.get("caused_by") or asset.get("covered_by"):
        return ""
    code = asset.get("error_code")
    if token_dead or code in RECONNECT_CODES:
        return CAUSE_TOKEN_DEAD
    if code is None or code in TRANSIENT_CODES:
        return ""
    if code in PERMISSION_CODES:
        return CAUSE_PERMISSION_MISSING
    if code in ASSET_GONE_CODES:
        can_see = _token_can_see(str(asset.get("asset_type") or ""), scopes)
        stalled = _stalled_before_onset(last_import_at, unhealthy_since)
        if can_see is True and stalled is True:
            return CAUSE_ASSET_GONE
    return CAUSE_PERMISSION_MISSING


def classify_assets(
    token: TokenProbe,
    assets: list[AssetProbe],
    import_recency: dict[tuple[str, str], str] | None = None,
    *,
    unhealthy_since: str | None = None,
) -> list[dict[str, Any]]:
    """Per-asset dicts for the health row, each stamped with ``cause`` and —
    when it has one — ``last_import_at``. ``import_recency`` maps
    ``(asset_type, asset_id)`` to the newest imported doc date, "" for never
    imported; an asset absent from the map — or a None map — has unknown
    recency. The date is written only when it exists: never "" (see MAPPING).
    ``unhealthy_since`` is the onset the row will carry; the asset_gone signal
    is "stalled before it".
    """
    dead = bool(token.error_code in RECONNECT_CODES or (token.reachable and not token.is_valid))
    out: list[dict[str, Any]] = []
    for a in assets:
        d = a.as_dict()
        key = (str(d.get("asset_type") or ""), str(d.get("asset_id") or ""))
        last = import_recency.get(key) if import_recency is not None else None
        if last:
            d["last_import_at"] = last
        d["cause"] = classify_asset_failure(
            d, token_dead=dead, scopes=token.scopes, last_import_at=last, unhealthy_since=unhealthy_since
        )
        out.append(d)
    return out


def apply_cross_credential_coverage(runs: list[tuple[str, list[AssetProbe]]]) -> int:
    """mark failed asset probes that another credential covers.

    ``runs`` is every credential probed in one sweep as ``(label, asset_probes)``.
    A credential's asset scope (``credential_asset_scope``) is exactly the set of
    assets the importers would try that credential for, and every importer walks
    its ladder and stops at the first token that works. So when asset A fails on
    credential X but probes ``ok`` on credential Y in the same sweep, A still
    imports — X's failure is annotated ``covered_by=<Y's label>`` and stops
    driving ``permission_lost`` (see :func:`derive_status`). An asset no probed
    credential reaches keeps every failure, so a real loss still alerts.

    ``ig_importability`` already applied this rule for the annotation
    ("ANY success imports"); this is the same rule at the credential level, so
    the banner and the annotation cannot disagree.

    Mutates the probes in place; returns how many were annotated.
    """
    reached_by: dict[tuple[str, str], str] = {}
    for label, probes in runs:
        for a in probes:
            if a.ok:
                reached_by.setdefault((a.asset_type, a.asset_id), label)
    annotated = 0
    for label, probes in runs:
        for a in probes:
            if a.ok or a.covered_by:
                continue
            other = reached_by.get((a.asset_type, a.asset_id))
            if other and other != label:
                a.covered_by = other
                annotated += 1
    return annotated


def record_probe(
    *,
    source_index: str,
    source_esid: str,
    provider: str,
    label: str,
    token: TokenProbe,
    assets: list[AssetProbe] | None = None,
    workspace_esid: str = "",
    principal: str = "",
    token_kind: str = "",
    renewal_threshold_days: int = RENEWAL_THRESHOLD_DAYS,
    import_recency: dict[tuple[str, str], str] | None = None,
) -> dict[str, Any] | None:
    """Persist a probe result. Returns the stored doc, or None on failure.

    ``import_recency`` feeds the per-asset cause; the sweep passes
    ``AssetScope.import_recency()``. None means "unknown", never "stopped".

    Self-clearing is implicit: a healthy probe overwrites whatever was there and
    clears ``unhealthy_since``. ``unknown`` never overwrites a known status —
    otherwise one unreachable Graph call would wipe the fleet's health.
    """
    assets = assets or []
    status, reason = derive_status(token, assets, renewal_threshold_days=renewal_threshold_days)

    es = get_elasticsearch_client()
    if es is None:
        logging.error("credential_health: no ES client; probe not recorded")
        return None
    if not _ensure_index(es):
        return None

    did = doc_id(source_index, source_esid)
    prior = get_document_by_id(INDEX, did) or {}

    if status == STATUS_UNKNOWN and prior.get("status") and prior["status"] != STATUS_UNKNOWN:
        # Keep the last known verdict; just record that we tried and failed.
        logging.info(
            "credential_health: probe of %s unreachable (%s) — keeping prior status %r",
            did,
            reason,
            prior["status"],
        )
        doc = dict(prior)
        doc["last_probed_at"] = _now()
        doc["updated_at"] = _now()
        doc["reason"] = f"{prior.get('reason') or ''} (last probe unreachable: {reason})".strip()
        es.index(index=INDEX, id=did, body=doc, refresh="wait_for")
        return doc

    now = _now()
    healthy = status == STATUS_HEALTHY
    unhealthy_since = None
    if not healthy and status != STATUS_UNKNOWN:
        # Preserve the original onset so "broken for 12 days" is answerable.
        unhealthy_since = prior.get("unhealthy_since") or now

    doc = {
        "source_index": source_index,
        "source_esid": source_esid,
        "provider": provider,
        "label": label,
        "workspace_esid": workspace_esid or prior.get("workspace_esid") or "",
        "principal": principal or prior.get("principal") or "",
        "token_kind": token_kind or prior.get("token_kind") or "",
        "scopes": list(token.scopes or []),
        "status": status,
        "reason": reason,
        "token_type": token.token_type,
        "app_id": token.app_id,
        "user_id": token.user_id,
        "expires_at": token.expires_at,
        "data_access_expires_at": token.data_access_expires_at,
        "days_until_data_access_expiry": token.days_until_data_access_expiry,
        "covered_assets": sum(1 for a in assets if a.ok),
        "total_assets": len(assets),
        "assets": classify_assets(token, assets, import_recency, unhealthy_since=unhealthy_since),
        "last_probed_at": now,
        "last_ok_at": now if healthy else prior.get("last_ok_at"),
        "unhealthy_since": unhealthy_since,
        "updated_at": now,
        "created_at": prior.get("created_at") or now,
    }
    try:
        es.index(index=INDEX, id=did, body=doc, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001
        logging.error("credential_health: failed to write %s: %s", did, exc)
        return None
    return doc


def record_classified_error(
    *,
    source_index: str,
    source_esid: str,
    provider: str,
    label: str,
    category: str,
    detail: str = "",
    workspace_esid: str = "",
    principal: str = "",
) -> dict[str, Any] | None:
    """Set health from a classified error raised by a REAL import run.

    The scheduled probe answers "is the credential usable in principle"; this
    answers "did it actually fail while importing". A category with no health
    meaning (rate limits, no_data) is ignored rather than guessed at.

    ``workspace_esid`` / ``principal`` — the banner is workspace-scoped
    and fails closed on a row without a workspace, so a credential no probe ever
    visits (TikTok) must stamp them here. Empty keeps whatever the row had.
    """
    status = CATEGORY_TO_STATUS.get(category)
    if not status:
        return None

    es = get_elasticsearch_client()
    if es is None or not _ensure_index(es):
        return None

    did = doc_id(source_index, source_esid)
    prior = get_document_by_id(INDEX, did) or {}
    now = _now()
    doc = dict(prior)
    doc.update(
        {
            "source_index": source_index,
            "source_esid": source_esid,
            "provider": provider,
            "label": label or prior.get("label") or "",
            "workspace_esid": workspace_esid or prior.get("workspace_esid") or "",
            "principal": principal or prior.get("principal") or "",
            "status": status,
            "reason": f"Import run reported {category}" + (f": {detail}" if detail else ""),
            "unhealthy_since": prior.get("unhealthy_since") or now,
            "updated_at": now,
            "created_at": prior.get("created_at") or now,
        }
    )
    try:
        es.index(index=INDEX, id=did, body=doc, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001
        logging.error("credential_health: failed to write %s: %s", did, exc)
        return None
    return doc


def record_healthy(
    *,
    source_index: str,
    source_esid: str,
    provider: str,
    label: str,
    workspace_esid: str = "",
    principal: str = "",
    reason: str = "Import run succeeded",
) -> dict[str, Any] | None:
    """the self-clearing half of :func:`record_classified_error` for a
    credential that only real use (an import, a reconnect) ever checks.

    Writes ``healthy`` and clears ``unhealthy_since``, so the banner drops the row
    on its next cache cycle. Only for families no probe sweep owns: a probe's
    row carries per-asset detail that this plain status would overwrite.
    """
    es = get_elasticsearch_client()
    if es is None or not _ensure_index(es):
        return None
    did = doc_id(source_index, source_esid)
    prior = get_document_by_id(INDEX, did) or {}
    now = _now()
    doc = dict(prior)
    doc.update(
        {
            "source_index": source_index,
            "source_esid": source_esid,
            "provider": provider,
            "label": label or prior.get("label") or "",
            "workspace_esid": workspace_esid or prior.get("workspace_esid") or "",
            "principal": principal or prior.get("principal") or "",
            "status": STATUS_HEALTHY,
            "reason": reason,
            "last_ok_at": now,
            "unhealthy_since": None,
            "updated_at": now,
            "created_at": prior.get("created_at") or now,
        }
    )
    try:
        es.index(index=INDEX, id=did, body=doc, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001
        logging.error("credential_health: failed to write %s: %s", did, exc)
        return None
    return doc


def get_health(source_index: str, source_esid: str) -> dict[str, Any] | None:
    return get_document_by_id(INDEX, doc_id(source_index, source_esid))


def health_for_fb_team(team_esid: str) -> list[dict[str, Any]]:
    """Every credential health row reachable from one Facebook team.

    Walks the SAME ladder the importer authenticates through, so the attribution
    a user sees matches the credential that actually failed them:

    * wizard cohort — ``de_asset_teams.wizard_connection_esid`` -> ``de_connection``
    * legacy chain  — team -> fb_pages -> fb_account -> fb_app -> fb_app_users
      (via ``resolve_team_filter_scope``, the importer's own resolver)

    Returns [] when nothing resolves. An empty list is meaningful and must not be
    rendered as "healthy": it means we could not attribute, which is a different
    statement from "the credentials are fine" — see the caller in
    ``data_quality_service``, which says so explicitly rather than implying either.
    """
    if not team_esid:
        return []

    es = get_elasticsearch_client()
    if es is None:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(source_index: str, source_esid: str) -> None:
        did = doc_id(source_index, source_esid)
        if did in seen:
            return
        seen.add(did)
        doc = get_document_by_id(INDEX, did)
        if doc:
            rows.append(doc | {"_id": did})

    team = get_document_by_id("de_asset_teams", team_esid) or {}
    # Verified against the live de_asset_teams mapping 2026-08-27, not assumed:
    # ``connection_esid`` is the populated link field (13/35 ds_facebook teams).
    # ``wizard_connection_esid`` — which get_page_insights.py reads — is present
    # on ZERO teams, so relying on it alone would resolve nothing. Both are
    # checked: the real one first, the importer's forward-looking one second so
    # this keeps working if that cohort ever lands.
    for field in ("connection_esid", "wizard_connection_esid"):
        if team.get(field):
            _add("de_connection", team[field])

    try:
        from import_scripts.facebook.lib.assets import resolve_team_filter_scope

        scope = resolve_team_filter_scope(team_esid) or {}
    except Exception as exc:  # noqa: BLE001 — attribution is best-effort
        logging.warning("credential_health: could not resolve team %s: %s", team_esid, exc)
        scope = {}

    app_esids = list(scope.get("fb_app_esids") or [])
    if app_esids:
        try:
            res = es.search(
                index="de_asset_fb_app_users",
                body={"size": 200, "query": {"terms": {"fb_app_esid": app_esids}}},
            )
            for h in res.get("hits", {}).get("hits", []):
                _add("de_asset_fb_app_users", h["_id"])
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_health: app_user lookup failed for %s: %s", team_esid, exc)

    return rows


def ig_import_ladder(ig_doc: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Health rows for the credentials the IG importer would actually try for
    one Instagram account, in try order.

    Mirrors ``get_instagram_insights``' auth loop, because a probe from a
    credential the importer never uses says nothing about whether imports work:

    1. **Wizard first** — if the IG asset doc carries ``wizard_connection_esid``
       (the bridge stamp), that ``de_connection`` token is prepended and
       tried first. The team's OTHER connection rows are NOT in the ladder: the
       importer reads the stamp on the IG doc, not ``de_asset_teams``.
    2. **Legacy app users** — narrowed to the app whose Business portfolio
       matches the IG's ``fb_account_esid`` / ``fb_account_title`` (the
       importer's "portfolio → app" selection); when nothing matches, every
       app user reachable from the team (its "try all apps" fallback).

    ``rows`` is ``health_for_fb_team(team_esid)``. Returns [] when no credential
    in the ladder has a health row — the caller renders that as UNKNOWN, never
    as importable.
    """
    ladder: list[dict[str, Any]] = []
    ig_doc = ig_doc or {}

    wce = str(ig_doc.get("wizard_connection_esid") or "").strip()
    if wce:
        wizard = [r for r in rows if r.get("source_index") == "de_connection" and str(r.get("source_esid")) == wce]
        if not wizard:
            # Stamped on the IG doc but not linked from the team — still what the
            # importer tries first, so fetch its row directly.
            direct = get_health("de_connection", wce)
            if direct:
                wizard = [direct | {"_id": doc_id("de_connection", wce)}]
        ladder.extend(wizard)

    legacy = [r for r in rows if r.get("source_index") == "de_asset_fb_app_users"]
    if legacy:
        want_esid = str(ig_doc.get("fb_account_esid") or "").strip()
        want_title = str(ig_doc.get("fb_account_title") or "").strip()
        matched: list[dict[str, Any]] = []
        if want_esid or want_title:
            apps: dict[str, dict[str, Any]] = {}
            for r in legacy:
                user = get_document_by_id("de_asset_fb_app_users", str(r.get("source_esid") or "")) or {}
                app_esid = str(user.get("fb_app_esid") or "").strip()
                if not app_esid:
                    continue
                if app_esid not in apps:
                    apps[app_esid] = get_document_by_id("de_asset_fb_apps", app_esid) or {}
                app = apps[app_esid]
                if (want_esid and str(app.get("fb_account_esid") or "") == want_esid) or (
                    want_title and str(app.get("fb_account_title") or "") == want_title
                ):
                    matched.append(r)
        ladder.extend(matched or legacy)

    return ladder


def ig_importability(ig_id: str, ladder: list[dict[str, Any]]) -> tuple[bool | None, str]:
    """``(importable, note)`` for one IG account from its ladder's probes.

    * ``True``  — at least one credential the importer tries can read the IG.
      The importer stops at the first working token, so ANY success imports.
    * ``False`` — every ladder credential that probed this IG failed. ``note``
      is ``"page_failed"`` when each failure was downstream of its parent page
      (prober ``caused_by``), else ``"not_reachable"``.
    * ``None``  — no ladder credential has probed this IG. Unknown is not broken;
      it is also not importable, and the caller must not paint it green.

    Only ``asset_type == "ig"`` probes count — a page probe with the same id
    (they are distinct id spaces, but the join must not depend on that) never
    satisfies an IG row.
    """
    ig_id = str(ig_id or "").strip()
    probes: list[dict[str, Any]] = []
    for row in ladder:
        for a in row.get("assets") or []:
            if a.get("asset_type") == "ig" and str(a.get("asset_id") or "").strip() == ig_id:
                probes.append(a)
    if not probes:
        return None, ""
    if any(p.get("ok") for p in probes):
        return True, ""
    if all(p.get("caused_by") for p in probes):
        return False, "page_failed"
    return False, "not_reachable"


def explain_team_stall(team_esid: str) -> dict[str, Any]:
    """Why is this team's import stalled?

    The continuity check deliberately "ignores WHY and alerts on the break
    itself", which leaves every stalled team looking identical — the ambiguity
    that hid team A / team B / team C / team D being merely PAUSED rather than
    broken. This resolves the three cases apart:

    * ``credential_unhealthy`` — a credential this team imports through is
      broken. ``detail`` names it and says what fixes it.
    * ``credentials_healthy``  — every credential resolved and all are healthy,
      so the stall is not an auth problem. Most likely paused campaigns or no ad
      activity. **This is the statement that closes the silent-zero ambiguity.**
    * ``unattributed``         — no credential resolved. We do not know, and we
      say so rather than implying health we have not verified (CLAUDE.md #23: a
      green pill over an unverified source is worse than no pill).
    """
    rows = health_for_fb_team(team_esid)
    if not rows:
        return {
            "verdict": "unattributed",
            "detail": "No credential could be resolved for this team — cause unknown.",
            "credentials": [],
        }

    unhealthy = [r for r in rows if r.get("status") in UNHEALTHY_STATUSES]
    if unhealthy:
        parts = [f"{r.get('label') or r.get('_id')}: {r.get('reason') or r.get('status')}" for r in unhealthy]
        return {
            "verdict": "credential_unhealthy",
            "detail": "; ".join(parts),
            "credentials": unhealthy,
        }

    return {
        "verdict": "credentials_healthy",
        "detail": (
            f"No credential errors recorded ({len(rows)} credential(s) checked and healthy) — "
            f"most likely paused campaigns or no ad activity, not a broken connection."
        ),
        "credentials": rows,
    }


def list_unhealthy(provider: str | None = None) -> list[dict[str, Any]]:
    """Every credential currently in a non-healthy, non-unknown state.

    This is what banners read. ``unknown`` is excluded on purpose: we
    could not determine it, which is not the same as knowing it is broken, and
    telling a workspace their connection is broken on the strength of one failed
    HTTP call is how a status surface loses credibility.
    """
    es = get_elasticsearch_client()
    if es is None:
        return []
    must: list[dict] = [{"terms": {"status": list(UNHEALTHY_STATUSES)}}]
    if provider:
        must.append({"term": {"provider": provider}})
    try:
        if not es.indices.exists(index=INDEX):
            return []
        res = es.search(index=INDEX, body={"size": 500, "query": {"bool": {"must": must}}})
    except Exception as exc:  # noqa: BLE001
        logging.error("credential_health: list_unhealthy failed: %s", exc)
        return []
    return [h["_source"] | {"_id": h["_id"]} for h in res.get("hits", {}).get("hits", [])]


# the credential families whose health rows can be orphaned by an
# Asset Explorer delete. ``__meta__`` (the heartbeat) and any future family not
# listed here are never pruned: an unknown source is not evidence of absence.
PRUNABLE_SOURCE_INDICES = ("de_asset_fb_app_users", "de_connection")


def prune_orphans(es=None, *, dry_run: bool = False) -> list[str]:
    """delete health rows whose source credential no longer exists.

    The sweep only ever UPSERTS (``record_probe``), so a credential deleted in
    the Asset Explorer left its last verdict behind forever and the
    banner kept rendering it (staging, 2026-09-09: a deleted test app user's
    ``renewal_required`` row). Pruning keys on CONFIRMED absence — an
    ``exists`` check on the source index — never on "not enumerated this run":
    a decrypt failure or a token-less row must not erase a credential's history.

    Returns the ids of the rows pruned (or that WOULD be pruned under
    ``dry_run``). Fail-soft: any ES error leaves the rows in place.
    """
    es = es or get_elasticsearch_client()
    if es is None:
        return []
    try:
        if not es.indices.exists(index=INDEX):
            return []
        res = es.search(
            index=INDEX,
            body={
                "size": 1000,
                "_source": ["source_index", "source_esid", "label", "status"],
                "query": {"terms": {"source_index": list(PRUNABLE_SOURCE_INDICES)}},
            },
        )
    except Exception as exc:  # noqa: BLE001 — a prune must never fail the sweep
        logging.warning("credential_health: prune_orphans could not read %s: %s", INDEX, exc)
        return []

    pruned: list[str] = []
    for hit in res.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        source_index = src.get("source_index") or ""
        source_esid = src.get("source_esid") or ""
        if source_index not in PRUNABLE_SOURCE_INDICES or not source_esid:
            continue
        try:
            if es.exists(index=source_index, id=source_esid):
                continue
        except Exception as exc:  # noqa: BLE001 — cannot confirm absence -> keep the row
            logging.warning("credential_health: prune_orphans could not check %s/%s: %s", source_index, source_esid, exc)
            continue
        hid = hit["_id"]
        logging.info(
            "credential_health: %s orphaned health row %s (%r, %s) — source credential no longer exists",
            "would prune" if dry_run else "pruning",
            hid,
            src.get("label") or "",
            src.get("status") or "",
        )
        if not dry_run:
            try:
                es.delete(index=INDEX, id=hid)
            except Exception as exc:  # noqa: BLE001 — leave it; the next sweep retries
                logging.warning("credential_health: prune_orphans could not delete %s: %s", hid, exc)
                continue
        pruned.append(hid)
    return pruned
