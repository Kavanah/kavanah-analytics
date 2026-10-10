"""Workspace banners for broken connections.

Turns the durable health state written into something a workspace
member actually sees, naming the affected assets and — critically — the CORRECT
fix, which differs by failure class.

Two message kinds, because the actions are genuinely different
--------------------------------------------------------------
``reconnect`` — an OAuth round-trip fixes it. Shown with a CTA for whoever is
allowed to perform it; **everyone else sees WHO can**, so the banner is never a
dead end for the person reading it.

``regrant``   — no OAuth fixes it. Someone must restore access at the provider
(e.g. "a channel owner must re-add access in YouTube Studio → Settings →
Permissions"). Rendering a Reconnect button here would send people round a loop
that cannot work, so this kind deliberately has no CTA.

Meta ``permission_lost`` maps to ``reconnect``, not ``regrant``: its dominant
cause is an asset left un-ticked in Meta's asset-selection step, which a re-auth
with the right boxes ticked does fix. The copy names the specific assets, because
"check your permissions" is what makes people file a ticket instead of retrying.

Cost
----
This runs in a Flask context processor — on EVERY page render. Two consequences
shaped the design: workspace is resolved at WRITE time (see credential_health's
mapping) rather than joined per request, and the whole result is cached for
``_CACHE_TTL_SECONDS``. A banner that made every page slower would get removed.

Tenancy
-------
Fails CLOSED. A health row with no resolvable workspace is shown to superusers
only — never broadcast to a workspace that may not own it. Leaking one client's
broken connection into another client's banner would be worse than showing
nothing.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from lib import credential_health as ch
from lib import job_health as jh

_CACHE: dict[str, Any] = {"rows": None, "ts": 0.0}
_CACHE_TTL_SECONDS = 60

KIND_RECONNECT = "reconnect"
KIND_REGRANT = "regrant"
# no connection we hold can reach these assets. No token
# operation changes that, so the banner offers no Reconnect and names the
# workspace administrator. It does NOT tell them to unlink: a code 100 reads the
# same for deleted and for unassigned, so the advice is to check Business
# Settings first (see _CAUSE_COPY[CAUSE_ASSET_GONE]).
KIND_UNLINK = "unlink"
# a tracked cron job failed or stopped reporting. Superusers only
# (a job has no workspace, and the fail-closed tenancy rule below applies);
# no CTA, because the fix is on the host, not in the app.
KIND_JOB = "job"
_JOB_CACHE: dict[str, Any] = {"rows": None, "ts": 0.0}

# Health status -> (kind, headline). Meta's permission_lost is a reconnect
# because the fix is re-auth-with-the-right-assets-ticked; a provider-side
# regrant is the YouTube-shaped case, keyed off the provider below.
_STATUS_COPY = {
    ch.STATUS_TOKEN_DEAD: (KIND_RECONNECT, "A data connection needs reconnecting"),
    ch.STATUS_RENEWAL_REQUIRED: (KIND_RECONNECT, "A data connection's Meta data access is expiring"),
    ch.STATUS_PERMISSION_LOST: (KIND_RECONNECT, "A data connection lost access to some assets"),
}

# Providers whose permission failures cannot be fixed by an OAuth round-trip.
_REGRANT_PROVIDERS = {"youtube"}

# Credential families with NO OAuth flow bound to them. A "Reconnect" button here
# would 404 (there is no connection_detail page for these rows) and, worse, would
# promise a fix that does not exist.
#
# de_asset_fb_app_users holds SYSTEM_USER tokens minted by hand in a client's
# Business Manager. Nothing in DEAM can re-mint one — a KM administrator must do
# it in Meta. This is the class originally called "legacy / contact us".
# It was nearly dropped on the assumption would retire legacy entirely;
# the corrected does NOT, because these are the credentials every
# ds_facebook team actually imports through. The class is required.
_NO_OAUTH_SOURCES = {"de_asset_fb_app_users"}

# per-cause copy: (group title, what fixes it, who can act, does a
# reconnect help). The role is the load-bearing part: a Business Manager admin
# is usually NOT a DEAM user, and a workspace administrator cannot grant Meta
# permissions — telling the wrong one sends the reader on a loop.
_CAUSE_COPY: dict[str, dict[str, Any]] = {
    ch.CAUSE_PERMISSION_MISSING: {
        "title": "Permission missing",
        "fix": (
            "Meta will not let this token see these assets. Grant the permission or "
            "assign the asset to the token's user in Meta Business Manager "
            "(Business Settings → Users / System users → Assigned assets), then reconnect "
            "so the new grant is picked up."
        ),
        "role": "a Business Manager admin",
        "reconnects": True,
    },
    # this group used to read "Meta no longer has these assets" and
    # end in "unlink them from the team in DEAM". Both overclaim. Meta answers
    # code 100 identically for an asset that was DELETED, one that was merely
    # UNASSIGNED, and one that never belonged to this business, and no probe
    # distinguishes them (four staging probes, 2026-09-18). Unlinking is the one
    # irreversible remedy the banner can recommend, so it must not ride on a
    # premise the system cannot establish. The wording below is correct under
    # every reading of a 100, and turns an unanswerable classification question
    # into an instruction the reader can actually carry out.
    ch.CAUSE_ASSET_GONE: {
        "title": "Asset not reachable by any connection",
        "fix": (
            "No connection we hold can reach these assets{stall}. Meta reports the same "
            "thing whether an asset was deleted or is simply no longer assigned to us, so "
            "which one it is has to be checked at Meta: open Business Settings "
            "(Users / System users → Assigned assets) and look for the asset. Re-assign it "
            "if it is there; unlink it from the team in DEAM only if it is not."
        ),
        "role": "a workspace administrator",
        "reconnects": False,
    },
    ch.CAUSE_TOKEN_DEAD: {
        "title": "Token rejected",
        "fix": (
            "The token itself is dead, so everything behind it fails together. One reconnect " "restores all of these at once."
        ),
        "role": "",  # filled per row: the principal, else a workspace administrator
        "reconnects": True,
    },
}


def _fetch_unhealthy() -> list[dict[str, Any]]:
    now = time.time()
    if _CACHE["rows"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_SECONDS:
        return _CACHE["rows"]
    try:
        rows = ch.list_unhealthy()
    except Exception as exc:  # noqa: BLE001 — a banner must never break a page render
        logging.warning("credential_banner_service: could not read health: %s", exc)
        rows = []
    _CACHE["rows"] = rows
    _CACHE["ts"] = now
    return rows


def _fetch_job_problems() -> list[dict[str, Any]]:
    """tracked cron jobs currently failed or stale; cached like the health rows."""
    now = time.time()
    if _JOB_CACHE["rows"] is not None and (now - _JOB_CACHE["ts"]) < _CACHE_TTL_SECONDS:
        return _JOB_CACHE["rows"]
    try:
        rows = jh.list_problems()
    except Exception as exc:  # noqa: BLE001 — a banner must never break a page render
        logging.warning("credential_banner_service: could not read job health: %s", exc)
        rows = []
    _JOB_CACHE["rows"] = rows
    _JOB_CACHE["ts"] = now
    return rows


def invalidate_cache() -> None:
    """Drop the cached rows — used by tests and after a re-auth completes."""
    _CACHE["rows"] = None
    _CACHE["ts"] = 0.0
    _JOB_CACHE["rows"] = None
    _JOB_CACHE["ts"] = 0.0


def _job_banner(job: dict[str, Any]) -> dict[str, Any]:
    """one banner per failed/stale tracked job.

    Carries every key the ``_credential_banners.html`` partial reads so the
    template needs no per-kind guards beyond the ``kind`` branch.
    """
    slug = job.get("job") or ""
    state = job.get("state") or ""
    exit_code = job.get("exit_code")
    summary = (job.get("summary") or "").strip()
    age = job.get("age_hours")
    if state == jh.STATE_STALE:
        headline = "A scheduled job has stopped reporting"
        level = "warning"
        ran = f"last run finished {age}h ago" if age is not None else "no run has ever finished"
        reason = f"{ran} (threshold {jh.DEFAULT_MAX_AGE_HOURS}h)" + (
            f"; last exit {exit_code}" if exit_code is not None else ""
        )
    else:
        headline = "A scheduled job failed on its last run"
        level = "error"
        reason = (
            f"exit {exit_code}"
            + (f" at {job.get('finished_at')}" if job.get("finished_at") else "")
            + (f" — {summary}" if summary else "")
        )
    return {
        "renewal_note": "",
        "days_until_data_access_expiry": None,
        "kind": KIND_JOB,
        "level": level,
        "headline": headline,
        "label": slug,
        "reason": reason,
        "provider": "",
        "status": state,
        "assets": [],
        "asset_summary": "",
        "unhealthy_since": job.get("finished_at") or "",
        "regrant_note": "",
        "can_fix": False,
        "who_can_fix": "a Kavanah administrator",
        "source_index": jh.INDEX,
        "source_esid": slug,
        "job_note": f"Read /var/log/{slug}.log on the host; the Status Reports page lists every tracked job.",
    }


def _affected_assets(row: dict[str, Any]) -> list[str]:
    """Names of the assets this credential can no longer reach.

    Downstream failures (``caused_by`` set) are excluded — they are consequences
    of another asset's failure and listing them inflates one problem into several.
    """
    out: list[str] = []
    for a in row.get("assets") or []:
        if a.get("ok") or a.get("caused_by"):
            continue
        out.append(a.get("asset_name") or a.get("asset_id") or "")
    return [a for a in out if a]


def _stall_clause(dates: list[str]) -> str:
    """" and nothing has imported for them since <date>", or "".

    The newest ``last_import_at`` in the group: after it, none of these assets
    imported, whatever the individual dates. Every ``asset_gone`` asset carries
    one by construction (the cause needs ``_stalled_before_onset`` True, which
    needs a date), but the clause is still guarded — a row written before
     can hold the cause without the field, and the banner must never
    render "since None".
    """
    known = sorted({str(d)[:10] for d in dates if d})
    return f" and nothing has imported for them since {known[-1]}" if known else ""


def _asset_groups(row: dict[str, Any], *, principal: str) -> list[dict[str, Any]]:
    """the row's lost assets grouped by their STORED cause, in fixed
    order, each group carrying its own fix text and the role that can apply it.

    Same exclusions as :func:`_affected_assets` (ok / downstream), plus covered
    assets, which are not losses. A row whose status is ``token_dead`` puts every
    loss in the token_dead group whatever cause is stored: the classified-error
    path flips status without restamping assets, so for one sweep cycle a dead
    token would otherwise keep rendering "Permission missing" advice. An asset
    with no stored cause (a row written, a transport blip)
    contributes no group — the pre-1494 render is kept rather than re-classified
    here, so there is exactly one classifier and it runs at sweep time. Order is
    permission_missing, asset_gone, token_dead so the banner reads "what you can
    do" before "what nobody can".
    """
    by_cause: dict[str, list[str]] = {c: [] for c in ch.CAUSES}
    stalls_by_cause: dict[str, list[str]] = {c: [] for c in ch.CAUSES}
    row_dead = row.get("status") == ch.STATUS_TOKEN_DEAD
    for a in row.get("assets") or []:
        if a.get("ok") or a.get("caused_by") or a.get("covered_by"):
            continue
        cause = ch.CAUSE_TOKEN_DEAD if row_dead else str(a.get("cause") or "")
        if cause not in ch.CAUSES:
            continue
        name = a.get("asset_name") or a.get("asset_id") or ""
        if name:
            by_cause[cause].append(name)
            stalls_by_cause[cause].append(str(a.get("last_import_at") or ""))
    groups: list[dict[str, Any]] = []
    for cause in ch.CAUSES:
        names = by_cause[cause]
        if not names:
            continue
        copy = _CAUSE_COPY[cause]
        role = copy["role"] or principal or "a workspace administrator"
        fix = copy["fix"]
        if "{stall}" in fix:
            fix = fix.format(stall=_stall_clause(stalls_by_cause[cause]))
        groups.append(
            {
                "cause": cause,
                "title": copy["title"],
                "assets": names,
                "asset_summary": ", ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else ""),
                "fix": fix,
                "role": role,
                "reconnects": bool(copy["reconnects"]),
            }
        )
    return groups


def build_banners(
    *,
    username: str,
    workspace_esids: list[str] | None = None,
    is_superuser: bool = False,
) -> list[dict[str, Any]]:
    """Banners this user should see. Empty list when everything is healthy.

    Self-clearing falls out of the data: a credential that returns to ``healthy``
    leaves ``list_unhealthy()``, so its banner disappears on the next cache cycle
    with no separate dismissal state to go stale.
    """
    workspace_esids = workspace_esids or []
    banners: list[dict[str, Any]] = []

    for row in _fetch_unhealthy():
        row_ws = row.get("workspace_esid") or ""
        if not is_superuser:
            # Fail closed on an unattributable row rather than broadcasting it.
            if not row_ws or row_ws not in workspace_esids:
                continue

        status = row.get("status") or ""
        source_index = row.get("source_index") or ""
        kind, headline = _STATUS_COPY.get(status, (KIND_RECONNECT, "A data connection needs attention"))

        regrant_note = ""
        # a system-user connection lives on de_connection (so it is
        # NOT caught by _NO_OAUTH_SOURCES) but has no OAuth flow either. Left
        # unhandled, the banner would offer Reconnect, and following it would
        # swap a non-expiring token for one on a 90-day clock — the same silent
        # downgrade start_reauthorize now refuses. Keyed on token_kind, checked
        # BEFORE the source_index branch so it wins regardless of where the row
        # lives.
        if (row.get("token_kind") or "") == "system_user":
            kind = KIND_REGRANT
            if status == ch.STATUS_PERMISSION_LOST:
                # for permission_lost the token itself is FINE: the
                # system user lost its assignment to the asset (or partner
                # sharing into the portfolio was removed). Telling the user to
                # mint a new token sends them through a fix that cannot work —
                # the fresh token has the same asset assignments, so the banner
                # comes straight back. Assignment changes take effect on the
                # LIVE token with no regeneration (verified on prod credentials,
                # 2026-08-28), so re-assignment is always the lever here.
                headline = "A data connection lost access to some assets"
                regrant_note = (
                    "This connection's system user token is fine — replacing it "
                    "will not help. Access to the affected asset(s) was removed "
                    "on the Meta side. An admin must re-assign them to the "
                    "system user in Meta Business Manager (Business Settings → "
                    "System users → Assigned assets), or restore the partner "
                    "sharing that granted them. The fix takes effect on the "
                    "existing token immediately."
                )
            else:
                headline = "A data connection needs its system user token replaced"
                regrant_note = (
                    "This connection uses a Business Manager system user token, which "
                    "does not expire — so reconnecting through Facebook would make it "
                    "worse, not better. Generate a new system user token in Meta "
                    "Business Manager and paste it in."
                )
        elif source_index in _NO_OAUTH_SOURCES:
            kind = KIND_REGRANT
            # the legacy family is MOSTLY system-user tokens, but not
            # only: a hand-authorised app user carries a plain USER token (Meta's
            # /debug_token ``type``, stamped on the row as ``token_type``). Its
            # data-access window is reset by re-authorising through the app
            # user's own authorisation link, not by anything in Business
            # Manager — the copy below used to send those readers to the wrong
            # place (staging, 2026-09-09: "DEAM Test User (Pages)", type USER).
            if (row.get("token_type") or "").upper() == "USER":
                headline = "A data connection's legacy user token needs re-authorising"
                regrant_note = (
                    "This connection uses a legacy Facebook user token, which has no "
                    "in-app reconnect. Re-authorise it through the app user's "
                    "authorisation link in the Asset Explorer (Facebook Apps → the app "
                    "user), or paste a new long-lived user token there. Business "
                    "Manager does not renew this kind of token."
                )
            else:
                headline = "A data connection needs its access token re-issued"
                regrant_note = (
                    "This connection uses a Business Manager system user, which has no "
                    "in-app reconnect. A Kavanah administrator must issue a new token in "
                    "Meta Business Manager."
                )
        elif row.get("provider") in _REGRANT_PROVIDERS and status == ch.STATUS_PERMISSION_LOST:
            kind = KIND_REGRANT
            headline = "A data connection needs access restored at the provider"
            regrant_note = "Reconnecting will not fix this — an account owner must re-add access " "at the provider."

        principal = row.get("principal") or ""
        # the permission this comment long promised. Rows reaching
        # this loop are already workspace-filtered for the viewer (the
        # fail-closed row_ws gate above), so the permission is checked against
        # the row's OWN workspace — mirroring the route gate's clause 3.5,
        # never an "anywhere" check.
        can_fix = bool(is_superuser or (principal and principal == username))
        if not can_fix and row_ws:
            try:
                from asset_management.web.services.permission_resolver import PermissionResolver
                from asset_management.web.services.permissions_enum import Permission

                can_fix = PermissionResolver.has_permission_in_workspace(username, row_ws, Permission.CONNECTION_REAUTHORIZE)
            except Exception as e:  # noqa: BLE001 — a banner must never break a page render
                logging.warning("credential_banner_service: permission check failed: %s", e)
        # a regrant has NO in-app fix by definition, so nobody can
        # "fix" it regardless of role. This was previously true only by accident:
        # the one regrant source (de_asset_fb_app_users) has no principal, so the
        # expression above happened to yield False. A system-user connection on
        # de_connection DOES have a principal, which flipped it to True and would
        # have offered that person an action that does not exist.
        if kind == KIND_REGRANT:
            can_fix = False

        assets = _affected_assets(row)
        # group the losses by cause so each names its own fix and the
        # role that can apply it. Only for rows whose fix IS a token operation
        # (kind reconnect, status permission_lost / token_dead): a regrant row's
        # note says reconnecting will not help and a renewal row's losses are a
        # consequence of the closing window, so per-cause copy under either
        # would contradict the note directly above it. A reconnect row whose
        # every loss is an asset no connection can reach gets no Reconnect at
        # all: the CTA would send the reader round a loop that cannot work (the
        # IG case).
        groups: list[dict[str, Any]] = []
        if kind == KIND_RECONNECT and status in (ch.STATUS_PERMISSION_LOST, ch.STATUS_TOKEN_DEAD):
            groups = _asset_groups(row, principal=principal)
        reconnect_assets = [n for g in groups if g["reconnects"] for n in g["assets"]]
        unlink_assets: list[str] = []
        if (
            kind == KIND_RECONNECT
            and status == ch.STATUS_PERMISSION_LOST
            and groups
            and all(g["cause"] == ch.CAUSE_ASSET_GONE for g in groups)
        ):
            kind = KIND_UNLINK
            # was "assets Meta no longer has". We cannot establish
            # that: a code 100 reads the same for deleted and for unassigned.
            # The headline now claims only what the probes actually showed.
            headline = "A data connection lists assets it can no longer reach"
            unlink_assets = list(groups[0]["assets"])
            can_fix = False
        # a closing data-access window is the one failure a reader can
        # SCHEDULE rather than react to, so the banner says who holds the
        # connection and what the fix does. Only a person's re-auth resets Meta's
        # 90-day window (the 02:30 token refresh does not), and the connection
        # page is where that happens.
        renewal_note = ""
        if status == ch.STATUS_RENEWAL_REQUIRED and kind == KIND_RECONNECT:
            days = row.get("days_until_data_access_expiry")
            holder = principal or "the connection owner"
            when = (
                f"in {days} day(s)"
                if isinstance(days, int) and days >= 0
                else ("already" if isinstance(days, int) else "soon")
            )
            renewal_note = (
                f"Meta's 90-day data-access window on this connection closes {when}. "
                f"{holder} holds it — re-authorising from the connection page resets the window; "
                f"nothing automatic will."
            )
        banners.append(
            {
                "renewal_note": renewal_note,
                "days_until_data_access_expiry": row.get("days_until_data_access_expiry"),
                "kind": kind,
                "level": "error" if status == ch.STATUS_TOKEN_DEAD else "warning",
                "headline": headline,
                "label": row.get("label") or row.get("_id") or "",
                "reason": row.get("reason") or "",
                "provider": row.get("provider") or "",
                "status": status,
                "assets": assets,
                "asset_summary": ", ".join(assets[:5]) + (f" (+{len(assets) - 5} more)" if len(assets) > 5 else ""),
                "unhealthy_since": row.get("unhealthy_since") or "",
                "regrant_note": regrant_note,

                "groups": groups,
                "reconnect_assets": reconnect_assets,
                "reconnect_asset_summary": ", ".join(reconnect_assets[:5])
                + (f" (+{len(reconnect_assets) - 5} more)" if len(reconnect_assets) > 5 else ""),
                # the asset_gone group's own names: the unlink sentence counts
                # these, not every affected asset (a transport blip may be listed too)
                "unlink_assets": unlink_assets,
                "can_fix": can_fix,
                # Who to go to when the reader cannot fix it themselves. Without
                # this the banner is a dead end for everyone but one person.
                "who_can_fix": (
                    "a workspace administrator" if kind == KIND_UNLINK else (principal or "a workspace administrator")
                ),
                "source_index": row.get("source_index") or "",
                "source_esid": row.get("source_esid") or "",
            }
        )

    # tracked cron jobs. No workspace to attribute them to, so they
    # follow the fail-closed rule: superusers only. never_ran is not a problem
    # state (see lib.job_health), so an unprovisioned dev cluster stays quiet.
    if is_superuser:
        for job in _fetch_job_problems():
            banners.append(_job_banner(job))

    banners.sort(key=lambda b: (0 if b["level"] == "error" else 1, b["label"]))
    return banners
