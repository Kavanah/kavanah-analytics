"""Deleted-team data purge.

Deleting a team writes a purge request to ``de_team_purge_requests``
(``asset_management/web/services/team_purge_service.py``) and deletes none of
its data. This job acts on those requests:

* A request is due once ``purge_after`` (requested_at + 7 days) has passed.
* Before anything is deleted the team doc is checked again — in the listing
  AND by a direct ``exists`` on ``de_asset_teams``. A team that is back
  (restored, or recreated with the same esid) cancels its request.
* What is deleted for a due team request, and nothing else:

  - the concrete indices named ``{team_esid}__{source}`` (lowercase, as
    ``build_data_index_name`` builds them) for a ``source`` in
    ``KNOWN_SOURCES``, plus their ``__remap_3`` temp copies. Names are
    matched exactly, never by an ES wildcard;
  - the legacy ``{slug}_{source}`` indices, ONLY when no live team's title
    sanitizes to the same slug (slugs collide) and no live team claims the
    same name. ``echo_user_stats`` is excluded: ``{x}_echo_user_stats`` is
    keyed by an Echo tenant subdomain, not by a team;
  - the team's raw import directories and files under ``/app/data`` (the
    layouts in ``RAW_DIR_LAYOUTS`` / ``RAW_FILE_LAYOUTS``, matched by exact
    directory name or exact file-name shape).

* Hard refusals: a blank or malformed esid / slug (which would turn a name
  into ``__*`` or ``_*``), a live team whose esid differs only by case (the
  index names would be shared), an index name that is not a concrete index,
  is dot-prefixed or ``de_*``, or contains a wildcard character.
* Mass-purge guard: more than ``MASS_PURGE_GUARD`` due team requests in one
  run refuses every deletion unless ``--force`` — a bug that deletes many
  team docs at once must not become a mass data purge a week later.

Orphan report: every concrete ``{prefix}__{source}`` index whose prefix names
no live team is listed, grouped by prefix, with whether a pending request
already covers it. ``--apply-orphans`` writes an ``orphan_index`` purge
request for each uncovered prefix — those then wait the same 7 days. Orphan
requests purge indices only: the team's esid case (needed for raw paths) and
title (needed for legacy names) are unknown. Names with ``__`` that do not
parse as ``{prefix}__{known source}`` are reported as unrecognised and never
become requests.

Dry-run is the default: it writes nothing (no deletes, no request updates).
``--apply`` deletes and marks requests done / cancelled; ``--apply-orphans``
only writes orphan requests. The cron line carries ``--apply`` (added
2026-10-14 after a teammate reviewed the first prod report) and never ``--force``;
``--apply-orphans`` stays a one-off manual run.

Never prints a document value — only index names, team esids, slugs and paths.

Usage::

    python3 /app/python/asset_management/jobs/team_data_purge.py                    # report only
    python3 /app/python/asset_management/jobs/team_data_purge.py --apply
    python3 /app/python/asset_management/jobs/team_data_purge.py --apply-orphans
    python3 /app/python/asset_management/jobs/team_data_purge.py --apply --force    # past the mass guard
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, "/app/python")

import lib.logging  # noqa: E402
from asset_management.web.services import team_purge_service as tps  # noqa: E402
from lib.es_helpers import KNOWN_SOURCES, get_elasticsearch_client, sanitize_team_title  # noqa: E402

LOG_PREFIX = "team-data-purge"

TEAMS_INDEX = "de_asset_teams"
DATA_ROOT = "/app/data"

# remap temp copies: ``{index}__remap_3``.
REMAP_SUFFIX = "__remap_3"

# Every source except echo_user_stats, whose legacy name is
# ``{echo subdomain}_echo_user_stats`` (send_user_stats_to_elastic.py) — keyed
# by an Echo tenant, so a team slug that happens to equal a subdomain must not
# take that tenant's stats with it.
LEGACY_SOURCES: tuple[str, ...] = tuple(s for s in KNOWN_SOURCES if s != "echo_user_stats")
# Longest first, so ``x__fb_insights_window`` parses as fb_insights_window.
_SOURCES_LONGEST_FIRST: tuple[str, ...] = tuple(sorted(KNOWN_SOURCES, key=len, reverse=True))

# Never candidates, whatever else matches: hidden/system dot indices and the
# app's own de_* indices.
PROTECTED_PREFIXES: tuple[str, ...] = (".", "de_")
# A prefix the importers use as a placeholder, never one team's id.
RESERVED_PREFIXES: frozenset[str] = frozenset({"unknown"})

ESID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_WILDCARD_CHARS = frozenset("*?,")

MASS_PURGE_GUARD = 10

# Row outcomes that are not a failure. Anything else (refused_*, purge_*,
# cancel_failed) makes the run exit 1, which run_tracked_job records.
_OK_STATUSES = frozenset({"waiting", "dry_run", "team_exists", "cancelled_team_exists", "purged"})

# (path under DATA_ROOT, depth of the team-esid directory below it). Read off
# the importers' writers (grep team_esid in their /app/data paths):
RAW_DIR_LAYOUTS: tuple[tuple[str, int], ...] = (
    ("facebook/ad_insights", 3),  # {ad_account}/{fb_app}/{team}/ — get_insights.py
    ("facebook/ad_insights_imported", 3),
    ("facebook/ad_insights_window", 1),  # {team}/ — get_insights_window.py (team/page/demo/asset files)
    ("facebook/ad_insights_window", 2),  # {ad_account}/{team}/ — get_insights_window.py (adset/ad files)
    ("facebook/page_insights", 1),  # {team}/ — get_page_insights.py
    ("facebook/page_insights_imported", 1),
    ("facebook/page_posts", 1),
    ("instagram/ig_insights", 2),  # {fb_app}/{team}/ — get_instagram_insights.py
    ("instagram/ig_insights_imported", 2),
    ("google_ads/app_campaigns", 2),  # {customer}/{team}/ — get_campaign_data.py
    ("google_ads/app_campaigns_imported", 2),
    ("youtube_analytics/channel_metrics", 2),  # {channel}/{team}/ — get_youtube_analytics.py
    ("youtube_analytics/channel_metrics_imported", 2),
    ("google_analytics/ga_metrics_window", 2),  # {property}/{team}/ — get_ga_metrics_window.py
    ("echo/conversations", 2),  # {tenant}/{team}/ — echo get_conversations.py
    ("echo/conversations_closed", 2),
    ("echo/conversations_expired", 2),
)
# (path under DATA_ROOT, depth of the directory holding the files, file-name
# regex with ``{esid}`` standing for the escaped esid). The tail after the esid
# may not contain ``__``, so a deleted team ``ab`` never matches the files of a
# live team ``ab__cd``.
_TAIL = r"(?:(?!__)[^/])+\.json"
RAW_FILE_LAYOUTS: tuple[tuple[str, int, str], ...] = (
    ("disciple_tools/raw", 0, r"{esid}__" + _TAIL),  # {team}__{post_type}.json — DT fetch.py
    ("disciple_tools/raw_imported", 0, r"\d{{14}}__{esid}__" + _TAIL),  # {stamp}__{team}__{post_type}.json
    ("tiktok/organic", 1, r"{esid}__" + _TAIL),  # {run}/{team}__{connection}.json — tiktok get_organic_stats.py
    ("tiktok/organic_imported", 1, r"{esid}__" + _TAIL),
)
# Not purged per team, because their files are not keyed by team: Bitly (one
# file per run, every team), Chatwoot (by account), Echo user stats (by tenant),
# GA daily metrics (by property). Retention ages those out.


class PurgeRefused(ValueError):
    """A name or listing the purge must not act on."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _body(resp: Any) -> Any:
    """Unwrap an elasticsearch-py ObjectApiResponse (or pass a dict through)."""
    return resp.body if hasattr(resp, "body") else resp


