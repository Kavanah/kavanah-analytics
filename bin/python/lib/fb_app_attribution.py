"""Which Meta app a team's Facebook pages authenticate under.

Some teams are still authenticated under their own legacy Meta app rather
than the unified Kavanah Analytics app. Legacy apps request materially fewer
OAuth scopes than the wizard (2 vs 7 on the Ads path — ``read_insights`` is
never requested at all), and the importer treats the resulting permission
denials as per-asset skips, so those teams silently lose data (same failure
shape as). Migration progress therefore needs a user-facing surface,
not just the offline ops CLI.

This module is the single source of truth for the app-attribution rule,
extracted from ``tools/fb_legacy_team_health.py`` (which now imports it) so
the Report Scheduler surfaces can reuse it without reimplementing the
mixed/no-pages edge cases:

* **Pages rule** — a ``de_asset_fb_pages`` doc stamped with
  ``wizard_connection_esid`` authenticates via the unified/wizard app; an
  unstamped page falls back to ``fb_account_esid`` → the legacy app that
  account belongs to. There is NO denormalized ``fb_app_id`` on the team doc.
* **Key on app identity, not observed scope gaps** — legacy OAuth never
  persisted granted scopes (no ``/debug_token`` call), so per-team scope
  truth would need live Meta calls and existing logs under-report the
  problem. Which app the team is on is reliable, cheap, and sufficient for
  migration tracking (ticket's detection caveat).

Consumers (both need a BOUNDED number of ES queries regardless of team
count — the report builder scans up to 500 schedules and the org-team tree
renders every visible team, so a per-team fan-out is not acceptable):

* ``report_types_service._build_import_errors_report`` — synthetic
  warning row per legacy/mixed team.
* ``blueprints/report_scheduler`` ``/api/org-team-tree`` — per-team amber
  badge in the page's team tree.

Kept in ``lib/`` (not web/services) so the ops CLI in ``tools/`` can import
it without pulling web-layer dependencies; nothing here touches Flask.
"""

from __future__ import annotations

import logging
from typing import Any

PATH_WIZARD = "wizard"
PATH_LEGACY = "legacy"
PATH_MIXED = "mixed"
PATH_NO_PAGES = "no-pages"

# The paths that mean "this team still authenticates (at least partly) under
# its own legacy Meta app" — the migration worklist. ``mixed`` is included
# deliberately: a partially-migrated team is still losing data on its
# unstamped pages and must not be classified as clean (acceptance criterion).
LEGACY_PATHS = frozenset({PATH_LEGACY, PATH_MIXED})

_PAGES_INDEX = "de_asset_fb_pages"
_APPS_INDEX = "de_asset_fb_apps"
_TEAMS_INDEX = "de_asset_teams"

# Team-doc fields the classification needs. ``ds_facebook`` gates the sweep
# (a team not importing Facebook has no migration urgency — mirrors the
# fb_legacy_team_health CLI's team filter); the page pointers drive the walk.
TEAM_SOURCE_FIELDS = ["ds_facebook", "team_facebook_page_esids"]


def resolve_page_path(
    page_esids: list[str],
    pages_by_esid: dict[str, dict],
    app_titles_by_fb_account_esid: dict[str, list[str]],
) -> tuple[str, int, int, list[str]]:
    """Walk one team's pages. Returns ``(path, wizard_pages, total_pages,
    legacy_apps)``.

    ``path`` is ``wizard`` (every page stamped ``wizard_connection_esid``),
    ``legacy`` (none stamped), ``mixed``, or ``no-pages``. ``legacy_apps``
    lists the distinct legacy app titles the unstamped pages fall back to
    (order of first encounter), so the reader knows what to migrate off.
    Pure function — all ES reads happen in the caller. Moved verbatim from
    ``tools/fb_legacy_team_health.py``; a page esid with no page
    doc is skipped, not counted.
    """
    total = 0
    stamped = 0
    legacy_apps: list[str] = []
    for page_esid in page_esids:
        page = pages_by_esid.get(page_esid)
        if page is None:
            continue
        total += 1
        if page.get("wizard_connection_esid"):
            stamped += 1
        else:
            fb_account_esid = page.get("fb_account_esid")
            for title in app_titles_by_fb_account_esid.get(fb_account_esid or "", []):
                if title not in legacy_apps:
                    legacy_apps.append(title)

    if total == 0:
        return PATH_NO_PAGES, 0, 0, []
    if stamped == total:
        return PATH_WIZARD, stamped, total, []
    if stamped == 0:
        return PATH_LEGACY, 0, total, legacy_apps
    return PATH_MIXED, stamped, total, legacy_apps


def _team_page_esids(team_doc: dict) -> list[str]:
    """The team's FB page pointers, tolerating the legacy scalar shape."""
    raw = (team_doc or {}).get("team_facebook_page_esids") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(e).strip() for e in raw if str(e).strip()]


