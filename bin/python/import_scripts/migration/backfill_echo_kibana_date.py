"""
Migrate Echo `kibana_date` from free-text English-month strings to
ISO ``yyyy-MM-dd``.

Echo's `kibana_date` was historically copied verbatim from the API's
``created_at`` field — e.g. ``"April 27, 2026 05:23:46 UTC"``. Every other
platform stores ``yyyy-MM-dd`` and the standard date helpers/dashboards/
 cleanup tooling all assume ISO.

**Why this is a reindex, not an in-place update_by_query.** The legacy
indices map ``kibana_date`` as ``{type: date, format: "MMMM dd, yyyy
HH:mm:ss 'UTC'"}``. ES 9 explicitly forbids changing the ``format``
parameter on an existing date field — ``put_mapping`` returns
``illegal_argument_exception: Cannot update parameter [format] …``. The
only supported path is to create a new index with the corrected mapping
and reindex into it.

Steps per legacy index ``X``:

  1. Skip if ``X`` is already an alias (already migrated and swapped).
  2. Create ``X_v2`` with the unified Echo schema
     (``kibana_date`` is ``yyyy-MM-dd``).
  3. ``_reindex`` ``X`` → ``X_v2`` with a Painless script that
     converts ``kibana_date`` to ISO. Already-ISO and unparseable values
     are left as-is (the script falls back to the source text); ES will
     reject anything still non-ISO at write time, surfaced via the
     reindex ``failures`` list.
  4. Delete ``X`` and add alias ``X`` → ``X_v2`` so existing query
     patterns (the writer, dashboards, cleanup scripts) keep working
     transparently.

Idempotent. Re-running on an already-migrated index detects the alias and
no-ops.

Execution:
    python3 /app/python/import.py migration backfill_echo_kibana_date [--dry-run=true]
    python3 /app/python/import_scripts/migration/backfill_echo_kibana_date.py
"""

import logging

import lib.logging
from lib.es_helpers import get_elasticsearch_client, put_regular_index_template
from migration.schema_loader import get_schema_for_index_type


_INDEX_PATTERN = "*_echo_conversations"
_NEW_SUFFIX = "_v2_2"

# Painless script that converts kibana_date during reindex. Handles three
# inputs in priority order:
#   1. ISO yyyy-MM-dd → leave as-is.
#   2. Legacy "MMMM dd, yyyy HH:mm:ss 'UTC'" → parse and rewrite to ISO.
#   3. Anything else → leave as-is. The destination index's ISO mapping
#      will reject these at index time; failures surface via the reindex
#      response so an operator can investigate.
_REWRITE_SCRIPT = {
    "lang": "painless",
    "source": r"""
        if (ctx._source.kibana_date == null) { return; }
        String kd = ctx._source.kibana_date;
        if (kd ==~ /^\d{4}-\d{2}-\d{2}$/) { return; }
        try {
            DateTimeFormatter src = DateTimeFormatter.ofPattern(
                "MMMM dd, yyyy HH:mm:ss 'UTC'", Locale.ENGLISH);
            ctx._source.kibana_date = LocalDate.parse(kd, src).toString();
        } catch (Exception e) {
            // Leave the original value — destination mapping will reject it,
            // surfacing it as a reindex failure for operator follow-up.
        }
    """,
}


def _list_concrete_legacy_indices(es):
    """Return concrete (non-alias) ``*_echo_conversations`` indices that
    still have the legacy date mapping. Aliased / already-migrated
    indices are skipped automatically because the alias keeps the old
    name pointed at the new ISO-mapped index, so the underlying concrete
    name is the ``_v2_2`` variant which we exclude here.
    """
    try:
        resp = es.indices.get(index=_INDEX_PATTERN, expand_wildcards="open", allow_no_indices=True)
        out = []
        for name, meta in resp.items():
            if name.endswith(_NEW_SUFFIX):
                continue
            kd = meta.get("mappings", {}).get("properties", {}).get("kibana_date", {})
            if kd.get("format") and "yyyy-MM-dd" not in kd["format"]:
                out.append(name)
        return sorted(out)
    except Exception as e:
        logging.error(f"Failed to list echo indices: {e}")
        return []