def _not_found(exc: Exception) -> bool:
    err = str(exc).lower()
    return "index_not_found" in err or "404" in err


# ── Names ─────────────────────────────────────────────────────────────────────


def team_index_prefix(team_esid: Any) -> str:
    """The lowercase ``{prefix}`` of a team's ``{prefix}__{source}`` indices.

    Refuses a blank / malformed esid: ``"" + "__"`` would name every index
    whose prefix is empty, and a wildcard character would widen any match.
    """
    esid = str(team_esid or "").strip()
    if not esid or not ESID_RE.match(esid):
        raise PurgeRefused(f"refusing team esid {esid!r}: blank or not an ES-safe id")
    prefix = esid.lower()
    if prefix in RESERVED_PREFIXES:
        raise PurgeRefused(f"refusing reserved prefix {prefix!r}")
    return prefix


def legacy_slug(slug: Any) -> str:
    """A legacy ``{slug}_{source}`` slug, validated. Refuses blank / ``unknown``."""
    s = str(slug or "").strip()
    if not s or s == "unknown" or not SLUG_RE.match(s):
        raise PurgeRefused(f"refusing legacy slug {s!r}: blank, shared or not a sanitized slug")
    return s


def is_protected(name: str) -> bool:
    return not name or name.startswith(PROTECTED_PREFIXES) or bool(_WILDCARD_CHARS & set(name))


