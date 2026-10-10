"""clean up FB ad-insights and FB page-insights docs that leaked
into the wrong team's index.

Leak definition
===============
A doc in ``<team>_fb_insights`` or ``<team>_fb_page_insights`` is a leak
when its FB-page-id field is not in that team's current
``team_facebook_page_id`` list. Concretely:

* ``*_fb_insights``       → leak if doc's ``page_id`` ∉ team's page list
* ``*_fb_page_insights``  → leak if doc's ``fb_page_id`` ∉ team's page list

This catches the historical contamination from earlier asset-config
errors (e.g. one team's index containing 71 docs for fb_page_id
987654321098765, a page that was once linked to the team
and later removed but whose insight docs persisted).

Audit
=====
Before cleanup, this script also checks ``de_asset_teams`` for genuine
``team_facebook_page_id`` collisions across teams — a page id that
appears under multiple teams' lists is the original Failure-Mode-2
trigger. If the audit finds collisions, the import-side
fix in ``get_page_insights.py`` is genuinely needed; if not, the leak
docs are purely historical and the cleanup script alone is enough.

Usage
=====
::

    # Dry-run audit + leak survey (default)
    python3 /app/python/import.py migration cleanup_leaked_fb_insights

    # Apply on dev/staging (snapshot + delete)
    python3 /app/python/import.py migration cleanup_leaked_fb_insights \\
        --apply --repo migration_repo

    # Apply on prod
    python3 /app/python/import.py migration cleanup_leaked_fb_insights \\
        --apply --repo daily_repo

Notes
=====
- Idempotent. Running on a clean cluster reports zero leaks and exits.
- Snapshot is taken before any delete; refusal to delete if snapshot
  fails. Snapshot name: ``pre-fb-leak-cleanup-<YYYYMMDD-HHMMSS>``.
- A team whose ``team_facebook_page_id`` list is empty is skipped (we
  can't tell what's a leak when there's no truth to compare against).
"""

from __future__ import annotations

import logging
from collections import defaultdict

import lib.logging
from migration.snapshot_gate import (  # the shared gate replaces the per-script copy
    MigrationRefused,
    resolve_apply_mode,
    take_snapshot,
)
from lib.es_helpers import get_elasticsearch_client


class CleanupError(Exception):
    """Raised when the cleanup cannot proceed safely."""


# (index suffix, page-id field name on the doc)
_TARGETS = [
    ("_fb_insights", "page_id"),
    ("_fb_page_insights", "fb_page_id"),
]


def _load_team_pages(es) -> dict[str, set[str]]:
    """Return ``{team_esid: set(page_ids)}`` from de_asset_teams.

    Includes only teams with at least one page id — empty lists mean
    "no truth to compare against," and we'd rather skip an index than
    delete every doc in it.
    """
    team_pages: dict[str, set[str]] = {}
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "_source": ["team_title", "team_facebook_page_id"],
            "query": {"match_all": {}},
        },
    )
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        page_ids = src.get("team_facebook_page_id") or []
        if isinstance(page_ids, str):
            page_ids = [page_ids]
        page_ids = {str(p) for p in page_ids if p}
        if page_ids:
            team_pages[hit["_id"]] = page_ids
    return team_pages


def _team_title_to_esid(es) -> dict[str, str]:
    """Return ``{team_title: team_esid}`` for resolving index → team."""
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "_source": ["team_title"],
            "query": {"match_all": {}},
        },
    )
    out = {}
    for hit in resp["hits"]["hits"]:
        title = hit["_source"].get("team_title")
        if title:
            out[title] = hit["_id"]
    return out


