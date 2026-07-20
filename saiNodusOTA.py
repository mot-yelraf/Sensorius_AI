"""Sensorius-side orchestration for Nodus OTA firmware updates.

This module validates Nodus OTA packages and coordinates the existing Nodus
MQTT-prepare plus HTTP chunk-transfer protocol. Runtime jobs are intentionally
kept in memory because OTA is an operator-driven maintenance workflow.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from saiUtils import mdns_hostname, normalize_hostname_base, printDM, debug_enabled

MODULE = "saiNodusOTA"
DEBUG = debug_enabled(MODULE)

SCHEMA = "nodus-ota/v1"
FWUPDATE_SCHEMA = "nodus-fwupdate/v1"
DEFAULT_CHUNK_SIZE = 1024
DEFAULT_HTTP_PORT = 8000
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_WAIT_AFTER_PREPARE_S = 60.0
DEFAULT_READY_TIMEOUT_S = 90.0
DEFAULT_HTTP_RETRY_ATTEMPTS = 3
DEFAULT_HTTP_RETRY_DELAY_S = 0.75
DEFAULT_FILE_TRANSFER_ATTEMPTS = 3
DEFAULT_DEVICE_JOB_TIMEOUT_S = 30.0 * 60.0
OTA_BOOTING_MESSAGE = "Nodus OTA mode booting..."
PRESERVED_CONFIG_FILES = {
    "settings.toml",
    "sensor_i2c.toml",
    "sensor_soil.toml",
    "switch.toml",
}


class NodusOTAError(ValueError):
    """Raised when a package or transfer cannot proceed safely."""


@dataclass(frozen=True)
class OTAPackage:
    """Validated Nodus OTA package metadata."""

    ref: str
    root: Path
    manifest: dict[str, Any]
    file_count: int
    total_bytes: int

    def summary(self) -> dict[str, Any]:
        requires = self.manifest.get("requires") if isinstance(self.manifest.get("requires"), dict) else {}
        target = self.manifest.get("target") if isinstance(self.manifest.get("target"), dict) else {}
        return {
            "ref": self.ref,
            "package_id": str(self.manifest.get("package_id") or ""),
            "schema": str(self.manifest.get("schema") or ""),
            "from_tag": str(self.manifest.get("from_tag") or ""),
            "to_tag": str(self.manifest.get("to_tag") or ""),
            "target_platform": str((target or {}).get("platform") or ""),
            "target_circuitpython": str((target or {}).get("circuitpython") or ""),
            "required_version": str((requires or {}).get("version") or ""),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "preserve": list(self.manifest.get("preserve") or []),
            "delete": list(self.manifest.get("delete") or []),
        }


def normalize_manifest_path(path: str) -> str:
    """Return a safe relative POSIX package path."""
    text = str(path or "").replace("\\", "/").strip()
    text = posixpath.normpath(text)
    if text in {"", "."}:
        raise NodusOTAError("empty_package_path")
    if text == ".." or text.startswith("../") or text.startswith("/"):
        raise NodusOTAError(f"unsafe_package_path:{path}")
    return text


class NodusOTAService:
    """Manage Nodus OTA package validation and asynchronous update jobs."""

    def __init__(self, *, settings=None, mqtt_ingest=None, package_root: str | Path = "ota_packages"):
        self.settings = settings
        self.mqtt_ingest = mqtt_ingest
        self.package_root = Path(package_root)
        self.package_root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.package_root / "jobs.json"
        self.jobs: dict[str, dict[str, Any]] = {}
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._load_job_history()

    def list_packages(self) -> list[dict[str, Any]]:
        packages = []
        for child in sorted(self.package_root.iterdir() if self.package_root.exists() else []):
            if not child.is_dir():
                continue
            try:
                packages.append(self.inspect_package(str(child)).summary())
            except Exception:
                continue
        return packages

    def inspect_package(self, package_ref: str) -> OTAPackage:
        root = self._resolve_package_ref(package_ref)
        manifest_path = root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise NodusOTAError("manifest_missing") from exc
        except ValueError as exc:
            raise NodusOTAError("manifest_invalid_json") from exc
        if not isinstance(manifest, dict):
            raise NodusOTAError("manifest_invalid_shape")
        if manifest.get("schema") != SCHEMA:
            raise NodusOTAError("manifest_schema_invalid")
        if not str(manifest.get("package_id") or "").strip():
            raise NodusOTAError("manifest_package_id_missing")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise NodusOTAError("manifest_files_invalid")

        total = 0
        seen: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict):
                raise NodusOTAError("manifest_file_entry_invalid")
            rel_path = normalize_manifest_path(str(entry.get("path") or ""))
            if rel_path in seen:
                raise NodusOTAError(f"manifest_duplicate_file:{rel_path}")
            seen.add(rel_path)
            file_path = root / "files" / Path(rel_path)
            if not file_path.is_file():
                raise NodusOTAError(f"package_file_missing:{rel_path}")
            data = file_path.read_bytes()
            expected_size = int(entry.get("size", -1) or -1)
            expected_sha = str(entry.get("sha256") or "").strip().lower()
            if len(data) != expected_size:
                raise NodusOTAError(f"package_file_size_mismatch:{rel_path}")
            if hashlib.sha256(data).hexdigest() != expected_sha:
                raise NodusOTAError(f"package_file_sha256_mismatch:{rel_path}")
            total += len(data)

        for raw_path in manifest.get("delete") or []:
            normalize_manifest_path(str(raw_path or ""))

        return OTAPackage(
            ref=str(root),
            root=root,
            manifest=manifest,
            file_count=len(files),
            total_bytes=total,
        )

    def list_devices(self) -> list[dict[str, Any]]:
        ingest = self.mqtt_ingest
        now = time.time()
        hosts: set[str] = set()
        if ingest is None:
            return []

        peer_map = getattr(ingest, "host_to_peer_ids", {}) or {}

        def _sensor_device_for(host: str, peers: list[str]) -> str:
            for raw_peer in peers or []:
                peer = normalize_hostname_base(str(raw_peer or ""))
                if peer and not peer.startswith("switch-") and not re.fullmatch(r"S\d+-[A-Za-z0-9_-]+", peer, flags=re.IGNORECASE):
                    return peer
            return host

        def _canonical_host(raw: str) -> str:
            base = normalize_hostname_base(str(raw or ""))
            if not base or re.fullmatch(r"S\d+-[A-Za-z0-9_-]+", base, flags=re.IGNORECASE):
                return ""

            for host_raw, peers_raw in peer_map.items():
                host = normalize_hostname_base(str(host_raw or ""))
                peers = [normalize_hostname_base(str(p or "")) for p in (peers_raw or [])]
                if base == host or base in peers:
                    return _sensor_device_for(host, peers)

            resolver = getattr(ingest, "resolve_nodus_hostname", None)
            if callable(resolver):
                try:
                    resolved = resolver(base, device_type="switch" if base.startswith("switch-") else None)
                    resolved_base = normalize_hostname_base(str(resolved or ""))
                    if resolved_base:
                        return resolved_base
                except Exception:
                    pass

            if base.startswith("switch-"):
                serial = base.rsplit("-", 1)[-1] if "-" in base else base
                suffix = f"-{serial}"
                try:
                    for candidate in getattr(ingest, "mqtt_clients", set()) or set():
                        cand = normalize_hostname_base(str(candidate or ""))
                        if cand and not cand.startswith("switch-") and cand.endswith(suffix):
                            return cand
                except Exception:
                    pass

            return base

        for source in (
            getattr(ingest, "mqtt_clients", set()) or set(),
            getattr(ingest, "device_status", {}).keys(),
            getattr(ingest, "host_to_peer_ids", {}).keys(),
            getattr(ingest, "nodus_firmware_versions", {}).keys(),
        ):
            for raw in source:
                host = _canonical_host(str(raw or ""))
                if host:
                    hosts.add(host)

        devices = []
        for host in sorted(hosts):
            peers = list((getattr(ingest, "host_to_peer_ids", {}) or {}).get(host) or [])
            if host not in peers:
                peers.insert(0, host)
            firmware = ""
            if hasattr(ingest, "get_nodus_firmware_version"):
                firmware = str(ingest.get_nodus_firmware_version(host) or "")
            board_type = ""
            if hasattr(ingest, "get_nodus_board_type"):
                board_type = str(ingest.get_nodus_board_type(host) or "")
            status = str(
                (getattr(ingest, "device_status", {}) or {}).get(host)
                or (getattr(ingest, "device_status", {}) or {}).get(mdns_hostname(host))
                or "unknown"
            )
            last_seen = float(
                (getattr(ingest, "last_mqtt_seen", {}) or {}).get(host)
                or (getattr(ingest, "last_mqtt_seen", {}) or {}).get(mdns_hostname(host))
                or 0.0
            )
            ip = str((getattr(ingest, "_host_ipv4addr", {}) or {}).get(host) or "")
            devices.append(
                {
                    "device_id": host,
                    "host": host,
                    "peers": peers,
                    "status": status,
                    "firmware_version": firmware,
                    "board_type": board_type,
                    "last_seen_s": round(max(now - last_seen, 0.0), 1) if last_seen else None,
                    "ip": ip,
                    "http_url": self._device_url(host, ip=ip),
                    "eligible": bool(firmware),
                }
            )
        return devices

    def start_job(
        self,
        package_ref: str,
        device_ids: list[str],
        *,
        concurrency: int = 1,
        force_version_mismatch: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> dict[str, Any]:
        package = self.inspect_package(package_ref)
        targets = [_clean_device_id(item) for item in device_ids]
        targets = [item for item in targets if item]
        if not targets:
            raise NodusOTAError("no_devices_selected")
        package_platform = _normalize_platform(package.summary().get("target_platform"))
        if package_platform:
            mismatches = []
            for device_id in targets:
                device_platform = _normalize_platform(self._board_type(device_id))
                if device_platform and device_platform != package_platform:
                    mismatches.append(f"{device_id}:{device_platform}")
            if mismatches:
                raise NodusOTAError(f"target_platform_mismatch:package={package_platform}:devices={','.join(mismatches)}")
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "package": package.summary(),
            "concurrency": max(1, int(concurrency or 1)),
            "force_version_mismatch": bool(force_version_mismatch),
            "chunk_size": max(0, int(chunk_size or DEFAULT_CHUNK_SIZE)),
            "cancel_requested": False,
            "devices": {
                device_id: {
                    "device_id": device_id,
                    "status": "queued",
                    "phase": "queued",
                    "progress": 0,
                    "message": "",
                    "error": "",
                    "bytes_sent": 0,
                    "total_bytes": package.total_bytes,
                    "current_file": "",
                }
                for device_id in targets
            },
        }
        self.jobs[job_id] = job
        self._persist_job_history()
        self._job_tasks[job_id] = asyncio.create_task(self._run_job(job_id, package, targets), name=f"NodusOTA:{job_id}")
        return self.job_snapshot(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return sorted(
            (_public_job(job) for job in self.jobs.values()),
            key=lambda item: float(item.get("created_at") or 0.0),
            reverse=True,
        )

    def job_snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(str(job_id))
        if not job:
            raise NodusOTAError("job_not_found")
        return _public_job(job)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(str(job_id))
        if not job:
            raise NodusOTAError("job_not_found")
        job["cancel_requested"] = True
        job["updated_at"] = time.time()
        for state in (job.get("devices") or {}).values():
            if state.get("status") == "queued":
                state["status"] = "aborted"
                state["phase"] = "aborted"
                state["message"] = "cancelled before start"
            elif state.get("status") == "running":
                state["cancel_requested"] = True
                ota_url = str(state.get("ota_url") or "")
                if ota_url:
                    asyncio.create_task(self._abort_ota_session(ota_url), name=f"NodusOTAAbort:{state.get('device_id', '')}")
        self._persist_job_history()
        return self.job_snapshot(job_id)

    async def _run_job(self, job_id: str, package: OTAPackage, targets: list[str]) -> None:
        job = self.jobs[job_id]
        job["status"] = "running"
        semaphore = asyncio.Semaphore(max(1, int(job.get("concurrency") or 1)))

        async def _one(device_id: str) -> None:
            async with semaphore:
                if job.get("cancel_requested"):
                    return
                await self._run_device(job, package, device_id)

        await asyncio.gather(*[_one(device_id) for device_id in targets])
        states = list((job.get("devices") or {}).values())
        if any(state.get("status") == "failed" for state in states):
            job["status"] = "failed"
        elif any(state.get("status") == "aborted" for state in states):
            job["status"] = "aborted"
        else:
            job["status"] = "complete"
        job["updated_at"] = time.time()
        self._persist_job_history()

    async def _run_device(self, job: dict[str, Any], package: OTAPackage, device_id: str) -> None:
        state = job["devices"][device_id]
        state["device_started_at"] = time.time()
        state["device_deadline_monotonic"] = time.monotonic() + DEFAULT_DEVICE_JOB_TIMEOUT_S
        try:
            self._set_device_state(state, "validating", "validating package and device")
            required = str(package.summary().get("required_version") or "")
            target_version = str(package.summary().get("to_tag") or "")
            current = self._firmware_version(device_id)
            if required and current and not job.get("force_version_mismatch"):
                allowed_versions = {required}
                if target_version:
                    allowed_versions.add(target_version)
                if current not in allowed_versions:
                    raise NodusOTAError(f"version_mismatch:current={current}:requires_current={required}:target={target_version}")
            package_platform = _normalize_platform(package.summary().get("target_platform"))
            device_platform = _normalize_platform(self._board_type(device_id))
            if package_platform and device_platform and package_platform != device_platform:
                raise NodusOTAError(f"target_platform_mismatch:device={device_platform}:package={package_platform}")

            url = self._device_url(device_id)
            state["ota_url"] = url
            state["prepare_started_at"] = time.time()
            package_id = str(package.manifest.get("package_id") or "")
            self._set_device_state(state, "preparing_mqtt", f"publishing prepare for {package_id}")
            state["prepare_message_id"] = await asyncio.to_thread(
                self._publish_prepare,
                device_id,
                package_id,
            )

            async with httpx.AsyncClient(timeout=10.0) as client:
                try:
                    ready = await self._wait_after_prepare(client, url, package_id, state)
                    if not ready:
                        self._set_device_state(state, "waiting_ota_http", OTA_BOOTING_MESSAGE)
                        await self._wait_ready(client, url, package_id, state)
                    state["ota_http_ready"] = True
                except Exception as exc:
                    await self._abort_ota_if_ready(
                        state,
                        reason="ready_timeout" if _is_ota_ready_timeout(str(exc)) else "ready_failed",
                        force=_is_ota_ready_timeout(str(exc)),
                    )
                    raise

            timeout = float(DEFAULT_TIMEOUT_S)
            try:
                remaining = max(
                    0.1,
                    float(state.get("device_deadline_monotonic") or 0.0) - time.monotonic(),
                )
                async with asyncio.timeout(remaining):
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        await self._push_package(
                            client,
                            url,
                            package,
                            state,
                            chunk_size=int(job.get("chunk_size") or DEFAULT_CHUNK_SIZE),
                        )
                state["commit_accepted_at"] = time.time()
            except TimeoutError as exc:
                await self._abort_ota_if_ready(state, reason="device_deadline_exceeded")
                raise NodusOTAError("ota_device_deadline_exceeded") from exc
            except Exception:
                await self._abort_ota_if_ready(state, reason="push_failed")
                raise

            self._set_device_state(state, "waiting_meta", "waiting for device to reboot")
            try:
                await self._wait_for_reboot_metadata(
                    device_id,
                    current,
                    target_version,
                    package_id,
                    state,
                )
            except NodusOTAError as exc:
                if _fwupdate_result_failure_needs_abort(str(exc)):
                    await self._abort_ota_if_ready(state, reason="fwupdate_result_failed")
                raise
            self._set_device_state(state, "complete", "update complete", progress=100, status="complete")
        except asyncio.CancelledError:
            self._set_device_state(state, "aborted", "job cancelled", status="aborted")
            raise
        except Exception as exc:
            if str(exc) == "update_cancelled":
                self._set_device_state(state, "aborted", "update cancelled", status="aborted")
                return
            self._set_device_state(
                state,
                "failed",
                _friendly_ota_error(str(exc)),
                status="failed",
                error=str(exc),
            )

    async def _wait_ready(self, client: httpx.AsyncClient, url: str, package_id: str, state: dict[str, Any]) -> None:
        deadline = time.monotonic() + DEFAULT_READY_TIMEOUT_S
        last_error = ""
        while time.monotonic() < deadline:
            self._raise_if_cancelled(state)
            self._raise_for_prepare_failure(package_id, state)
            try:
                resp = await client.get(f"{url}/ota/status")
                data = resp.json()
                phase = str(data.get("phase") or "")
                if phase == "ready":
                    state["ota_http_ready"] = True
                    if data.get("package_id") and data.get("package_id") != package_id:
                        raise NodusOTAError("device_package_mismatch")
                    self._set_device_state(state, "status_ready", "OTA mode ready", progress=5)
                    return
                state["diagnostic_message"] = f"OTA phase {phase or 'unknown'}"
                self._set_device_state(state, "waiting_ota_http", OTA_BOOTING_MESSAGE)
            except NodusOTAError:
                raise
            except Exception as exc:
                last_error = _http_probe_error(exc)
                state["diagnostic_message"] = f"OTA HTTP unavailable: {last_error}"
                self._set_device_state(state, "waiting_ota_http", OTA_BOOTING_MESSAGE, progress=2)
            await asyncio.sleep(2.0)
        detail = f":{last_error}" if last_error else ""
        raise NodusOTAError(f"ota_http_ready_timeout{detail}")

    async def _wait_after_prepare(self, client: httpx.AsyncClient, url: str, package_id: str, state: dict[str, Any]) -> bool:
        deadline = time.monotonic() + DEFAULT_WAIT_AFTER_PREPARE_S
        self._set_device_state(
            state,
            "waiting_after_prepare",
            OTA_BOOTING_MESSAGE,
            progress=2,
        )
        last_error = ""
        while time.monotonic() < deadline:
            self._raise_if_cancelled(state)
            self._raise_for_prepare_failure(package_id, state)
            try:
                resp = await client.get(f"{url}/ota/status")
                data = resp.json()
                phase = str(data.get("phase") or "")
                if phase == "ready":
                    state["ota_http_ready"] = True
                    if data.get("package_id") and data.get("package_id") != package_id:
                        raise NodusOTAError("device_package_mismatch")
                    self._set_device_state(state, "status_ready", "OTA mode ready", progress=5)
                    return True
                state["diagnostic_message"] = f"OTA phase {phase or 'unknown'}"
                self._set_device_state(state, "waiting_after_prepare", OTA_BOOTING_MESSAGE, progress=2)
            except NodusOTAError:
                raise
            except Exception as exc:
                last_error = _http_probe_error(exc)
                state["diagnostic_message"] = f"OTA HTTP unavailable: {last_error}"
                self._set_device_state(state, "waiting_after_prepare", OTA_BOOTING_MESSAGE, progress=2)
            await asyncio.sleep(min(2.0, max(deadline - time.monotonic(), 0.0)))
        if last_error:
            state["diagnostic_message"] = f"OTA HTTP unavailable: {last_error}"
        return False

    async def _push_package(self, client: httpx.AsyncClient, url: str, package: OTAPackage, state: dict[str, Any], *, chunk_size: int) -> None:
        self._raise_if_cancelled(state)
        self._set_device_state(state, "manifest_sent", "sending manifest", progress=8)
        begin = await self._request_json(client, "POST", f"{url}/ota/begin", json_body=package.manifest, retry_transient=True)
        if begin.get("accepted") is not True:
            raise NodusOTAError(f"begin_rejected:{begin.get('error', '')}")

        sent = 0
        total = max(package.total_bytes, 1)
        for entry in package.manifest.get("files") or []:
            self._raise_if_cancelled(state)
            rel_path = normalize_manifest_path(str(entry.get("path") or ""))
            file_path = package.root / "files" / Path(rel_path)
            state["current_file"] = rel_path
            if chunk_size > 0:
                for attempt in range(1, DEFAULT_FILE_TRANSFER_ATTEMPTS + 1):
                    state["file_attempt"] = attempt
                    state["message"] = f"Uploading {rel_path} (attempt {attempt} of {DEFAULT_FILE_TRANSFER_ATTEMPTS})..."
                    try:
                        await self._push_file_chunks(client, url, rel_path, file_path, state, sent, total, chunk_size)
                        break
                    except NodusOTAError as exc:
                        if attempt >= DEFAULT_FILE_TRANSFER_ATTEMPTS or not _file_transfer_error_retryable(str(exc)):
                            raise
                        state["diagnostic_message"] = str(exc)
                        state["message"] = (
                            f"Retrying {rel_path} (attempt {attempt + 1} of "
                            f"{DEFAULT_FILE_TRANSFER_ATTEMPTS})..."
                        )
            else:
                payload = file_path.read_bytes()
                result = await self._request_json(
                    client,
                    "PUT",
                    f"{url}/ota/file?path={quote(rel_path, safe='/')}",
                    content=payload,
                    headers={"X-Nodus-File-Path": rel_path, "Content-Type": "application/octet-stream"},
                )
                if result.get("accepted") is not True:
                    raise NodusOTAError(f"file_rejected:{rel_path}:{result.get('error', '')}")
            sent += int(entry.get("size", 0) or 0)
            state["bytes_sent"] = sent
            state["progress"] = min(85, 10 + int((sent / total) * 70))

        self._set_device_state(state, "committing", "committing staged files", progress=88)
        commit = await self._request_json(client, "POST", f"{url}/ota/commit", json_body={}, retry_transient=True)
        if commit.get("accepted") is not True:
            raise NodusOTAError(f"commit_rejected:{commit.get('error', '')}")
        self._set_device_state(state, "rebooting", "commit accepted, rebooting", progress=92)

    async def _push_file_chunks(
        self,
        client: httpx.AsyncClient,
        url: str,
        rel_path: str,
        file_path: Path,
        state: dict[str, Any],
        base_sent: int,
        total_bytes: int,
        chunk_size: int,
    ) -> None:
        encoded = quote(rel_path, safe="/")
        begin = await self._request_json(
            client,
            "POST",
            f"{url}/ota/file/begin?path={encoded}",
            json_body={},
            headers={"X-Nodus-File-Path": rel_path},
            retry_transient=True,
        )
        if begin.get("accepted") is not True:
            raise NodusOTAError(f"file_begin_rejected:{rel_path}:{begin.get('error', '')}")
        offset = 0
        stalled_offsets: dict[int, int] = {}
        with file_path.open("rb") as handle:
            while offset < file_path.stat().st_size:
                self._raise_if_cancelled(state)
                await asyncio.to_thread(handle.seek, offset)
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                result = await self._request_json(
                    client,
                    "PUT",
                    f"{url}/ota/file/chunk?path={encoded}&offset={offset}",
                    content=chunk,
                    headers={"X-Nodus-File-Path": rel_path, "Content-Type": "application/octet-stream"},
                    retry_transient=True,
                )
                if result.get("accepted") is not True:
                    if result.get("error") == "chunk_offset_mismatch" and result.get("offset") is not None:
                        device_offset = int(result.get("offset"))
                        if 0 <= device_offset <= file_path.stat().st_size:
                            stalled_offsets[device_offset] = stalled_offsets.get(device_offset, 0) + 1
                            if stalled_offsets[device_offset] > 3:
                                raise NodusOTAError(
                                    f"file_chunk_offset_stalled:{rel_path}:device={device_offset}:expected={offset}"
                                )
                            offset = device_offset
                            continue
                    raise NodusOTAError(f"file_chunk_rejected:{rel_path}:{result.get('error', '')}")
                expected_offset = offset + len(chunk)
                reported_offset = result.get("offset")
                next_offset = expected_offset if reported_offset is None else int(reported_offset)
                if next_offset > expected_offset or next_offset < offset:
                    raise NodusOTAError(f"file_chunk_offset_mismatch:{rel_path}:device={next_offset}:expected={expected_offset}")
                if next_offset < expected_offset:
                    stalled_offsets[next_offset] = stalled_offsets.get(next_offset, 0) + 1
                    if stalled_offsets[next_offset] > 3:
                        raise NodusOTAError(f"file_chunk_offset_stalled:{rel_path}:device={next_offset}:expected={expected_offset}")
                else:
                    stalled_offsets.clear()
                offset = next_offset
                state["bytes_sent"] = base_sent + offset
                state["progress"] = min(85, 10 + int((state["bytes_sent"] / max(total_bytes, 1)) * 70))
        self._set_device_state(state, "verifying_file", f"Verifying {rel_path}...")
        end = await self._request_json(
            client,
            "POST",
            f"{url}/ota/file/end?path={encoded}",
            json_body={},
            headers={"X-Nodus-File-Path": rel_path},
            retry_transient=True,
        )
        if end.get("accepted") is not True:
            raise NodusOTAError(f"file_end_rejected:{rel_path}:{end.get('error', '')}")
        self._set_device_state(state, "file_verified", f"Verified {rel_path}.")

    async def _abort_ota_session(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(f"{url}/ota/abort", json={})
                data = response.json()
                return bool(response.status_code < 400 and isinstance(data, dict) and data.get("accepted") is True)
        except Exception:
            return False

    async def _abort_ota_if_ready(self, state: dict[str, Any], *, reason: str, force: bool = False) -> None:
        if not force and not state.get("ota_http_ready"):
            return
        if state.get("ota_abort_attempted"):
            return
        url = str(state.get("ota_url") or "")
        if not url:
            return
        state["ota_abort_attempted"] = True
        state["ota_abort_reason"] = str(reason or "")
        state["updated_at"] = time.time()
        try:
            abort_result = await self._abort_ota_session(url)
            succeeded = abort_result is not False
            if not succeeded:
                device_id = str(state.get("device_id") or "")
                fallback_url = f"http://{mdns_hostname(device_id)}:{DEFAULT_HTTP_PORT}" if device_id else ""
                if fallback_url and fallback_url != url:
                    succeeded = await self._abort_ota_session(fallback_url)
            state["ota_abort_succeeded"] = bool(succeeded)
        except Exception:
            state["ota_abort_succeeded"] = False

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json_body=None,
        content=None,
        headers=None,
        retry_transient: bool = False,
    ) -> dict[str, Any]:
        attempts = DEFAULT_HTTP_RETRY_ATTEMPTS if retry_transient else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = await client.request(method, url, json=json_body, content=content, headers=headers)
                break
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
            ) as exc:
                if attempt >= attempts:
                    raise NodusOTAError(f"http_request_failed:{method}:{url}:{exc}") from exc
                await asyncio.sleep(DEFAULT_HTTP_RETRY_DELAY_S * attempt)
        try:
            data = resp.json()
        except Exception as exc:
            raise NodusOTAError(f"http_invalid_json:{url}") from exc
        if not isinstance(data, dict):
            raise NodusOTAError(f"http_invalid_shape:{url}")
        if resp.status_code >= 400 and data.get("accepted") is not True:
            return data
        resp.raise_for_status()
        return data

    async def _wait_for_reboot_metadata(
        self,
        device_id: str,
        previous_version: str,
        target_version: str,
        package_id: str,
        state: dict[str, Any],
    ) -> None:
        deadline = time.monotonic() + 180.0
        commit_accepted_at = float(state.get("commit_accepted_at") or 0.0)
        while time.monotonic() < deadline:
            self._raise_if_cancelled(state)
            result = self._fwupdate_result(device_id)
            phase = str((result or {}).get("phase") or "").strip().lower()
            result_package_id = str((result or {}).get("package_id") or "")
            result_received_at = float((result or {}).get("received_at") or 0.0)
            result_is_current = (
                result_received_at > commit_accepted_at
                and (not package_id or result_package_id == package_id)
            )
            if result_is_current and phase in {"applied", "complete", "completed"}:
                state["message"] = f"OTA result reported {phase}"
                return
            if result_is_current and phase in {"failed", "rollback", "rolled_back", "aborted"}:
                raise NodusOTAError(f"fwupdate_result:{phase}:{(result or {}).get('error', '')}")
            current = self._firmware_version(device_id)
            status = self._device_status(device_id)
            last_seen = self._device_last_seen(device_id)
            if (
                target_version
                and target_version != previous_version
                and current == target_version
                and status == "online"
                and last_seen > commit_accepted_at
            ):
                state["message"] = f"firmware reported {current}"
                return
            await asyncio.sleep(3.0)
        raise NodusOTAError("ota_confirmation_timeout")

    def _publish_prepare(self, device_id: str, package_id: str) -> str:
        ingest = self.mqtt_ingest
        if ingest is None or not getattr(ingest, "client", None):
            raise NodusOTAError("mqtt_ingest_unavailable")
        topic = f"nodus/{device_id}/fwupdate"
        message_id = uuid.uuid4().hex
        payload = {
            "schema": FWUPDATE_SCHEMA,
            "message_id": message_id,
            "command": "prepare",
            "package_id": package_id,
        }
        info = ingest.client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)
        wait = getattr(info, "wait_for_publish", None)
        if callable(wait):
            wait(timeout=10)
        return message_id

    def _firmware_version(self, device_id: str) -> str:
        ingest = self.mqtt_ingest
        if ingest is not None and hasattr(ingest, "get_nodus_firmware_version"):
            return str(ingest.get_nodus_firmware_version(device_id) or "")
        return ""

    def _board_type(self, device_id: str) -> str:
        ingest = self.mqtt_ingest
        if ingest is not None and hasattr(ingest, "get_nodus_board_type"):
            return str(ingest.get_nodus_board_type(device_id) or "")
        return ""

    def _device_status(self, device_id: str) -> str:
        ingest = self.mqtt_ingest
        if ingest is None:
            return "unknown"
        base = normalize_hostname_base(device_id)
        return str((getattr(ingest, "device_status", {}) or {}).get(base) or "unknown")

    def _device_seen_after_prepare(self, device_id: str, state: dict[str, Any]) -> bool:
        ingest = self.mqtt_ingest
        if ingest is None:
            return False
        prepare_started = float(state.get("prepare_started_at") or 0.0)
        if prepare_started <= 0:
            return False
        base = normalize_hostname_base(device_id)
        last_seen = float(
            (getattr(ingest, "last_mqtt_seen", {}) or {}).get(base)
            or (getattr(ingest, "last_mqtt_seen", {}) or {}).get(mdns_hostname(base))
            or 0.0
        )
        if last_seen <= prepare_started:
            return False
        return self._device_status(base).lower() in {"online", "ready", "unknown"}

    def _device_last_seen(self, device_id: str) -> float:
        ingest = self.mqtt_ingest
        if ingest is None:
            return 0.0
        base = normalize_hostname_base(device_id)
        return float(
            (getattr(ingest, "last_mqtt_seen", {}) or {}).get(base)
            or (getattr(ingest, "last_mqtt_seen", {}) or {}).get(mdns_hostname(base))
            or 0.0
        )

    def _raise_for_prepare_failure(self, package_id: str, state: dict[str, Any]) -> None:
        result = self._fwupdate_result(str(state.get("device_id") or ""))
        if not result or result.get("prepared") is not False:
            return
        received_at = float(result.get("received_at") or 0.0)
        if received_at <= float(state.get("prepare_started_at") or 0.0):
            return
        result_message_id = str(result.get("message_id") or "")
        prepare_message_id = str(state.get("prepare_message_id") or "")
        result_package_id = str(result.get("package_id") or "")
        if prepare_message_id and result_message_id:
            matches = prepare_message_id == result_message_id
        else:
            matches = bool(package_id and result_package_id == package_id)
        if matches:
            raise NodusOTAError(f"prepare_rejected:{result.get('error', '')}")

    def _fwupdate_result(self, device_id: str) -> dict[str, Any]:
        ingest = self.mqtt_ingest
        if ingest is None:
            return {}
        base = normalize_hostname_base(device_id)
        result = (getattr(ingest, "fwupdate_result_by_device", {}) or {}).get(base) or {}
        return dict(result) if isinstance(result, dict) else {}

    def _device_url(self, device_id: str, *, ip: str = "") -> str:
        base = normalize_hostname_base(device_id)
        ingest = self.mqtt_ingest
        host = ip
        if not host and ingest is not None:
            host = str((getattr(ingest, "_host_ipv4addr", {}) or {}).get(base) or "")
        if not host:
            host = mdns_hostname(base)
        return f"http://{host}:{DEFAULT_HTTP_PORT}"

    def _resolve_package_ref(self, package_ref: str) -> Path:
        text = str(package_ref or "").strip()
        if not text:
            raise NodusOTAError("package_ref_missing")
        path = Path(text)
        if not path.is_absolute():
            path = self.package_root / text
        path = path.resolve()
        if not path.is_dir():
            raise NodusOTAError("package_not_found")
        return path

    def _set_device_state(self, state: dict[str, Any], phase: str, message: str, *, progress: int | None = None, status: str | None = None, error: str = "") -> None:
        state["phase"] = phase
        state["message"] = message
        state["updated_at"] = time.time()
        if progress is not None:
            state["progress"] = int(progress)
        if status:
            state["status"] = status
        elif state.get("status") in {"queued", ""}:
            state["status"] = "running"
        if error:
            state["error"] = error

    def _load_job_history(self) -> None:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        for item in data[:25]:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("job_id") or "")
            if not job_id:
                continue
            job = dict(item)
            devices = job.get("devices") or {}
            if isinstance(devices, list):
                job["devices"] = {
                    str(device.get("device_id") or idx): dict(device)
                    for idx, device in enumerate(devices)
                    if isinstance(device, dict)
                }
            if job.get("status") in {"queued", "running"}:
                job["status"] = "interrupted"
                for state in (job.get("devices") or {}).values():
                    if isinstance(state, dict) and state.get("status") in {"queued", "running"}:
                        state["status"] = "interrupted"
                        state["phase"] = "interrupted"
                        state["message"] = "Sensorius restarted while job was active"
            self.jobs[job_id] = job

    def _persist_job_history(self) -> None:
        try:
            snapshots = self.list_jobs()[:25]
            tmp = self.history_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snapshots, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, self.history_path)
        except Exception as exc:
            if DEBUG:
                printDM(f"[ota-history] persist failed: {exc}", location=MODULE)

    def _raise_if_cancelled(self, state: dict[str, Any]) -> None:
        if state.get("cancel_requested"):
            raise NodusOTAError("update_cancelled")
        deadline = float(state.get("device_deadline_monotonic") or 0.0)
        if deadline > 0.0 and time.monotonic() >= deadline:
            raise NodusOTAError("ota_device_deadline_exceeded")


def _clean_device_id(value: str) -> str:
    return normalize_hostname_base(str(value or "").strip())


def _normalize_platform(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _file_transfer_error_retryable(error: str) -> bool:
    text = str(error or "")
    if text.startswith("http_request_failed:PUT:") and "/ota/file/chunk?" in text:
        return True
    if text.startswith("file_chunk_offset_mismatch:") or text.startswith("file_chunk_offset_stalled:"):
        return True
    if not (
        text.startswith("file_begin_rejected:")
        or text.startswith("file_chunk_rejected:")
        or text.startswith("file_end_rejected:")
    ):
        return False
    reason = text.rsplit(":", 1)[-1]
    return reason in {
        "file_size_mismatch",
        "sha256_mismatch",
        "staged_file_missing",
    }


def _fwupdate_result_failure_needs_abort(error: str) -> bool:
    text = str(error or "")
    return (
        text.startswith("fwupdate_result:failed:")
        or text.startswith("fwupdate_result:rollback:")
        or text.startswith("fwupdate_result:rolled_back:")
    )


def _http_probe_error(exc: Exception) -> str:
    text = str(exc).strip()
    name = exc.__class__.__name__
    return f"{name}:{text}" if text else name


def _is_ota_ready_timeout(error: str) -> bool:
    return str(error or "").startswith("ota_http_ready_timeout")


def _friendly_ota_error(error: str) -> str:
    text = str(error or "")
    if _is_ota_ready_timeout(text):
        return "Nodus OTA mode did not start within 150 seconds. Recovery was requested."
    if text == "ota_confirmation_timeout":
        return "The Nodus rebooted, but the completed update could not be confirmed."
    if text == "ota_device_deadline_exceeded":
        return "The update exceeded the 30-minute safety limit and was stopped."
    if text.startswith("prepare_rejected:"):
        return "The Nodus could not enter OTA mode."
    if text.startswith("file_") or text.startswith("http_request_failed:"):
        return "The file transfer failed and the Nodus was asked to return to normal operation."
    return text


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "package": dict(job.get("package") or {}),
        "concurrency": job.get("concurrency"),
        "devices": list((job.get("devices") or {}).values()),
    }
