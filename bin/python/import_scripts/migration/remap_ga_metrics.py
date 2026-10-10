"""reindex existing ``*_ga_metrics`` (and ``*_ga_metrics_window``)
indices that adopted the fields via DYNAMIC mapping into fresh indices
built from the current schema.

Background
==========
 added report_type + focused-report dimensions/metrics and shipped the
correct mappings in ``unified_schema_ga_metrics.json`` (keyword dims, float
ratios). But a schema change only affects NEWLY-created indices. Existing
``{team_esid}__ga_metrics`` indices kept their old mapping and picked the new
fields up via dynamic mapping — producing an inconsistent, per-field shape:

  * report_type + the string dims (unifiedScreenClass, firstUserSource,
    newVsReturning, audienceName, eventName, linkUrl, deviceCategory, ...) ->
    ``text`` (+keyword subfield)
  * some older dims (platform, city, country, sessionSource, ...) -> bare keyword

So a ``terms`` agg / filter on the bare field name silently returns empty for the
``text`` ones (fielddata disabled), and the right aggregatable handle differs per
field. ES forbids changing a field's
type in place, so each index must be rebuilt with the correct mapping.

Difference (``remap_ga_metrics_2.py``)
========================================================
 fixed a DIFFERENT drift (``date`` degraded to ``text`` by the
ESID migration) and skips an index when ``date`` is already mapped as ``date``.
For the ``date`` field is typically already fine — the drift is in
report_type / the string dims / team_country. So this migration's idempotency
guard checks the MARKER fields, not ``date``. (Running the
script would no-op exactly the indices that need fixing.)

team_country override
=====================
``get_ga_metrics.py`` writes ``team_country`` into every non-window ga_metrics
doc, but the field was missing from ``unified_schema_ga_metrics.json`` (it is
defined in the ``_window`` schema). The schema fix that adds
``team_country: keyword`` ships separately; until prod has pulled it, this
migration MERGES an explicit ``team_country: keyword`` into the create mapping so
the rebuilt index pins it regardless of which code version is deployed. Once the
schema has the field, the override is a no-op (same type).

What this does (per index, two reindexes — inherent to landing at the canonical
name since ES has no rename), mirroring the guarded sequence
================================================================================
  1. Capture the index's aliases + doc count.
  2. Create a temp ``{index}__remap`` with the correct schema.
  3. Reindex live -> temp; abort this index if temp count != live count.
  4. Delete the live index (drops aliases), recreate it with the correct schema.
  5. Reindex temp -> live; abort if count != temp count (temp KEPT on mismatch).
  6. Re-add the captured aliases atomically.
  7. Verify the marker fields are now bare keyword, then delete temp.

Safety
======
* **Dry-run by default.** ``--apply`` is required to mutate. The dry-run reports
  every index, its doc count, aliases, and whether it is already clean.
* **Snapshot first.** ``--apply`` refuses to run unless a snapshot
  ``daily_repo/<PRESNAPSHOT_NAME>`` exists, OR ``--snapshot-repo`` is given (a
  fresh snapshot of the in-scope indices is then taken). No mutation without a
  restore point.
* **Per-index isolation.** A failure on one index logs + skips to ``errors``; a
  count mismatch leaves the temp index in place for manual recovery.
* **Idempotent.** An index whose marker fields are already bare keyword is
  skipped — so an index already remapped during the cutover is a no-op.

Usage
=====
::

    # Dry-run (default) — report only.
    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration remap_ga_metrics

    # Apply (requires a pre-snapshot to exist).
    ... migration remap_ga_metrics --apply

    # Apply, taking a fresh snapshot into an explicit repo first.
    ... migration remap_ga_metrics --apply --snapshot-repo=daily_repo

    # Restrict to the window indices only (or the base indices only).
    ... migration remap_ga_metrics --apply --only=window
    ... migration remap_ga_metrics --apply --only=base
"""

from __future__ import annotations

import copy
import logging

import lib.logging
from lib.es_helpers import (
    get_elasticsearch_client,
    create_index_if_not_exists,
)
from migration.schema_loader import get_schema_for_index_type
from migration.snapshot_gate import (  # the shared gate replaces the per-script copy
    finalize_report,
    MigrationRefused,
    ensure_restore_point,
    resolve_apply_mode,
)

