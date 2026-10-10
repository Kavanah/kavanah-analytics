"""Phase 4 — team ad-account link panel + admin edits.

The Asset Explorer team page renders a team's effective-dated
``fb_ad_account_links`` (the which ad account(s) a team
runs ads from, and when) and lets operators with the Data Connectors
permission correct the list — close a link, add a manual link, edit a
link's dates/note. Admin writes stamp ``source="manual"`` plus who/when,
so wizard-written and hand-written links stay distinguishable.

The mutation core (``apply_add`` / ``apply_close`` / ``apply_update``)
is pure (no ES) so the release-plan tests exercise the validation rules
directly — same layering as the bridge's merge helper.

Scalar sync: the team's legacy ``fb_ad_account_esid`` scalar stays
derived from the newest OPEN link (the importer prefers links since
Phase 2, but the Data Connectors card + scalar-fallback paths still read
it). When an edit leaves NO open link, the scalar is cleared — that is
the runbook's "page-only / no ad account set" state, and it stops the
card claiming an account the team no longer uses.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_document_by_id, get_elasticsearch_client  # noqa: E402
from lib.errors import UserFacingError

TEAMS_INDEX = "de_asset_teams"
AD_ACCOUNTS_INDEX = "de_asset_fb_ad_accounts"
CONNECTION_INDEX = "de_connection"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# More concurrently-open links than this is almost certainly a data-entry
# mistake (the plan's validation guardrail) — surfaced as a WARNING on the
# response, never a rejection (genuine dual-account teams exist).
_CONCURRENT_OPEN_WARN_THRESHOLD = 2


class LinkEditError(UserFacingError):
    """Operator-facing validation/lookup failure. ``"Team not found"`` is
    collapsed to 404 by the route (deny/not-found collapse style)."""


def _validate_date(value: str | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not _DATE_RE.match(value):
        raise LinkEditError(f"{label} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise LinkEditError(f"{label} is not a valid date")
    return value


def _check_window(start: str | None, end: str | None) -> None:
    if start and end and start > end:
        raise LinkEditError("start_date must be on or before end_date")


def _open_links(links: list) -> list:
    return [link for link in links if not link.get("end_date")]


def _concurrency_warnings(links: list) -> list[str]:
    open_count = len(_open_links(links))
    if open_count > _CONCURRENT_OPEN_WARN_THRESHOLD:
        return [
            f"{open_count} links are now open at the same time — more than "
            f"{_CONCURRENT_OPEN_WARN_THRESHOLD} concurrently-active ad accounts "
            f"is usually a mistake; review the list."
        ]
    return []


def apply_add(
    links: list,
    *,
    ad_account_esid: str,
    fb_ad_account_id: str | None,
    connection_esid: str | None,
    start_date: str | None,
    note: str | None,
    username: str,
    now_iso: str,
) -> tuple[list, list[str]]:
    """Append a manual open link. Rejects a second OPEN link to the same
    account (close it first); ended links to the same account are fine
    (that is exactly the switch-back history case)."""
    start_date = _validate_date(start_date, "start_date")
    for link in _open_links(links):
        if link.get("ad_account_esid") == ad_account_esid:
            raise LinkEditError("This team already has an open link to that ad account — close it first or edit its dates.")
    new_link: dict[str, Any] = {
        "ad_account_esid": ad_account_esid,
        "connection_esid": connection_esid or None,
        "source": "manual",
        "updated_by": username,
        "updated_at": now_iso,
    }
    if fb_ad_account_id:
        new_link["fb_ad_account_id"] = fb_ad_account_id
    if start_date:
        new_link["start_date"] = start_date
    if note:
        new_link["note"] = str(note)[:500]
    new_link = {k: v for k, v in new_link.items() if v is not None}
    merged = list(links) + [new_link]
    return merged, _concurrency_warnings(merged)


def apply_close(
    links: list,
    *,
    ad_account_esid: str,
    end_date: str | None,
    username: str,
    now_iso: str,
) -> tuple[list, list[str]]:
    """Close the OPEN link to ``ad_account_esid`` (end_date defaults to
    today, UTC). History is never deleted — closing is the only removal."""
    end_date = _validate_date(end_date, "end_date") or datetime.now(timezone.utc).date().isoformat()
    merged = []
    closed = False
    for link in links:
        link = dict(link)
        if not closed and not link.get("end_date") and link.get("ad_account_esid") == ad_account_esid:
            _check_window(link.get("start_date"), end_date)
            link["end_date"] = end_date
            link["updated_by"] = username
            link["updated_at"] = now_iso
            link["source"] = "manual"
            closed = True
        merged.append(link)
    if not closed:
        raise LinkEditError("No open link to that ad account was found.")
    return merged, []


def apply_update(
    links: list,
    *,
    link_index: int,
    start_date: str | None = ...,
    end_date: str | None = ...,
    note: str | None = ...,
    username: str,
    now_iso: str,
) -> tuple[list, list[str]]:
    """Edit dates/note on the link at ``link_index`` (the array position
    shown in the panel). ``...`` (not passed) leaves a field alone; an
    empty string clears it — clearing ``end_date`` REOPENS the link."""
    if not isinstance(link_index, int) or link_index < 0 or link_index >= len(links):
        raise LinkEditError("Unknown link.")
    merged = [dict(existing) for existing in links]
    link = merged[link_index]
    if start_date is not ...:
        value = _validate_date(start_date, "start_date")
        if value:
            link["start_date"] = value
        else:
            link.pop("start_date", None)
    if end_date is not ...:
        value = _validate_date(end_date, "end_date")
        if value:
            link["end_date"] = value
        else:
            link.pop("end_date", None)
    if note is not ...:
        if note:
            link["note"] = str(note)[:500]
        else:
            link.pop("note", None)
    _check_window(link.get("start_date"), link.get("end_date"))
    if not link.get("end_date"):
        for i, other in enumerate(merged):
            if i != link_index and not other.get("end_date") and other.get("ad_account_esid") == link.get("ad_account_esid"):
                raise LinkEditError("Reopening this link would leave two open links to the same ad account.")
    link["updated_by"] = username
    link["updated_at"] = now_iso
    return merged, _concurrency_warnings(merged)


def apply_delete(
    links: list,
    *,
    link_index: int,
) -> tuple[list, list[str]]:
    """Remove the link at ``link_index`` entirely.

    Deletion is an explicit ADMIN CORRECTION for an erroneous row (wrong
    account picked, bad seed) — routine end-of-use goes through
    :func:`apply_close`, which preserves the history the importer
    attributes from. The route logs who deleted what; already-imported
    data for the account/window is untouched (only future attribution
    changes).
    """
    if not isinstance(link_index, int) or link_index < 0 or link_index >= len(links):
        raise LinkEditError("Unknown link.")
    merged = [dict(l) for i, l in enumerate(links) if i != link_index]
    return merged, []


def derive_scalars(links: list) -> dict:
    """The team's legacy scalar pair, derived from the newest open link.

    Returns a partial team-doc patch: with open links, the scalar points at
    the LAST open link in array order (newest append — same rule as the
    wizard bridge, which appends new links); with none, ``fb_ad_account_esid``
    clears to the runbook's "no ad account set" state (``None`` here becomes
    a field removal in the write path). ``connection_esid`` is only synced
    when the winning link carries one (never cleared — it is also the page/IG
    credential pointer).
    """
    open_links = _open_links(links)
    if not open_links:
        return {"fb_ad_account_esid": None}
    winner = open_links[-1]
    patch: dict[str, Any] = {"fb_ad_account_esid": winner.get("ad_account_esid")}
    if winner.get("connection_esid"):
        patch["connection_esid"] = winner["connection_esid"]
    return patch


# ── ES-facing layer ──────────────────────────────────────────────────────────


def _get_team_source(team_esid: str, allowed_workspaces: list[str] | None) -> dict:
    """Tenant-scoped team fetch; deny and missing both collapse to
    ``"Team not found"`` (mirrors connector_status_service.disconnect_connector)."""
    if allowed_workspaces is not None and not allowed_workspaces:
        raise LinkEditError("Team not found")
    try:
        source = get_document_by_id(TEAMS_INDEX, team_esid)
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning("team_ad_account_link_service: team lookup failed for %s: %s", team_esid, e)
        raise LinkEditError("Team not found") from e
    if not source:
        raise LinkEditError("Team not found")
    if allowed_workspaces is not None:
        team_ws = source.get("workspace_esid")
        if not team_ws or team_ws not in allowed_workspaces:
            raise LinkEditError("Team not found")
    return source


def build_panel(team_source: dict, *, can_manage: bool) -> dict:
    """Display payload for the team page's "Ad accounts" section.

    Enriches each link with the account title (from the asset doc) and the
    connection status (from de_connection); lookups fail soft — the row
    still renders from the link's own fields. Rows keep their ARRAY INDEX
    (`link_index`) — that is the handle the edit endpoints address.
    """
    links = team_source.get("fb_ad_account_links") or []
    rows = []
    for idx, link in enumerate(links):
        row = {
            "link_index": idx,
            "ad_account_esid": link.get("ad_account_esid") or "",
            "fb_ad_account_id": link.get("fb_ad_account_id") or "",
            "connection_esid": link.get("connection_esid") or "",
            "start_date": (link.get("start_date") or "")[:10],
            "end_date": (link.get("end_date") or "")[:10],
            "is_open": not link.get("end_date"),
            "source": link.get("source") or "",
            "note": link.get("note") or "",
            "title": "",
            "connection_status": "",
        }
        try:
            asset = get_document_by_id(AD_ACCOUNTS_INDEX, row["ad_account_esid"]) if row["ad_account_esid"] else None
            if asset:
                row["title"] = asset.get("fb_ad_account_title") or ""
                row["fb_ad_account_id"] = row["fb_ad_account_id"] or asset.get("fb_ad_account_id") or ""
        except Exception:  # noqa: BLE001 — display enrichment only
            pass
        try:
            conn = get_document_by_id(CONNECTION_INDEX, row["connection_esid"]) if row["connection_esid"] else None
            if conn:
                row["connection_status"] = conn.get("status") or ""
        except Exception:  # noqa: BLE001
            pass
        rows.append(row)
    # Open links first, then by start_date (newest first, unknown last).
    rows.sort(key=lambda r: (not r["is_open"], r["start_date"] == "", r["start_date"]), reverse=False)
    rows.sort(key=lambda r: r["start_date"] or "0000", reverse=True)
    rows.sort(key=lambda r: not r["is_open"])
    return {"rows": rows, "can_manage": can_manage, "has_links": bool(rows)}


def build_panel_for_page(page_esid: str, page_source: dict, *, can_manage: bool) -> dict | None:
    """the same panel, rendered on the Edit Facebook Page form.

    Ad-account links are TEAM-scoped (an ad account is owned by a Meta
    Business Portfolio, not by a page — a teammate's rule 6), so this resolves the
    page's owning team and returns THAT team's links. The panel is a view onto
    the team's links from the page that prompted the question; edits still go
    to the team-keyed endpoints, so link semantics are untouched.

    Ownership is stored on the team side (``team_facebook_page_esids`` /
    ``team_facebook_page_id``), so this is a reverse lookup. Returns None when
    no owning team resolves — the caller then renders no panel.

    Adds ``team_esid`` / ``team_title`` to the payload so the page form can say
    WHOSE links these are (they are not the page's own).

    Known limitation (accepted discussion 2026-07-27): if a page is
    later moved to a different team, this panel follows the NEW owner, so the
    page form stops narrating the account history from its previous team. The
    per-team link histories both stay individually correct, and the imported ad
    docs retain their own page+account stamps, so nothing is lost — only the
    page-form display loses that continuity. Judged rare enough not to warrant
    page-scoped links.
    """
    if not page_esid:
        return None
    es = get_elasticsearch_client()
    if es is None:
        return None

    fb_page_id = str((page_source or {}).get("fb_page_id") or "").strip()
    # Match either the esid back-link or the raw page id — both forms are in
    # use across teams (the wizard writes esids; older/CSV-imported teams carry
    # the numeric id). ``team_facebook_page_id`` is a keyword array.
    should: list[dict] = [{"term": {"team_facebook_page_esids": page_esid}}]
    if fb_page_id:
        should.append({"term": {"team_facebook_page_id": fb_page_id}})

    try:
        resp = es.search(
            index=TEAMS_INDEX,
            body={
                "size": 2,  # 2 so we can detect (and log) an ambiguous claim
                "query": {"bool": {"should": should, "minimum_should_match": 1}},
            },
            ignore_unavailable=True,
        )
        hits = ((resp or {}).get("hits") or {}).get("hits") or []
    except Exception as exc:  # noqa: BLE001 — panel is display-only
        logging.warning("page->team lookup failed for page %s: %s", page_esid, exc)
        return None

    if not hits:
        return None
    if len(hits) > 1:
        # Rule 1 says a page belongs to exactly one team; a second claimant is
        # a data problem worth surfacing rather than silently picking one.
        logging.warning(
            "page %s (fb_page_id=%s) is claimed by %d teams (%s) — "
            "showing the first; a page should belong to exactly one team.",
            page_esid,
            fb_page_id or "?",
            len(hits),
            ", ".join((h.get("_source") or {}).get("team_title", "?") for h in hits),
        )

    team_doc = hits[0]
    team_source = team_doc.get("_source") or {}
    panel = build_panel(team_source, can_manage=can_manage)
    panel["team_esid"] = team_doc.get("_id") or ""
    panel["team_title"] = team_source.get("team_title") or ""
    return panel


def _write_links(team_esid: str, links: list) -> None:
    es = get_elasticsearch_client()
    if es is None:
        raise LinkEditError("Storage is unavailable — try again shortly.")
    doc: dict[str, Any] = {"fb_ad_account_links": links}
    scalars = derive_scalars(links)
    if scalars.get("fb_ad_account_esid") is None:
        # No open link → clear the scalar via script (doc-merge can't remove).
        try:
            es.update(
                index=TEAMS_INDEX,
                id=team_esid,
                body={
                    "script": {
                        "source": "ctx._source.fb_ad_account_links = params.links; ctx._source.remove('fb_ad_account_esid')",
                        "lang": "painless",
                        "params": {"links": links},
                    }
                },
                refresh="wait_for",
            )
            return
        except Exception as e:  # noqa: BLE001
            logging.warning("team_ad_account_link_service: scripted write failed for %s: %s", team_esid, e)
            raise LinkEditError("Saving the link change failed.") from e
    doc.update(scalars)
    try:
        es.update(index=TEAMS_INDEX, id=team_esid, doc=doc, refresh="wait_for")
    except Exception as e:  # noqa: BLE001
        logging.warning("team_ad_account_link_service: write failed for %s: %s", team_esid, e)
        raise LinkEditError("Saving the link change failed.") from e


def edit_links(
    action: str,
    team_esid: str,
    payload: dict,
    *,
    username: str,
    allowed_workspaces: list[str] | None,
) -> dict:
    """Apply one admin edit (``add`` / ``close`` / ``update``) and return the
    refreshed panel + any warnings."""
    source = _get_team_source(team_esid, allowed_workspaces)
    links = source.get("fb_ad_account_links") or []
    now_iso = datetime.now(timezone.utc).isoformat()

    if action == "add":
        ad_account_esid = str(payload.get("ad_account_esid") or "").strip()
        if not ad_account_esid:
            raise LinkEditError("ad_account_esid is required")
        asset = None
        try:
            asset = get_document_by_id(AD_ACCOUNTS_INDEX, ad_account_esid)
        except Exception:  # noqa: BLE001
            asset = None
        if not asset:
            raise LinkEditError("Ad account not found — it must already exist in the Asset Explorer.")
        # both esids come from the request body. The ad-account doc
        # must be in the team's own workspace (teams in one workspace may share
        # an account; Meta rule 2 says the account never identifies a team, so
        # this only stops another tenant's doc being linked), and a connection
        # named in the body must be one the caller may use — its token is what
        # the importer pulls this account's insights with.
        team_ws = str(source.get("workspace_esid") or "").strip()
        if not team_ws or str(asset.get("workspace_esid") or "").strip() != team_ws:
            logging.warning(
                "refused ad-account link: team=%s account=%s (other workspace)", team_esid, ad_account_esid
            )
            raise LinkEditError("Ad account not found — it must already exist in the Asset Explorer.")
        body_connection = str(payload.get("connection_esid") or "").strip()
        if body_connection and body_connection != (source.get("connection_esid") or ""):
            try:
                from web.services.connector_wizard_service import _viewer_may_use_connection
            except ImportError:
                from asset_management.web.services.connector_wizard_service import _viewer_may_use_connection
            if not _viewer_may_use_connection(body_connection):
                logging.warning(
                    "refused ad-account link: team=%s connection=%s not visible to %s",
                    team_esid,
                    body_connection,
                    username,
                )
                raise LinkEditError("Connection not found.")
        merged, warnings = apply_add(
            links,
            ad_account_esid=ad_account_esid,
            fb_ad_account_id=asset.get("fb_ad_account_id"),
            connection_esid=(body_connection or source.get("connection_esid") or None),
            start_date=payload.get("start_date"),
            note=payload.get("note"),
            username=username,
            now_iso=now_iso,
        )
    elif action == "close":
        merged, warnings = apply_close(
            links,
            ad_account_esid=str(payload.get("ad_account_esid") or "").strip(),
            end_date=payload.get("end_date"),
            username=username,
            now_iso=now_iso,
        )
    elif action == "update":
        try:
            link_index = int(payload.get("link_index"))
        except (TypeError, ValueError):
            raise LinkEditError("Unknown link.")
        kwargs: dict[str, Any] = {}
        for field in ("start_date", "end_date", "note"):
            if field in payload:
                kwargs[field] = payload.get(field)
        merged, warnings = apply_update(
            links,
            link_index=link_index,
            username=username,
            now_iso=now_iso,
            **kwargs,
        )
    elif action == "delete":
        try:
            link_index = int(payload.get("link_index"))
        except (TypeError, ValueError):
            raise LinkEditError("Unknown link.")
        deleted = links[link_index] if 0 <= link_index < len(links) else {}
        merged, warnings = apply_delete(links, link_index=link_index)
        logging.warning(
            "admin link DELETE: team=%s by=%s removed link %s "
            "(account=%s window=%s->%s source=%s) — correction, not closure",
            team_esid,
            username,
            link_index,
            deleted.get("fb_ad_account_id") or deleted.get("ad_account_esid"),
            deleted.get("start_date"),
            deleted.get("end_date") or "open",
            deleted.get("source"),
        )
    else:
        raise LinkEditError(f"Unknown action: {action}")

    _write_links(team_esid, merged)
    logging.info(
        "admin link edit: action=%s team=%s by=%s (links now %d, open %d)",
        action,
        team_esid,
        username,
        len(merged),
        len(_open_links(merged)),
    )
    refreshed = dict(source)
    refreshed["fb_ad_account_links"] = merged
    return {"panel": build_panel(refreshed, can_manage=True), "warnings": warnings}
