"""Focused coverage for WebServerController startup behavior."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensorius.saiWebServer import SensoriusStaticFiles, WebServerController


class _Settings:
    def get_setting(self, section, key, default=None):
        if section == "Network" and key == "HTTPHOST":
            return "127.0.0.1"
        if section == "Network" and key == "HTTPPORT":
            return 8000
        return default


def test_prewarm_startup_handler_schedules_without_returning_awaitable(monkeypatch):
    async def _fake_prewarm(self):
        return None

    created = []

    def _fake_create_task(coro):
        created.append(coro)
        coro.close()
        return SimpleNamespace(name="prewarm-task")

    monkeypatch.setattr(WebServerController, "_prewarm", _fake_prewarm)
    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)

    controller = WebServerController(_Settings(), None, None, None, None)

    result = controller._schedule_prewarm()

    assert result is None
    assert len(created) == 1


@pytest.mark.asyncio
async def test_versioned_overview_asset_has_immutable_cache_header(tmp_path):
    (tmp_path / "01-sensorius-overview-v4.png").write_bytes(b"image")
    app = FastAPI()
    app.mount("/ui_static", SensoriusStaticFiles(directory=str(tmp_path)))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ui_static/01-sensorius-overview-v4.png?v=test")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
