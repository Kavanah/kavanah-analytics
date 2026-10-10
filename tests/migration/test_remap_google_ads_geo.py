"""tests for remap_google_ads_geo.

Focus on the pieces that carry real bug risk (the live two-pass reindex is
exercised by the dry-run smoke on the box; ES interactions here are mocked):

  * ``_is_clean`` — the idempotency guard. A geo field mapped as text+keyword is
    the mixed-mapping drift -> dirty; all-bare-keyword -> clean; no geo markers
    at all -> dirty (must still adopt the pinned schema).
  * ``_schema_for`` — the geo overrides (geo_country_code/name keyword,
    location geo_point) that pin the fields even when deployed code predates the
    schema fix, and don't clobber when the schema already defines them.
  * ``_concrete_indices`` — must catch both ESID form (``__google_ads``) and slug
    form (``_google_ads``) and exclude temp indices.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "bin" / "python"
sys.path.insert(0, str(PYTHON_DIR))

from import_scripts.migration import remap_google_ads_geo as mod  # noqa: E402


# -- hijack guard for the temp index -------------------------------

def test_temp_index_name_is_covered_by_a_project_pattern():
    """REGRESSION: the temp index
    ``{index}__remap_2`` does NOT end in ``_google_ads``, so the bare
    ``*_google_ads`` pattern misses it. Without a trailing-star ``*_google_ads*``
    pattern it falls through to ES's data-stream hijacker and ``create`` fails.
    This pins that a project pattern covers the temp name."""
    import fnmatch
    from lib import es_index_template_service as svc

    temp_name = f"oe_925sbim2adjkuzgs5__google_ads{mod.TEMP_SUFFIX}"
    assert any(
        fnmatch.fnmatch(temp_name, pat) for pat in svc.PROJECT_INDEX_PATTERNS
    ), f"No PROJECT_INDEX_PATTERNS entry covers {temp_name!r} — it will be hijacked as a data stream."

_TEXT_KW = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}}


def _es_with_mapping(index: str, props: dict) -> mock.MagicMock:
    es = mock.MagicMock()
    es.indices.get_mapping.return_value = {index: {"mappings": {"properties": props}}}
    return es


# --- _is_clean -------------------------------------------------------------

def test_is_clean_false_for_text_mapped_geo_country_code():
    """geo_country_code as text+keyword is the drift -> dirty.

    This is the exact rogue-index shape on prod (kk4x8j4b6pt) — the one that
    raised "Fielddata is disabled" on a bare-field aggregation.
    """
    es = _es_with_mapping(
        "kk4x8j4b6pt_nsoncaxx__google_ads",
        {
            "geo_country_code": _TEXT_KW,
            "geo_country_name": _TEXT_KW,
        },
    )
    assert mod._is_clean(es, "kk4x8j4b6pt_nsoncaxx__google_ads") is False


def test_is_clean_false_when_one_geo_marker_drifts():
    """Mixed within an index: name bare keyword but code text -> still dirty."""
    es = _es_with_mapping(
        "x__google_ads",
        {
            "geo_country_name": {"type": "keyword"},
            "geo_country_code": _TEXT_KW,
        },
    )
    assert mod._is_clean(es, "x__google_ads") is False


def test_is_clean_true_when_all_present_markers_are_bare_keyword():
    """All present geo markers bare keyword -> clean (healthy-sibling case)."""
    es = _es_with_mapping(
        "v0_h4jsbim2adjku4-8o__google_ads",
        {
            "geo_country_code": {"type": "keyword"},
            "geo_country_name": {"type": "keyword"},
        },
    )
    assert mod._is_clean(es, "v0_h4jsbim2adjku4-8o__google_ads") is True


def test_is_clean_false_when_no_geo_markers_present():
    """An index with no geo fields yet must NOT read clean — it still needs the
    pinned schema applied, otherwise it would never get rebuilt and the next geo
    doc would dynamically map the field again."""
    es = _es_with_mapping("old__google_ads", {"campaign_id": {"type": "keyword"}})
    assert mod._is_clean(es, "old__google_ads") is False


# --- _schema_for -----------------------------------------------------------

def test_schema_for_pins_geo_fields_keyword_and_location_geo_point():
    """The create-mapping always carries the geo overrides regardless of whether
    the deployed schema file has them yet."""
    schema = mod._schema_for("google_ads")
    props = schema["properties"]
    assert props["geo_country_code"] == {"type": "keyword"}
    assert props["geo_country_name"] == {"type": "keyword"}
    assert props["location"] == {"type": "geo_point"}


def test_schema_carries_app_installs_float():
    """app_installs must be float, not long — the importer writes conversions
    (a float), so a long mapping truncated fractional installs. The reindex
    picks this up from the schema (long->float widens losslessly)."""
    schema = mod._schema_for("google_ads")
    assert schema["properties"]["app_installs"]["type"] == "float"


def test_schema_for_override_does_not_clobber_existing_schema_field():
    """setdefault must not overwrite a field the schema already defines — if the
    schema PR has landed, geo_country_code is keyword and stays keyword."""
    base = {"properties": {"geo_country_code": {"type": "keyword"}}}
    with mock.patch.object(mod, "get_schema_for_index_type", return_value=base):
        schema = mod._schema_for("google_ads")
    assert schema["properties"]["geo_country_code"]["type"] == "keyword"


# -- snapshot idempotency ---------------

def test_take_snapshot_reuses_existing_does_not_recreate():
    """REGRESSION: a re-run after an aborted apply
    must NOT re-create the snapshot. moved the mechanism into
    migration.snapshot_gate (this script's own copy is gone): reuse now keys on
    the prefix + SUCCESS state + index coverage + recency, and fresh names are
    timestamped so a collision cannot exist at all."""
    import time as _time

    from migration import snapshot_gate as sg

    es = mock.MagicMock()
    es.snapshot.get.return_value = {
        "snapshots": [
            {
                "snapshot": "pre-google-ads-geo-remap-100",
                "state": "SUCCESS",
                "indices": ["a__google_ads"],
                "start_time_in_millis": _time.time() * 1000,
            }
        ]
    }
    name = sg.take_snapshot(es, "reindex_safety_repo", ["a__google_ads"], name_prefix="pre-google-ads-geo-remap")
    assert name == "pre-google-ads-geo-remap-100"
    es.options.return_value.snapshot.create.assert_not_called()


def test_take_snapshot_creates_when_absent():
    """When no reusable prior snapshot exists, create a fresh timestamped one."""
    from migration import snapshot_gate as sg

    es = mock.MagicMock()
    es.snapshot.get.return_value = {"snapshots": []}
    es.options.return_value.snapshot.create.return_value = {"snapshot": {"state": "SUCCESS"}}
    name = sg.take_snapshot(es, "reindex_safety_repo", ["a__google_ads"], name_prefix="pre-google-ads-geo-remap")
    assert name.startswith("pre-google-ads-geo-remap-")
    es.options.return_value.snapshot.create.assert_called_once()


# --- _concrete_indices -----------------------------------------------------

def test_concrete_indices_matches_esid_and_slug_forms_excludes_temp():
    """`*_google_ads` must catch both `{esid}__google_ads` and `{slug}_google_ads`
    and drop temp indices."""
    es = mock.MagicMock()
    es.indices.get.return_value = {
        "gmclvd0g5fclosup__google_ads": {},      # esid form
        "kavanah_team_google_ads": {},           # slug form (single underscore)
        "x__google_ads__remap": {},       # temp — excluded
    }
    got = mod._concrete_indices(es)
    assert got == ["gmclvd0g5fclosup__google_ads", "kavanah_team_google_ads"]
