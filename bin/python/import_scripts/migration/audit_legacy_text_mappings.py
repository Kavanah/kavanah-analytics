"""follow-up: audit and clean up indices whose mappings drifted from
the unified schema (text where the schema says keyword, etc.).

Background
==========
ES auto-detects string fields as ``text + .keyword`` whenever an index is
created without an explicit mapping. That happens when the first doc lands
in a team-specific ``*_fb_insights`` (or other unified-schema) index without
the schema being applied. The fix in ``lib.no_data_markers`` writes
the schema on first marker write, but indices that were already created
the wrong way before the fix shipped stay broken until they're rebuilt.

Symptoms of a broken index
==========================
- ``team_title`` aggregations on Kibana dashboards return empty or hit the
  text-field-not-aggregatable error.
- The pre-filter falls through to "skip" because keyword fields
  aren't actually keyword.
- The Data Source Import Status page shows wrong coverage.

What this script does
=====================
1. Surveys all ``*_<suffix>.`` indices for every suffix in the schema map.
2. For each index, fetches its ``_mapping`` and compares each top-level
   field against the unified schema. A field is **broken** when:
     * The schema declares ``keyword`` (or ``date``) but the live mapping
       declares ``text``.
3. Reports a summary: how many indices are broken, by which fields.

With ``--apply`` (and only with ``--apply``):
4. Snapshots every broken index to a configurable repo first.
5. Deletes the broken indices.
6. The next import recreates them correctly via ``_ensure_index_with_schema``.

This script is **idempotent**. Running it on a clean cluster reports zero
broken indices and exits without writing.

Usage
=====
    # Dry-run survey (default)
    python3 /app/python/import.py migration audit_legacy_text_mappings

    # Apply on dev/staging (snapshot + delete + report)
    python3 /app/python/import.py migration audit_legacy_text_mappings \\
        --apply --repo migration_repo

    # Apply on prod (uses daily_repo if --repo not given)
    python3 /app/python/import.py migration audit_legacy_text_mappings \\
        --apply --repo daily_repo

Notes
=====
- The snapshot name is ``pre-legacy-cleanup-<YYYYMMDD-HHMMSS>``.
- Indices the schema map doesn't cover (chatwoot_insights, youtube_analytics,
  google_ads, etc.) are skipped. Add their schema_type to the schema map
  in ``lib.no_data_markers._SUFFIX_TO_SCHEMA_TYPE`` first.
- The expected ``no_data`` field is allowed to be ``boolean`` even if the
  schema doesn't list it — markers add this field at write time.
"""

from __future__ import annotations

import logging

import lib.logging
from migration.snapshot_gate import (  # the shared gate replaces the per-script copy
    MigrationRefused,
    resolve_apply_mode,
    take_snapshot,
)
from lib.es_helpers import get_elasticsearch_client
from lib.no_data_markers import _SUFFIX_TO_SCHEMA_TYPE


class AuditError(Exception):
    """Raised when the audit cannot proceed safely."""


def _load_schema(schema_type: str) -> dict:
    from migration.schema_loader import get_schema_for_index_type

    return get_schema_for_index_type(schema_type)


def _compare_field(
    field_name: str,
    schema_def: dict,
    live_def: dict,
) -> str | None:
    """Return a one-line description of a mismatch, or None if OK.

    Only flags mismatches that actually break aggregations / queries:
    schema ``keyword`` rendered as live ``text`` (no usable .keyword
    subfield, or one that exists but isn't what the schema asked for).
    Schema ``date`` rendered as anything other than ``date`` is also a
    break.
    """
    schema_type = schema_def.get("type")
    live_type = live_def.get("type")

    if schema_type == "keyword" and live_type == "text":
        # ES auto-mapping creates a .keyword subfield on text — but the
        # schema asked for the field itself to be keyword. Aggregations
        # against the bare field name will fail; queries that use
        # ``field.keyword`` will work. Either way the index doesn't match
        # what the rest of the system expects.
        return f"  {field_name}: schema=keyword, live=text"

    if schema_type == "date" and live_type != "date":
        return f"  {field_name}: schema=date, live={live_type}"

    return None