def split_team_index(name: str) -> tuple[str, str] | None:
    """``(prefix, source)`` for ``{prefix}__{source}[__remap_3]``, else None.

    ``source`` must be a KNOWN_SOURCES entry and the prefix an ES-safe id, so
    ``ab__cd__fb_insights`` parses as prefix ``ab__cd`` — never as ``ab``.
    """
    if is_protected(name) or "__" not in name:
        return None
    base = name[: -len(REMAP_SUFFIX)] if name.endswith(REMAP_SUFFIX) else name
    for source in _SOURCES_LONGEST_FIRST:
        tail = f"__{source}"
        if base.endswith(tail):
            prefix = base[: -len(tail)]
            if prefix and ESID_RE.match(prefix) and prefix not in RESERVED_PREFIXES:
                return prefix, source
            return None
    return None


def team_index_names(prefix: str, concrete: set[str]) -> list[str]:
    """The concrete indices that belong to ``prefix`` — exact parse, no wildcard."""
    return sorted(n for n in concrete if (split_team_index(n) or ("", ""))[0] == prefix)


def unrecognised_names_for(prefix: str, concrete: set[str]) -> list[str]:
    """Indices that start ``{prefix}__`` but do not parse as any team's (an
    unknown source) — reported, never deleted. A name that parses to a longer
    prefix (``{prefix}__x__{source}``) belongs to that prefix, not this one."""
    return sorted(n for n in concrete if n.startswith(f"{prefix}__") and not is_protected(n) and split_team_index(n) is None)


def legacy_names(slug: str) -> set[str]:
    names = {f"{slug}_{s}" for s in LEGACY_SOURCES}
    return names | {f"{n}{REMAP_SUFFIX}" for n in names}


# ── ES reads ──────────────────────────────────────────────────────────────────


def list_concrete_indices(es: Any) -> set[str]:  # Any: the ES client is untyped
    """Every concrete open/closed index name (aliases are keys of nothing here)."""
    body = _body(es.indices.get_alias(index="*", expand_wildcards="open,closed"))
    return {str(n) for n in (body.keys() if hasattr(body, "keys") else [])}


