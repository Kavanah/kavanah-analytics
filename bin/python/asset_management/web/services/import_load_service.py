"""import_load_service.py — concurrent-import capacity testing.

Backs the Testing page's import-load mode: kick off N simultaneous re-imports
of already-imported date ranges and watch what the server does. Three parts:

  candidates  what CAN be re-imported: the per-team data indices
              (``{team_esid}__{source_suffix}``, the scheme) say
              directly which team has which source's data and how fresh it
              is — far sturdier than reverse-engineering progress docs. Each
              candidate carries its effective import-lock group so the UI
              can warn before two selections would silently serialize.
  run         validate + delegate to ImportService.run_imports. Selections
              sharing a non-empty lock group are REJECTED, not warned:
              the lock loser exits cleanly (verified: the importer's
              lock-loser branch is a silent no-op), which would fake a
              "server survived N imports" result at N-1 actual imports.
  status      per-source start/end/duration merged from the import-runner
              status file, for the load-report's import markers.

force_refresh is hard-coded off: re-imports must stay within the /
 skip guards (FB) and plain upserts (everything else) so a capacity
test never burns real Meta API quota. FB rows are flagged ``light`` because
those same guards make an FB re-import nearly free — real load comes from
the other connectors.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

from lib.prod_hosts import is_prod_host
from lib.errors import UserFacingError

logger = logging.getLogger(__name__)

# raised from 8 once status records keyed per batch item instead
# of per source (the old artificial one-import-per-source cap). The lock layer
# still bounds true parallelism; validate_imports rejects conflicting groups.
MAX_CONCURRENT_IMPORTS = 20
# Bound the per-import work so a capacity test can't accidentally queue a
# 90-day backfill × 8.
MAX_RANGE_DAYS = 14
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Same prod posture as the identity seeder: capacity tests belong on staging.
# The hostname list lives in lib.prod_hosts.
_ALLOW_PROD_ENV = "LOAD_TEST_ALLOW_PROD"

# Where run-import-load records the latest run so the load report can overlay
# import markers on the degradation/resource charts.
LATEST_RUN_PATH = "/app/data/logs/import_load_latest.json"

# per-team index suffix → manual-import source (ImportService).
# ``*_window`` suffixes are derived read-model indices, not import targets.
_SUFFIX_TO_SOURCE = {
    "fb_insights": "facebook_ads",
    "fb_page_insights": "facebook_pages",
    "fb_page_posts": "facebook_pages",
    "ig_insights": "instagram",
    "echo_conversations": "echo",
    "ga_metrics": "google_analytics",
    "google_ads": "google_ads",
    "youtube_analytics": "youtube",
    "chatwoot_conversations": "chatwoot",
    "chatwoot_insights": "chatwoot",
    "disciple_tools": "disciple_tools",

    "tiktok_organic": "tiktok",
}

# The (per-ad) + (per-day) skip guards make a no-force FB
# re-import skip both the fetch AND the write — it exercises spawn/lock/
# progress but generates almost no CPU/ES load.
_LIGHT_SOURCES = frozenset({"facebook_ads", "facebook_pages"})

# The Meta family (fb_ads/fb_pages/instagram) locks on "facebook". Since
# the key is PER-CONNECTION (facebook::<connection_esid>) for teams
# on a wizard connection; legacy/unresolvable cohorts stay connector-global.
_META_SOURCES = frozenset({"facebook_ads", "facebook_pages", "instagram"})

# Importers that take no cluster lock — never serialize, so no lock group.
# tiktok locks only per connection, around its token refresh.
_UNLOCKED_SOURCES = frozenset({"youtube", "chatwoot", "disciple_tools", "tiktok"})


class ImportLoadError(UserFacingError):
    """Validation/guard failure with an operator-readable message."""


def refuse_prod(hostname: str) -> None:
    """Hard-fail on a prod hostname unless LOAD_TEST_ALLOW_PROD=1 (same
    belt-and-suspenders posture as tools/seed_load_test_identities)."""
    lower = (hostname or "").lower()
    if not is_prod_host(lower):
        return
    if os.environ.get(_ALLOW_PROD_ENV) == "1":
        logger.warning("import-load: prod opt-in present (%s=1); proceeding on %s", _ALLOW_PROD_ENV, hostname)
        return
    raise ImportLoadError(
        f"Refusing to run an import capacity test against production ({hostname}). "
        f"Set {_ALLOW_PROD_ENV}=1 to do this deliberately."
    )


def lock_group(source: str, team_ids: str, es=None) -> str:
    """The effective import-lock key a manual import of (source, team) will
    contend on. "" = the importer takes no lock (never serializes).

    Mirrors the real lock sites: Meta family → per-CONNECTION key from the
    team's wizard connection (; legacy/unresolvable → connector-global
    "facebook"); google_ads / GA4 → per-scope key from the resolved
    customer/property ids; echo → per-tenant key.
    Resolution failures fall back to the connector-global key —
    over-serializing a warning is safe, under-serializing would hide a
    silent no-op.

    NOTE: a connector-global group ("facebook") and a scoped group
    of the same connector ("facebook::X") are DIFFERENT strings but still
    mutually exclusive at acquire time — compare with :func:`groups_conflict`,
    not equality.
    """
    from lib.import_lock_service import ImportLockService

    if source in _META_SOURCES:
        scope = ImportLockService.meta_connection_scope_key(es, team_ids)
        return f"facebook::{scope}" if scope else "facebook"
    if source in _UNLOCKED_SOURCES:
        return ""
    if source == "echo":
        scope = ImportLockService.echo_tenant_scope_key(es, team_ids)
        return f"echo::{scope}" if scope else "echo"
    if source in ("google_ads", "google_analytics"):
        from asset_management.web.services.import_service import ImportService

        try:
            scope_arg = ImportService._resolve_scope_arg(source, team_ids, es)
        except Exception:  # noqa: BLE001 — boundary: degrade to connector-global
            logger.warning("import-load: scope resolution failed for %s/%s", source, team_ids, exc_info=True)
            scope_arg = None
        if scope_arg:
            ids = scope_arg.split("=", 1)[1]
            return f"{source}::{ImportLockService.scope_key_for_ids(ids)}"
        return source
    return source


def groups_conflict(g1: str, g2: str) -> bool:
    """True when two lock groups' acquires would mutually exclude.

    Same key always conflicts (same doc). Additionally, a connector-GLOBAL
    key ("<connector>") and a scoped key ("<connector>::<scope>") of the
    same connector conflict — ImportLockService.acquire enforces that
    global↔scoped exclusion, so string inequality is not independence.
    Two DIFFERENT scoped keys of the same connector do not conflict (that's
    the point of scoping). "" (unlocked) never conflicts.
    """
    if not g1 or not g2:
        return False
    if g1 == g2:
        return True
    if g1.split("::", 1)[0] != g2.split("::", 1)[0]:
        return False
    return ("::" not in g1) or ("::" not in g2)


# heaviness order for the worst-case picker. echo does per-tenant
# API pulls plus the heaviest ES writes; google_ads / GA4 / youtube do real
# API pulls; chatwoot / disciple_tools are lighter pulls; the FB family is
# nearly free on a no-force re-import (the ``light`` flag's skip guards), so
# light rows rank last and only pad out a pick when nothing heavier is left.
_SOURCE_WEIGHT = {
    "echo": 4,
    "google_ads": 3,
    "google_analytics": 3,
    "youtube": 3,
    "chatwoot": 2,
    "disciple_tools": 2,
}


def _candidate_weight(candidate: dict) -> int:
    if candidate.get("light"):
        return 0
    return _SOURCE_WEIGHT.get(str(candidate.get("source") or ""), 1)


def select_worst_case(candidates: list[dict], n: int) -> list[int]:
    """greedy worst-case pick: indices (into ``candidates``) of up
    to ``n`` rows whose lock groups are pairwise non-conflicting, heaviest
    sources first, freshest data as the tiebreaker.

    Pure function over the panel's already-loaded candidate list (the route
    passes the client's list back) so the returned indices match the rows on
    the operator's screen — selection stays reviewable before Run.
    """
    n = max(0, min(int(n), MAX_CONCURRENT_IMPORTS))
    # Stable two-pass sort: freshest data first, then heaviest source first.
    order = sorted(
        range(len(candidates)),
        key=lambda i: str(candidates[i].get("last_data_date") or ""),
        reverse=True,
    )
    order.sort(key=lambda i: _candidate_weight(candidates[i]), reverse=True)

    picked: list[int] = []
    picked_groups: list[str] = []
    for i in order:
        group = str(candidates[i].get("lock_group") or "")
        if group and any(groups_conflict(group, prior) for prior in picked_groups):
            continue
        picked.append(i)
        if group:
            picked_groups.append(group)
        if len(picked) >= n:
            break
    return picked


def list_candidates(es) -> list[dict]:
    """Enumerate (team, source) pairs with already-imported data, newest-data
    first: one row per pair, with the team title, data recency, a suggested
    re-import range (last 3 already-imported days), the effective lock
    group, and the FB ``light`` flag."""
    try:
        index_names = sorted(dict(es.indices.get(index="*__*", ignore_unavailable=True)).keys())
    except Exception:  # noqa: BLE001 — boundary: no candidates beats a 500 on a testing page
        logger.warning("import-load: index enumeration failed", exc_info=True)
        return []

    # ES index names are forcibly lowercase, so the esid embedded in a
    # ``{team_esid}__{source}`` index name is the LOWERCASED form of the real
    # (mixed-case) de_asset_teams _id. Join back through a lowercase→real
    # lookup — passing the lowercased form as team_ids would resolve to
    # nothing and every import would silently no-op. Indices whose team no
    # longer exists are skipped (nothing to re-import for a deleted team).
    teams_by_lower = _teams_by_lowercase_esid(es)

    pairs: dict[tuple[str, str], list[str]] = {}
    for name in index_names:
        lower_esid, _, suffix = name.partition("__")
        source = _SUFFIX_TO_SOURCE.get(suffix)
        if not lower_esid or source is None or lower_esid not in teams_by_lower:
            continue
        pairs.setdefault((lower_esid, source), []).append(name)

    candidates: list[dict] = []
    for (lower_esid, source), indices in sorted(pairs.items()):
        latest = _latest_data_date(es, indices)
        if latest is None:
            continue  # empty indices: nothing already-imported to re-pull
        team = teams_by_lower[lower_esid]
        suggested_end = min(latest, datetime.now().date() - timedelta(days=1))
        suggested_start = suggested_end - timedelta(days=2)
        candidates.append(
            {
                "source": source,
                "team_esid": team["esid"],
                "team_title": team["title"],
                "last_data_date": latest.isoformat(),
                "suggested_start": suggested_start.isoformat(),
                "suggested_end": suggested_end.isoformat(),
                "lock_group": lock_group(source, team["esid"], es),
                "light": source in _LIGHT_SOURCES,
            }
        )
    candidates.sort(key=lambda c: c["last_data_date"], reverse=True)
    return candidates


def _teams_by_lowercase_esid(es) -> dict[str, dict]:
    """{lowercased _id: {"esid": real _id, "title": team_title}} for every
    team — the join key between lowercase index names and mixed-case docs."""
    try:
        resp = es.search(
            index="de_asset_teams",
            body={"query": {"match_all": {}}, "_source": ["team_title"], "size": 1000},
        )
        return {
            h["_id"].lower(): {"esid": h["_id"], "title": (h.get("_source") or {}).get("team_title", h["_id"])}
            for h in resp.get("hits", {}).get("hits", [])
        }
    except Exception:  # noqa: BLE001 — boundary: no candidates beats a 500 on a testing page
        logger.warning("import-load: team lookup failed", exc_info=True)
        return {}


def _latest_data_date(es, indices: list[str]):
    """Max kibana_date across a candidate's indices, as a date (None if empty)."""
    try:
        resp = es.search(
            index=",".join(indices),
            body={"size": 0, "aggs": {"latest": {"max": {"field": "kibana_date"}}}},
            ignore_unavailable=True,
        )
        value = (resp.get("aggregations", {}).get("latest") or {}).get("value_as_string")
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except Exception:  # noqa: BLE001 — boundary: skip the candidate, keep the list
        logger.warning("import-load: latest-date agg failed for %s", indices, exc_info=True)
        return None


