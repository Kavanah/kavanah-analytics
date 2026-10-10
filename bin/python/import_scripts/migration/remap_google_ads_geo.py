"""reindex existing ``*_google_ads`` indices whose ``geo_country_name``
/ ``geo_country_code`` fields adopted an inconsistent DYNAMIC mapping into fresh
indices built from the current schema (plus explicit geo overrides).

Background
==========
``geo_country_name`` and ``geo_country_code`` are written only onto
``record_type=geo`` docs by ``import_scripts/google_ads/get_campaign_data.py``
but were never declared in ``unified_schema_google_ads.json``. They were left to
ES dynamic mapping, so their type differs per index depending on when the index
was first created:

  * Indices born under the per-source template -> bare ``keyword``.
  * An index created before that template took effect -> ``text`` (+keyword
    subfield), ES's default dynamic string mapping.

A single ``terms`` agg across the ``*_google_ads`` pattern then cannot pick one
aggregatable field handle that works for every index:

  * bare ``geo_country_code`` -> "Fielddata is disabled" on the ``text`` index
    (failed shard).
  * ``geo_country_name.keyword`` -> empty from the bare-``keyword`` indices (no
    such subfield).

So the dashboard-builder country table comes back empty. ES forbids
changing a field's type in place, so each affected index must be rebuilt.

This is the same mixed-mapping class as, and this
migration mirrors ``remap_ga_metrics.py`` step for step.

geo override
============
The schema fix that adds ``geo_country_name`` / ``geo_country_code`` (both
keyword) to ``unified_schema_google_ads.json`` ships in the same PR. Until prod
has pulled it, this migration MERGES explicit ``keyword`` overrides into the
create mapping so the rebuilt index pins them regardless of which code version is
deployed. Once the schema has the fields, the overrides are a no-op (same type).

location override
=================
``location`` is also schema-absent and dynamically mapped — healthy indices
inferred ``geo_point`` (the importer writes ``{lat, lon}`` centroids), but at
least one index inferred a plain ``object``. We pin ``location: geo_point`` in
the create mapping so the rebuilt index matches the working indices and the geo
map keeps reading it. (geo_point accepts the existing ``{lat, lon}`` docs on
reindex.)

app_installs
============
``app_installs`` was schema-typed ``long`` but the importer writes
``conversions`` (a float), so fractional installs were truncated on every index.
The same PR widens the schema to ``app_installs: float``. Because this migration
rebuilds from the schema, the reindex picks the float mapping up automatically
(``long`` -> ``float`` widens losslessly); no per-field override is needed here.

Deferred within (documented, NOT done by this migration)
=================================================================
``campaign_start_date_time`` / ``campaign_end_date_time`` drifted to ``text`` on
some indices (the schema and the importer both intend ``date`` — the importer
normalizes space-separated values to ISO-8601 for the ``date`` mapping). Forcing
``date`` in this reindex is risky: legacy docs written before that normalization
fix may hold space-separated strings that ``strict_date_optional_time`` rejects,
which would abort the reindex mid-flight. That drift affects nothing user-facing
today, so it is handled as a separate guarded pass (a normalization ``script`` in
the reindex body) rather than bundled into this geo unblock.

What this does (per index, two reindexes — inherent to landing at the canonical
name since ES has no rename), mirroring the guarded sequence
================================================================================
  1. Capture the index's aliases + doc count.
  2. Create a temp ``{index}__remap_2`` with the correct schema + overrides.
  3. Reindex live -> temp; abort this index if temp count != live count.
  4. Delete the live index (drops aliases), recreate it with the correct schema.
  5. Reindex temp -> live; abort if count != temp count (temp KEPT on mismatch).
  6. Re-add the captured aliases atomically.
  7. Verify the geo marker fields are now bare keyword, then delete temp.

Safety
======
* **Dry-run by default.** ``--apply`` is required to mutate.
* **Snapshot first.** ``--apply`` refuses to run unless a snapshot
  ``daily_repo/<PRESNAPSHOT_NAME>`` exists, OR ``--snapshot-repo`` is given (a
  fresh snapshot of the in-scope indices is then taken). No mutation without a
  restore point.
* **Per-index isolation.** A failure on one index logs + skips; a count mismatch
  leaves the temp index in place for manual recovery.
* **Idempotent.** An index whose geo markers are already bare keyword is skipped.

Usage
=====
::

    # Dry-run (default) — report only.
    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration remap_google_ads_geo

    # Apply (requires a pre-snapshot to exist).
    ... migration remap_google_ads_geo --apply

    # Apply, taking a fresh snapshot into an explicit repo first.
    ... migration remap_google_ads_geo --apply --snapshot-repo=daily_repo
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

SOURCE = "google_ads"
# Matches both ESID form ``{esid}__google_ads`` and slug form ``{slug}_google_ads``.
SUFFIX = "_google_ads"
TEMP_SUFFIX = "__remap_2"
PRESNAPSHOT_REPO = "daily_repo"
PRESNAPSHOT_NAME = "pre-esid-cleanup"

# marker fields: the dynamically-mapped geo dims. An index is "already
# clean" iff every marker present in its mapping is bare keyword.
MARKER_FIELDS = ["geo_country_code", "geo_country_name"]


def _schema_for(source: str) -> dict:
    """Schema for the source, with the geo overrides guaranteed present.

    Defensive: if the deployed code predates the schema fix that adds the geo
    fields, merge them in (plus location: geo_point) so the rebuilt index pins
    them. No-op once the schema defines them with the same type.
    """
    schema = copy.deepcopy(get_schema_for_index_type(source))
    props = schema.setdefault("properties", {})
    props.setdefault("geo_country_code", {"type": "keyword"})
    props.setdefault("geo_country_name", {"type": "keyword"})
    props.setdefault("location", {"type": "geo_point"})
    props.setdefault("geo_criterion_id", {"type": "long"})
    return schema


def _concrete_indices(es) -> list[str]:
    """Concrete ``*_google_ads`` indices, excluding temp indices."""
    idx = es.indices.get(index=f"*{SUFFIX}", ignore_unavailable=True)
    out = []
    for name in idx.keys():
        if TEMP_SUFFIX in name:
            continue
        if not name.endswith(SUFFIX):
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

    A marker absent from the index (no geo docs written there yet) does not block
    — it cannot be mixed-mapped if it does not exist. But at least one marker must
    be present and clean, otherwise an index with no geo fields yet would falsely
    read "clean" and never adopt the pinned mapping.
    """
    props = _props(es, index)
    present = [f for f in MARKER_FIELDS if f in props]
    if not present:
        return False  # no geo markers at all -> rebuild to adopt the schema
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
    lib.logging.define("remap-google-ads-geo")
    # shared parse: --apply must be an explicit true value, and
    # -apply + explicit --dry-run=true refuses (the review contract).
    try:
        dry_run, snapshot_repo = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1

    es = get_elasticsearch_client()
    if es is None:
        logging.error("No ES client.")
        return 1

    # The temp index name ``{index}__remap_2`` does NOT end in ``_google_ads``,
    # so it only matches the override via the trailing-star ``*_google_ads*``
    # pattern. The migration runs in imports_container, which does NOT refresh the
    # template at startup (only the web app's app.py does). Refresh it here so the
    # temp creates land as regular indices instead of being hijacked as data streams
    # by ES's priority-5 default template. Idempotent (put_index_template is upsert).
    # Without this the create fails: "matches with template
    # [default_replicas_0_for_single_node] that creates data streams only" (
    # staging failure 2026-06-29).
    from lib.es_index_template_service import ensure_project_default_template

    if not ensure_project_default_template():
        logging.error(
            "Refusing to run:  default template install failed — temp index creates would be hijacked as data streams."
        )
        return 1

    indices = _concrete_indices(es)
    logging.info("%s: %d *_google_ads index(es) in scope", "DRY-RUN" if dry_run else "APPLY", len(indices))

    if not dry_run:
        # shared snapshot-or-refuse gate: timestamped + state-checked
        # fresh snapshots (with recent-reuse on retry), coverage-checked
        # pre-snapshot acceptance, refusal exits non-zero.
        try:
            ensure_restore_point(
                es,
                indices,
                snapshot_repo=snapshot_repo,
                name_prefix="pre-google-ads-geo-remap",
                presnapshot_repo=PRESNAPSHOT_REPO,
                presnapshot_name=PRESNAPSHOT_NAME,
            )
        except MigrationRefused as e:
            logging.error("Refusing --apply: %s", e)
            return 1

    schema = _schema_for(SOURCE)
    report = {"remapped": [], "skipped": [], "errors": []}
    for index in indices:
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

    lib.logging.define("remap-google-ads-geo")
    logging.info(
        "google_ads geo remap  %s: %d remapped, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["remapped"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