def list_live_teams(es: Any) -> dict[str, str]:  # Any: the ES client is untyped
    """``{team_esid: team_title}`` for every team. Fails closed.

    An unreadable, empty or truncated listing raises PurgeRefused: with no
    live teams every data index would look deletable.
    """
    try:
        resp = _body(
            es.search(
                index=TEAMS_INDEX,
                body={"query": {"match_all": {}}, "_source": ["team_title"], "size": 10000, "track_total_hits": True},
            )
        )
    except Exception as exc:  # noqa: BLE001 — refusal boundary
        raise PurgeRefused(f"could not list {TEAMS_INDEX}: {type(exc).__name__}") from exc
    hits = (resp.get("hits") or {}).get("hits") or []
    total = int((((resp.get("hits") or {}).get("total")) or {}).get("value") or len(hits))
    if not hits:
        raise PurgeRefused(f"{TEAMS_INDEX} returned no teams — refusing to treat every index as deletable")
    if total > len(hits):
        raise PurgeRefused(f"{TEAMS_INDEX} listing truncated ({len(hits)} of {total})")
    return {str(h["_id"]): str((h.get("_source") or {}).get("team_title") or "") for h in hits}


def list_pending_requests(es: Any) -> list[dict[str, Any]]:  # Any: the ES client is untyped
    try:
        resp = _body(
            es.search(
                index=tps.TOMBSTONE_INDEX,
                body={"query": {"term": {"status": tps.STATUS_PENDING}}, "size": 10000},
            )
        )
    except Exception as exc:  # noqa: BLE001
        if _not_found(exc):
            return []
        raise
    return [{**(h.get("_source") or {}), "_id": h["_id"]} for h in (resp.get("hits") or {}).get("hits") or []]


# ── Raw files ─────────────────────────────────────────────────────────────────


def _subdirs(path: str) -> list[str]:
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    return [
        os.path.join(path, n)
        for n in names
        if os.path.isdir(os.path.join(path, n)) and not os.path.islink(os.path.join(path, n))
    ]


def _dirs_at_depth(root: str, depth: int) -> list[str]:
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    level = [root]
    for _ in range(depth):
        level = [d for parent in level for d in _subdirs(parent)]
    return level


def find_raw_paths(team_esid: str, *, data_root: str = DATA_ROOT) -> list[str]:
    """The team's raw directories and files. Exact name matches only
    (``os.listdir`` membership, so a case-insensitive filesystem cannot widen
    it); symlinks are never followed or returned."""
    if not ESID_RE.match(team_esid or ""):
        raise PurgeRefused(f"refusing raw-file search for esid {team_esid!r}")
    out: list[str] = []
    for rel, depth in RAW_DIR_LAYOUTS:
        for parent in _dirs_at_depth(os.path.join(data_root, rel), depth - 1):
            try:
                names = os.listdir(parent)
            except OSError:
                continue
            if team_esid in names:
                path = os.path.join(parent, team_esid)
                if os.path.isdir(path) and not os.path.islink(path):
                    out.append(path)
    for rel, depth, template in RAW_FILE_LAYOUTS:
        pattern = re.compile(template.format(esid=re.escape(team_esid)))
        for d in _dirs_at_depth(os.path.join(data_root, rel), depth):
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for name in names:
                path = os.path.join(d, name)
                if pattern.fullmatch(name) and os.path.isfile(path) and not os.path.islink(path):
                    out.append(path)
    return sorted(set(out))


def _remove_raw(path: str, data_root: str) -> None:
    root = os.path.realpath(data_root) + os.sep
    real = os.path.realpath(path)
    if os.path.islink(path) or not real.startswith(root):
        raise PurgeRefused(f"refusing to remove {path!r}: a symlink or outside {data_root}")
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


# ── Plan + apply ──────────────────────────────────────────────────────────────


