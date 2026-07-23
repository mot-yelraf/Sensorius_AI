import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.saiSettings import saiSettings
from Sensorius import bootstrap_astral_auto_location


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return dict(self._payload)


class _FakeHttpClient:
    responses_by_url: dict[str, object] = {}
    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        self.__class__.calls.append(url)
        response = self.__class__.responses_by_url[url]
        if isinstance(response, Exception):
            raise response
        return response


def _settings_for_astral_test(tmp_path):
    saiSettings.invalidate_cache()
    settings = saiSettings(
        base_dir=str(tmp_path / "system_settings"),
        device_id="test-hub",
        apply_live=False,
        make_startup_backup=False,
        filename=None,
    )
    settings.set_many_in_memory(
        [
            ("Time", "TZ", "America/Denver"),
            ("Astral", "AUTO_IP", True),
            ("Astral", "LATITUDE", ""),
            ("Astral", "LONGITUDE", ""),
            ("Astral", "TIMEZONE", ""),
        ]
    )
    settings.save_settings()
    return settings


def test_resolve_astral_location_falls_back_to_ipv4_provider_before_ipwho(tmp_path, monkeypatch):
    settings = _settings_for_astral_test(tmp_path)
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses_by_url = {
        "https://ipapi.co/json/": _FakeResponse(503, {}),
        "http://ip-api.com/json/": _FakeResponse(
            200,
            {
                "status": "success",
                "lat": 32.79,
                "lon": -108.2749,
                "timezone": "America/Denver",
            },
        ),
        "https://ipwho.is/": _FakeResponse(
            200,
            {
                "success": True,
                "latitude": 35.0853336,
                "longitude": -106.6055534,
                "timezone": {"id": "America/Denver"},
            },
        ),
    }
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_FakeHttpClient))

    resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=0.1)

    assert resolved["source"] == "ip"
    assert resolved["provider"] == "ip-api.com"
    assert resolved["lat"] == pytest.approx(32.79)
    assert resolved["lon"] == pytest.approx(-108.2749)
    assert resolved["tz"] == "America/Denver"
    assert _FakeHttpClient.calls == ["https://ipapi.co/json/", "http://ip-api.com/json/"]

    reloaded = saiSettings(
        base_dir=str(tmp_path / "system_settings"),
        device_id="test-hub",
        apply_live=False,
        make_startup_backup=False,
        filename=None,
    )
    assert reloaded.get_setting("Astral", "LATITUDE") == "32.790000"
    assert reloaded.get_setting("Astral", "LONGITUDE") == "-108.274900"
    assert reloaded.get_setting("Astral", "TIMEZONE") == "America/Denver"
    assert reloaded.get_setting("Astral", "SOURCE") == "ip"
    assert reloaded.get_setting("Astral", "PROVIDER") == "ip-api.com"


def test_resolve_astral_location_refreshes_auto_saved_ip_coordinates(tmp_path, monkeypatch):
    settings = _settings_for_astral_test(tmp_path)
    settings.set_many_in_memory(
        [
            ("Astral", "LATITUDE", "35.085334"),
            ("Astral", "LONGITUDE", "-106.605553"),
            ("Astral", "TIMEZONE", "America/Denver"),
            ("Astral", "SOURCE", "ip"),
            ("Astral", "PROVIDER", "ipwho.is"),
        ]
    )
    settings.save_settings()
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses_by_url = {
        "https://ipapi.co/json/": _FakeResponse(
            200,
            {
                "latitude": 32.79,
                "longitude": -108.2749,
                "timezone": "America/Denver",
            },
        ),
        "http://ip-api.com/json/": _FakeResponse(200, {}),
        "https://ipwho.is/": _FakeResponse(200, {}),
    }
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_FakeHttpClient))

    resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=0.1)

    assert resolved["source"] == "ip"
    assert resolved["provider"] == "ipapi.co"
    assert resolved["lat"] == pytest.approx(32.79)
    assert resolved["lon"] == pytest.approx(-108.2749)
    assert _FakeHttpClient.calls == ["https://ipapi.co/json/"]

    reloaded = saiSettings(
        base_dir=str(tmp_path / "system_settings"),
        device_id="test-hub",
        apply_live=False,
        make_startup_backup=False,
        filename=None,
    )
    assert reloaded.get_setting("Astral", "LATITUDE") == "32.790000"
    assert reloaded.get_setting("Astral", "LONGITUDE") == "-108.274900"
    assert reloaded.get_setting("Astral", "SOURCE") == "ip"
    assert reloaded.get_setting("Astral", "PROVIDER") == "ipapi.co"


def test_resolve_astral_location_keeps_saved_manual_coordinates(tmp_path, monkeypatch):
    settings = _settings_for_astral_test(tmp_path)
    settings.set_many_in_memory(
        [
            ("Astral", "LATITUDE", "40.015000"),
            ("Astral", "LONGITUDE", "-105.270500"),
            ("Astral", "TIMEZONE", "America/Denver"),
            ("Astral", "SOURCE", "manual"),
            ("Astral", "PROVIDER", ""),
        ]
    )
    settings.save_settings()
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses_by_url = {
        "https://ipapi.co/json/": _FakeResponse(200, {"latitude": 32.79, "longitude": -108.2749}),
        "http://ip-api.com/json/": _FakeResponse(200, {}),
        "https://ipwho.is/": _FakeResponse(200, {}),
    }
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_FakeHttpClient))

    resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=0.1)

    assert resolved["source"] == "manual"
    assert resolved["provider"] == ""
    assert resolved["lat"] == pytest.approx(40.015)
    assert resolved["lon"] == pytest.approx(-105.2705)
    assert _FakeHttpClient.calls == []


def test_resolve_astral_location_reports_provider_errors(tmp_path, monkeypatch):
    settings = _settings_for_astral_test(tmp_path)
    _FakeHttpClient.calls = []
    _FakeHttpClient.responses_by_url = {
        "https://ipapi.co/json/": RuntimeError("dns failed"),
        "http://ip-api.com/json/": _FakeResponse(200, {"status": "fail", "message": "private range"}),
        "https://ipwho.is/": _FakeResponse(200, {"success": False, "message": "rate limited"}),
    }
    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_FakeHttpClient))

    resolved = settings.resolve_astral_location(persist_if_auto=True, timeout_sec=0.1)

    assert resolved["source"] == "none"
    assert resolved["lat"] is None
    assert resolved["lon"] is None
    assert "ipapi.co" in resolved["error"]
    assert "ipwho.is" in resolved["error"]
    assert "ip-api.com" in resolved["error"]


@pytest.mark.asyncio
async def test_startup_astral_bootstrap_retries_until_auto_location_succeeds():
    class _Settings:
        def __init__(self):
            self.calls = 0

        def resolve_astral_location(self, *, persist_if_auto=False, timeout_sec=0):
            self.calls += 1
            if self.calls == 1:
                return {"lat": None, "lon": None, "tz": "America/Denver", "source": "none", "error": "network not ready"}
            return {
                "lat": 35.0,
                "lon": -106.0,
                "tz": "America/Denver",
                "source": "ip",
                "provider": "ipwho.is",
                "error": "",
            }

    settings = _Settings()

    resolved = await bootstrap_astral_auto_location(settings, attempts=2, delay_sec=0.0)

    assert settings.calls == 2
    assert resolved["source"] == "ip"
    assert resolved["lat"] == 35.0
