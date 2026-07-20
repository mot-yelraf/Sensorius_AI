"""Focused tests for Sensorius-side Nodus OTA package handling."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import httpx

import saiNodusOTA
from saiNodusOTA import DEFAULT_WAIT_AFTER_PREPARE_S, NodusOTAError, NodusOTAService


def _write_package(
    root: Path,
    *,
    version: str = "v0.26.124.10",
    to_version: str = "v0.26.124.11",
    platform: str = "pico2w",
) -> Path:
    pkg = root / "pkg"
    files = pkg / "files"
    target = files / "cpynodus_ii" / "app.py"
    target.parent.mkdir(parents=True)
    payload = b"print('ota')\n"
    target.write_bytes(payload)
    manifest = {
        "schema": "nodus-ota/v1",
        "package_id": "ota-test",
        "from_tag": version,
        "to_tag": to_version,
        "target": {"platform": platform, "circuitpython": "9.2.8"},
        "requires": {"version": version},
        "files": [
            {
                "path": "cpynodus_ii/app.py",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "delete": [],
        "preserve": ["settings.toml", "sensor_i2c.toml", "sensor_soil.toml", "switch.toml"],
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pkg


def test_inspect_package_validates_manifest_and_files(tmp_path):
    pkg = _write_package(tmp_path)
    service = NodusOTAService(package_root=tmp_path / "ota")

    inspected = service.inspect_package(str(pkg))

    assert inspected.summary()["package_id"] == "ota-test"
    assert inspected.summary()["required_version"] == "v0.26.124.10"
    assert inspected.summary()["target_platform"] == "pico2w"
    assert inspected.summary()["target_circuitpython"] == "9.2.8"
    assert inspected.summary()["file_count"] == 1
    assert inspected.summary()["total_bytes"] == len(b"print('ota')\n")


def test_inspect_package_rejects_bad_sha(tmp_path):
    pkg = _write_package(tmp_path)
    manifest_path = pkg / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service = NodusOTAService(package_root=tmp_path / "ota")

    with pytest.raises(NodusOTAError, match="sha256"):
        service.inspect_package(str(pkg))


def test_list_devices_uses_mqtt_ingest_metadata(tmp_path):
    ingest = SimpleNamespace(
        mqtt_clients={"co2-v5p04u"},
        device_status={"co2-v5p04u": "online"},
        host_to_peer_ids={"co2-v5p04u": ["co2-v5p04u", "switch-v5p04u"]},
        nodus_firmware_versions={"co2-v5p04u": "v0.26.124.10"},
        last_mqtt_seen={"co2-v5p04u": 100.0},
        _host_ipv4addr={"co2-v5p04u": "10.0.0.42"},
        get_nodus_firmware_version=lambda device_id: "v0.26.124.10",
        get_nodus_board_type=lambda device_id: "pico2w",
    )
    service = NodusOTAService(mqtt_ingest=ingest, package_root=tmp_path / "ota")

    devices = service.list_devices()

    assert devices[0]["device_id"] == "co2-v5p04u"
    assert devices[0]["firmware_version"] == "v0.26.124.10"
    assert devices[0]["board_type"] == "pico2w"
    assert devices[0]["http_url"] == "http://10.0.0.42:8000"


def test_list_devices_collapses_combo_sensor_switch_to_sensor_id(tmp_path):
    def _resolve(device_id, device_type=None):
        if device_id in {"apvpd-test123", "switch-test123"}:
            return "apvpd-test123"
        return device_id

    ingest = SimpleNamespace(
        mqtt_clients={"apvpd-test123"},
        device_status={"apvpd-test123": "online", "switch-test123": "online"},
        host_to_peer_ids={"apvpd-test123": ["apvpd-test123", "switch-test123"]},
        nodus_firmware_versions={
            "apvpd-test123": "v0.26.125.4",
            "switch-test123": "v0.26.125.4",
        },
        last_mqtt_seen={"apvpd-test123": 100.0, "switch-test123": 100.0},
        _host_ipv4addr={},
        resolve_nodus_hostname=_resolve,
        get_nodus_firmware_version=lambda device_id: "v0.26.125.4",
        get_nodus_board_type=lambda device_id: "pico2w",
    )
    service = NodusOTAService(mqtt_ingest=ingest, package_root=tmp_path / "ota")

    devices = service.list_devices()

    assert [d["device_id"] for d in devices] == ["apvpd-test123"]


def test_start_job_rejects_package_for_different_board_type(tmp_path):
    pkg = _write_package(tmp_path, platform="pico2w")
    ingest = SimpleNamespace(
        get_nodus_firmware_version=lambda device_id: "v0.26.124.10",
        get_nodus_board_type=lambda device_id: "xesp32s3",
    )
    service = NodusOTAService(mqtt_ingest=ingest, package_root=tmp_path / "ota")

    with pytest.raises(NodusOTAError, match="target_platform_mismatch"):
        service.start_job(str(pkg), ["switch-xesps3"])


def test_run_device_blocks_version_mismatch_without_force(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    service._firmware_version = lambda device_id: "v0.26.172.5"

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert "version_mismatch:current=v0.26.172.5:requires_current=v0.26.174.1" in state["error"]


def test_run_device_allows_version_mismatch_with_force(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": True,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    service._firmware_version = lambda device_id: "v0.26.172.5"
    service._board_type = lambda device_id: "pico2w"
    service._publish_prepare = lambda device_id, package_id: None

    async def _raise_after_version_check(*_args, **_kwargs):
        raise NodusOTAError("after_version_check")

    service._wait_after_prepare = _raise_after_version_check

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "after_version_check"


def test_run_device_allows_reapplying_package_target_version(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1", to_version="v0.26.174.2")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    service._firmware_version = lambda device_id: "v0.26.174.2"
    service._board_type = lambda device_id: "pico2w"
    service._publish_prepare = lambda device_id, package_id: None

    async def _raise_after_version_check(*_args, **_kwargs):
        raise NodusOTAError("after_version_check")

    service._wait_after_prepare = _raise_after_version_check

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "after_version_check"


def test_run_device_aborts_ota_session_when_push_fails_after_ready(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    aborts = []
    service._firmware_version = lambda device_id: "v0.26.174.1"
    service._board_type = lambda device_id: "pico2w"
    service._device_url = lambda device_id: "http://device:8000"
    service._publish_prepare = lambda device_id, package_id: None

    async def _ready(*_args, **_kwargs):
        return True

    async def _push_fails(*_args, **_kwargs):
        raise NodusOTAError("begin_rejected:manifest_invalid_json")

    async def _abort(url):
        aborts.append(url)

    service._wait_after_prepare = _ready
    service._push_package = _push_fails
    service._abort_ota_session = _abort

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "begin_rejected:manifest_invalid_json"
    assert state["ota_abort_attempted"] is True
    assert state["ota_abort_reason"] == "push_failed"
    assert aborts == ["http://device:8000"]


def test_run_device_keeps_original_error_when_abort_fails(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    service._firmware_version = lambda device_id: "v0.26.174.1"
    service._board_type = lambda device_id: "pico2w"
    service._device_url = lambda device_id: "http://device:8000"
    service._publish_prepare = lambda device_id, package_id: None

    async def _ready(*_args, **_kwargs):
        return True

    async def _push_fails(*_args, **_kwargs):
        raise NodusOTAError("commit_rejected:sha256_mismatch")

    async def _abort_fails(url):
        raise RuntimeError("abort unavailable")

    service._wait_after_prepare = _ready
    service._push_package = _push_fails
    service._abort_ota_session = _abort_fails

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "commit_rejected:sha256_mismatch"
    assert state["ota_abort_attempted"] is True
    assert state["ota_abort_reason"] == "push_failed"


def test_run_device_does_not_abort_before_ota_readiness(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    aborts = []
    service._firmware_version = lambda device_id: "v0.26.174.1"
    service._board_type = lambda device_id: "pico2w"
    service._device_url = lambda device_id: "http://device:8000"
    service._publish_prepare = lambda device_id, package_id: None

    async def _not_ready(*_args, **_kwargs):
        raise NodusOTAError("ota_http_ready_timeout")

    async def _abort(url):
        aborts.append(url)

    service._wait_after_prepare = _not_ready
    service._abort_ota_session = _abort

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "ota_http_ready_timeout"
    assert "ota_abort_attempted" not in state
    assert aborts == []


def test_run_device_aborts_after_failed_fwupdate_result(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    pkg = service.inspect_package(str(_write_package(tmp_path, version="v0.26.174.1")))
    job = {
        "force_version_mismatch": False,
        "chunk_size": 1024,
        "devices": {
            "aht-yuk0nv": {
                "device_id": "aht-yuk0nv",
                "status": "queued",
                "phase": "queued",
            }
        },
    }
    aborts = []
    service._firmware_version = lambda device_id: "v0.26.174.1"
    service._board_type = lambda device_id: "pico2w"
    service._device_url = lambda device_id: "http://device:8000"
    service._publish_prepare = lambda device_id, package_id: None

    async def _ready(*_args, **_kwargs):
        return True

    async def _push_ok(*_args, **_kwargs):
        return None

    async def _failed_result(*_args, **_kwargs):
        raise NodusOTAError("fwupdate_result:failed:manifest_invalid_json")

    async def _abort(url):
        aborts.append(url)

    service._wait_after_prepare = _ready
    service._push_package = _push_ok
    service._wait_for_reboot_metadata = _failed_result
    service._abort_ota_session = _abort

    import asyncio
    asyncio.run(service._run_device(job, pkg, "aht-yuk0nv"))

    state = job["devices"]["aht-yuk0nv"]
    assert state["status"] == "failed"
    assert state["error"] == "fwupdate_result:failed:manifest_invalid_json"
    assert state["ota_abort_attempted"] is True
    assert state["ota_abort_reason"] == "fwupdate_result_failed"
    assert aborts == ["http://device:8000"]


def test_push_file_chunks_sends_binary_content_type_and_validates_offset(tmp_path):
    payload = b"x" * 71
    file_path = tmp_path / "tiny.mpy"
    file_path.write_bytes(payload)
    service = NodusOTAService(package_root=tmp_path / "ota")
    calls = []

    async def fake_request_json(client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/ota/file/begin?path=cpynodus_ii/__init__.mpy"):
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            assert kwargs["headers"]["Content-Type"] == "application/octet-stream"
            assert kwargs["content"] == payload
            return {"accepted": True, "offset": 71}
        if url.endswith("/ota/file/end?path=cpynodus_ii/__init__.mpy"):
            return {"accepted": True}
        raise AssertionError(url)

    service._request_json = fake_request_json

    import asyncio
    asyncio.run(
        service._push_file_chunks(
            types.SimpleNamespace(),
            "http://device:8000",
            "cpynodus_ii/__init__.mpy",
            file_path,
            {"bytes_sent": 0, "progress": 0},
            0,
            71,
            1024,
        )
    )

    assert any("/ota/file/chunk?" in url for _method, url, _kwargs in calls)


def test_push_file_chunks_rejects_unexpected_offset(tmp_path):
    file_path = tmp_path / "tiny.mpy"
    file_path.write_bytes(b"x" * 71)
    service = NodusOTAService(package_root=tmp_path / "ota")

    async def fake_request_json(client, method, url, **kwargs):
        if "/ota/file/begin?" in url:
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            return {"accepted": True, "offset": 0}
        return {"accepted": True}

    service._request_json = fake_request_json

    import asyncio
    with pytest.raises(NodusOTAError, match="file_chunk_offset_stalled"):
        asyncio.run(
            service._push_file_chunks(
                types.SimpleNamespace(),
                "http://device:8000",
                "cpynodus_ii/__init__.mpy",
                file_path,
                {"bytes_sent": 0, "progress": 0},
                0,
                71,
                1024,
            )
        )


def test_push_file_chunks_resends_from_reported_stale_offset(tmp_path):
    payload = (b"x" * 1024) + (b"y" * 999)
    file_path = tmp_path / "runtime.mpy"
    file_path.write_bytes(payload)
    service = NodusOTAService(package_root=tmp_path / "ota")
    chunk_offsets = []
    chunk_payloads = []
    stale_reported = False

    async def fake_request_json(client, method, url, **kwargs):
        nonlocal stale_reported
        if "/ota/file/begin?" in url:
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            offset = int(url.rsplit("offset=", 1)[-1])
            chunk_offsets.append(offset)
            chunk_payloads.append(kwargs["content"])
            if offset == 0:
                return {"accepted": True, "offset": 1024}
            if offset == 1024 and not stale_reported:
                stale_reported = True
                return {"accepted": True, "offset": 1024}
            return {"accepted": True, "offset": 2023}
        if "/ota/file/end?" in url:
            return {"accepted": True}
        raise AssertionError(url)

    service._request_json = fake_request_json

    import asyncio
    asyncio.run(
        service._push_file_chunks(
            types.SimpleNamespace(),
            "http://device:8000",
            "cpynodus_ii/ota/runtime.mpy",
            file_path,
            {"bytes_sent": 0, "progress": 0},
            0,
            2023,
            1024,
        )
    )

    assert chunk_offsets == [0, 1024, 1024]
    assert chunk_payloads == [payload[:1024], payload[1024:], payload[1024:]]


def test_request_json_retries_transient_control_response_failure(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    calls = 0

    class FakeClient:
        async def request(self, method, url, **kwargs):
            nonlocal calls
            calls += 1
            assert method == "POST"
            assert kwargs["json"] == {}
            if calls == 1:
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
            return httpx.Response(200, json={"accepted": True}, request=httpx.Request(method, url))

    import asyncio
    result = asyncio.run(
        service._request_json(
            FakeClient(),
            "POST",
            "http://device:8000/ota/file/end?path=cpynodus_ii/core/ntp.mpy",
            json_body={},
            retry_transient=True,
        )
    )

    assert result == {"accepted": True}
    assert calls == 2


def test_request_json_does_not_retry_chunk_by_default(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    calls = 0

    class FakeClient:
        async def request(self, method, url, **kwargs):
            nonlocal calls
            calls += 1
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    import asyncio
    with pytest.raises(NodusOTAError, match="http_request_failed:PUT"):
        asyncio.run(
            service._request_json(
                FakeClient(),
                "PUT",
                "http://device:8000/ota/file/chunk?path=x&offset=0",
                content=b"x",
            )
        )

    assert calls == 1


def test_push_package_retries_chunked_file_after_retryable_end_rejection(tmp_path):
    pkg = _write_package(tmp_path)
    service = NodusOTAService(package_root=tmp_path / "ota")
    package = service.inspect_package(str(pkg))
    calls = []
    end_calls = 0

    async def fake_request_json(client, method, url, **kwargs):
        nonlocal end_calls
        calls.append((method, url))
        if url.endswith("/ota/begin"):
            return {"accepted": True}
        if "/ota/file/begin?" in url:
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            return {"accepted": True, "offset": len(b"print('ota')\n")}
        if "/ota/file/end?" in url:
            end_calls += 1
            if end_calls == 1:
                return {"accepted": False, "error": "sha256_mismatch"}
            return {"accepted": True}
        if url.endswith("/ota/commit"):
            return {"accepted": True}
        raise AssertionError(url)

    service._request_json = fake_request_json

    import asyncio
    asyncio.run(
        service._push_package(
            types.SimpleNamespace(),
            "http://device:8000",
            package,
            {"bytes_sent": 0, "progress": 0},
            chunk_size=1024,
        )
    )

    begin_calls = [url for _method, url in calls if "/ota/file/begin?" in url]
    assert len(begin_calls) == 2
    assert end_calls == 2


def test_push_package_retries_chunked_file_after_retryable_chunk_rejection(tmp_path):
    pkg = _write_package(tmp_path)
    service = NodusOTAService(package_root=tmp_path / "ota")
    package = service.inspect_package(str(pkg))
    chunk_calls = 0
    begin_calls = 0

    async def fake_request_json(client, method, url, **kwargs):
        nonlocal chunk_calls, begin_calls
        if url.endswith("/ota/begin"):
            return {"accepted": True}
        if "/ota/file/begin?" in url:
            begin_calls += 1
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            chunk_calls += 1
            if chunk_calls == 1:
                return {"accepted": False, "error": "staged_file_missing"}
            return {"accepted": True, "offset": len(b"print('ota')\n")}
        if "/ota/file/end?" in url:
            return {"accepted": True}
        if url.endswith("/ota/commit"):
            return {"accepted": True}
        raise AssertionError(url)

    service._request_json = fake_request_json

    import asyncio
    asyncio.run(
        service._push_package(
            types.SimpleNamespace(),
            "http://device:8000",
            package,
            {"bytes_sent": 0, "progress": 0},
            chunk_size=1024,
        )
    )

    assert begin_calls == 2
    assert chunk_calls == 2


def test_push_package_restarts_chunked_file_after_chunk_transport_failure(tmp_path):
    pkg = _write_package(tmp_path)
    service = NodusOTAService(package_root=tmp_path / "ota")
    package = service.inspect_package(str(pkg))
    chunk_calls = 0
    begin_calls = 0

    async def fake_request_json(client, method, url, **kwargs):
        nonlocal chunk_calls, begin_calls
        if url.endswith("/ota/begin"):
            return {"accepted": True}
        if "/ota/file/begin?" in url:
            begin_calls += 1
            return {"accepted": True}
        if "/ota/file/chunk?" in url:
            chunk_calls += 1
            if chunk_calls == 1:
                raise NodusOTAError(f"http_request_failed:PUT:{url}:connection reset")
            return {"accepted": True, "offset": len(b"print('ota')\n")}
        if "/ota/file/end?" in url:
            return {"accepted": True}
        if url.endswith("/ota/commit"):
            return {"accepted": True}
        raise AssertionError(url)

    service._request_json = fake_request_json

    import asyncio
    asyncio.run(
        service._push_package(
            types.SimpleNamespace(),
            "http://device:8000",
            package,
            {"bytes_sent": 0, "progress": 0},
            chunk_size=1024,
        )
    )

    assert begin_calls == 2
    assert chunk_calls == 2


def test_wait_after_prepare_returns_when_ota_status_is_ready(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    state = {"bytes_sent": 0, "progress": 0}

    class FakeResponse:
        def json(self):
            return {"phase": "ready", "package_id": "ota-test"}

    class FakeClient:
        calls = 0

        async def get(self, url):
            self.calls += 1
            return FakeResponse()

    import asyncio
    client = FakeClient()
    ready = asyncio.run(
        service._wait_after_prepare(
            client,
            "http://device:8000",
            "ota-test",
            state,
        )
    )

    assert ready is True
    assert client.calls == 1
    assert state["phase"] == "status_ready"
    assert state["progress"] == 5


def test_wait_ready_reports_last_http_probe_error(tmp_path, monkeypatch):
    monkeypatch.setattr(saiNodusOTA, "DEFAULT_READY_TIMEOUT_S", 0.01)
    service = NodusOTAService(package_root=tmp_path / "ota")
    state = {"device_id": "aht-yuk0nv", "bytes_sent": 0, "progress": 0}

    class FakeClient:
        async def get(self, url):
            raise httpx.ConnectError("network unreachable")

    import asyncio
    with pytest.raises(NodusOTAError, match="ota_http_ready_timeout:ConnectError:network unreachable"):
        asyncio.run(
            service._wait_ready(
                FakeClient(),
                "http://device:8000",
                "ota-test",
                state,
            )
        )

    assert state["phase"] == "waiting_ota_http"
    assert state["message"] == "OTA HTTP unavailable: ConnectError:network unreachable"


def test_wait_after_prepare_detects_device_returned_to_runtime(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    service.mqtt_ingest = SimpleNamespace(
        device_status={"aht-yuk0nv": "online"},
        last_mqtt_seen={"aht-yuk0nv": 200.0},
    )
    state = {
        "device_id": "aht-yuk0nv",
        "prepare_started_at": 100.0,
        "bytes_sent": 0,
        "progress": 0,
    }

    class FakeClient:
        async def get(self, url):
            raise httpx.ConnectError("connection refused")

    import asyncio
    with pytest.raises(NodusOTAError, match="ota_http_unavailable_device_runtime_online:ConnectError:connection refused"):
        asyncio.run(
            service._wait_after_prepare(
                FakeClient(),
                "http://device:8000",
                "ota-test",
                state,
            )
        )


def test_prepare_settle_wait_allows_slow_wifi_ota_startup():
    assert DEFAULT_WAIT_AFTER_PREPARE_S == 60.0


def test_job_history_marks_running_jobs_interrupted_on_restart(tmp_path):
    service = NodusOTAService(package_root=tmp_path / "ota")
    service.jobs["job1"] = {
        "job_id": "job1",
        "status": "running",
        "created_at": 1.0,
        "updated_at": 2.0,
        "package": {"package_id": "ota-test"},
        "concurrency": 1,
        "devices": {
            "co2-v5p04u": {
                "device_id": "co2-v5p04u",
                "status": "running",
                "phase": "uploading_file",
                "message": "",
            }
        },
    }
    service._persist_job_history()

    restored = NodusOTAService(package_root=tmp_path / "ota")
    job = restored.job_snapshot("job1")

    assert job["status"] == "interrupted"
    assert job["devices"][0]["status"] == "interrupted"