# (source_slug, index suffix) — base must be checked AFTER window so the
# ``*__ga_metrics`` glob doesn't also catch ``*__ga_metrics_window`` (it won't:
# the suffix match below is exact, but the order keeps the report readable).
TARGETS = [
    ("ga_metrics_window", "__ga_metrics_window"),
    ("ga_metrics", "__ga_metrics"),
]
TEMP_SUFFIX = "__remap"
PRESNAPSHOT_REPO = "daily_repo"
PRESNAPSHOT_NAME = "pre-esid-cleanup"

# marker fields: the string dims + report_type + team_country.
# An index is "already clean" iff every marker present in its mapping is bare
# keyword (type == keyword and no text). team_country is the field most likely
# to re-drift (schema gap), so it is included.
MARKER_FIELDS = [
    "report_type",
    "team_country",
    "unifiedScreenClass",
    "firstUserSource",
    "firstUserMedium",
    "newVsReturning",
    "audienceName",
    "eventName",
    "linkUrl",
    "deviceCategory",
]


def _schema_for(source: str) -> dict:
    """Schema for the source, with team_country: keyword guaranteed present.

    Defensive: if the deployed code predates the schema fix that adds
    team_country to the base ga_metrics schema, merge it in so the rebuilt index
    still pins it. No-op once the schema defines the field.
    """
    schema = copy.deepcopy(get_schema_for_index_type(source))
    schema.setdefault("properties", {})
    schema["properties"].setdefault("team_country", {"type": "keyword"})
    return schema


def _concrete_indices(es, suffix: str) -> list[str]:
    """Concrete ``*{suffix}`` indices, excluding temp + the longer-suffix sibling."""
    idx = es.indices.get(index=f"*{suffix}", ignore_unavailable=True)
    out = []
    for name in idx.keys():
        if TEMP_SUFFIX in name:
            continue
        # Exact suffix: ``__ga_metrics`` must not swallow ``__ga_metrics_window``.
        if not name.endswith(suffix):
            continue
        if suffix == "__ga_metrics" and name.endswith("__ga_metrics_window"):
            continue
        out.append(name)
    return sorted(out)


def _doc_count(es, index: str) -> int:
    return int(es.count(index=index).get("count", 0))


def _props(es, index: str) -> dict:
    m = es.indices.get_mapping(index=index)
    return list(m.values())[0]["mappings"].get("properties", {})


def _field_is_bare_keyword(props: dict, field: str) -> bool:
    """True iff ``field`` is mapped as bare keyword (type keyword, no subfields)."""
    spec = props.get(field)
    if not spec:
        return False  # absent — not yet pinned
    return spec.get("type") == "keyword" and "fields" not in spec


def _is_clean(es, index: str) -> bool:
    """True iff every marker field PRESENT in the index is bare keyword.

    A marker that is absent from the index (the importer never wrote it there)
    does not block — it cannot be mixed-mapped if it does not exist. But at least
    one marker must be present and clean, otherwise an index with none of the
     fields yet would falsely read "clean" and never get the schema.
    """
    props = _props(es, index)
    present = [f for f in MARKER_FIELDS if f in props]
    if not present:
        return False  # no markers at all -> rebuild to adopt the schema
    return all(_field_is_bare_keyword(props, f) for f in present)


def _aliases_of(es, index: str) -> list[str]:
    try:
        meta = es.indices.get_alias(index=index)
        return sorted((meta.get(index, {}).get("aliases", {}) or {}).keys())
    except Exception:  # noqa: BLE001 — boundary; absence = no aliases
        return []


