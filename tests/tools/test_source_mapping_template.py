"""per-source ES mapping templates (dynamic-mapping race guard).

Background
----------
``create_index_if_not_exists`` applies a source's schema ONLY if the index does
not already exist. When a doc write reaches ES before that call (the race), ES
auto-creates the index with DYNAMIC mapping — every field becomes ``text``.
Dashboard ``terms``/metric aggregations on the bare field then throw
``Fielddata is disabled … Text fields are not optimised for aggregations``
(HTTP 400) → empty panels, while imports look fine. FLKCH/FLKOI/FLKOO hit this
on prod.

The template stops the data-stream hijack but carries NO mappings, so
an auto-created index is regular-but-dynamic (the bug). adds one
mapping-carrying composable template per ``SCHEMA_MAP`` source, out-ranking the
 (99) and legacy ``<source>_no_datastream`` (250) templates, so an
auto-created ``*_<source>`` index is born correctly typed.

These pins are shape/behaviour only (no live ES). The end-to-end "fresh index
is born keyword + aggregation succeeds" proof was run on staging (2026-06-17).

Mutation-verified locally:
  - Drop ``mappings`` from build_source_mapping_template_body → test_body fails
    (a mapping-less template is exactly the gap this fixes).
  - Set SOURCE_MAPPING_PRIORITY below 250 → test_priority fails (would lose to
    the legacy <source>_no_datastream templates and never apply).
  - Add a ``data_stream`` block to the body → test_body fails (would re-hijack).
  - Make a SCHEMA_MAP source's es_type for ad_account_id ``text`` → test_types
    fails (that IS the bug).
  - Make ensure_source_mapping_templates raise instead of returning {} on ES
    down → test_fail_soft fails (startup must not crash).
"""

from __future__ import annotations

from unittest import mock

import pytest


def test_every_schema_source_template_carries_correct_mappings():
    """Every SCHEMA_MAP source builds a template whose mappings == its unified
    schema and whose aggregatable fields are keyword/numeric, not text."""
    from lib import es_index_template_service as svc
    from migration.schema_loader import SCHEMA_MAP, get_schema_for_index_type

    for source in SCHEMA_MAP:
        body = svc.build_source_mapping_template_body(source)

        # exact pattern + out-ranking priority + no data_stream
        assert body["index_patterns"] == [f"*_{source}"], f"{source} template must match exactly '*_{source}'"
        assert body["priority"] == svc.SOURCE_MAPPING_PRIORITY
        assert "data_stream" not in body, f"{source} template must NOT carry a data_stream block"

        # the load-bearing fix: mappings present and equal to the unified schema
        mappings = body["template"].get("mappings")
        assert mappings and mappings.get("properties"), (
            f"{source} template MUST carry non-empty mappings — a "
            f"mapping-less template is the  gap that lets ES auto-create "
            f"the index with dynamic text mapping."
        )
        assert mappings == get_schema_for_index_type(source), (
            f"{source} template mappings must equal load_schema(source) "
            f"so the template and create_index_if_not_exists never disagree."
        )


def test_priority_outranks_legacy_no_datastream_templates():
    """Priority MUST beat the legacy <source>_no_datastream templates (250) and
    the template (99), or the mapping template never wins on auto-create."""
    from lib import es_index_template_service as svc

    assert svc.SOURCE_MAPPING_PRIORITY > 250, (
        "legacy '<source>_no_datastream' templates sit at priority 250 "
        "(pattern '*_<source>', NO mappings). The mapping template must out-rank "
        f"them or it never applies on auto-create. Got {svc.SOURCE_MAPPING_PRIORITY}."
    )


def test_aggregatable_fields_are_not_text():
    """The fields the dashboards aggregate/filter on must resolve to
    keyword/numeric in every source template that defines them — never text
    (text is the bug that throws the fielddata 400)."""
    from lib import es_index_template_service as svc
    from migration.schema_loader import SCHEMA_MAP

    # representative bare fields dashboards aggregate on (per the ticket)
    AGG_FIELDS = ("ad_account_id", "insight_spend", "publisher_platform", "team_esid")
    NON_TEXT = {"keyword", "integer", "long", "float", "double", "date", "boolean"}

    checked = 0
    for source in SCHEMA_MAP:
        props = svc.build_source_mapping_template_body(source)["template"]["mappings"]["properties"]
        for f in AGG_FIELDS:
            if f in props:
                t = props[f].get("type")
                assert t in NON_TEXT, (
                    f"{source}.{f} must be aggregatable ({NON_TEXT}), "
                    f"got {t!r} — a text type here throws the dashboard fielddata 400."
                )
                checked += 1
    assert checked > 0, "expected at least one aggregatable field across sources"


def test_install_happy_path_and_fail_soft():
    """ensure_source_mapping_templates installs one template per source on the
    happy path, and never raises when ES is unreachable (startup must continue)."""
    from lib import es_index_template_service as svc
    from migration.schema_loader import SCHEMA_MAP

    # happy path — ES reachable
    fake_es = mock.Mock()
    with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=fake_es):
        results = svc.ensure_source_mapping_templates()
    assert set(results) == set(SCHEMA_MAP) and all(
        results.values()
    ), "every SCHEMA_MAP source should install on the happy path"
    assert fake_es.indices.put_index_template.call_count == len(SCHEMA_MAP)

    # ES unreachable — fail soft, no raise, empty dict
    with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
        results = svc.ensure_source_mapping_templates()
    assert results == {}, "ES-down must return {} without raising"

    # one source's put raises — that source is False, others unaffected, no raise
    flaky = mock.Mock()
    calls = {"n": 0}

    def _maybe_raise(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ES booboo")
        return None

    flaky.indices.put_index_template.side_effect = _maybe_raise
    with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=flaky):
        results = svc.ensure_source_mapping_templates()
    assert (
        sum(1 for v in results.values() if not v) == 1
    ), "a single failing source must be isolated, not abort the rest"
