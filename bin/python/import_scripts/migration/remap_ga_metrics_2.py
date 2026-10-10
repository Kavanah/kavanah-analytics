"""re-map the GA metrics ESID indices that the migration
created with degraded (dynamic) mapping.

Background
==========
``migrate_to_esid_indices`` reindexed ``ga_metrics`` into ``{esid}__ga_metrics``
WITHOUT applying the explicit GA schema, so Elasticsearch auto-created the
targets with dynamic mapping: ``date`` -> ``text``, all keyword dims -> ``text``,
counts -> ``long``. A ``text`` date can't be range-queried and ``text`` dims
can't be aggregated without fielddata, so every GA dashboard returns
"Error running dashboard query".

ES forbids changing an existing field's type (proven: ``illegal_argument_exception
mapper [country] cannot be changed from type [text] to [keyword]``), so an
in-place ``put_mapping`` fix is impossible. Each index must be rebuilt with the
correct mapping and its docs reindexed.

What this does (per ``{esid}__ga_metrics`` index)
=================================================
The index is ALREADY at the canonical name, so we cannot reindex it into itself.
The final state must be the SAME canonical name + correct mapping + the same
reverse alias(es) it had. Sequence (two reindexes, inherent to landing at the
clean name since ES has no rename):

  1. Capture the index's current aliases (the reverse alias, e.g.
     ``<team>_ga_metrics``) and doc count.
  2. Create a temp ``{esid}__ga_metrics__remap`` with the CORRECT schema
     (``get_schema_for_index_type('ga_metrics')``).
  3. Reindex live -> temp; abort this index if the temp count != live count.
  4. Delete the live index (this also drops its aliases).
  5. Re-create the live (canonical) index with the CORRECT schema.
  6. Reindex temp -> live; abort if count != temp count (temp is NOT deleted on
     a count mismatch, so the data is still recoverable).
  7. In one ``update_aliases`` block, re-add the captured aliases onto the
     canonical index.
  8. Delete the temp index.

Safety
======
* **Dry-run by default.** ``--apply`` is required to mutate. The dry-run reports
  every index, its doc count, and its aliases — review before applying.
* **Snapshot first.** ``--apply`` refuses to run unless a pre-existing snapshot
  named ``daily_repo/pre-esid-cleanup`` (the cutover snapshot that
  already covers these indices) is present, OR ``--snapshot-repo`` is given and a
  fresh snapshot is taken. No mutation without a restore point.
* **Per-index isolation.** A failure on one index logs + skips to the report's
  ``errors`` list; it does not abort the others. A count-mismatch leaves the temp
  index in place for manual recovery.
* **Idempotent-ish.** Re-running after a clean pass is a near no-op: an index
  whose mapping already matches the schema (``date`` is ``date``) is skipped.

Usage
=====
::

    # Dry-run (default) — report only.
    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration remap_ga_metrics_2

    # Apply (requires the pre-snapshot to exist).
    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration remap_ga_metrics_2 --apply

    # Apply, taking a fresh snapshot into an explicit repo first.
    ... remap_ga_metrics_2 --apply --snapshot-repo=daily_repo
"""

from __future__ import annotations

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

SOURCE = "ga_metrics"
TEMP_SUFFIX = "__remap_4"
PRESNAPSHOT_REPO = "daily_repo"
PRESNAPSHOT_NAME = "pre-esid-cleanup"


def _esid_ga_indices(es) -> list[str]:
    """Concrete ``{esid}__ga_metrics`` indices (exclude any temp + non-ESID)."""
    idx = es.indices.get(index=f"*__{SOURCE}", ignore_unavailable=True)
    return sorted(
        name
        for name in idx.keys()
        if name.endswith(f"__{SOURCE}") and not name.endswith(TEMP_SUFFIX + f"__{SOURCE}") and TEMP_SUFFIX not in name
    )


def _doc_count(es, index: str) -> int:
    return int(es.count(index=index).get("count", 0))


def _date_type(es, index: str) -> str | None:
    """Return the mapped type of the ``date`` field (None if absent)."""
    m = es.indices.get_mapping(index=index)
    props = list(m.values())[0]["mappings"].get("properties", {})
    return (props.get("date") or {}).get("type")


def _aliases_of(es, index: str) -> list[str]:
    try:
        meta = es.indices.get_alias(index=index)
        return sorted((meta.get(index, {}).get("aliases", {}) or {}).keys())
    except Exception:  # noqa: BLE001 — boundary; absence = no aliases
        return []


def _remap_one(es, index: str, schema: dict, *, dry_run: bool) -> dict:
    """Rebuild one ``{esid}__ga_metrics`` with the correct mapping."""
    result = {"index": index, "status": "", "old_count": None, "new_count": None}

    # Already correct? (date mapped as date) -> skip (idempotency).
    if _date_type(es, index) == "date":
        result["status"] = "skip_already_correct"
        return result

    old_count = _doc_count(es, index)
    aliases = _aliases_of(es, index)
    result["old_count"] = old_count
    result["aliases"] = aliases

    temp = f"{index}{TEMP_SUFFIX}"

    if dry_run:
        result["status"] = f"DRY-RUN would remap {index} ({old_count} docs, aliases={aliases}) " f"via {temp}"
        return result

    # 1) temp with correct schema
    es.indices.delete(index=temp, ignore_unavailable=True)
    if not create_index_if_not_exists(es, temp, schema):
        result["status"] = "error_create_temp"
        return result

    # 2) live -> temp, verify
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

    # 4) temp -> live, verify
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

    # 6) verify mapping is now correct, then drop temp
    if _date_type(es, index) != "date":
        result["status"] = "error_mapping_still_wrong — TEMP KEPT"
        return result
    es.indices.delete(index=temp, ignore_unavailable=True)
    result["status"] = "remapped"
    return result


def main(params):
    lib.logging.define("remap-ga-metrics_2")
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

    indices = _esid_ga_indices(es)
    logging.info("%s: found %d GA ESID index(es).", "DRY-RUN" if dry_run else "APPLY", len(indices))

    if not dry_run:
        # shared snapshot-or-refuse gate: timestamped + state-checked
        # fresh snapshots (with recent-reuse on retry), coverage-checked
        # pre-snapshot acceptance, refusal exits non-zero.
        try:
            ensure_restore_point(
                es,
                indices,
                snapshot_repo=snapshot_repo,
                name_prefix="pre-ga-remap_2",
                presnapshot_repo=PRESNAPSHOT_REPO,
                presnapshot_name=PRESNAPSHOT_NAME,
            )
        except MigrationRefused as e:
            logging.error("Refusing --apply: %s", e)
            return 1

    schema = get_schema_for_index_type(SOURCE)
    report = {"remapped": [], "skipped": [], "errors": []}
    for index in indices:
        r = _remap_one(es, index, schema, dry_run=dry_run)
        status = r["status"]
        if status.startswith("DRY-RUN") or status == "remapped":
            report["remapped"].append(r)
            logging.info("  %s", status if status.startswith("DRY-RUN") else f"remapped {index} ({r['new_count']} docs)")
        elif status.startswith("skip"):
            report["skipped"].append(r)
            logging.info("  skip %s [%s]", index, status)
        else:
            report["errors"].append(r)
            logging.error("  ERROR %s [%s]", index, status)

    lib.logging.define("remap-ga-metrics_2")
    logging.info(
        "GA remap %s: %d remapped, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["remapped"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
