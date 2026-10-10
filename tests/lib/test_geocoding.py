"""Tests for ``lib.geocoding``."""

import sys
import types
from unittest import mock

import pytest


def _install_fake_geopy(geocode_side_effect):
    """Install a fake geopy package on sys.modules with a Nominatim that
    returns/raises whatever the test wants. Returns the patcher dict so the
    caller can inspect call counts."""
    fake_geocoder = mock.MagicMock()
    fake_geocoder.geocode = mock.MagicMock(side_effect=geocode_side_effect)

    fake_nominatim = mock.MagicMock(return_value=fake_geocoder)

    geopy_module = types.ModuleType("geopy")
    geopy_geocoders = types.ModuleType("geopy.geocoders")
    geopy_geocoders.Nominatim = fake_nominatim
    geopy_module.geocoders = geopy_geocoders

    geopy_exc = types.ModuleType("geopy.exc")

    class GeocoderUnavailable(Exception):
        pass

    geopy_exc.GeocoderUnavailable = GeocoderUnavailable

    return {
        "geopy": geopy_module,
        "geopy.geocoders": geopy_geocoders,
        "geopy.exc": geopy_exc,
        "geocoder": fake_geocoder,
        "GeocoderUnavailable": GeocoderUnavailable,
    }


@pytest.fixture
def stub_cache(tmp_path, monkeypatch):
    """Point the geocoding module's cache at a fresh temp file per test."""
    import lib.geocoding as geo

    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(geo, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(geo, "_cache", None, raising=False)
    monkeypatch.setattr(geo, "_cache_mtime", None, raising=False)
    return geo


@pytest.fixture
def fast_sleep(monkeypatch):
    """Make time.sleep a no-op so retries don't wall-clock the test."""
    import lib.geocoding as geo

    monkeypatch.setattr(geo.time, "sleep", lambda _: None)


@pytest.fixture
def short_retry_delays(monkeypatch):
    """Reduce the retry budget so the test runs fast even with real sleep."""
    import lib.geocoding as geo

    monkeypatch.setattr(geo, "_NOMINATIM_RETRY_DELAYS", [0, 0, 0])


class TestGeocodeCityRetry:
    """429-style failures should retry with backoff before giving up."""

    def test_returns_coords_on_first_attempt(self, stub_cache, fast_sleep, monkeypatch):
        loc = mock.MagicMock(latitude=10.0, longitude=20.0)
        modules = _install_fake_geopy(geocode_side_effect=lambda *a, **kw: loc)
        for name, mod in modules.items():
            if name not in {"geocoder", "GeocoderUnavailable"}:
                monkeypatch.setitem(sys.modules, name, mod)

        result = stub_cache.geocode_city("CityName", region="", country_code="us")

        assert result == {"lat": 10.0, "lon": 20.0}
        assert modules["geocoder"].geocode.call_count == 1

    def test_retries_on_geocoder_unavailable_then_succeeds(
        self, stub_cache, fast_sleep, short_retry_delays, monkeypatch
    ):
        loc = mock.MagicMock(latitude=10.0, longitude=20.0)
        modules = _install_fake_geopy(geocode_side_effect=None)
        # Two 429s, then success.
        modules["geocoder"].geocode.side_effect = [
            modules["GeocoderUnavailable"]("HTTP Error 429: Too Many Requests"),
            modules["GeocoderUnavailable"]("HTTP Error 429: Too Many Requests"),
            loc,
        ]
        for name, mod in modules.items():
            if name not in {"geocoder", "GeocoderUnavailable"}:
                monkeypatch.setitem(sys.modules, name, mod)

        result = stub_cache.geocode_city("CityName", region="", country_code="us")

        assert result == {"lat": 10.0, "lon": 20.0}
        assert modules["geocoder"].geocode.call_count == 3

    def test_gives_up_after_retries_exhausted(
        self, stub_cache, fast_sleep, short_retry_delays, monkeypatch
    ):
        modules = _install_fake_geopy(geocode_side_effect=None)
        modules["geocoder"].geocode.side_effect = modules["GeocoderUnavailable"](
            "HTTP Error 429: Too Many Requests"
        )
        for name, mod in modules.items():
            if name not in {"geocoder", "GeocoderUnavailable"}:
                monkeypatch.setitem(sys.modules, name, mod)

        result = stub_cache.geocode_city("CityName", region="", country_code="us")

        assert result is None
        # Initial attempt + 3 retries = 4 calls.
        assert modules["geocoder"].geocode.call_count == 4

    def test_non_retryable_exception_is_not_retried(
        self, stub_cache, fast_sleep, monkeypatch
    ):
        modules = _install_fake_geopy(geocode_side_effect=None)
        modules["geocoder"].geocode.side_effect = ValueError("boom")
        for name, mod in modules.items():
            if name not in {"geocoder", "GeocoderUnavailable"}:
                monkeypatch.setitem(sys.modules, name, mod)

        result = stub_cache.geocode_city("CityName", region="", country_code="us")

        assert result is None
        assert modules["geocoder"].geocode.call_count == 1
