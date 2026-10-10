"""Phase 5 — reconcile team ad-account links from the ministry-team CSV.

Turns the hand-executed Kibana-Dev-Tools steps of
``the private docs`` into a
reviewed, dry-run-able two-stage tool driven by the completed
``FB-team-ad-account-mapping`` spreadsheet (the "Actually run by THIS
team?" column filled in by a teammate's team).

Stage 1 — ``plan``: read the mapping CSV + live ES state and emit a
DECISIONS CSV — one proposed action per classifiable row, plus
``needs_human`` rows for anything the free-text confirmation could not be
parsed into a safe action. The operator reviews/edits this file; nothing
is written to ES.

Stage 2 — ``apply``: read the (reviewed) decisions CSV and apply each
action to the team's ``fb_ad_account_links`` using the Phase-4
service rules (open-link dedup, date validation, scalar sync). Dry-run by
default; ``--execute`` writes.

Decision actions:

* ``keep``            — confirmed correct; no write.
* ``set_current``     — close every open link, then open a link to
                        ``ad_account_id`` (optionally ``start_date``).
* ``clear``           — close every open link (page-only / paused / training
                        teams — the "no ad account set" state).
* ``add_historical``  — append an ENDED link (``ad_account_id`` +
                        ``start_date``/``end_date``; end_date required) so
                        backfills attribute the old account's era.
* ``skip``            — do nothing (unconfirmed / needs discussion).

Ordering caution: on prod, run ``update_v26`` (the scalar→link
seed) and this reconciliation in the SAME maintenance window, before the
next daily import — a corrupt scalar seeded as an open link is an
ownership claim under the inverted guard, not just a frozen import.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.reconcile_fb_ad_account_links plan \\
        --csv /app/the private docs \\
        --out /app/data/decisions_2.csv

    # review/edit the decisions file, then:
    docker exec -w /app/python imports_container python3 \\
        -m tools.reconcile_fb_ad_account_links apply \\
        --decisions /app/data/decisions_2.csv # dry-run
    ... --execute                                              # write
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from asset_management.web.services.team_ad_account_link_service import (  # noqa: E402
    LinkEditError,
    apply_add,
    apply_close,
    derive_scalars,
)

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"
AD_ACCOUNTS_INDEX = "de_asset_fb_ad_accounts"

DECISION_FIELDS = [
    "team_title",
    "team_esid",
    "action",
    "ad_account_id",
    "start_date",
    "end_date",
    "note",
    "classification",
    "detail",
]

_DIGITS_RE = re.compile(r"\d{8,}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Reviewer initials accepted in the "Reviewed/Corrected" column as an
# affirmation of our record. An ALLOW-LIST on purpose: this column authorizes
# ES writes, so an unrecognized value must fall to needs_human rather than be
# read as consent. Add an entry only when the person's authority is confirmed.
_REVIEWER_INITIALS = {"SWP"}


# ── Stage 1: classification (pure — release-plan tested) ─────────────────────


def classify_confirmation(confirm: str, row_account_id: str, notes: str = "", reviewed: str = "") -> dict:
    """Map one mapping-CSV confirmation cell onto a proposed decision.

    Deliberately conservative: only unambiguous phrasings produce an
    action; everything else is ``needs_human`` so free-text never turns
    into a surprise write. Returns
    ``{"action", "ad_account_id", "classification", "detail"}``.

    ``reviewed`` is the "Reviewed/Corrected" column. The team returned the
    2026-07-27 sheet with the per-row Y/N confirmation left blank and this
    column initialled instead (``SWP`` — the client's Meta admin), who
    confirmed means "our record is correct". A recognized reviewer initial
    is therefore treated as a ``Y`` on that row, but only when the row's
    free-text confirmation is EMPTY: an explicit confirmation always wins,
    so a later "N - actually 12345" can never be overridden by a stale
    review stamp. Unrecognized initials stay ``needs_human`` — we do not
    infer authority from arbitrary text.
    """
    text = (confirm or "").strip()
    lowered = text.lower()
    notes_lowered = (notes or "").lower()
    reviewed_token = (reviewed or "").strip()

    page_only = any(k in lowered for k in ("page only", "no ads", "no ad account", "none"))
    paused = any(k in notes_lowered for k in ("paused", "training", "no current ads", "no data needs", "not needed"))

    # Paused/training teams that hold no ad account are the "no ad account set"
    # state regardless of HOW they were confirmed — checked before the review
    # branch so a reviewed row whose notes say "team is paused, no data pulls
    # needed" clears instead of stalling on its missing account id (two teams
    # on the 2026-07-27 sheet).
    if paused and not row_account_id and not text:
        return {
            "action": "clear",
            "ad_account_id": "",
            "classification": "page_only_or_paused",
            "detail": f"notes indicate paused/training, no ad account: {notes.strip()[:120]!r}",
        }

    if not text:
        if reviewed_token:
            if reviewed_token.upper() not in _REVIEWER_INITIALS:
                return {
                    "action": "skip",
                    "ad_account_id": "",
                    "classification": "needs_human",
                    "detail": f"unrecognized reviewer initial {reviewed_token!r} — confirm who this is before writing",
                }
            # A review stamp affirms OUR record, so it needs one to affirm.
            if not row_account_id:
                return {
                    "action": "skip",
                    "ad_account_id": "",
                    "classification": "needs_human",
                    "detail": f"reviewed by {reviewed_token} but the row has no ad-account id",
                }
            return {
                "action": "set_current",
                "ad_account_id": row_account_id,
                "classification": "confirmed_by_review",
                "detail": f"Reviewed/Corrected={reviewed_token} (our record affirmed)",
            }
        return {
            "action": "skip",
            "ad_account_id": "",
            "classification": "unconfirmed",
            "detail": "confirmation column empty",
        }

    if page_only or (lowered.startswith("n") and paused):
        return {
            "action": "clear",
            "ad_account_id": "",
            "classification": "page_only_or_paused",
            "detail": text,
        }

    if lowered.startswith("y"):
        if not row_account_id:
            return {
                "action": "skip",
                "ad_account_id": "",
                "classification": "needs_human",
                "detail": "confirmed Y but the row has no ad-account id",
            }
        return {
            "action": "set_current",
            "ad_account_id": row_account_id,
            "classification": "confirmed",
            "detail": text,
        }

    if lowered.startswith("n"):
        ids = [m for m in _DIGITS_RE.findall(text) if m != row_account_id]
        if len(ids) == 1:
            return {
                "action": "set_current",
                "ad_account_id": ids[0],
                "classification": "corrected",
                "detail": text,
            }
        return {
            "action": "skip",
            "ad_account_id": "",
            "classification": "needs_human",
            "detail": f"answered N but no single corrected account id found in: {text!r}",
        }

    return {
        "action": "skip",
        "ad_account_id": "",
        "classification": "needs_human",
        "detail": f"unparseable confirmation: {text!r}",
    }


# ── Stage 2: apply transforms (pure — release-plan tested) ───────────────────


def build_new_links(
    links: list,
    action: str,
    *,
    ad_account_esid: str = "",
    fb_ad_account_id: str = "",
    connection_esid: str | None = None,
    start_date: str = "",
    end_date: str = "",
    note: str = "",
    username: str,
    now_iso: str,
    today_iso: str,
) -> list:
    """Return the team's new link list for one decision row.

    Raises ``LinkEditError`` on rule violations (bad dates, historical
    without end_date, unknown action). ``keep``/``skip`` return the input
    unchanged.
    """
    if action in ("keep", "skip"):
        return links

    if action == "clear":
        merged = links
        for link in [open_link for open_link in links if not open_link.get("end_date")]:
            merged, _ = apply_close(
                merged,
                ad_account_esid=link.get("ad_account_esid"),
                end_date=end_date or today_iso,
                username=username,
                now_iso=now_iso,
            )
        return merged

    if action == "set_current":
        if not ad_account_esid:
            raise LinkEditError("set_current needs a resolved ad_account_esid")
        merged = links
        already_open = False
        for link in [open_link for open_link in links if not open_link.get("end_date")]:
            if link.get("ad_account_esid") == ad_account_esid:
                already_open = True
                continue
            merged, _ = apply_close(
                merged,
                ad_account_esid=link.get("ad_account_esid"),
                end_date=today_iso,
                username=username,
                now_iso=now_iso,
            )
        if not already_open:
            merged, _ = apply_add(
                merged,
                ad_account_esid=ad_account_esid,
                fb_ad_account_id=fb_ad_account_id or None,
                connection_esid=connection_esid,
                start_date=start_date or None,
                note=note or "reconciliation",
                username=username,
                now_iso=now_iso,
            )
        return merged

    if action == "add_historical":
        if not ad_account_esid:
            raise LinkEditError("add_historical needs a resolved ad_account_esid")
        if not end_date or not _DATE_RE.match(end_date):
            raise LinkEditError("add_historical requires end_date (YYYY-MM-DD) — an ended link is the whole point")
        if start_date and (not _DATE_RE.match(start_date) or start_date > end_date):
            raise LinkEditError("add_historical start_date must be YYYY-MM-DD and <= end_date")
        historical = {
            "ad_account_esid": ad_account_esid,
            "end_date": end_date,
            "source": "reconciliation_2",
            "updated_by": username,
            "updated_at": now_iso,
        }
        if fb_ad_account_id:
            historical["fb_ad_account_id"] = fb_ad_account_id
        if connection_esid:
            historical["connection_esid"] = connection_esid
        if start_date:
            historical["start_date"] = start_date
        if note:
            historical["note"] = note
        return list(links) + [historical]

    raise LinkEditError(f"Unknown action: {action}")


# ── ES helpers ───────────────────────────────────────────────────────────────


def _team_by_title(es, team_title: str) -> dict | None:
    """Resolve a team doc by team_title (the CSV's Team column). team _ids
    are mixed-case esids — never guess them from the title (runbook caution)."""
    resp = es.search(
        index=TEAMS_INDEX,
        size=2,
        body={"query": {"term": {"team_title": team_title}}},
    )
    hits = resp.get("hits", {}).get("hits", []) or []
    if len(hits) != 1:
        return None
    doc = hits[0]
    src = dict(doc.get("_source") or {})
    src["_id"] = doc.get("_id")
    return src


def _account_doc_by_fb_id(es, fb_ad_account_id: str) -> tuple[str, dict] | None:
    """Resolve the canonical account doc for an fb id — the keep
    rule: prefer the newest wizard doc; fall back to the newest doc of any
    origin (legacy-only accounts exist)."""
    resp = es.search(
        index=AD_ACCOUNTS_INDEX,
        size=10,
        body={
            "query": {"term": {"fb_ad_account_id": fb_ad_account_id}},
            "sort": [{"@timestamp": {"order": "desc", "missing": "_last"}}],
        },
    )
    hits = resp.get("hits", {}).get("hits", []) or []
    if not hits:
        return None
    wizard = [h for h in hits if (h.get("_source") or {}).get("created_by_wizard")]
    chosen = wizard[0] if wizard else hits[0]
    return chosen.get("_id"), dict(chosen.get("_source") or {})


def _write_team_links(es, team_esid: str, links: list) -> None:
    """Write the new list + derived scalars (same policy as the Phase-4
    service: scalar follows the newest open link; cleared when none)."""
    scalars = derive_scalars(links)
    if scalars.get("fb_ad_account_esid") is None:
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
    doc = {"fb_ad_account_links": links}
    doc.update(scalars)
    es.update(index=TEAMS_INDEX, id=team_esid, doc=doc, refresh="wait_for")


# ── Subcommands ──────────────────────────────────────────────────────────────


def run_plan(csv_path: str, out_path: str) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES client unavailable")
        return 1

    decisions: list[dict] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            team_title = (row.get("Team") or "").strip()
            if not team_title:
                continue
            confirm = (row.get("Actually run by THIS team? (confirm Y/N + correct account)") or "").strip()
            notes = (row.get("NOTES") or "").strip()
            # The returned sheet renamed this column; accept both spellings so
            # an older copy of the mapping CSV still plans correctly.
            row_account_id = (row.get("Ad Account ID") or row.get("FB Account ID") or "").strip()
            reviewed = (row.get("Reviewed/Corrected") or "").strip()

            verdict = classify_confirmation(confirm, row_account_id, notes, reviewed)

            team = _team_by_title(es, team_title)
            if team is None:
                verdict = {
                    "action": "skip",
                    "ad_account_id": verdict.get("ad_account_id", ""),
                    "classification": "needs_human",
                    "detail": f"team_title {team_title!r} did not resolve to exactly one team doc",
                }

            decisions.append(
                {
                    "team_title": team_title,
                    "team_esid": (team or {}).get("_id", ""),
                    "action": verdict["action"],
                    "ad_account_id": verdict["ad_account_id"],
                    "start_date": "",
                    "end_date": "",
                    "note": notes[:200],
                    "classification": verdict["classification"],
                    "detail": verdict["detail"],
                }
            )

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DECISION_FIELDS)
        writer.writeheader()
        writer.writerows(decisions)

    by_class: dict[str, int] = {}
    for d in decisions:
        by_class[d["classification"]] = by_class.get(d["classification"], 0) + 1
    logger.info("plan: %d rows -> %s (%s)", len(decisions), out_path, by_class)
    print(f"Wrote {len(decisions)} decision rows to {out_path}: {by_class}")
    print("Review/edit the file (esp. needs_human rows and historical dates), then run apply.")
    return 0


def run_apply(decisions_path: str, execute: bool, username: str) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES client unavailable")
        return 1

    now_iso = datetime.now(timezone.utc).isoformat()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    applied = skipped = failed = 0

    with open(decisions_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            team_title = (row.get("team_title") or "").strip()
            team_esid = (row.get("team_esid") or "").strip()
            action = (row.get("action") or "").strip()
            if action in ("", "keep", "skip"):
                skipped += 1
                continue
            try:
                if not team_esid:
                    raise LinkEditError("no team_esid on the decision row")
                team_resp = es.get(index=TEAMS_INDEX, id=team_esid)
                team_src = team_resp.get("_source") or {}
                links = team_src.get("fb_ad_account_links") or []

                ad_account_esid = ""
                fb_id = (row.get("ad_account_id") or "").strip()
                connection_esid = None
                if fb_id:
                    resolved = _account_doc_by_fb_id(es, fb_id)
                    if resolved is None:
                        raise LinkEditError(f"no de_asset_fb_ad_accounts doc for fb id {fb_id} — provision it first")
                    ad_account_esid, account_src = resolved
                    connection_esid = account_src.get("wizard_connection_esid") or team_src.get("connection_esid")

                new_links = build_new_links(
                    links,
                    action,
                    ad_account_esid=ad_account_esid,
                    fb_ad_account_id=fb_id,
                    connection_esid=connection_esid,
                    start_date=(row.get("start_date") or "").strip(),
                    end_date=(row.get("end_date") or "").strip(),
                    note=(row.get("note") or "").strip(),
                    username=username,
                    now_iso=now_iso,
                    today_iso=today_iso,
                )
                open_after = [entry for entry in new_links if not entry.get("end_date")]
                print(
                    f"{'WRITE ' if execute else 'DRY   '}{team_title} ({team_esid}): {action} "
                    f"-> {len(new_links)} link(s), {len(open_after)} open "
                    f"(current: {[entry.get('fb_ad_account_id') or entry.get('ad_account_esid') for entry in open_after]})"
                )
                if execute:
                    _write_team_links(es, team_esid, new_links)
                applied += 1
            except (LinkEditError, Exception) as e:  # noqa: BLE001 — per-row boundary; report and continue
                failed += 1
                print(f"FAIL  {team_title} ({team_esid}): {action} — {e}")
                logger.warning("apply failed for %s: %s", team_title, e)

    mode = "EXECUTED" if execute else "DRY-RUN (no writes; pass --execute)"
    print(f"{mode}: {applied} action(s) {'applied' if execute else 'planned'}, {skipped} keep/skip, {failed} failed")
    return 0 if failed == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="classify the mapping CSV against live ES into a decisions file")
    p_plan.add_argument("--csv", required=True, help="the FB-team-ad-account-mapping CSV (completed)")
    p_plan.add_argument("--out", required=True, help="decisions CSV to write")

    p_apply = sub.add_parser("apply", help="apply a reviewed decisions file (dry-run by default)")
    p_apply.add_argument("--decisions", required=True, help="the reviewed decisions CSV")
    p_apply.add_argument("--execute", action="store_true", help="write to ES (default: dry-run)")
    p_apply.add_argument("--username", default="reconcile_2", help="stamped as updated_by on links")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd == "plan":
        return run_plan(args.csv, args.out)
    return run_apply(args.decisions, args.execute, args.username)


if __name__ == "__main__":
    sys.exit(main())