def legacy_app_findings(es: Any, team_docs_by_esid: dict[str, dict]) -> dict[str, dict]:
    """Classify the given teams; return only the legacy/mixed findings.

    ``{team_esid: {"path", "legacy_apps", "pages_stamped", "pages_total"}}``
    for every ``ds_facebook`` team whose page path is ``legacy`` or ``mixed``.
    A fully-wizard team, a no-pages team, and a team without ``ds_facebook``
    produce NO entry (no false positives — acceptance criterion).

    Bounded ES cost regardless of team count: ONE ids read over the union of
    the teams' page esids + ONE read of the (small) ``de_asset_fb_apps``
    index. The apps index is fetched whole rather than filtered by
    ``fb_account_esid`` terms so the mixed text/keyword mapping on that field
    can't silently drop matches — same posture as the CLI, which loads all
    app docs.

    Fail-soft: any ES error logs and returns ``{}`` — a missing badge/row
    beats a failed report build or a 500 on the team tree.
    """
    fb_teams = {
        esid: doc for esid, doc in (team_docs_by_esid or {}).items() if str((doc or {}).get("ds_facebook")).lower() == "true"
    }
    page_esids = sorted({e for doc in fb_teams.values() for e in _team_page_esids(doc)})
    if not page_esids:
        return {}

    try:
        pages_res = es.search(
            index=_PAGES_INDEX,
            body={
                "query": {"ids": {"values": page_esids}},
                "_source": ["wizard_connection_esid", "fb_account_esid"],
                "size": len(page_esids),
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
        apps_res = es.search(
            index=_APPS_INDEX,
            body={
                "query": {"match_all": {}},
                "_source": ["fb_app_title", "fb_app_id", "fb_account_esid"],
                "size": 1000,
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — a missing badge beats a failed build
        logging.warning("fb_app_attribution.legacy_app_findings: %s", e)
        return {}

    pages_by_esid = {h["_id"]: h.get("_source") or {} for h in pages_res.get("hits", {}).get("hits", [])}
    app_titles_by_fb_account_esid: dict[str, list[str]] = {}
    for h in apps_res.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        fb_account_esid = src.get("fb_account_esid")
        if fb_account_esid:
            app_titles_by_fb_account_esid.setdefault(fb_account_esid, []).append(
                src.get("fb_app_title") or src.get("fb_app_id") or h["_id"]
            )

    findings: dict[str, dict] = {}
    for esid, doc in fb_teams.items():
        path, stamped, total, legacy_apps = resolve_page_path(
            _team_page_esids(doc), pages_by_esid, app_titles_by_fb_account_esid
        )
        if path in LEGACY_PATHS:
            findings[esid] = {
                "path": path,
                "legacy_apps": legacy_apps,
                "pages_stamped": stamped,
                "pages_total": total,
            }
    return findings


def legacy_app_findings_for_esids(team_esids: list[str], es: Any = None) -> dict[str, dict]:
    """:func:`legacy_app_findings` for callers holding only team esids (the
    org-team-tree route). Adds ONE bounded ids read of the team docs; resolves
    its own ES client when none is passed. Fail-soft to ``{}`` like the core.
    """
    esids = [str(e).strip() for e in (team_esids or []) if str(e).strip()]
    if not esids:
        return {}
    try:
        if es is None:
            from lib.es_helpers import get_elasticsearch_client

            es = get_elasticsearch_client()
        if es is None:
            return {}
        res = es.search(
            index=_TEAMS_INDEX,
            body={
                "query": {"ids": {"values": esids}},
                "_source": TEAM_SOURCE_FIELDS,
                "size": len(esids),
            },
        )
    except Exception as e:  # noqa: BLE001 — a missing badge beats a 500 on the tree
        logging.warning("fb_app_attribution.legacy_app_findings_for_esids: %s", e)
        return {}
    team_docs = {h["_id"]: h.get("_source") or {} for h in res.get("hits", {}).get("hits", [])}
    return legacy_app_findings(es, team_docs)


def finding_detail(finding: dict) -> str:
    """One human sentence naming what to migrate off — shared by the report's
    Detail cell and the tree badge tooltip so the two surfaces can't drift.

    Names the offending legacy app title(s) when known; the stamped/total
    ratio makes a ``mixed`` team's partial progress legible. ``legacy_apps``
    can be legitimately empty (unstamped pages whose fb_account_esid matches
    no app doc) — still a legacy path, so still reported.
    """
    apps = ", ".join(finding.get("legacy_apps") or []) or "an unidentified legacy app"
    stamped = finding.get("pages_stamped") or 0
    total = finding.get("pages_total") or 0
    return (
        f"Still authenticated via {apps} — {stamped} of {total} Facebook page"
        f"{'' if total == 1 else 's'} on the unified app. Legacy apps request fewer "
        "permissions, so imports may silently miss data; reconnect Facebook via Data "
        "Connectors to migrate."
    )
