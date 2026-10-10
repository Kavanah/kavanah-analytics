"""Generate ES index-mapping schemas from the field catalog.

The catalog (``bin/python/catalog/<source>.json``) is the single source of truth
for a data source's fields. This tool walks it and emits the per-index
``schemas/unified_schema_*.json`` mapping files that ``migration.schema_loader``
already loads and ``lib.es_helpers.create_index_if_not_exists`` already applies.

For ``fb_ads`` the catalog drives two index schemas:
  - ``unified_schema_fb_insights``         (mapped entries whose mapped_in ∋ "daily")
  - ``unified_schema_fb_insights_window``  (mapped entries whose mapped_in ∋ "window")

An entry has two independent membership axes: ``mapped_in`` (which index schemas
pin this es_field — drives this generator) and ``requested_in`` (which datasets
request this api_field from the source — drives the importer list). They mostly
coincide but can diverge (a field requested in daily yet only pinned in the window
schema, e.g. estimated_ad_recallers). Only entries with ``mapped: true`` produce a
mapping property. Entries with
``mapped: false`` (action-array parents, response-driven scalars) are requested
from the API but land via ES dynamic mapping, exactly as before the catalog
existed — so the generated files are byte-equivalent to the hand-maintained ones.

Usage::

    # regenerate the schema files from the catalog
    python -m tools.catalog_to_schema --source fb_ads

    # drift check (CI): exit 1 if committed schemas != catalog-generated
    python -m tools.catalog_to_schema --source fb_ads --check

The ``--check`` mode is the build guard: it fails if a developer edits a
``schemas/*.json`` by hand or forgets to regenerate after a catalog edit.
"""

import argparse
import json
import os
import sys
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_PY_ROOT = os.path.dirname(_HERE)  # bin/python
CATALOG_DIR = os.path.join(_PY_ROOT, "catalog")
SCHEMAS_DIR = os.path.join(_PY_ROOT, "schemas")

# Which catalog source generates which index schemas, and the dataset filter for
# each. A catalog entry lands in an index when its `dataset` is the index's
# dataset or "both".
SOURCE_INDICES: dict[str, list[tuple[str, str]]] = {
    # source -> [(schema_name, dataset_tag), ...]
    "fb_ads": [
        ("unified_schema_fb_insights", "daily"),
        ("unified_schema_fb_insights_window", "window"),
    ],
}


def load_catalog(source: str) -> dict[str, Any]:
    path = os.path.join(CATALOG_DIR, f"{source}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Catalog not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _mapping_spec(entry: dict[str, Any]) -> dict[str, Any]:
    """The ES mapping property for one catalog entry.

    Object / geo_point / dynamic fields carry a verbatim ``es_mapping`` passthrough
    (the generator never tries to reconstruct nested shapes). Scalars build
    ``{"type": ...}`` plus an optional ``format`` for dates.
    """
    if "es_mapping" in entry:
        return entry["es_mapping"]
    spec: dict[str, Any] = {"type": entry["es_type"]}
    if "es_format" in entry:
        spec["format"] = entry["es_format"]
    return spec


def generate_schema(catalog: dict[str, Any], dataset_tag: str) -> dict[str, Any]:
    """Build one index schema (``{"properties": {...}}``) for a dataset tag."""
    props: dict[str, Any] = {}
    for entry in catalog["fields"]:
        if not entry.get("mapped"):
            continue
        if dataset_tag not in (entry.get("mapped_in") or []):
            continue
        es_field = entry.get("es_field")
        if not es_field:
            continue
        props[es_field] = _mapping_spec(entry)
    return {"properties": props}


def _write_json(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def run(source: str, check: bool) -> int:
    if source not in SOURCE_INDICES:
        raise ValueError(f"Unknown source '{source}'. Known: {', '.join(SOURCE_INDICES)}")
    catalog = load_catalog(source)
    drift = []
    for schema_name, dataset_tag in SOURCE_INDICES[source]:
        generated = generate_schema(catalog, dataset_tag)
        target = os.path.join(SCHEMAS_DIR, f"{schema_name}.json")
        if check:
            if not os.path.exists(target):
                drift.append(f"{schema_name}: committed file missing")
                continue
            committed = _load_json(target)
            if committed != generated:
                gk, ck = set(generated["properties"]), set(committed["properties"])
                detail = []
                if gk - ck:
                    detail.append(f"+{sorted(gk - ck)}")
                if ck - gk:
                    detail.append(f"-{sorted(ck - gk)}")
                for k in gk & ck:
                    if generated["properties"][k] != committed["properties"][k]:
                        detail.append(f"~{k}: {committed['properties'][k]} != {generated['properties'][k]}")
                drift.append(f"{schema_name}: {'; '.join(detail) or 'differs'}")
        else:
            _write_json(target, generated)
            print(f"wrote {target} ({len(generated['properties'])} properties)")
    if check:
        if drift:
            print("CATALOG DRIFT — committed schemas do not match the catalog:", file=sys.stderr)
            for d in drift:
                print(f"  {d}", file=sys.stderr)
            print(f"\nRegenerate with: python -m tools.catalog_to_schema --source {source}", file=sys.stderr)
            return 1
        print(f"OK: {source} schemas match the catalog.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate ES schemas from the field catalog.")
    ap.add_argument("--source", default="fb_ads", help="Catalog source (default: fb_ads)")
    ap.add_argument("--check", action="store_true", help="Drift check only; exit 1 if schemas are stale")
    args = ap.parse_args()
    return run(args.source, args.check)


if __name__ == "__main__":
    raise SystemExit(main())
