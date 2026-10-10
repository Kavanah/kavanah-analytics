"""audit ``de_asset_*`` index mappings against the explicit asset
schemas (``schemas/asset_schema_*.json``, registered in
``migration.schema_loader.ASSET_SCHEMA_MAP``).

Background
==========
Asset-tier indices were historically created by the first document write, so
ES auto-mapped every string field as ``text + .keyword``. Restored or newer
indices are explicit ``keyword``. That drift is why the dual-shape
query workarounds exist. This audit is the asset-tier sibling of
``audit_legacy_text_mappings``, with one deliberate
difference: it NEVER deletes. Asset documents are source-of-truth records
(not re-importable like insights data), so remediation is the reindex tool
``tools.reindex_asset_mappings``, not snapshot+delete.

What this script does
=====================
1. For every index in ``ASSET_SCHEMA_MAP``, fetches the live ``_mapping`` and
   compares each schema field against it. A field is **broken** when the
   schema declares ``keyword`` (or ``date``) but the live mapping declares
   ``text`` (or a non-``date`` type) — the same rules as the data-tier audit.
2. Additionally flags any live top-level string field NOT in the schema that
   is mapped ``text`` — those are drifted dynamic fields the reindex will
   convert to ``keyword``.
3. Surveys the cluster for ``de_asset_*`` indices with NO registered schema
   and lists them as informational (visible, not failing).

This script is read-only and idempotent.

Usage
=====
    python3 /app/python/import.py migration audit_asset_mappings
"""

from __future__ import annotations

import logging

import lib.logging
from lib.es_helpers import get_elasticsearch_client

# Same break rules as the data-tier audit — one source of truth for what
# counts as a broken field on either tier.
from import_scripts.migration.audit_legacy_text_mappings import AuditError, _compare_field


def _load_asset_schema_map() -> dict:
    from migration.schema_loader import ASSET_SCHEMA_MAP

    return ASSET_SCHEMA_MAP


def _load_asset_schema(index_name: str) -> dict:
    from migration.schema_loader import get_schema_for_asset_index

    return get_schema_for_asset_index(index_name)


def _live_properties(es, index_name: str) -> dict | None:
    """Top-level properties of ``index_name``'s live mapping, or None when the
    index is missing / unreadable."""
    try:
        live = es.indices.get_mapping(index=index_name)
    except Exception as e:  # noqa: BLE001 — survey must not die on one index
        logging.warning(f"Could not fetch mapping for {index_name}: {e}")
        return None
    return live.get(index_name, {}).get("mappings", {}).get("properties", {}) or {}


def audit_asset_index(es, index_name: str, schema: dict) -> list[str] | None:
    """Return mismatch lines for ``index_name`` (empty = clean), or None when
    the index doesn't exist on this cluster."""
    if not es.indices.exists(index=index_name):
        return None

    live_props = _live_properties(es, index_name)
    if live_props is None:
        return []

    schema_props = schema.get("properties", {})
    mismatches: list[str] = []
    for field_name, schema_def in schema_props.items():
        live_def = live_props.get(field_name)
        if live_def is None:
            # Field absent from live mapping — fine; the schema (or its
            # dynamic template) covers it on first write after the reindex.
            continue
        msg = _compare_field(field_name, schema_def, live_def)
        if msg:
            mismatches.append(msg)

    # Drifted dynamic fields: live text fields the schema doesn't enumerate.
    # The reindex converts these to keyword via its live-mapping union.
    for field_name, live_def in live_props.items():
        if field_name in schema_props:
            continue
        if live_def.get("type") == "text":
            mismatches.append(f"  {field_name}: (not in schema), live=text")

    return mismatches


def survey_unregistered(es, registered: set[str]) -> list[str]:
    """All ``de_asset_*`` indices on the cluster without a registered schema."""
    try:
        indices = es.indices.get(index="de_asset_*", ignore_unavailable=True)
    except Exception as e:  # noqa: BLE001 — informational only
        logging.warning(f"Could not list de_asset_* indices: {e}")
        return []
    return sorted(name for name in indices.keys() if name not in registered)


def main(params: dict) -> None:
    lib.logging.define("asset-mapping-audit")

    es = get_elasticsearch_client()
    if es is None:
        raise AuditError("Elasticsearch client unavailable")

    asset_schema_map = _load_asset_schema_map()

    logging.info("Auditing de_asset_* mappings against asset schemas...")
    broken: dict[str, list[str]] = {}
    missing: list[str] = []
    for index_name in sorted(asset_schema_map.keys()):
        try:
            schema = _load_asset_schema(index_name)
        except Exception as e:  # noqa: BLE001 — report and keep surveying
            logging.warning(f"Could not load schema for {index_name}: {e}; skipping")
            continue
        mismatches = audit_asset_index(es, index_name, schema)
        if mismatches is None:
            missing.append(index_name)
        elif mismatches:
            broken[index_name] = mismatches

    unregistered = survey_unregistered(es, set(asset_schema_map.keys()))

    for name, mismatches in broken.items():
        logging.warning(f"- {name}")
        for line in mismatches:
            logging.warning(line)

    # Operator-visible summary on stdout, matching the data-tier audit's style.
    if broken:
        print(f"audit_asset_mappings: {len(broken)} broken de_asset_* index(es):")
        for name in sorted(broken.keys()):
            print(f"  - {name} ({len(broken[name])} field(s))")
        print(
            "Remediate with: python3 -m tools.reindex_asset_mappings "
            "(dry-run first; --apply to reindex). See log file for per-field detail."
        )
    else:
        print("audit_asset_mappings: 0 broken de_asset_* indices. Asset tier is clean.")

    if missing:
        print(f"Not present on this cluster ({len(missing)}): {', '.join(missing)}")
    if unregistered:
        print(f"No schema registered ({len(unregistered)} — informational, not audited):")
        for name in unregistered:
            print(f"  - {name}")