def _verify_teams_exist(es, team_ids: str, label: str) -> None:
    """Reject team esids that don't resolve to a real team. A typo'd esid
    would otherwise spawn a silent no-op import AND derail lock-group
    derivation (unresolvable scope falls back to connector-global, which no
    longer collides with the correctly-resolved key — observed live on
    staging 2026-07-05). Best-effort: an ES hiccup skips the check rather
    than blocking the run."""
    esids = [t.strip() for t in team_ids.split(",") if t.strip()]
    if es is None or not esids:
        return
    try:
        resp = es.search(
            index="de_asset_teams",
            body={"query": {"ids": {"values": esids}}, "_source": False, "size": len(esids)},
        )
        found = {h["_id"] for h in resp.get("hits", {}).get("hits", [])}
    except Exception:  # noqa: BLE001 — boundary: degraded check must not block the run
        logger.warning("import-load: team existence check failed for %s", label, exc_info=True)
        return
    missing = [e for e in esids if e not in found]
    if missing:
        raise ImportLoadError(
            f"{label}: unknown team id(s) {', '.join(missing)} — check the candidates list (esids are case-sensitive)"
        )


def validate_imports(imports: list[dict], es=None) -> list[dict]:
    """Validate the run request; returns the enriched items (with lock
    groups) or raises ImportLoadError with an operator-readable message."""
    from asset_management.web.services.import_service import ImportService

    if not isinstance(imports, list) or not imports:
        raise ImportLoadError("imports must be a non-empty list")
    if len(imports) > MAX_CONCURRENT_IMPORTS:
        raise ImportLoadError(f"at most {MAX_CONCURRENT_IMPORTS} concurrent imports (got {len(imports)})")

    enriched: list[dict] = []
    seen_groups: dict[str, str] = {}
    for i, item in enumerate(imports):
        source = str(item.get("source") or "")
        team_ids = str(item.get("team_ids") or "").strip()
        start_date = str(item.get("start_date") or "")
        end_date = str(item.get("end_date") or "")
        label = f"imports[{i}] ({source or 'no source'})"
        if source not in ImportService.DATA_PATHS:
            raise ImportLoadError(f"{label}: unknown source")
        # duplicate sources are allowed now that the status
        # plumbing keys completion records per batch item (import_runner
        # --item + get_import_status's item join); only the lock-group
        # conflicts below still serialize a batch.
        if not team_ids:
            raise ImportLoadError(f"{label}: team_ids is required (unscoped capacity imports are not allowed)")
        _verify_teams_exist(es, team_ids, label)
        if not _DATE_RE.fullmatch(start_date) or not _DATE_RE.fullmatch(end_date):
            raise ImportLoadError(f"{label}: start_date/end_date must be YYYY-MM-DD")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise ImportLoadError(f"{label}: invalid date") from None
        if start > end:
            raise ImportLoadError(f"{label}: start_date is after end_date")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ImportLoadError(f"{label}: range longer than {MAX_RANGE_DAYS} days")

        group = lock_group(source, team_ids, es)
        if group:
            # pairwise, not equality: a connector-global group and a
            # scoped group of the same connector also mutually exclude.
            for prior_group, prior_label in seen_groups.items():
                if groups_conflict(group, prior_group):
                    raise ImportLoadError(
                        f"{label} import lock '{group}' conflicts with {prior_label} "
                        f"('{prior_group}') — the lock loser exits as a silent no-op, "
                        f"so these would NOT run concurrently. Pick a different team/source."
                    )
            seen_groups[group] = label
        enriched.append(
            {
                "source": source,
                # mirrors ImportService.run_imports' item key (same
                # enumerate order over the same list) so the panel can join a
                # selection row to its per-item status marker.
                "item": f"{source}:{i}",
                "team_ids": team_ids,
                "start_date": start_date,
                "end_date": end_date,
                "lock_group": group,
            }
        )
    return enriched


