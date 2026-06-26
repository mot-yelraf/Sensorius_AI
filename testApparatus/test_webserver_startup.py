"""Focused coverage for WebServerController startup behavior."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saiWebServer import WebServerController


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
