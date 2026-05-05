"""Focused tests for Sensorius-side Nodus OTA package handling."""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from saiNodusOTA import NodusOTAError, NodusOTAService


def _write_package(root: Path, *, version: str = "v0.26.124.10") -> Path:
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
        "to_tag": "v0.26.124.11",
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


def test_import_zip_package_accepts_single_root_directory(tmp_path):
    pkg = _write_package(tmp_path)
    archive = tmp_path / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in pkg.rglob("*"):
            if path.is_file():
                zf.write(path, Path("wrapped") / path.relative_to(pkg))
    service = NodusOTAService(package_root=tmp_path / "ota")

    inspected = service.import_zip_package("pkg.zip", archive.read_bytes())

    assert inspected.summary()["package_id"] == "ota-test"


def test_list_devices_uses_mqtt_ingest_metadata(tmp_path):
    ingest = SimpleNamespace(
        mqtt_clients={"co2-v5p04u"},
        device_status={"co2-v5p04u": "online"},
        host_to_peer_ids={"co2-v5p04u": ["co2-v5p04u", "switch-v5p04u"]},
        nodus_firmware_versions={"co2-v5p04u": "v0.26.124.10"},
        last_mqtt_seen={"co2-v5p04u": 100.0},
        _host_ipv4addr={"co2-v5p04u": "10.0.0.42"},
        get_nodus_firmware_version=lambda device_id: "v0.26.124.10",
    )
    service = NodusOTAService(mqtt_ingest=ingest, package_root=tmp_path / "ota")

    devices = service.list_devices()

    assert devices[0]["device_id"] == "co2-v5p04u"
    assert devices[0]["firmware_version"] == "v0.26.124.10"
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
    )
    service = NodusOTAService(mqtt_ingest=ingest, package_root=tmp_path / "ota")

    devices = service.list_devices()

    assert [d["device_id"] for d in devices] == ["apvpd-test123"]


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
