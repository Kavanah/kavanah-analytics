"""DT pull importer fetches baptism_generation +
quick_button_contact_established.

The cutover dropped these two fields (the legacy push plugin carried
them), silently zeroing the builder's multiplier_contacts and
spiritual_conversations map stats. A live-site survey (2026-06-12) showed both
fields populated on both prod DT sites (baptism_generation 17/305 + 4/195,
quick_button ~50-64% of contacts), so the fix is to map them, not retire the
stats.

Both map stats are `exists`-type queries server-side, so the mapping must OMIT
the fields when absent / unparseable / 0 — an emitted 0 or "" would count
contacts with no generations / no quick-button clicks.
"""

from __future__ import annotations

import json
import os

import pytest

from import_scripts.disciple_tools import mapping

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.abspath(mapping.__file__)), "..", "..", "schemas", "unified_schema_disciple_tools.json"
)

_BASE_CONTACT = {
    "ID": "901",
    "post_type": "contacts",
    "post_title": "Stat Fields Contact",
    "post_date": {"timestamp": 1776447400, "formatted": "2026-04-17"},
    "last_modified": {"timestamp": 1778903128, "formatted": "2026-05-16"},
}


class TestStatFieldsMapped:

    def test_fields_mapped_as_ints_when_carried(self):
        """should map baptism_generation and quick_button_contact_established
        to ints whether DT returns them as numeric strings or ints."""
        post = dict(_BASE_CONTACT, baptism_generation="2", quick_button_contact_established=5)

        meta = mapping.map_contact(post, "team_abc")["meta"]

        assert meta["baptism_generation"] == 2
        assert meta["quick_button_contact_established"] == 5

    def test_fields_omitted_when_absent_zero_or_unparseable(self):
        """should omit both fields entirely (not emit 0/"") when unset — the
        map stats are exists-type queries and ES exists matches any value."""
        absent = mapping.map_contact(dict(_BASE_CONTACT), "team_abc")["meta"]
        assert "baptism_generation" not in absent
        assert "quick_button_contact_established" not in absent

        zeroed = mapping.map_contact(
            dict(_BASE_CONTACT, baptism_generation=0, quick_button_contact_established="0"),
            "team_abc",
        )["meta"]
        assert "baptism_generation" not in zeroed
        assert "quick_button_contact_established" not in zeroed

        junk = mapping.map_contact(
            dict(_BASE_CONTACT, baptism_generation="", quick_button_contact_established="n/a"),
            "team_abc",
        )["meta"]
        assert "baptism_generation" not in junk
        assert "quick_button_contact_established" not in junk


class TestSchemaCarriesStatFields:

    def test_schema_maps_both_fields_as_long(self):
        """should map both fields explicitly as long in the unified schema —
        dynamic mapping from a string value would break sum aggregations."""
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            schema = json.load(f)

        meta_props = schema["properties"]["meta"]["properties"]
        assert meta_props["baptism_generation"] == {"type": "long"}
        assert meta_props["quick_button_contact_established"] == {"type": "long"}