def plan_request(
    req: dict[str, Any], *, now: datetime, live: dict[str, str], concrete: set[str], data_root: str = DATA_ROOT
) -> dict[str, Any]:
    """What one pending request would delete (read-only)."""
    kind = req.get("kind") or tps.KIND_TEAM
    row: dict[str, Any] = {
        "request": req.get("_id"),
        "kind": kind,
        "team_esid": req.get("team_esid") or "",
        "index_prefix": req.get("index_prefix") or "",
        "legacy_slug": req.get("legacy_slug") or "",
        "purge_after": req.get("purge_after"),
    }
    if kind not in (tps.KIND_TEAM, tps.KIND_ORPHAN):
        row["status"] = "refused_unknown_kind"
        return row
    purge_after = _parse_iso(req.get("purge_after"))
    if purge_after is None:
        row["status"] = "refused_bad_purge_after"
        return row
    try:
        prefix = team_index_prefix(req.get("team_esid") if kind == tps.KIND_TEAM else req.get("index_prefix"))
    except PurgeRefused as exc:
        row.update(status="refused_bad_esid", error=str(exc))
        return row
    if row["index_prefix"] and row["index_prefix"] != prefix:
        row.update(status="refused_prefix_mismatch")
        return row
    row["index_prefix"] = prefix

    if kind == tps.KIND_TEAM and row["team_esid"] in live:
        row["status"] = "team_exists"
        return row
    live_lower = {e.lower(): e for e in live}
    if prefix in live_lower:
        # Orphan request whose team is back, or a live team whose esid differs
        # only by case: the index names are shared — never delete them.
        row.update(status="team_exists" if kind == tps.KIND_ORPHAN else "refused_case_collision")
        return row

    row["indices"] = team_index_names(prefix, concrete)
    row["unrecognised"] = unrecognised_names_for(prefix, concrete)
    row["legacy_indices"] = []
    if kind == tps.KIND_TEAM and row["legacy_slug"]:
        try:
            slug = legacy_slug(row["legacy_slug"])
        except PurgeRefused as exc:
            row.update(status="refused_bad_slug", error=str(exc))
            return row
        live_slugs = {sanitize_team_title(t) for t in live.values()}
        claimed = {n for s in live_slugs for n in legacy_names(s)}
        if slug in live_slugs:
            row["legacy_skipped"] = "slug_shared_with_live_team"
        else:
            row["legacy_indices"] = sorted(
                n for n in legacy_names(slug) & concrete if n not in claimed and not is_protected(n)
            )
    row["raw_paths"] = find_raw_paths(row["team_esid"], data_root=data_root) if kind == tps.KIND_TEAM else []
    row["status"] = "due" if purge_after <= now else "waiting"
    return row


def _delete_index(es: Any, name: str, concrete: set[str]) -> None:  # Any: the ES client is untyped
    if is_protected(name) or name not in concrete:
        raise PurgeRefused(f"refusing to delete {name!r}: protected, wildcard or not a concrete index")
    es.indices.delete(index=name, expand_wildcards="none")


def _update_request(es: Any, req_id: str, fields: dict[str, Any]) -> None:  # Any: the ES client is untyped
    es.update(index=tps.TOMBSTONE_INDEX, id=req_id, body={"doc": fields}, refresh="wait_for")