def _audit_page_id_collisions(es) -> dict[str, list[str]]:
    """Return ``{page_id: [team_titles]}`` for page ids that appear in
    more than one team's ``team_facebook_page_id`` list.

    These are the Failure-Mode-2 collisions from a genuine
    asset-config corruption that the import-side last-team-wins logic
    silently masks.
    """
    page_to_teams: dict[str, list[str]] = defaultdict(list)
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "_source": ["team_title", "team_facebook_page_id"],
            "query": {"match_all": {}},
        },
    )
    for hit in resp["hits"]["hits"]:
        title = hit["_source"].get("team_title", hit["_id"])
        pids = hit["_source"].get("team_facebook_page_id") or []
        if isinstance(pids, str):
            pids = [pids]
        for pid in pids:
            page_to_teams[str(pid)].append(title)
    return {pid: titles for pid, titles in page_to_teams.items() if len(titles) > 1}


def _scan_leaks_in_index(
    es,
    index_name: str,
    page_id_field: str,
    expected_pages: set[str],
) -> list[dict]:
    """Return doc hits whose page_id field is not in expected_pages.

    Drains the scroll into a list before returning so callers can do
    deletes without invalidating an open scroll.
    """
    leaks = []
    resp = es.search(
        index=index_name,
        body={
            "size": 1000,
            "_source": [page_id_field, "team_title"],
            "sort": ["_doc"],
            "query": {"match_all": {}},
        },
        scroll="2m",
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                page_id = hit["_source"].get(page_id_field)
                if page_id is not None and str(page_id) not in expected_pages:
                    leaks.append(hit)
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass
    return leaks


def _take_snapshot(es, repo: str, indices: list[str]) -> str:
    """delegates to the shared gate: timestamped name, real
    request_timeout (the old copy ran a wait_for_completion snapshot on
    the ~10s default client timeout), state == SUCCESS check (a PARTIAL
    snapshot is not a restore point), recent-reuse on retry."""
    return take_snapshot(es, repo, indices, name_prefix="pre-fb-leak-cleanup")


def _delete_leaks(es, leaks_by_index: dict[str, list[dict]]) -> int:
    """Delete the listed leak docs. Returns count actually deleted."""
    total = 0
    for index_name, hits in leaks_by_index.items():
        for hit in hits:
            try:
                es.delete(index=index_name, id=hit["_id"])
                total += 1
            except Exception as e:
                logging.error(f"Failed to delete {index_name}/{hit['_id']}: {e}")
    return total


def main(params: dict) -> None:
    lib.logging.define("fb-leak-cleanup")
    # shared parse: --apply must be an explicit true value
    # (import.py delivers `--apply=false` as the truthy string "false",
    # which the old bool check read as write mode — the review
    # finding); --apply + explicit --dry-run=true refuses.
    try:
        _dry_run, _ = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    apply_changes = not _dry_run
    repo = params.get("repo") or "daily_repo"

    es = get_elasticsearch_client()
    if es is None:
        raise CleanupError("Elasticsearch client unavailable")

    # ── Audit first: surface genuine collisions in de_asset_teams ────────
    logging.info("Auditing de_asset_teams for team_facebook_page_id collisions...")
    collisions = _audit_page_id_collisions(es)
    if collisions:
        logging.warning(f"Found {len(collisions)} page id(s) shared across multiple teams:")
        for pid, titles in sorted(collisions.items()):
            logging.warning(f"  page_id {pid}: {titles}")
        logging.warning(
            "These are real asset-config collisions. The import-side fix in "
            "get_page_insights.py is needed to detect-and-warn at import time. "
            "The cleanup below will use each team's current page list as truth, "
            "which means BOTH teams will retain their docs for a shared page id."
        )
    else:
        logging.info("No team_facebook_page_id collisions found.")

    # ── Build the truth: team_esid → set(page_ids), and team_title → esid ─
    team_pages = _load_team_pages(es)
    title_to_esid = _team_title_to_esid(es)

    # ── Scan every *_fb_insights and *_fb_page_insights index for leaks ──
    leaks_by_index: dict[str, list[dict]] = {}
    skipped_no_team: list[str] = []
    skipped_empty_pages: list[str] = []

    for suffix, page_id_field in _TARGETS:
        try:
            indices = es.indices.get(index=f"*{suffix}", ignore_unavailable=True)
        except Exception as e:
            logging.warning(f"Could not list indices for *{suffix}: {e}")
            continue

        for index_name in sorted(indices.keys()):
            # Resolve the index back to a team. Index name format:
            # <sanitized_team_title><suffix>. Look up team_title by trying
            # each team's sanitize() against this prefix — but we don't
            # need to: we can sample a doc and read team_title from it.
            sample = es.search(
                index=index_name,
                body={"size": 1, "_source": ["team_title"]},
                ignore_unavailable=True,
            )
            sample_hits = sample.get("hits", {}).get("hits", [])
            if not sample_hits:
                # Empty index — nothing to clean up
                continue
            team_title = sample_hits[0]["_source"].get("team_title")
            if not team_title:
                logging.warning(f"{index_name}: no team_title in sample doc; skipping")
                skipped_no_team.append(index_name)
                continue

            team_esid = title_to_esid.get(team_title)
            if not team_esid:
                logging.warning(f"{index_name}: team_title={team_title!r} not found in " "de_asset_teams; skipping")
                skipped_no_team.append(index_name)
                continue

            expected_pages = team_pages.get(team_esid)
            if not expected_pages:
                logging.warning(
                    f"{index_name}: team {team_title!r} has empty "
                    "team_facebook_page_id list; skipping (no truth to "
                    "compare against)"
                )
                skipped_empty_pages.append(index_name)
                continue

            leaks = _scan_leaks_in_index(es, index_name, page_id_field, expected_pages)
            if leaks:
                leaks_by_index[index_name] = leaks

    # ── Report ────────────────────────────────────────────────────────────
    total_leaks = sum(len(hits) for hits in leaks_by_index.values())
    if total_leaks == 0:
        logging.info("No leaked docs found. Nothing to do.")
        print("cleanup_leaked_fb_insights: 0 leaks. Cluster is clean.")
        return

    logging.warning(f"Found {total_leaks} leak doc(s) across {len(leaks_by_index)} index(es):")
    for index_name in sorted(leaks_by_index.keys()):
        hits = leaks_by_index[index_name]
        # Group by leaked page_id for a compact summary
        by_pid: dict[str, int] = defaultdict(int)
        page_field = next(field for suffix, field in _TARGETS if index_name.endswith(suffix))
        for hit in hits:
            by_pid[str(hit["_source"].get(page_field))] += 1
        logging.warning(f"  {index_name}: {len(hits)} leak(s)")
        for pid, count in sorted(by_pid.items()):
            logging.warning(f"    page_id {pid}: {count} doc(s)")

    if not apply_changes:
        print(f"cleanup_leaked_fb_insights: {total_leaks} leak doc(s) found across {len(leaks_by_index)} index(es) (dry run):")
        for index_name in sorted(leaks_by_index.keys()):
            print(f"  - {index_name}: {len(leaks_by_index[index_name])} leak(s)")
        if collisions:
            print(f"Plus {len(collisions)} de_asset_teams page-id collision(s) — see log for details.")
        print("Re-run with --apply to snapshot + delete. See log file for per-page detail.")
        return

    # ── Apply mode: snapshot affected indices, then delete leaks ─────────
    affected_indices = sorted(leaks_by_index.keys())
    try:
        snapshot_name = _take_snapshot(es, repo, affected_indices)
    except Exception as e:
        raise CleanupError(
            f"Snapshot to {repo} failed ({e}); refusing to delete without "
            "a backup. Try a different --repo, or check that the snapshot "
            "repo exists."
        ) from e

    deleted = _delete_leaks(es, leaks_by_index)
    logging.info(f"Cleanup complete. Deleted {deleted} leak doc(s). " f"Snapshot: {repo}/{snapshot_name}.")
    print(
        f"cleanup_leaked_fb_insights: deleted {deleted} leak doc(s) across "
        f"{len(affected_indices)} index(es). Snapshot: {repo}/{snapshot_name}."
    )
