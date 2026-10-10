"""tests for remap_ga_metrics.

Focus on the three pieces that carry real bug risk:

  * ``_is_clean`` — the idempotency guard. Unlike (which guards on the
    ``date`` field) drift is in report_type / team_country / the
     string dims, so the guard must classify a mixed-mapped index as
    dirty even when ``date`` is fine, and an already-bare-keyword index as clean.
  * ``_schema_for`` — the team_country: keyword override that pins the field even
    when the deployed code predates the schema fix.
  * ``_concrete_indices`` — the suffix-overlap guard: ``__ga_metrics`` must not
    swallow ``__ga_metrics_window``, and temp indices must be excluded.

The two-pass reindex itself is exercised by the smoke run on the box (dry-run
first); the ES interactions here are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "bin" / "python"
sys.path.insert(0, str(PYTHON_DIR))

from import_scripts.migration import remap_ga_metrics as mod  # noqa: E402


def _es_with_mapping(index: str, props: dict) -> mock.MagicMock:
    es = mock.MagicMock()
    es.indices.get_mapping.return_value = {index: {"mappings": {"properties": props}}}
    return es


# --- _is_clean -------------------------------------------------------------

def test_is_clean_false_for_text_mapped_report_type():
    """A report_type mapped as text+keyword subfield is the drift -> dirty."""
    es = _es_with_mapping(
        "x__ga_metrics",
        {
            "date": {"type": "date"},  # date is fine — guard would skip this
            "report_type": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "team_country": {"type": "keyword"},
        },
    )
    assert mod._is_clean(es, "x__ga_metrics") is False


def test_is_clean_false_for_text_mapped_team_country():
    """team_country drifting to text (the schema-gap re-drift) -> dirty."""
    es = _es_with_mapping(
        "x__ga_metrics",
        {
            "report_type": {"type": "keyword"},
            "team_country": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        },
    )
    assert mod._is_clean(es, "x__ga_metrics") is False


def test_is_clean_true_when_all_present_markers_are_bare_keyword():
    """All present markers bare keyword -> clean (FLKVT post-cutover case)."""
    es = _es_with_mapping(
        "flkvt__ga_metrics",
        {
            "report_type": {"type": "keyword"},
            "team_country": {"type": "keyword"},
            "newVsReturning": {"type": "keyword"},
        },
    )
    assert mod._is_clean(es, "flkvt__ga_metrics") is True


def test_is_clean_false_when_no_markers_present():
    """An index with none of the /marker fields must NOT read clean — it
    still needs the schema applied, otherwise it would never get rebuilt."""
    es = _es_with_mapping("old__ga_metrics", {"date": {"type": "date"}, "sessions": {"type": "integer"}})
    assert mod._is_clean(es, "old__ga_metrics") is False


# --- _schema_for -----------------------------------------------------------

def test_schema_for_base_pins_team_country_keyword():
    """The base ga_metrics create-mapping always carries team_country: keyword."""
    schema = mod._schema_for("ga_metrics")
    assert schema["properties"]["team_country"] == {"type": "keyword"}


def test_schema_for_override_no_op_when_schema_already_has_field():
    """If the schema already defines team_country, the override does not clobber
    it (setdefault), and the type stays keyword."""
    schema = mod._schema_for("ga_metrics_window")  # window already defines it
    assert schema["properties"]["team_country"]["type"] == "keyword"


# --- _concrete_indices -----------------------------------------------------

def test_concrete_indices_base_excludes_window_and_temp():
    """`__ga_metrics` glob must not return the _window sibling nor temp indices."""
    es = mock.MagicMock()
    es.indices.get.return_value = {
        "a__ga_metrics": {},
        "b__ga_metrics": {},
        "a__ga_metrics_window": {},  # must be excluded from the base list
        "c__ga_metrics__remap": {},  # temp — excluded
    }
    got = mod._concrete_indices(es, "__ga_metrics")
    assert got == ["a__ga_metrics", "b__ga_metrics"]


def test_concrete_indices_window_only_matches_window():
    es = mock.MagicMock()
    es.indices.get.return_value = {
        "a__ga_metrics_window": {},
        "a__ga_metrics": {},
        "a__ga_metrics_window__remap": {},
    }
    got = mod._concrete_indices(es, "__ga_metrics_window")
    assert got == ["a__ga_metrics_window"]
