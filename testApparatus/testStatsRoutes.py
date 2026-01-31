import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from rPiWebRoutes import register_routes
from rPiStats import rPiStats
from unittest.mock import MagicMock

@pytest.fixture
async def test_app():
    app = FastAPI()

    # Mock components
    mock_sensor = MagicMock()
    mock_sensor.devID = "sensor_001"
    mock_sensor.current_data_set.return_value = (
        {"temp": 23.5, "rh": 55.0}, "valid", "2025/05/24 12:00:00"
    )
    mock_sensor.unit_map = {"temp": "°C", "rh": "%"}
    mock_sensor.measurements = [("temp", "°C", None, 1), ("rh", "%", None, 1)]

    mock_settings = MagicMock()
    mock_gc_mgr = MagicMock()
    mock_gc_mgr.freeMem.return_value = 123456
    mock_mqtt = MagicMock()
    mock_mqtt.broker = "localhost"
    mock_mqtt.topic = "sensor/topic"

    await register_routes(app, mock_sensor, mock_settings, mock_gc_mgr, mock_mqtt)
    yield app

@pytest.mark.asyncio
async def test_stats_json(test_app):
    async with AsyncClient(app=test_app, base_url="http://test") as ac:
        response = await ac.get("/stats")
        assert response.status_code == 200
        assert isinstance(response.json(), dict)

@pytest.mark.asyncio
async def test_dashboard_html(test_app):
    async with AsyncClient(app=test_app, base_url="http://test") as ac:
        response = await ac.get("/dashboard")
        assert response.status_code == 200
        assert "<html>" in response.text.lower()
        assert "dashboard" in response.text.lower()

@pytest.mark.asyncio
async def test_itaot_includes_dashboard(test_app):
    async with AsyncClient(app=test_app, base_url="http://test") as ac:
        response = await ac.get("/itaot")
        assert response.status_code == 200
        commands = response.json().get("commands", [])
        paths = [cmd["path"] for cmd in commands]
        assert "/dashboard" in paths
        assert any("24hr" in cmd["description"].lower() for cmd in commands)