def run_import_load(imports: list[dict], hostname: str, es=None) -> dict:
    """Validate and launch the concurrent-import batch via
    ImportService.run_imports (which wraps each source in lib.import_runner
    for real exit codes). Records the run for load-report markers."""
    refuse_prod(hostname)
    enriched = validate_imports(imports, es)

    from asset_management.web.services.import_service import ImportService

    # NOTE: force_refresh deliberately omitted (defaults off) — see module
    # docstring.
    configs = [
        {
            "source": item["source"],
            "start_date": item["start_date"],
            "end_date": item["end_date"],
            "team_ids": item["team_ids"],
        }
        for item in enriched
    ]
    result = ImportService.run_imports(configs)
    # Per-item (not keyed by source — two items may share a source).
    result["imports"] = enriched

    try:
        with open(LATEST_RUN_PATH, "w", encoding="utf-8") as fh:
            json.dump({"import_id": result["import_id"], "started_at": datetime.now().isoformat()}, fh)
    except OSError:
        logger.warning("import-load: could not record latest run marker", exc_info=True)
    return result


# Below this duration a multi-day import almost certainly did no real work —
# a lock loss against an external run (cron), or an importer-side skip. The
# UI shows it as "suspiciously fast" rather than a green success.
SUSPICIOUS_DURATION_S = 5