def _reindex_one(es, src_index, dry_run):
    """Reindex ``src_index`` → ``src_index + _NEW_SUFFIX`` with the ISO
    mapping. Returns a stats dict.
    """
    dst_index = f"{src_index}{_NEW_SUFFIX}"
    stats = {"src": src_index, "dst": dst_index, "created": 0, "errors": 0}

    if dry_run:
        try:
            stats["src_doc_count"] = es.count(index=src_index)["count"]
        except Exception:
            stats["src_doc_count"] = -1
        logging.info(f"[DRY RUN] {src_index}: would reindex {stats['src_doc_count']} docs → {dst_index}")
        return stats

    # Step 1: create destination with the unified schema (ISO kibana_date).
    # On the dev/prod single-node cluster a low-priority index template
    # ``default_replicas_0_for_single_node`` matches every index name and
    # forces ``data_stream`` creation. Override it via a higher-priority
    # regular-index template so the create call produces a normal index.
    schema = get_schema_for_index_type("echo_conversations")
    try:
        if not es.indices.exists(index=dst_index):
            put_regular_index_template(es, dst_index, schema, priority=1000)
            es.indices.create(index=dst_index, body={"mappings": schema})
            logging.info(f"  created {dst_index} with ISO kibana_date mapping")
        else:
            logging.info(f"  {dst_index} already exists — proceeding (resumable reindex)")
    except Exception as e:
        logging.error(f"  failed to create {dst_index}: {e}")
        stats["errors"] += 1
        stats["error_detail"] = str(e)
        return stats

    # Step 2: reindex with the conversion script. wait_for_completion=True so
    # we get failures back inline; conflicts=proceed so concurrent imports on
    # src don't abort us. refresh after so the alias-swap sees all docs.
    # request_timeout overrides the elasticsearch-py client's 10s default —
    # without it, large indices (>~5k docs) error with a client-side
    # connection timeout even though the server-side reindex completes.
    try:
        resp = es.options(request_timeout=1800).reindex(
            body={
                "source": {"index": src_index},
                "dest": {"index": dst_index, "op_type": "index"},
                "script": _REWRITE_SCRIPT,
                "conflicts": "proceed",
            },
            refresh=True,
            wait_for_completion=True,
            timeout="30m",
        )
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        stats["created"] = rd.get("created", 0) + rd.get("updated", 0)
        stats["version_conflicts"] = rd.get("version_conflicts", 0)
        failures = rd.get("failures", [])
        stats["errors"] = len(failures)
        if failures:
            stats["failure_samples"] = failures[:3]
            logging.warning(f"  {len(failures)} doc(s) failed to reindex on {src_index}; samples: {failures[:2]}")
            return stats  # Don't swap if any docs failed — operator must investigate.
        logging.info(f"  reindexed {stats['created']} docs into {dst_index}")
    except Exception as e:
        logging.error(f"  reindex failed on {src_index}: {e}")
        stats["errors"] += 1
        stats["error_detail"] = str(e)
        return stats

    # Step 3: swap — delete the old concrete index and add an alias of the
    # same name pointing at the new index. Done atomically via the aliases
    # update API so there's no read-empty window.
    try:
        es.indices.update_aliases(
            body={
                "actions": [
                    {"remove_index": {"index": src_index}},
                    {"add": {"index": dst_index, "alias": src_index}},
                ]
            }
        )
        logging.info(f"  ✓ swapped: alias {src_index} → {dst_index}")
        stats["swapped"] = True
    except Exception as e:
        logging.error(f"  alias swap failed on {src_index}: {e}")
        stats["errors"] += 1
        stats["error_detail"] = str(e)

    return stats


def main(params):
    lib.logging.define("backfill-echo-kibana-date")

    dry_run = str(params.get("dry-run", params.get("dry_run", ""))).lower() == "true"
    if dry_run:
        logging.info("=" * 70)
        logging.info("DRY RUN MODE - No changes will be made")
        logging.info("=" * 70)

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    indices = _list_concrete_legacy_indices(es)
    if not indices:
        logging.info("No legacy-format *_echo_conversations indices found — nothing to do.")
        return "success"

    logging.info(f"Found {len(indices)} legacy index(es) to migrate: {indices}")

    results = [_reindex_one(es, idx, dry_run) for idx in indices]

    total_created = sum(r.get("created", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)
    swapped = sum(1 for r in results if r.get("swapped"))

    logging.info("=" * 70)
    logging.info("MIGRATION SUMMARY")
    logging.info("=" * 70)
    for r in results:
        logging.info(f"  {r['src']}: {r}")
    logging.info(f"Totals — reindexed: {total_created}, swapped: {swapped}, errors: {total_errors}")

    if total_errors and not dry_run:
        logging.warning(
            "Some indices errored. Their alias swap was skipped — the legacy "
            "concrete index is still readable; investigate failures and re-run."
        )
        return "abort"
    return "success"


# Module-level helpers used by the test suite as well — keep them visible.
__all__ = [
    "main",
    "_REWRITE_SCRIPT",
    "_NEW_SUFFIX",
    "_list_concrete_legacy_indices",
    "_reindex_one",
]


if __name__ == "__main__":
    main({})