def apply_request(
    es: Any, row: dict[str, Any], *, now: datetime, concrete: set[str], data_root: str = DATA_ROOT
) -> dict[str, Any]:  # Any: the ES client is untyped
    """Delete a due request's data, after re-checking the team is still gone."""
    if row["kind"] == tps.KIND_TEAM:
        try:
            exists = bool(es.exists(index=TEAMS_INDEX, id=row["team_esid"]))
        except Exception as exc:  # noqa: BLE001 — cannot prove it is gone: keep everything
            row.update(status="refused_recheck_failed", error=type(exc).__name__)
            return row
        if exists:
            row["status"] = "team_exists"
            return row

    deleted: list[str] = []
    errors: list[str] = []
    for name in row.get("indices", []) + row.get("legacy_indices", []):
        try:
            _delete_index(es, name, concrete)
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001 — record and keep the request pending
            errors.append(f"{name}: {type(exc).__name__}")
    removed: list[str] = []
    for path in row.get("raw_paths", []):
        try:
            _remove_raw(path, data_root)
            removed.append(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {type(exc).__name__}")
    row.update(deleted_indices=deleted, removed_paths=removed, errors=errors)
    row["status"] = "purged" if not errors else "purge_incomplete"
    result = {"deleted_indices": deleted, "removed_paths": removed, "errors": errors}
    if errors:
        _update_request(es, row["request"], {"result": result})
    else:
        _update_request(es, row["request"], {"status": tps.STATUS_DONE, "done_at": _iso(now), "result": result})
    return row


# ── Orphans ───────────────────────────────────────────────────────────────────


def orphan_report(concrete: set[str], live: dict[str, str], pending_prefixes: set[str]) -> dict[str, Any]:
    """Data indices whose prefix names no live team (read-only)."""
    live_lower = {e.lower() for e in live}
    orphans: dict[str, list[str]] = {}
    covered: dict[str, list[str]] = {}
    unrecognised: list[str] = []
    for name in sorted(concrete):
        if is_protected(name) or "__" not in name:
            continue
        parsed = split_team_index(name)
        if parsed is None:
            unrecognised.append(name)
            continue
        prefix = parsed[0]
        if prefix in live_lower:
            continue
        (covered if prefix in pending_prefixes else orphans).setdefault(prefix, []).append(name)
    return {"orphans": orphans, "covered_by_request": covered, "unrecognised": unrecognised}


def record_orphan_requests(es: Any, prefixes: list[str], *, now: datetime) -> int:  # Any: the ES client is untyped
    if not prefixes:
        return 0
    if not tps.ensure_tombstone_index(es):
        raise PurgeRefused(f"could not ensure {tps.TOMBSTONE_INDEX}")
    written = 0
    for prefix in prefixes:
        team_index_prefix(prefix)  # refuse a malformed prefix outright
        doc = {
            "kind": tps.KIND_ORPHAN,
            "team_esid": "",
            "index_prefix": prefix,
            "legacy_slug": "",
            "team_title": "",
            "workspace_esid": "",
            "reason": "orphan_index",
            "requested_by": "system:team-data-purge --apply-orphans",
            "requested_at": _iso(now),
            "purge_after": _iso(now + timedelta(days=tps.GRACE_DAYS)),
            "status": tps.STATUS_PENDING,
        }
        es.index(index=tps.TOMBSTONE_INDEX, id=tps.tombstone_id(tps.KIND_ORPHAN, prefix), body=doc, refresh="wait_for")
        written += 1
    return written


# ── Entry point ───────────────────────────────────────────────────────────────


def sweep(
    es: Any,  # Any: the ES client is untyped
    *,
    apply: bool,
    apply_orphans: bool,
    force: bool = False,
    now: datetime | None = None,
    data_root: str = DATA_ROOT,
) -> dict[str, Any]:
    """One pass: plan every pending request, act on the due ones (``apply``),
    then report orphans (and request them with ``apply_orphans``)."""
    now = now or _now()
    report: dict[str, Any] = {"apply": apply, "apply_orphans": apply_orphans, "ok": True, "requests": []}
    try:
        live = list_live_teams(es)
        concrete = list_concrete_indices(es)
        pending = list_pending_requests(es)
    except Exception as exc:  # noqa: BLE001 — refusal boundary: no partial view acts
        report.update(ok=False, status="refused", error=str(exc) if isinstance(exc, PurgeRefused) else type(exc).__name__)
        return report

    rows = [plan_request(r, now=now, live=live, concrete=concrete, data_root=data_root) for r in pending]
    due = [r for r in rows if r["status"] == "due"]
    guard_tripped = len(due) > MASS_PURGE_GUARD and not force
    for row in rows:
        if row["status"] == "due":
            if guard_tripped:
                row["status"] = "refused_mass_purge_guard"
            elif not apply:
                row["status"] = "dry_run"
            else:
                try:
                    apply_request(es, row, now=now, concrete=concrete, data_root=data_root)
                except Exception as exc:  # noqa: BLE001 — one request must not sink the rest
                    row.update(status="purge_failed", error=type(exc).__name__)
        if row["status"] == "team_exists" and apply:
            # The team is back (restored / recreated with the same esid): its
            # request is cancelled; a later delete writes a new one.
            try:
                _update_request(es, row["request"], {"status": tps.STATUS_CANCELLED, "cancelled_reason": "team_exists"})
                row["status"] = "cancelled_team_exists"
            except Exception as exc:  # noqa: BLE001
                row.update(status="cancel_failed", error=type(exc).__name__)
        if row["status"] not in _OK_STATUSES:
            report["ok"] = False
        report["requests"].append(row)
    if guard_tripped:
        report["mass_purge_guard"] = {"due": len(due), "limit": MASS_PURGE_GUARD}

    pending_prefixes = {r["index_prefix"] for r in rows if r.get("index_prefix")}
    # A request done or cancelled this run no longer covers its prefix.
    pending_prefixes -= {r["index_prefix"] for r in rows if r["status"] in ("purged", "cancelled_team_exists")}
    orphans = orphan_report(concrete - _deleted(rows), live, pending_prefixes)
    report["orphans"] = orphans
    if apply_orphans and orphans["orphans"]:
        try:
            report["orphan_requests_written"] = record_orphan_requests(es, sorted(orphans["orphans"]), now=now)
        except Exception as exc:  # noqa: BLE001
            report.update(ok=False, orphan_requests_error=str(exc) if isinstance(exc, PurgeRefused) else type(exc).__name__)
    return report


def _deleted(rows: list[dict[str, Any]]) -> set[str]:
    return {n for r in rows for n in r.get("deleted_indices", [])}


def _log_report(report: dict[str, Any]) -> None:
    logging.info(
        "%s: mode=%s%s ok=%s",
        LOG_PREFIX,
        "APPLY" if report["apply"] else "DRY-RUN",
        " +ORPHAN-REQUESTS" if report["apply_orphans"] else "",
        report["ok"],
    )
    if report.get("status") == "refused":
        logging.error("%s: REFUSED — %s", LOG_PREFIX, report.get("error"))
        return
    logging.info("%s: purge requests — %d row(s)", LOG_PREFIX, len(report["requests"]))
    for row in report["requests"]:
        logging.info("%s:   %s", LOG_PREFIX, json.dumps(row, sort_keys=True, default=str))
    orphans = report.get("orphans") or {}
    logging.info(
        "%s: orphan report — %d uncovered prefix(es), %d covered by a request, %d unrecognised name(s)",
        LOG_PREFIX,
        len(orphans.get("orphans") or {}),
        len(orphans.get("covered_by_request") or {}),
        len(orphans.get("unrecognised") or []),
    )
    for prefix, names in sorted((orphans.get("orphans") or {}).items()):
        logging.info("%s:   orphan %s: %s", LOG_PREFIX, prefix, ", ".join(names))
    for name in orphans.get("unrecognised") or []:
        logging.info("%s:   unrecognised %s", LOG_PREFIX, name)


def main(argv: list[str] | None = None) -> int:
    lib.logging.define(LOG_PREFIX)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="delete due requests' data (default: report only)")
    ap.add_argument("--apply-orphans", action="store_true", help="write purge requests for orphan index prefixes")
    ap.add_argument("--force", action="store_true", help=f"allow more than {MASS_PURGE_GUARD} due team purges in one run")
    args = ap.parse_args(argv)
    es = get_elasticsearch_client()
    if es is None:
        logging.error("%s: no Elasticsearch connection", LOG_PREFIX)
        return 1
    report = sweep(es, apply=args.apply, apply_orphans=args.apply_orphans, force=args.force)
    _log_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