def import_load_status(import_id: str) -> dict:
    """ImportService.get_import_status plus per-item timing markers for
    the charts: [{source, item, team_ids, started_at, completed_at,
    duration_s, success, suspiciously_fast}]."""
    from asset_management.web.services.import_service import ImportService

    status = ImportService.get_import_status(import_id)
    raw = ImportService._read_status(import_id) or {}
    started_at = raw.get("started_at")
    # results are per batch item now; join back to the pid entry
    # to label two same-source items by team. earlier status files have
    # no item on either side, so the lookup just misses (team_ids stays None).
    pids_by_item = {p["item"]: p for p in raw.get("pids", []) if p.get("item")}
    markers: list[dict] = []
    for result in raw.get("results", []):
        completed_at = result.get("completed_at")
        duration_s = None
        if started_at and completed_at:
            try:
                duration_s = round(
                    (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds(), 1
                )
            except ValueError:
                duration_s = None
        item = result.get("item")
        pid_info = pids_by_item.get(item, {}) if item else {}
        markers.append(
            {
                "source": result.get("source"),
                "item": item,
                "team_ids": pid_info.get("team_ids"),
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_s": duration_s,
                "success": bool(result.get("success")),
                "skipped": bool(result.get("skipped")),
                "suspiciously_fast": (
                    duration_s is not None and duration_s < SUSPICIOUS_DURATION_S and not result.get("skipped")
                ),
            }
        )
    status["markers"] = markers
    status["started_at"] = started_at
    return status


def latest_run_markers() -> list[dict]:
    """Markers of the most recent import-load run, for the load report's
    chart overlay. Empty when no run has happened (or the file is stale)."""
    try:
        with open(LATEST_RUN_PATH, encoding="utf-8") as fh:
            latest = json.load(fh)
    except (OSError, ValueError):
        return []
    import_id = latest.get("import_id")
    if not import_id:
        return []
    try:
        return import_load_status(import_id).get("markers", [])
    except Exception:  # noqa: BLE001 — boundary: markers are decoration on the report
        logger.warning("import-load: marker lookup failed for %s", import_id, exc_info=True)
        return []
