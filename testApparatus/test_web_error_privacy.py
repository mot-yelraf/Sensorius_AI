"""Keep internal failure details out of HTTP error responses.

Injected service failures exercise OTA and email routes without contacting
devices or SMTP servers, while preserving their response status contracts.
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from sensorius.saiNodusOTA import NodusOTAError
from testApparatus.test_nodus_settings_schema_writes import _build_app


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,operation,status", [
    ("POST", "/api/nodus-ota/package/inspect", "inspect_package", 400),
    ("POST", "/api/nodus-ota/jobs", "start_job", 400),
    ("GET", "/api/nodus-ota/jobs/missing", "job_snapshot", 404),
    ("POST", "/api/nodus-ota/jobs/missing/cancel", "cancel_job", 404),
])
async def test_ota_errors_hide_internal_details(tmp_path, monkeypatch, method, path, operation, status):
    def fail(*args, **kwargs):
        raise NodusOTAError("job_not_found:private-path-and-service-details")

    service = SimpleNamespace(**{operation: fail})
    app, *_ = await _build_app(tmp_path, monkeypatch, ota_service=service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        response = await client.request(method, path, json={})
    assert response.status_code == status
    assert response.json() == {
        "ok": False, "error": "Nodus OTA request failed. Check the Sensorius log.",
    }


@pytest.mark.asyncio
async def test_email_failure_hides_smtp_exception(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-smtp-server-and-credentials")

    monkeypatch.setattr("sensorius.saiWebRoutes.SMTPEmailSender.send", fail)
    app, *_ = await _build_app(tmp_path, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"sec-fetch-site": "same-origin"}) as client:
        response = await client.post("/notifications/test-email", json={})
    assert response.status_code == 400
    assert response.json() == {
        "ok": False, "error": "Test email failed. Check the SMTP settings and Sensorius log.",
    }
