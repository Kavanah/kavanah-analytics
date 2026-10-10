"""Field-catalog loader.

Sibling to ``schema_loader`` (which loads ES index mappings). This module loads
the field catalog — the single source of truth that *generates* those mappings
(via ``tools.catalog_to_schema``) and *feeds the importer request lists*.

The catalog lives at ``bin/python/catalog/<source>.json``. Its ``fields`` array
follows the entry schema documented in PHASE-106. The two consumers:

  - ``request_fields(source, dataset)`` — the API ``fields=[...]`` list an
    importer asks the source for. Derived as the distinct ``api_field`` of every
    ``origin == "insight"`` entry that is ``requested`` and in-dataset. Action
    metric children collapse onto their requested parent, exactly like PMA's
    request-builder.
  - ``tools.catalog_to_schema`` reads the same catalog for the ES mapping side.

Loading is cached; ``clear_cache()`` exists for tests.
"""

import json
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_PY_ROOT = os.path.dirname(_HERE)  # bin/python
CATALOG_DIR = os.path.join(_PY_ROOT, "catalog")

_CACHE: dict[str, dict[str, Any]] = {}


def load_catalog(source: str) -> dict[str, Any]:
    """Load and cache ``catalog/<source>.json``."""
    if source in _CACHE:
        return _CACHE[source]
    path = os.path.join(CATALOG_DIR, f"{source}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Field catalog not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        catalog = json.load(fh)
    if "fields" not in catalog:
        raise KeyError(f"Catalog {source} is missing 'fields'")
    _CACHE[source] = catalog
    return catalog


def request_fields(source: str, dataset: str) -> list[str]:
    """The source-API ``fields=[...]`` request list for one dataset.

    Args:
        source:  catalog source, e.g. ``"fb_ads"``.
        dataset: ``"daily"`` or ``"window"``. An entry is in-dataset when its
                 ``dataset`` is this value or ``"both"``.

    Returns:
        Distinct ``api_field`` values, sorted for determinism. Order is
        irrelevant to the API; sorting keeps the output stable for tests.
    """
    catalog = load_catalog(source)
    apis: set[str] = set()
    for entry in catalog["fields"]:
        if entry.get("origin") != "insight":
            continue
        if not entry.get("requested"):
            continue
        if dataset not in (entry.get("requested_in") or []):
            continue
        api = entry.get("api_field")
        if api:
            apis.add(api)
    return sorted(apis)


# the field-picker's toggle universe. Only these kinds are per-tenant
# toggleable; everything else (system/metadata/dimension/parents) is fixed.
# adds "breakdown": pull-level toggles (no api_field/es_field of their
# own) that gate an entire breakdown call, e.g. fb_ads.breakdown_demographics.
PICKER_KINDS = frozenset({"metric", "action_metric", "breakdown"})


def _toggleable(entry: dict[str, Any]) -> bool:
    """A tenant may override this entry: picker family, not always-on core."""
    return entry.get("kind") in PICKER_KINDS and not entry.get("core")


def request_fields_for_tenant(source: str, dataset: str, overrides: dict[str, Any]) -> list[str]:
    """Per-tenant variant of :func:`request_fields`.

    ``overrides`` maps ``field_id`` -> ``{"enabled": bool, ...}`` (the sparse
    per-workspace selection doc; ``{}`` or ``None`` means "no overrides").
    With empty overrides the result is exactly ``request_fields(source, dataset)``
    — the behavior-preserving default.

    Semantics per toggleable entry (kind metric/action_metric, non-core):
      - enabled False  -> its ``api_field`` contribution is dropped (other still-
        enabled entries sharing the api_field, or non-toggleable entries such as
        the action-metric parents, keep it in the union).
      - enabled True   -> included where the field belongs: ``requested_in`` when
        non-empty, else ``mapped_in`` — and a non-additive field
        (``aggregation: "none"``) can only ever be enabled into ``"window"``,
        never the daily additive dataset.
    Core / non-picker entries ignore overrides entirely.
    """
    catalog = load_catalog(source)
    overrides = overrides or {}
    apis: set[str] = set()
    for entry in catalog["fields"]:
        if entry.get("origin") != "insight":
            continue
        api = entry.get("api_field")
        if not api:
            continue
        base = bool(entry.get("requested")) and dataset in (entry.get("requested_in") or [])
        override = overrides.get(entry.get("field_id")) if _toggleable(entry) else None
        if override is None:
            include = base
        elif override.get("enabled"):
            eligible_in = entry.get("requested_in") or entry.get("mapped_in") or []
            include = dataset in eligible_in
            if entry.get("aggregation") == "none" and not (entry.get("requested_in") or []):
                include = include and dataset == "window"
        else:
            include = False
        if include:
            apis.add(api)
    return sorted(apis)


def disabled_es_fields(source: str, overrides: dict[str, Any]) -> set[str]:
    """ES doc keys a tenant has disabled.

    Action metrics arrive bundled under the always-requested ``actions`` /
    ``cost_per_action_type`` parents, so disabling one cannot change the API
    request — the importers instead drop these keys from each reformatted doc
    before indexing. Plain metrics land here too (belt and suspenders on top of
    the request-list removal). Core / non-picker entries never appear.
    """
    catalog = load_catalog(source)
    overrides = overrides or {}
    dropped: set[str] = set()
    for entry in catalog["fields"]:
        if not _toggleable(entry):
            continue
        override = overrides.get(entry.get("field_id"))
        if override is not None and not override.get("enabled"):
            es_field = entry.get("es_field")
            if es_field:
                dropped.add(es_field)
    return dropped


def clear_cache() -> None:
    """Drop the module cache (tests / runtime catalog edits)."""
    _CACHE.clear()