def _remap_one(es, index: str, schema: dict, *, dry_run: bool) -> dict:
    """Rebuild one index with the correct mapping (two-pass, count-verified)."""
    result = {"index": index, "status": "", "old_count": None, "new_count": None}

    if _is_clean(es, index):
        result["status"] = "skip_already_clean"
        return result

    old_count = _doc_count(es, index)
    aliases = _aliases_of(es, index)
    result["old_count"] = old_count
    result["aliases"] = aliases

    temp = f"{index}{TEMP_SUFFIX}"

    if dry_run:
        result["status"] = f"DRY-RUN would remap {index} ({old_count} docs, aliases={aliases}) via {temp}"
        return result

    # 1) temp with correct schema
    es.indices.delete(index=temp, ignore_unavailable=True)
    if not create_index_if_not_exists(es, temp, schema):
        result["status"] = "error_create_temp"
        return result

    # 2) live -> temp, verify count
    es.options(request_timeout=1800).reindex(
        body={"source": {"index": index}, "dest": {"index": temp}},
        wait_for_completion=True,
        refresh=True,
        timeout="30m",
    )
    if _doc_count(es, temp) != old_count:
        result["status"] = f"error_count_live_to_temp(old={old_count},temp={_doc_count(es, temp)}) — TEMP KEPT"
        return result

    # 3) delete live (drops aliases), recreate with correct schema
    es.indices.delete(index=index)
    if not create_index_if_not_exists(es, index, schema):
        result["status"] = "error_recreate_live — DATA IN TEMP"
        return result

    # 4) temp -> live, verify count
    es.options(request_timeout=1800).reindex(
        body={"source": {"index": temp}, "dest": {"index": index}},
        wait_for_completion=True,
        refresh=True,
        timeout="30m",
    )
    new_count = _doc_count(es, index)
    result["new_count"] = new_count
    if new_count != old_count:
        result["status"] = f"error_count_temp_to_live(old={old_count},new={new_count}) — TEMP KEPT"
        return result

    # 5) restore aliases atomically
    if aliases:
        es.indices.update_aliases(body={"actions": [{"add": {"index": index, "alias": a}} for a in aliases]})

    # 6) verify the marker fields are now clean, then drop temp
    if not _is_clean(es, index):
        result["status"] = "error_mapping_still_mixed — TEMP KEPT"
        return result
    es.indices.delete(index=temp, ignore_unavailable=True)
    result["status"] = "remapped"
    return result


def main(params):
    lib.logging.define("remap-ga-metrics")
    # shared parse: --apply must be an explicit true value, and
    # -apply + explicit --dry-run=true refuses (the review contract).
    try:
        dry_run, snapshot_repo = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    only = (params.get("only") or "").lower()  # "", "base", "window"

    es = get_elasticsearch_client()
    if es is None:
        logging.error("No ES client.")
        return 1

    targets = TARGETS
    if only == "base":
        targets = [t for t in TARGETS if t[0] == "ga_metrics"]
    elif only == "window":
        targets = [t for t in TARGETS if t[0] == "ga_metrics_window"]

    # Collect all in-scope indices (for the snapshot) up front.
    by_source = {source: _concrete_indices(es, suffix) for source, suffix in targets}
    all_indices = [i for lst in by_source.values() for i in lst]
    logging.info(
        "%s: %s",
        "DRY-RUN" if dry_run else "APPLY",
        ", ".join(f"{src}={len(lst)}" for src, lst in by_source.items()) or "no indices",
    )

    if not dry_run:
        # shared snapshot-or-refuse gate: timestamped + state-checked
        # fresh snapshots (with recent-reuse on retry), coverage-checked
        # pre-snapshot acceptance, refusal exits non-zero.
        try:
            ensure_restore_point(
                es,
                all_indices,
                snapshot_repo=snapshot_repo,
                name_prefix="pre-ga-remap",
                presnapshot_repo=PRESNAPSHOT_REPO,
                presnapshot_name=PRESNAPSHOT_NAME,
            )
        except MigrationRefused as e:
            logging.error("Refusing --apply: %s", e)
            return 1

    report = {"remapped": [], "skipped": [], "errors": []}
    for source, _suffix in targets:
        schema = _schema_for(source)
        for index in by_source[source]:
            r = _remap_one(es, index, schema, dry_run=dry_run)
            status = r["status"]
            if status.startswith("DRY-RUN") or status == "remapped":
                report["remapped"].append(r)
                logging.info(
                    "  %s",
                    status if status.startswith("DRY-RUN") else f"remapped {index} ({r['new_count']} docs)",
                )
            elif status.startswith("skip"):
                report["skipped"].append(r)
                logging.info("  skip %s [%s]", index, status)
            else:
                report["errors"].append(r)
                logging.error("  ERROR %s [%s]", index, status)

    lib.logging.define("remap-ga-metrics")
    logging.info(
        "GA remap  %s: %d remapped, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["remapped"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
