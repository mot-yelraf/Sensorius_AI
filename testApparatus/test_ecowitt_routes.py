"""Test Ecowitt discovery and configuration routes.

The route cases verify request validation and service integration while using
controlled gateway responses instead of the local network.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import sensorius.saiWebRoutes as saiWebRoutes


class _FastStats:
    def __init__(self, *_args, **_kwargs):
        pass

    async def start(self):
        return None

    def stop(self):
        return None


class _Settings:
    def get_all_sensor_ids(self):
        return []

    def get_setting(self, _section, _key, default=None, **_kwargs):
        return default


class _Service:
    poll_interval_sec = 120

    def __init__(self):
        self.saved = None
        self.disabled = False

    async def discover(self, gateway_url):
        if gateway_url == "bad":
            raise saiWebRoutes.EcowittError("Ecowitt gateway URL is invalid.")
        return {
            "ok": True,
            "gateway_url": "http://gw1100.local",
            "sensor_id": "ecowitt-e8db840f1543",
            "gateway_model": "GW1100A_V2.3.1",
            "inventory": [{"id": "E8", "type": "0", "name": "Weather array", "reporting": True}],
            "live_metric_count": 9,
        }

    def save_configuration(self, discovery, interval):
        self.saved = (discovery, interval)

    def status(self):
        return {"state": "online", "enabled": True, "inventory": []}

    def disable(self):
        self.disabled = True


@pytest_asyncio.fixture
async def ecowitt_client(monkeypatch):
    monkeypatch.setattr(saiWebRoutes, "FastStats", _FastStats)
    app = FastAPI()
    app.state.ecowitt_service = _Service()
    await saiWebRoutes.register_routes(app, _Settings(), object(), object(), object())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, app.state.ecowitt_service


@pytest.mark.asyncio
async def test_discover_populates_valid_sensor_list(ecowitt_client):
    client, _service = ecowitt_client
    response = await client.post("/ecowitt/discover", json={"gateway_url": "http://gw1100.local"})
    assert response.status_code == 200
    assert response.json()["inventory"][0]["reporting"] is True


@pytest.mark.asyncio
async def test_invalid_discovery_returns_concise_validation_error(ecowitt_client):
    client, _service = ecowitt_client
    response = await client.post("/ecowitt/discover", json={"gateway_url": "bad"})
    assert response.status_code == 400
    assert response.json()["error"] == "Ecowitt gateway URL is invalid."


@pytest.mark.asyncio
async def test_save_revalidates_gateway_and_disable_preserves_service(ecowitt_client):
    client, service = ecowitt_client
    response = await client.post(
        "/ecowitt/save",
        json={"gateway_url": "http://gw1100.local", "poll_interval_sec": 120},
    )
    assert response.status_code == 200
    assert service.saved[1] == 120

    response = await client.post("/ecowitt/disable")
    assert response.status_code == 200
    assert service.disabled is True
