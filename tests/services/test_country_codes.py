"""Unit tests for lib.country_codes.resolve_country_to_iso2."""

import pytest

from lib.country_codes import resolve_country_to_iso2


class TestResolveCountryToIso2:
    """Tests for resolve_country_to_iso2()."""

    # ── ISO2 pass-through ─────────────────────────────────────────────
    @pytest.mark.parametrize("code", ["US", "IN", "TR", "GB", "EG", "XK"])
    def test_valid_iso2_returned_as_is(self, code):
        assert resolve_country_to_iso2(code) == code

    def test_iso2_lowercase_normalised(self):
        assert resolve_country_to_iso2("us") == "US"

    def test_iso2_with_whitespace(self):
        assert resolve_country_to_iso2("  IN  ") == "IN"

    # ── ISO3 conversion ───────────────────────────────────────────────
    @pytest.mark.parametrize("iso3,expected", [
        ("IND", "IN"),
        ("TUR", "TR"),
        ("EGY", "EG"),
        ("USA", "US"),
        ("GBR", "GB"),
        ("ZWE", "ZW"),
    ])
    def test_iso3_converted_to_iso2(self, iso3, expected):
        assert resolve_country_to_iso2(iso3) == expected

    # ── Country name lookup ───────────────────────────────────────────
    @pytest.mark.parametrize("name,expected", [
        ("India", "IN"),
        ("Turkey", "TR"),
        ("Egypt", "EG"),
        ("United States", "US"),
        ("United Kingdom", "GB"),
        ("South Africa", "ZA"),
        ("Nigeria", "NG"),
        ("Brazil", "BR"),
        ("Philippines", "PH"),
        ("Bangladesh", "BD"),
    ])
    def test_common_country_names(self, name, expected):
        assert resolve_country_to_iso2(name) == expected

    # ── Variant / alias names ─────────────────────────────────────────
    @pytest.mark.parametrize("name,expected", [
        ("Turkiye", "TR"),
        ("türkiye", "TR"),
        ("Myanmar", "MM"),
        ("Burma", "MM"),
        ("Czechia", "CZ"),
        ("Czech Republic", "CZ"),
        ("Eswatini", "SZ"),
        ("Swaziland", "SZ"),
        ("Cabo Verde", "CV"),
        ("Cape Verde", "CV"),
        ("Timor-Leste", "TL"),
        ("East Timor", "TL"),
        ("Ivory Coast", "CI"),
        ("Cote d'Ivoire", "CI"),
        ("South Korea", "KR"),
        ("Korea", "KR"),
        ("North Korea", "KP"),
        ("Russia", "RU"),
        ("Russian Federation", "RU"),
        ("USA", "US"),
        ("UK", "GB"),
        ("UAe", "AE"),
        ("North Macedonia", "MK"),
        ("Kosovo", "XK"),
        ("DR Congo", "CD"),
        ("Democratic Republic of the Congo", "CD"),
    ])
    def test_variant_names(self, name, expected):
        assert resolve_country_to_iso2(name) == expected

    # ── Case insensitivity ────────────────────────────────────────────
    def test_case_insensitive(self):
        assert resolve_country_to_iso2("india") == "IN"
        assert resolve_country_to_iso2("INDIA") == "IN"
        assert resolve_country_to_iso2("India") == "IN"

    # ── None / empty / unknown ────────────────────────────────────────
    @pytest.mark.parametrize("value", [None, "", "  ", "unknown", "Unknown", "UNKNOWN"])
    def test_empty_and_unknown_return_none(self, value):
        assert resolve_country_to_iso2(value) is None

    # ── Unresolvable ──────────────────────────────────────────────────
    def test_unresolvable_returns_none(self):
        assert resolve_country_to_iso2("XyzLand") is None

    def test_unresolvable_two_char_not_valid_iso2(self):
        assert resolve_country_to_iso2("XX") is None