def _audit_index(
    es,
    index_name: str,
    schema: dict,
) -> list[str]:
    """Return a list of mismatch lines for ``index_name``. Empty = clean."""
    schema_props = schema.get("properties", {})
    if not schema_props:
        return []

    try:
        live = es.indices.get_mapping(index=index_name)
    except Exception as e:
        logging.warning(f"Could not fetch mapping for {index_name}: {e}")
        return []

    live_props = live.get(index_name, {}).get("mappings", {}).get("properties", {}) or {}
    mismatches = []
    for field_name, schema_def in schema_props.items():
        live_def = live_props.get(field_name)
        if live_def is None:
            # Field absent from live mapping — fine. ES will auto-add on
            # first doc with that field. Don't flag.
            continue
        msg = _compare_field(field_name, schema_def, live_def)
        if msg:
            mismatches.append(msg)
    return mismatches


def _list_broken_indices(es) -> dict[str, list[str]]:
    """Return ``{index_name: [mismatch lines]}`` for every broken index."""
    broken: dict[str, list[str]] = {}
    for suffix, schema_type in _SUFFIX_TO_SCHEMA_TYPE.items():
        # Suffixes start with "_" (e.g. "_fb_insights"); index pattern is
        # everything that ends with that.
        pattern = f"*{suffix}"
        try:
            schema = _load_schema(schema_type)
        except Exception as e:
            logging.warning(f"Could not load schema {schema_type}: {e}; skipping {pattern}")
            continue

        try:
            indices = es.indices.get(index=pattern, ignore_unavailable=True)
        except Exception as e:
            logging.warning(f"Could not list indices for {pattern}: {e}")
            continue

        for name in sorted(indices.keys()):
            mismatches = _audit_index(es, name, schema)
            if mismatches:
                broken[name] = mismatches
    return broken


def _take_snapshot(es, repo: str, indices: list[str]) -> str:
    """delegates to the shared gate: timestamped name, real
    request_timeout (the old copy ran a wait_for_completion snapshot on
    the ~10s default client timeout), state == SUCCESS check (a PARTIAL
    snapshot is not a restore point), recent-reuse on retry."""
    return take_snapshot(es, repo, indices, name_prefix="pre-legacy-cleanup")


def _delete_indices(es, indices: list[str]) -> None:
    for name in indices:
        try:
            es.indices.delete(index=name)
            logging.info(f"Deleted {name}")
        except Exception as e:
            logging.error(f"Failed to delete {name}: {e}")


def main(params: dict) -> None:
    lib.logging.define("legacy-mapping-audit")
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
        raise AuditError("Elasticsearch client unavailable")

    logging.info("Auditing index mappings against unified schemas...")
    broken = _list_broken_indices(es)

    if not broken:
        logging.info("No broken indices found. Nothing to do.")
        # Operator-visible summary on stdout — the log file is the
        # detailed record but the terminal user wants a one-liner.
        print("audit_legacy_text_mappings: 0 broken indices. Cluster is clean.")
        return

    logging.warning(f"Found {len(broken)} broken index(es):")
    for name, mismatches in broken.items():
        logging.warning(f"- {name}")
        for line in mismatches:
            logging.warning(line)

    if not apply_changes:
        logging.info(
            "Dry run only. Re-run with --apply (and optionally --repo <repo>) to "
            "snapshot and delete. The next import will recreate them with the "
            "correct schema via _ensure_index_with_schema."
        )
        # Print the index names to stdout so the operator sees them
        # without having to tail the log file.
        print(f"audit_legacy_text_mappings: {len(broken)} broken index(es) (dry run):")
        for name in sorted(broken.keys()):
            print(f"  - {name}")
        print("Re-run with --apply to snapshot + delete. See log file for per-field detail.")
        return

    # Apply mode: snapshot first, then delete.
    broken_names = sorted(broken.keys())
    try:
        snapshot_name = _take_snapshot(es, repo, broken_names)
    except Exception as e:
        raise AuditError(
            f"Snapshot to {repo} failed ({e}); refusing to delete without a backup. "
            f"Try a different --repo, or check that the snapshot repo exists."
        ) from e

    _delete_indices(es, broken_names)
    logging.info(
        f"Cleanup complete. Snapshot: {repo}/{snapshot_name}. "
        f"Re-run the relevant imports to repopulate the deleted indices "
        f"with the correct schema."
    )
    print(
        f"audit_legacy_text_mappings: deleted {len(broken_names)} broken index(es). "
        f"Snapshot: {repo}/{snapshot_name}. Re-run the relevant imports to repopulate."
    )
