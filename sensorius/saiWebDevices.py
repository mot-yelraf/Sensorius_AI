"""Register device inventory, OTA, Wi-Fi, location, halt, and removal routes.

Shared device resolution and paced configuration callbacks are returned for sensor,
calibration, and switch settings. Runtime services and access/cache dependencies
are explicit; no import of the main route module is required.
"""
from __future__ import annotations

import asyncio
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable, Dict, Set
from uuid import uuid4
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from .saiRuntimeServices import RuntimeServices
from .saiUtils import printDM, normalize_hostname_base, mdns_hostname, normalize_sensor_id

MODULE = "saiWebRoutes"

import os
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass
from .saiWebRouteState import RouteState
from .saiNodusOTA import NodusOTAError, NodusOTAService
from .saiSensorSettingsManager import is_direct_local_sensor_id
from .saiEcowitt import ecowitt_sensor_health
from .saiAddDevice import get_hub_settings_path


@dataclass(frozen=True)
class DeviceRouteDependencies:
    """Supply shared access, settings, inventory, and cache operations."""

    get_dashboard_sensor_map: Callable
    get_host_lock: Callable
    invalidate_dashboard_caches: Callable
    is_switch_id: Callable
    is_valid_device_id: Callable
    require_protected_access: Callable
    safe_child_path: Callable
    sensor_shadow_is_remote_nodus: Callable
    settings_base_path: Callable
    sqlite_connect_with_recovery: Callable


@dataclass(frozen=True)
class DeviceRouteHelpers:
    """Share device identity and configuration operations with other route groups."""

    build_device_network_info: Callable
    build_system_hostname_index: Callable
    display_nodus_board_type: Callable
    display_nodus_sensor_hardware: Callable
    nodus_values_match: Callable
    read_hostname_from_system_settings: Callable
    request_nodus_device_restart: Callable
    resolve_system_settings_root: Callable
    push_nodus_settings_batch: Callable


def register_device_routes(
    router: APIRouter, *, app, services: RuntimeServices, route_state: RouteState,
    settings, dependencies: DeviceRouteDependencies, settings_factory,
    sensor_settings_factory, switch_settings_factory, get_system_settings_manager,
    get_settings_default_root: Callable,
    sensor_base_dir, switch_base_dir, system_base_dir,
    ack_timeout: float, result_timeout: float, debug: bool = False,
) -> DeviceRouteHelpers:
    """Register device management and return helpers shared by settings routes."""
    saiSettings = settings_factory
    SensorSettingsManager = sensor_settings_factory
    SwitchSettingsManager = switch_settings_factory
    _SENSOR_BASE_DIR = sensor_base_dir
    _SWITCH_BASE_DIR = switch_base_dir
    _SYS_BASE_DIR = system_base_dir
    _NODUS_CONFIG_ACK_TIMEOUT_SEC = ack_timeout
    _NODUS_CONFIG_RESULT_TIMEOUT_SEC = result_timeout
    LOCATIONS_ROUTE_TAG = "device-locations"
    DEBUG = debug
    ota_service = getattr(app.state, "nodus_ota_service", None)
    if ota_service is None:
        ota_service = NodusOTAService(settings=settings, mqtt_ingest=services.mqtt_ingest)
        app.state.nodus_ota_service = ota_service
    _get_dashboard_sensor_map = dependencies.get_dashboard_sensor_map
    _get_host_lock = dependencies.get_host_lock
    _invalidate_dashboard_caches = dependencies.invalidate_dashboard_caches
    _is_switch_id = dependencies.is_switch_id
    _is_valid_device_id = dependencies.is_valid_device_id
    _require_protected_access = dependencies.require_protected_access
    _safe_child_path = dependencies.safe_child_path
    _sensor_shadow_is_remote_nodus = dependencies.sensor_shadow_is_remote_nodus
    _settings_base_path = dependencies.settings_base_path
    _sqlite_connect_with_recovery = dependencies.sqlite_connect_with_recovery

    def _ota_error_response(exc: Exception, *, status_code: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status_code)


    @router.get("/api/nodus-ota/devices", response_class=JSONResponse)
    async def api_nodus_ota_devices(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "devices": ota_service.list_devices()})


    @router.get("/api/nodus-ota/packages", response_class=JSONResponse)
    async def api_nodus_ota_packages(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "packages": ota_service.list_packages()})


    @router.post("/api/nodus-ota/package/inspect", response_class=JSONResponse)
    async def api_nodus_ota_package_inspect(request: Request):
        _require_protected_access(request, require_csrf=True)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        package_ref = str((payload or {}).get("package_ref") or "").strip()
        try:
            package = await asyncio.to_thread(ota_service.inspect_package, package_ref)
            return JSONResponse({"ok": True, "package": package.summary()})
        except NodusOTAError as exc:
            return _ota_error_response(exc)


    def _ota_folder_roots() -> list[str]:
        roots: list[str] = []
        seen: set[str] = set()

        def _append_root(candidate: object) -> None:
            try:
                path = Path(candidate).expanduser().resolve()
            except Exception:
                return
            try:
                if not path.exists() or not path.is_dir():
                    return
            except Exception:
                return
            text = str(path)
            if text and text not in seen:
                roots.append(text)
                seen.add(text)

        _append_root(getattr(ota_service, "package_root", Path.cwd() / "ota_packages"))
        try:
            home = str(Path.home())
            if home:
                _append_root(home)
        except Exception:
            pass
        return roots


    def _ota_folder_listing(raw_path: str | None = None) -> dict[str, Any]:
        roots = _ota_folder_roots()
        fallback = roots[0] if roots else str(Path.cwd())
        path_text = str(raw_path or "").strip() or fallback
        try:
            path = Path(path_text).expanduser().resolve()
        except Exception:
            path = Path(fallback).resolve()
        if not path.exists() or not path.is_dir():
            path = Path(fallback).resolve()

        dirs = []
        error = ""

        def _is_hidden_dir(candidate: Path) -> bool:
            name = candidate.name
            if name.startswith("."):
                return True
            if os.name == "nt":
                try:
                    attrs = getattr(candidate.stat(), "st_file_attributes", 0)
                    return bool(attrs & 0x2)
                except Exception:
                    return False
            return False

        try:
            for child in path.iterdir():
                try:
                    if child.is_dir() and not _is_hidden_dir(child):
                        dirs.append({"name": child.name, "path": str(child.resolve())})
                except OSError:
                    continue
        except Exception as exc:
            printDM(f"[nodus-ota] folder listing failed: {exc}", location=MODULE)
            error = "Unable to list this folder. Check the Sensorius log."
        dirs.sort(key=lambda item: item["name"].lower())
        valid = (path / "manifest.json").is_file() and (path / "files").is_dir()
        parent = ""
        try:
            if path.parent != path:
                parent = str(path.parent)
        except Exception:
            parent = ""
        return {
            "path": str(path),
            "parent": parent,
            "directories": dirs,
            "valid_package": valid,
            "error": error,
        }


    @router.get("/api/nodus-ota/package/browse", response_class=JSONResponse)
    async def api_nodus_ota_package_browse(request: Request, path: str = ""):
        _require_protected_access(request, require_csrf=True)
        listing = await asyncio.to_thread(_ota_folder_listing, path)
        return JSONResponse({"ok": True, "folder": listing})


    @router.post("/api/nodus-ota/jobs", response_class=JSONResponse)
    async def api_nodus_ota_start_job(request: Request):
        _require_protected_access(request, require_csrf=True)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
        try:
            job = ota_service.start_job(
                str((payload or {}).get("package_ref") or ""),
                list((payload or {}).get("device_ids") or []),
                concurrency=int((payload or {}).get("concurrency") or 1),
                force_version_mismatch=bool((payload or {}).get("force_version_mismatch", False)),
                chunk_size=int((payload or {}).get("chunk_size") or 1024),
            )
            return JSONResponse({"ok": True, "job": job})
        except NodusOTAError as exc:
            return _ota_error_response(exc)


    @router.get("/api/nodus-ota/jobs", response_class=JSONResponse)
    async def api_nodus_ota_jobs(request: Request):
        _require_protected_access(request, require_csrf=True)
        return JSONResponse({"ok": True, "jobs": ota_service.list_jobs()})


    @router.get("/api/nodus-ota/jobs/{job_id}", response_class=JSONResponse)
    async def api_nodus_ota_job(request: Request, job_id: str):
        _require_protected_access(request, require_csrf=True)
        try:
            return JSONResponse({"ok": True, "job": ota_service.job_snapshot(job_id)})
        except NodusOTAError as exc:
            return _ota_error_response(exc, status_code=404 if "not_found" in str(exc) else 400)


    @router.post("/api/nodus-ota/jobs/{job_id}/cancel", response_class=JSONResponse)
    async def api_nodus_ota_cancel_job(request: Request, job_id: str):
        _require_protected_access(request, require_csrf=True)
        try:
            return JSONResponse({"ok": True, "job": ota_service.cancel_job(job_id)})
        except NodusOTAError as exc:
            return _ota_error_response(exc, status_code=404 if "not_found" in str(exc) else 400)


    def _normalize_system_settings_root(system_root: str | None) -> str:
        """Return the absolute `system_settings` directory for a candidate root."""
        import os

        text = str(system_root or "").strip()
        if not text:
            return str(_settings_base_path("system_settings"))
        try:
            root_path = _settings_base_path(text)
            root = str(root_path)
        except Exception:
            return text
        if os.path.basename(root) == "system_settings":
            return root
        nested = os.path.join(root, "system_settings")
        if os.path.isdir(nested):
            return os.path.abspath(nested)
        return root


    def _resolve_system_settings_root(system_mgr=None) -> str:
        """Resolve the on-disk `system_settings` root from manager or app settings."""
        import os

        candidates: list[str | None] = []
        class_default_root = get_settings_default_root()
        if class_default_root:
            try:
                if os.path.isabs(str(class_default_root)):
                    candidates.append(str(class_default_root))
            except Exception:
                pass
        try:
            app_settings = saiSettings(apply_live=False)
            candidates.extend([
                getattr(app_settings, "base_dir", None),
                getattr(app_settings, "system_dir", None),
                getattr(app_settings, "settings_root", None),
            ])
        except Exception:
            pass
        if system_mgr is not None:
            candidates.extend([
                getattr(system_mgr, "base_dir", None),
                getattr(system_mgr, "system_dir", None),
                getattr(system_mgr, "settings_root", None),
            ])

        for cand in candidates:
            root = _normalize_system_settings_root(cand)
            if os.path.isdir(root):
                return root
        return _normalize_system_settings_root("system_settings")


    def _build_system_hostname_index(system_root: str) -> dict[str, str]:
        """
        Scan system_settings/*/settings.toml and return {serial_suffix: HOSTNAME}.
        E.g. {'nz6g89': 'aqi-nz6g89', 'oe2ed6': 'aqi-oe2ed6', ...}
        """
        import os
        try:
            import tomllib
        except Exception:
            tomllib = None

        serial_to_host: dict[str, str] = {}
        if not tomllib:
            return serial_to_host

        system_root = _normalize_system_settings_root(system_root)
        try:
            for name in os.listdir(system_root):
                if name in {"factory", "__pycache__"}:
                    continue
                settings_path = os.path.join(system_root, name, "settings.toml")
                if not os.path.isfile(settings_path):
                    continue
                try:
                    with open(settings_path, "rb") as f:
                        data = tomllib.load(f) or {}
                    host = (data.get("Network", {}).get("HOSTNAME") or "").strip()
                    if host and "-" in host:
                        serial = host.rsplit("-", 1)[-1]
                        prev = str(serial_to_host.get(serial) or "").strip()
                        # Prefer the sensor-hosted device name over a synthetic
                        # switch-only hostname when both folders exist for the
                        # same physical Sensor+Switch Nodus serial.
                        if prev and prev.lower().startswith("switch-") and not host.lower().startswith("switch-"):
                            serial_to_host[serial] = host
                        elif not prev or not host.lower().startswith("switch-"):
                            serial_to_host[serial] = host
                except Exception:
                    continue
        except Exception:
            pass
        return serial_to_host


    def _read_hostname_from_system_settings(
        device_id: str,
        system_mgr=None,
        system_root: str | None = None,
        *,
        device_type: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> str | None:
        """
        Truth source: system_settings/<system-id>/settings.toml → [Network].HOSTNAME.

        For sensors, <system-id> == sensor-id.
        For switches on a sensor+switch Nodus, <system-id> == sensor-id with same serial
        (we find it via sys_host_index).
        For switch-only Nodus, <system-id> == 'switch-<serial>'.
        """
        ingest = services.mqtt_ingest
        try:
            if ingest and getattr(ingest, 'resolve_nodus_hostname', None):
                host = ingest.resolve_nodus_hostname(device_id, device_type=device_type)
                if host:
                    return host
        except Exception:
            pass

        dev_id = (device_id or "").strip()
        if not dev_id:
            return None

        serial = (dev_id.rsplit("-", 1)[-1] if "-" in dev_id else dev_id).strip()

        def _sensor_settings_manager():
            mgr_cls = SensorSettingsManager
            if mgr_cls is not None:
                try:
                    return mgr_cls("sensor_settings")
                except TypeError:
                    return mgr_cls()
            try:
                from .saiSensorSettingsManager import SensorSettingsManager as _SensorSettingsManager
                return _SensorSettingsManager("sensor_settings")
            except TypeError:
                return _SensorSettingsManager()
            except Exception:
                return None

        # Child sensor shadows persist the physical Nodus identity. This keeps
        # settings and restart commands routed correctly before retained meta
        # has rebuilt the ingest maps after a Sensorius restart.
        if (device_type or "").lower() == "sensor":
            try:
                sm = _sensor_settings_manager()
                doc = sm.load(dev_id) if sm else {}
                nodus = doc.get("Nodus", {}) if isinstance(doc, dict) else {}
                host = str((nodus or {}).get("DEVICE_ID") or "").strip()
                if host:
                    return host
            except Exception:
                pass

        def _paired_sensor_host_for_switch() -> str | None:
            if not serial:
                return None
            try:
                sm = _sensor_settings_manager()
                if not sm:
                    return None
                for sid in (sm.list_ids() or []):
                    sid = str(sid or "").strip()
                    if not sid or sid.lower().startswith("switch-"):
                        continue
                    if sid.endswith(f"-{serial}"):
                        paired_host = _read_hostname_from_system_settings(
                            sid,
                            system_mgr,
                            system_root,
                            device_type="sensor",
                            sys_host_index=sys_host_index,
                        )
                        if paired_host:
                            return str(paired_host).strip()
            except Exception:
                pass
            return None

        # For shared Sensor+Switch Nodus devices, prefer the physical host
        # derived from the system hostname index before consulting any stale
        # switch-specific folder that may still advertise "switch-<serial>".
        if (device_type or "").lower() == "switch" and serial:
            indexed_host = str((sys_host_index or {}).get(serial) or "").strip()
            if indexed_host:
                return indexed_host
            paired_host = _paired_sensor_host_for_switch()
            if paired_host:
                return paired_host

        # Fast-path for sensors: try manager then direct file
        if (device_type or "").lower() == "sensor":
            try:
                if system_mgr and hasattr(system_mgr, "get_setting"):
                    host = (system_mgr.get_setting(dev_id, "Network.HOSTNAME", "") or "").strip()
                    if host:
                        return host
            except Exception:
                pass

        # Resolve system_root
        if not system_root:
            system_root = _resolve_system_settings_root(system_mgr)
        else:
            system_root = _normalize_system_settings_root(system_root)

        import os
        try:
            import tomllib
        except Exception:
            tomllib = None

        # Direct file read for sensors and true switch-only devices.
        # For shared Sensor+Switch Nodus devices we intentionally prefer the
        # paired sensor host above, before consulting the switch folder here.
        settings_path = os.path.join(system_root, dev_id, "settings.toml")
        if os.path.isfile(settings_path) and tomllib:
            try:
                with open(settings_path, "rb") as f:
                    data = tomllib.load(f) or {}
                host = (data.get("Network", {}).get("HOSTNAME") or "").strip()
                if host:
                    return host
            except Exception:
                pass

        # If it's a switch and ingest didn't know yet, derive host from paired sensor id
        if (device_type or "").lower() == "switch":
            if serial:
                paired_host = _paired_sensor_host_for_switch()
                if paired_host:
                    return paired_host
                # 2a) Try to match any known mqtt_clients like '<anything>-<serial>'
                try:
                    if ingest and getattr(ingest, 'mqtt_clients', None):
                        for cand in (ingest.mqtt_clients or []):
                            cand = str(cand)
                            if cand.endswith(f"-{serial}"):
                                return cand  # bare hostname, e.g. "aqi-nz6g89"
                except Exception:
                    pass

                # 2b) Try to match any known sensor_id in sensor settings
                try:
                    sm = _sensor_settings_manager()
                    if not sm:
                        return None
                    for sid in (sm.list_ids() or []):
                        sid = str(sid)
                        if sid.lower().startswith("switch-"):
                            continue
                        if sid.endswith(f"-{serial}"):
                            return sid  # use the sensor_id as the host (bare)
                except Exception:
                    pass

        return None


    def _read_system_settings_doc(system_root: str, device_id: str | None) -> dict[str, Any]:
        dev = normalize_hostname_base(str(device_id or "").strip()) or str(device_id or "").strip()
        if not dev:
            return {}
        root = _normalize_system_settings_root(system_root)
        path = os.path.join(root, dev, "settings.toml")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f) or {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


    def _settings_value(section: dict | None, *keys: str) -> str:
        if not isinstance(section, dict):
            return ""
        for key in keys:
            value = section.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""


    def _broker_from_network_doc(doc: dict[str, Any]) -> str:
        mqtt_doc = doc.get("MQTT") if isinstance(doc, dict) else {}
        sensor_network_doc = doc.get("SensorNetwork") if isinstance(doc, dict) else {}
        network_doc = doc.get("Network") if isinstance(doc, dict) else {}
        broker = (
            _settings_value(mqtt_doc, "BROKER", "broker")
            or _settings_value(sensor_network_doc, "BROKER", "broker")
            or _settings_value(network_doc, "BROKER", "broker")
        )
        if broker:
            return broker
        try:
            return str(settings.get_setting("SensorNetwork", "BROKER", "") or "").strip()
        except Exception:
            return ""


    def _broker_health_status(broker: str, ingest=None) -> str:
        if not str(broker or "").strip():
            return "Not configured"
        if ingest is None:
            return "Unavailable"

        connected: bool | None = None
        try:
            client = getattr(ingest, "client", None)
            checker = getattr(client, "is_connected", None)
            if callable(checker):
                connected = bool(checker())
        except Exception:
            connected = None

        if connected is None:
            try:
                event = getattr(ingest, "_connected_evt", None)
                is_set = getattr(event, "is_set", None)
                if callable(is_set):
                    connected = bool(is_set())
            except Exception:
                connected = None

        if connected is True:
            return "Connected"
        if connected is False:
            return "Disconnected"
        return "Unknown"


    def _runtime_ipv4_from_ingest(
        device_id: str,
        *,
        device_type: str,
        source_host: str,
        host_base: str,
        ingest=None,
    ) -> str:
        if ingest is None:
            return ""

        def _norm_ipv4(value) -> str:
            raw = str(value or "").strip()
            if not raw:
                return ""
            parts = raw.split(".")
            if len(parts) != 4:
                return ""
            for part in parts:
                if not part.isdigit():
                    return ""
                try:
                    num = int(part)
                except Exception:
                    return ""
                if num < 0 or num > 255:
                    return ""
            return raw

        def _add_candidate(raw_value, bucket: list[str]) -> None:
            raw = str(raw_value or "").strip()
            if not raw:
                return
            for value in (raw, normalize_hostname_base(raw), mdns_hostname(normalize_hostname_base(raw) or raw)):
                text = str(value or "").strip()
                if text and text not in bucket:
                    bucket.append(text)

        candidates: list[str] = []
        _add_candidate(device_id, candidates)
        _add_candidate(source_host, candidates)
        _add_candidate(host_base, candidates)
        try:
            _add_candidate(ingest.resolve_nodus_hostname(device_id, device_type=device_type), candidates)
        except Exception:
            pass

        try:
            candidate_set = {str(item or "").strip() for item in candidates if str(item or "").strip()}
            candidate_bases = {normalize_hostname_base(item) or item for item in candidate_set}
            for host, peers in (getattr(ingest, "host_to_peer_ids", {}) or {}).items():
                peer_set = {str(peer or "").strip() for peer in (peers or []) if str(peer or "").strip()}
                host_base_candidate = normalize_hostname_base(str(host or "")) or str(host or "").strip()
                peer_bases = {normalize_hostname_base(peer) or peer for peer in peer_set}
                if (
                    host in candidate_set
                    or host_base_candidate in candidate_bases
                    or bool(peer_set & candidate_set)
                    or bool(peer_bases & candidate_bases)
                ):
                    _add_candidate(host, candidates)
                    for peer in peer_set:
                        _add_candidate(peer, candidates)
        except Exception:
            pass

        for attr in ("_host_ipv4addr",):
            try:
                mapping = getattr(ingest, attr, {}) or {}
                for key in candidates:
                    ip = _norm_ipv4(mapping.get(key))
                    if ip:
                        return ip
            except Exception:
                pass

        def _meta_ipv4(meta: dict | None) -> str:
            if not isinstance(meta, dict):
                return ""
            network_meta = meta.get("network") if isinstance(meta.get("network"), dict) else {}
            for container in (network_meta, meta):
                for key in ("ipv4addr", "IPV4ADDR", "IPv4Addr", "ipv4"):
                    ip = _norm_ipv4(container.get(key))
                    if ip:
                        return ip
            return ""

        def _meta_has_identifier(meta: dict, raw_value: str) -> bool:
            want = str(raw_value or "").strip().lower()
            if not want or not isinstance(meta, dict):
                return False
            observed: set[str] = set()

            def _observe(value) -> None:
                text = str(value or "").strip()
                if not text:
                    return
                observed.add(text.lower())
                normalized = normalize_hostname_base(text)
                if normalized:
                    observed.add(normalized.lower())

            _observe(meta.get("device_id"))
            _observe(meta.get("hostname"))
            sensor_meta = meta.get("sensor") if isinstance(meta.get("sensor"), dict) else {}
            _observe(sensor_meta.get("sensor_id"))
            switch_meta = meta.get("switch") if isinstance(meta.get("switch"), dict) else {}
            _observe(switch_meta.get("device_id"))
            _observe(switch_meta.get("switch_device_id"))
            channels = switch_meta.get("channels")
            if isinstance(channels, list):
                for row in channels:
                    if isinstance(row, dict):
                        _observe(row.get("channel_id"))
            return want in observed

        try:
            cache = getattr(ingest, "discovery_cache", {}) or {}
            for key in candidates:
                meta = cache.get(key) or cache.get(normalize_hostname_base(key) or key)
                ip = _meta_ipv4(meta)
                if ip:
                    return ip
            for meta in cache.values():
                if not isinstance(meta, dict):
                    continue
                if any(_meta_has_identifier(meta, key) for key in candidates):
                    ip = _meta_ipv4(meta)
                    if ip:
                        return ip
        except Exception:
            pass

        try:
            mapping = getattr(ingest, "_host_ip_cache", {}) or {}
            for key in candidates:
                ip = _norm_ipv4(mapping.get(key))
                if ip:
                    return ip
        except Exception:
            pass

        return ""


    async def _build_device_network_info(device_id: str, *, device_type: str) -> dict[str, str]:
        """
        Build read-only broker details for the Info panes.

        Switches on a combined sensor+switch Nodus use the paired sensor host,
        matching the settings push/restart behavior when locating the broker
        setting for that physical device.
        """
        sid = str(device_id or "").strip()
        ingest = services.mqtt_ingest
        system_root = _resolve_system_settings_root()
        sys_host_index = _build_system_hostname_index(system_root)
        source_host = _read_hostname_from_system_settings(
            sid,
            system_root=system_root,
            device_type=device_type,
            sys_host_index=sys_host_index,
        )
        source_host = str(source_host or "").strip()
        if not source_host:
            source_host = normalize_hostname_base(sid) or sid
        host_base = normalize_hostname_base(source_host) or source_host

        doc = _read_system_settings_doc(system_root, host_base)
        if not doc and sid and sid != host_base:
            doc = _read_system_settings_doc(system_root, sid)
        broker = _broker_from_network_doc(doc)
        ip_address = _runtime_ipv4_from_ingest(
            sid,
            device_type=device_type,
            source_host=source_host,
            host_base=host_base,
            ingest=ingest,
        )

        if device_type == "sensor" and sid.startswith("ecowitt-"):
            ip_address = ecowitt_sensor_health(services.data_logger, sid)["gateway_ip"]

        return {
            "ip_address": ip_address or "Unknown",
            "broker": broker or "Unknown",
            "broker_status": _broker_health_status(broker, ingest),
        }


    def _display_nodus_board_type(device_id: str, *, device_type: str, settings_block: dict | None = None) -> str:
        board_type = ""
        ingest = services.mqtt_ingest
        if ingest and hasattr(ingest, "get_nodus_board_type"):
            try:
                board_type = str(
                    ingest.get_nodus_board_type(device_id, device_type=device_type)
                ).strip()
            except Exception:
                board_type = ""
        if not board_type and isinstance(settings_block, dict):
            for key in ("MCU", "mcu", "BOARD_TYPE", "board_type", "BOARDTYPE", "boardtype", "BOARD", "board"):
                board_type = str(settings_block.get(key) or "").strip()
                if board_type:
                    break
        if not board_type and isinstance(settings_block, dict):
            device_class = str(settings_block.get("TYPE") or settings_block.get("type") or "").strip().lower()
            if device_class in {"nodus", "picow", "pico2w", "remote", "mqtt"}:
                board_type = "pico2w"
            elif device_type == "sensor" and device_class == "pi":
                board_type = "rPi"
        return board_type


    def _resolve_sensor_controller(sensor_id: str):
        sid_norm = normalize_sensor_id(sensor_id)
        smap = _get_dashboard_sensor_map()
        if smap is None:
            return None
        if hasattr(smap, "get"):
            try:
                hit = smap.get(sensor_id) or smap.get(sid_norm) or smap.get(sid_norm.lower())
                if hit:
                    return hit
            except Exception:
                pass
            try:
                for key, value in smap.items():
                    if normalize_sensor_id(str(key or "")) == sid_norm:
                        return value
            except Exception:
                pass
        try:
            for item in smap:
                controller = item
                candidate = getattr(item, "sensor_id", None)
                sensor_obj = getattr(item, "sensor", None)
                if not candidate and sensor_obj is not None:
                    candidate = getattr(sensor_obj, "sensor_id", None)
                if candidate and normalize_sensor_id(str(candidate)) == sid_norm:
                    return controller
        except TypeError:
            return None
        except Exception:
            return None
        return None


    def _local_sensor_hardware_from_controller(sensor_id: str) -> str:
        controller = _resolve_sensor_controller(sensor_id)
        sensor_obj = getattr(controller, "sensor", None) if controller is not None else None
        if sensor_obj is None:
            sensor_obj = controller
        if sensor_obj is None:
            return ""

        for attr in ("hardware", "sensor_hardware", "model", "sensor_model", "_co2_model"):
            value = str(getattr(sensor_obj, attr, "") or "").strip()
            if value:
                return value

        if getattr(sensor_obj, "bme680", None) is not None:
            return "BME680"
        if getattr(sensor_obj, "thp280_plant", None) is not None:
            return "BME280"
        if getattr(sensor_obj, "thp280", None) is not None:
            return "BME280"
        if getattr(sensor_obj, "aht", None) is not None:
            return "AHTx0"
        if getattr(sensor_obj, "veml", None) is not None:
            return "VEML7700"
        return ""


    def _local_sensor_hardware_from_settings(settings_block: dict | None) -> str:
        if not isinstance(settings_block, dict):
            return ""
        device_kind = str(settings_block.get("DEVICE") or settings_block.get("device") or "").strip().lower()
        hardware_by_device = {
            "apvpd": "BME280",
            "avpd": "BME280",
            "vpd": "BME280",
            "bme280": "BME280",
            "aqi": "BME680",
            "bme680": "BME680",
            "bme688": "BME688",
            "aht": "AHTx0",
            "aht10": "AHT10",
            "ahtx0": "AHTx0",
            "co2": "SCD30/SCD4x",
            "scd30": "SCD30",
            "scd4x": "SCD4x",
            "veml": "VEML7700",
            "lux": "VEML7700",
            "dummy": "Dummy",
            "test": "Dummy",
        }
        return hardware_by_device.get(device_kind, "")


    def _display_nodus_sensor_hardware(device_id: str, *, settings_block: dict | None = None) -> str:
        sensor_hardware = ""
        ingest = services.mqtt_ingest
        if ingest and hasattr(ingest, "get_nodus_sensor_hardware"):
            try:
                sensor_hardware = str(
                    ingest.get_nodus_sensor_hardware(device_id, device_type="sensor")
                ).strip()
            except Exception:
                sensor_hardware = ""
        if not sensor_hardware and isinstance(settings_block, dict):
            for key in ("HARDWARE", "hardware", "SENSOR_HARDWARE", "sensor_hardware"):
                sensor_hardware = str(settings_block.get(key) or "").strip()
                if sensor_hardware:
                    break
        if not sensor_hardware and isinstance(settings_block, dict):
            device_class = str(settings_block.get("TYPE") or settings_block.get("type") or "").strip().lower()
            if device_class == "pi":
                sensor_hardware = _local_sensor_hardware_from_controller(device_id)
                if not sensor_hardware:
                    sensor_hardware = _local_sensor_hardware_from_settings(settings_block)
        return sensor_hardware


    async def push_nodus_setting_simple(
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        section: str,
        key: str,
        value,
        sensor_file_name: str | None,
        system_mgr=None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        update = {"section": section, "key": key, "value": value}
        if setting_file_key == "sensor" and sensor_file_name:
            update["name"] = sensor_file_name
        elif setting_file_key == "switch":
            update["name"] = "switch.toml"
        return await push_nodus_settings_batch(
            device_id=device_id,
            device_type=device_type,
            setting_file_key=setting_file_key,
            updates=[(section, key, value)],
            sensor_file_name=sensor_file_name,
            system_mgr=system_mgr,
            system_root=system_root,
            ip_hint=ip_hint,
            sys_host_index=sys_host_index,
        )


    async def _publish_nodus_config_update(
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        update_payload: dict[str, Any],
    ) -> bool:
        def _config_result_applied(result_payload: dict[str, Any] | None) -> bool:
            # Gate the next queued update on Nodus confirming the apply completed.
            return isinstance(result_payload, dict) and result_payload.get("applied") is True

        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_config"):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] mqtt_ingest unavailable; skipping publish",
                    location=MODULE,
                )
            return False

        if DEBUG:
            client_desc = ""
            try:
                if hasattr(ingest, "_describe_publish_client"):
                    client_desc = str(ingest._describe_publish_client(use_ha_client=False) or "")
            except Exception:
                client_desc = ""
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] preparing publish target={target_device} topic=nodus/{target_device}/config/set update={str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()} {client_desc}".strip(),
                location=MODULE,
            )

        publish_result = ingest.publish_nodus_config(
            target_device,
            payload={"updates": [update_payload]},
            restart=False,
        )
        if not bool(publish_result.get("ok", False)):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] failed to publish config/set to nodus/{target_device}/config/set",
                    location=MODULE,
                )
            return False

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_config_ack(message_id, timeout=_NODUS_CONFIG_ACK_TIMEOUT_SEC)
        if not ack or not bool(ack.get("accepted", False)):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config ack failed for nodus/{target_device}/config/set message_id={message_id}: {ack}",
                    location=MODULE,
                )
            return False

        result = await ingest.wait_for_config_result(message_id, timeout=_NODUS_CONFIG_RESULT_TIMEOUT_SEC)
        if result is None:
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config result timeout for nodus/{target_device}/config/set message_id={message_id}",
                    location=MODULE,
                )
            return False
        if not _config_result_applied(result):
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] config apply failed for nodus/{target_device}/config/set message_id={message_id}: {result}",
                    location=MODULE,
                )
            return False

        if DEBUG:
            printDM(
                f"[push_nodus_setting:{device_type}:{device_id}] OK via MQTT nodus/{target_device}/config/set: {str(update_payload.get('section') or '').strip()}.{str(update_payload.get('key') or '').strip()}",
                location=MODULE,
            )
        return True


    async def _request_nodus_device_restart(
        *,
        target_device: str,
        device_id: str,
        device_type: str,
        restart_mode: str = "soft",
    ) -> tuple[bool, str]:
        def _truthy(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            return str(value or "").strip().lower() in {"1", "true", "yes", "on", "ok", "accepted", "applied", "rebooting", "restarted"}

        def _restart_ack_accepted(ack_payload: dict[str, Any] | None) -> bool:
            if not isinstance(ack_payload, dict):
                return False
            if "accepted" in ack_payload:
                return _truthy(ack_payload.get("accepted"))
            if "ok" in ack_payload:
                return _truthy(ack_payload.get("ok"))
            return True

        def _restart_result_applied(result_payload: dict[str, Any] | None) -> bool:
            if not isinstance(result_payload, dict):
                return False
            for key in ("applied", "restarted", "rebooting", "accepted", "ok", "success"):
                if key in result_payload and _truthy(result_payload.get(key)):
                    return True
            return False

        ingest = services.mqtt_ingest
        if not ingest:
            return False, "MQTT ingest unavailable"

        message_id = f"rst-{int(time.time())}-{uuid4().hex[:8]}"
        publish_result: dict[str, Any] | None = None

        if hasattr(ingest, "publish_nodus_restart"):
            publish_result = ingest.publish_nodus_restart(
                target_device,
                restart_mode=restart_mode,
                message_id=message_id,
            )
        elif hasattr(ingest, "publish_json"):
            topic = f"nodus/{target_device}/config/set"
            envelope = {
                "message_id": message_id,
                "payload": {},
                "restart": True,
                "restart_mode": restart_mode,
            }
            ok = bool(ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
            publish_result = {"ok": ok, "message_id": message_id, "topic": topic, "payload": envelope}
        else:
            return False, "MQTT publish unavailable"

        if not bool((publish_result or {}).get("ok", False)):
            return False, "Failed to publish restart request"

        ack = None
        if hasattr(ingest, "wait_for_config_ack"):
            ack = await ingest.wait_for_config_ack(message_id, timeout=_NODUS_CONFIG_ACK_TIMEOUT_SEC)
        if not isinstance(ack, dict):
            return False, "Restart request was not acknowledged by the device"
        if not _restart_ack_accepted(ack):
            error_text = str(ack.get("error") or "").strip()
            return False, error_text or "Restart request was rejected by the device"

        result = None
        if hasattr(ingest, "wait_for_config_result"):
            result = await ingest.wait_for_config_result(message_id, timeout=_NODUS_CONFIG_RESULT_TIMEOUT_SEC)
        if result is None:
            return False, "Restart request timed out waiting for device result"
        if not _restart_result_applied(result):
            error_text = str((result or {}).get("error") or "").strip()
            return False, error_text or "Restart request was not applied by the device"

        if DEBUG:
            printDM(
                f"[restart_nodus_device:{device_type}:{device_id}] target={target_device} message_id={message_id} ack={ack} result={result}",
                location=MODULE,
            )
        return True, "Device restarting..."


    def _nodus_wifi_inventory() -> list[dict[str, Any]]:
        """Return deduplicated physical Nodus targets and current MQTT liveness."""
        ingest = services.mqtt_ingest
        if not ingest:
            return []
        try:
            raw_devices = ota_service.list_devices()
        except Exception:
            raw_devices = []

        inventory: list[dict[str, Any]] = []
        seen: set[str] = set()
        known_physical_ids: set[str] = set()
        known_ids: set[str] = set()
        for getter_name in ("get_known_devices", "get_known_switch_devices"):
            getter = getattr(ingest, getter_name, None)
            if not callable(getter):
                continue
            try:
                known_ids.update(
                    normalize_hostname_base(str(value or ""))
                    for value in (getter() or [])
                )
            except Exception:
                pass
        known_ids.discard("")
        sensor_hosts = getattr(ingest, "nodus_sensor_hosts", {}) or {}
        resolver = getattr(ingest, "resolve_nodus_hostname", None)
        for known_id in known_ids:
            physical = normalize_hostname_base(str(sensor_hosts.get(known_id) or ""))
            if not physical and callable(resolver):
                try:
                    physical = normalize_hostname_base(
                        str(
                            resolver(
                                known_id,
                                device_type="switch" if known_id.startswith("switch-") else None,
                            )
                            or ""
                        )
                    )
                except Exception:
                    physical = ""
            known_physical_ids.add(str(physical or known_id).lower())

        command_supported = all(
            callable(getattr(ingest, name, None))
            for name in ("publish_nodus_config", "wait_for_config_ack", "wait_for_config_result")
        )
        for raw in raw_devices:
            if not isinstance(raw, dict):
                continue
            device_id = normalize_hostname_base(
                str(raw.get("device_id") or raw.get("host") or "")
            )
            key = str(device_id or "").strip().lower()
            if not device_id or key in seen:
                continue
            if key not in known_physical_ids:
                continue
            removed = getattr(ingest, "is_nodus_device_removed", None)
            if callable(removed):
                try:
                    if removed(device_id):
                        continue
                except Exception:
                    pass
            seen.add(key)
            liveness = {}
            getter = getattr(ingest, "get_nodus_liveness", None)
            if callable(getter):
                try:
                    liveness = getter(device_id) or {}
                except Exception:
                    liveness = {}
            state = str(liveness.get("state") or raw.get("status") or "unknown").strip().lower()
            eligible = state == "online" and command_supported
            reason = ""
            if not command_supported:
                reason = "configuration commands unavailable"
            elif state != "online":
                reason = f"device is {state or 'unknown'}"
            inventory.append(
                {
                    "device_id": device_id,
                    "status": state or "unknown",
                    "eligible": eligible,
                    "reason": reason,
                    "firmware_version": str(raw.get("firmware_version") or ""),
                    "last_seen_s": liveness.get("last_seen_s", raw.get("last_seen_s")),
                }
            )
        return sorted(inventory, key=lambda row: str(row.get("device_id") or "").lower())


    def _nodus_wifi_response(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
        return JSONResponse(
            payload,
            status_code=status_code,
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )


    def _validate_nodus_wifi_credentials(ssid: str, password: str) -> str:
        try:
            ssid_bytes = ssid.encode("utf-8", errors="strict")
        except UnicodeError:
            return "SSID contains invalid text."
        if not ssid_bytes or len(ssid_bytes) > 32:
            return "SSID must contain between 1 and 32 UTF-8 bytes."
        password_ok = 8 <= len(password) <= 63 or bool(re.fullmatch(r"[0-9A-Fa-f]{64}", password))
        if not password_ok:
            return "Password must contain 8 to 63 characters, or be a 64-digit hexadecimal key."
        return ""


    @router.get("/api/nodus-wifi/current", response_class=JSONResponse)
    async def api_nodus_wifi_current(request: Request):
        """Read the host's active Wi-Fi credentials for transient form display."""
        _require_protected_access(request, require_csrf=True)
        try:
            from . import saiAddDevice

            ssid, password = await asyncio.to_thread(saiAddDevice.resolve_pi_wifi_credentials)
        except Exception:
            ssid, password = "", ""
        return _nodus_wifi_response(
            {
                "ok": True,
                "ssid": str(ssid or ""),
                "password": str(password or ""),
                "password_available": bool(password),
            }
        )


    @router.get("/api/nodus-wifi/devices", response_class=JSONResponse)
    async def api_nodus_wifi_devices(request: Request):
        """List physical Nodus devices eligible for a coordinated Wi-Fi update."""
        _require_protected_access(request, require_csrf=True)
        devices = _nodus_wifi_inventory()
        return _nodus_wifi_response(
            {
                "ok": True,
                "devices": devices,
                "eligible_count": sum(1 for row in devices if row.get("eligible") is True),
            }
        )


    @router.post("/api/nodus-wifi/update", response_class=JSONResponse)
    async def api_nodus_wifi_update(request: Request):
        """Stage new credentials on online Nodus devices, then restart successes."""
        _require_protected_access(request, require_csrf=True)
        try:
            body = await request.json()
        except Exception:
            body = {}
        ssid = str((body or {}).get("ssid") or "")
        password = str((body or {}).get("password") or "")
        validation_error = _validate_nodus_wifi_credentials(ssid, password)
        if validation_error:
            return _nodus_wifi_response({"ok": False, "error": validation_error}, status_code=400)

        requested_raw = (body or {}).get("device_ids")
        if requested_raw is not None and not isinstance(requested_raw, list):
            return _nodus_wifi_response({"ok": False, "error": "device_ids must be a list."}, status_code=400)
        requested = [str(value or "").strip() for value in (requested_raw or []) if str(value or "").strip()]
        invalid = [value for value in requested if not _is_valid_device_id(value)]
        if invalid:
            return _nodus_wifi_response({"ok": False, "error": "One or more device IDs are invalid."}, status_code=400)

        inventory = _nodus_wifi_inventory()
        inventory_by_id = {str(row.get("device_id") or "").lower(): row for row in inventory}
        if requested_raw is None:
            requested_keys = [key for key, row in inventory_by_id.items() if row.get("eligible") is True]
        else:
            requested_keys = list(dict.fromkeys(value.lower() for value in requested))

        results: dict[str, dict[str, Any]] = {}
        targets: list[str] = []
        for key in requested_keys:
            row = inventory_by_id.get(key)
            device_id = str((row or {}).get("device_id") or key)
            if not row:
                results[key] = {
                    "device_id": device_id,
                    "status": "unavailable",
                    "staged": False,
                    "restarted": False,
                    "message": "Device is no longer available.",
                }
            elif row.get("eligible") is not True:
                results[key] = {
                    "device_id": device_id,
                    "status": "skipped",
                    "staged": False,
                    "restarted": False,
                    "message": str(row.get("reason") or "Device is not online."),
                }
            else:
                targets.append(device_id)

        if not targets:
            return _nodus_wifi_response(
                {
                    "ok": False,
                    "error": "No online Nodus devices are eligible for the update.",
                    "results": list(results.values()),
                },
                status_code=409,
            )

        ingest = services.mqtt_ingest
        concurrency = asyncio.Semaphore(4)

        async def _stage_one(device_id: str) -> dict[str, Any]:
            async with concurrency:
                try:
                    staged = await push_nodus_settings_batch(
                        device_id=device_id,
                        device_type="wifi",
                        setting_file_key="system",
                        updates=[
                            ("Network", "SSID", ssid),
                            ("Network", "PASSWORD", password),
                        ],
                        sensor_file_name=None,
                    )
                    if not staged:
                        raise RuntimeError("configuration_not_applied")
                    return {
                        "device_id": device_id,
                        "status": "staged",
                        "staged": True,
                        "restarted": False,
                        "message": "Credentials staged and confirmed.",
                    }
                except Exception as exc:
                    error_code = str(exc or "stage_failed")
                    safe_messages = {
                        "configuration_not_applied": "Device did not confirm both credential updates.",
                    }
                    if DEBUG:
                        printDM(
                            f"[nodus-wifi:{device_id}] credential staging failed error={error_code}",
                            location=MODULE,
                        )
                    return {
                        "device_id": device_id,
                        "status": "failed",
                        "staged": False,
                        "restarted": False,
                        "message": safe_messages.get(error_code, "Credential update failed."),
                    }

        staged_rows = await asyncio.gather(*(_stage_one(device_id) for device_id in targets))
        for row in staged_rows:
            results[str(row.get("device_id") or "").lower()] = row

        staged_devices = [str(row.get("device_id") or "") for row in staged_rows if row.get("staged") is True]

        async def _restart_one(device_id: str) -> dict[str, Any]:
            async with concurrency:
                ok, message = await _request_nodus_device_restart(
                    target_device=device_id,
                    device_id=device_id,
                    device_type="system",
                )
                if ok:
                    return {
                        "device_id": device_id,
                        "status": "restarting",
                        "staged": True,
                        "restarted": True,
                        "message": "Credentials confirmed; device is restarting.",
                    }
                return {
                    "device_id": device_id,
                    "status": "restart_failed",
                    "staged": True,
                    "restarted": False,
                    "message": str(message or "Credentials were staged, but restart failed."),
                }

        restarted_rows = await asyncio.gather(*(_restart_one(device_id) for device_id in staged_devices))
        for row in restarted_rows:
            results[str(row.get("device_id") or "").lower()] = row

        ordered_results = [results[key] for key in requested_keys if key in results]
        all_restarted = bool(ordered_results) and all(row.get("restarted") is True for row in ordered_results)
        return _nodus_wifi_response(
            {
                "ok": all_restarted,
                "requested": len(requested_keys),
                "staged": sum(1 for row in ordered_results if row.get("staged") is True),
                "restarting": sum(1 for row in ordered_results if row.get("restarted") is True),
                "results": ordered_results,
            }
        )


    async def push_nodus_settings_batch(
        *,
        device_id: str,
        device_type: str,
        setting_file_key: str,
        updates: list[tuple[str, str, Any]],
        sensor_file_name: str | None,
        system_mgr=None,
        system_root: str | None = None,
        ip_hint: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> bool:
        hostname = _read_hostname_from_system_settings(
            device_id, system_mgr, system_root,
            device_type=device_type, sys_host_index=sys_host_index
        )
        target_device = str(hostname or device_id or "").strip()
        if not target_device:
            if DEBUG:
                printDM(
                    f"[push_nodus_setting:{device_type}:{device_id}] no MQTT target device resolved; skipping publish",
                    location=MODULE,
                )
            return False

        updates_payload: list[dict[str, Any]] = []
        for section, key, value in (updates or []):
            item = {"section": section, "key": key, "value": value}
            if setting_file_key == "sensor":
                item["sensor_id"] = str(device_id or "").strip()
                if sensor_file_name:
                    item["name"] = sensor_file_name
            elif setting_file_key == "switch":
                item["name"] = "switch.toml"
            updates_payload.append(item)
        if not updates_payload:
            return True
        # Serialize config writes per physical Nodus host. This preserves the
        # one-key-at-a-time behavior even when multiple routes/tasks target the
        # same device concurrently (for example paired sensor + switch updates).
        async with _get_host_lock(target_device):
            for update_payload in updates_payload:
                ok = await _publish_nodus_config_update(
                    target_device=target_device,
                    device_id=device_id,
                    device_type=device_type,
                    update_payload=update_payload,
                )
                if not ok:
                    return False
        return True


    def _nodus_values_match(previous: Any, current: Any) -> bool:
        if previous == current:
            return True
        if previous is None and current in (None, ""):
            return True
        if current is None and previous in (None, ""):
            return True
        return False


    @router.get("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def list_device_locations(request: Request) -> JSONResponse:
        try:
            system_dir = _resolve_system_settings_root()

            sensor_settings = SensorSettingsManager()
            sensor_dir = getattr(sensor_settings, "base_dir", None)
            if not sensor_dir:
                sensor_dir = "sensor_settings"

            switch_settings = SwitchSettingsManager()
            switch_dir = getattr(switch_settings, "base_dir", None)
            if not switch_dir:
                switch_dir = "switch_settings"

            # Log the resolved paths
            if DEBUG:
                printDM(f"system_dir={system_dir}", location="saiWebRoutes.list_device_locations")
                printDM(f"sensor_dir={sensor_dir}", location="saiWebRoutes.list_device_locations")
                printDM(f"switch_dir={switch_dir}", location="saiWebRoutes.list_device_locations")


            # Instantiate managers, tolerating different ctor signatures
            try:
                sensor_mgr = SensorSettingsManager(sensor_dir)
            except TypeError:
                sensor_mgr = SensorSettingsManager()  # falls back to default if ctor takes no args

            try:
                switch_mgr = SwitchSettingsManager(switch_dir)
            except TypeError:
                switch_mgr = SwitchSettingsManager()

            sensor_items = []
            try:
                for sensor_id in sensor_mgr.list_ids():
                    if (sensor_id or "").lower() == "factory":
                        continue
                    doc = sensor_mgr.load(sensor_id) or {}
                    loc = (doc.get("Sensor", {}) or {}).get("LOCATION", "Unknown") or "Unknown"
                    sensor_items.append({"id": sensor_id, "type": "sensor", "location": loc})
                    if DEBUG:
                        printDM(f"sensor: {sensor_id}, sw_loc: {loc}", location="saiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"sensor_mgr error: {e}", location="saiWebRoutes.list_device_locations")

            switch_items = []
            try:
                for switch_id in switch_mgr.list_switches():
                    if (switch_id or "").lower() == "factory":
                        continue
                    doc = switch_mgr.load(switch_id) or {}
                    sw_loc = (doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "") or "Unknown"
                    switch_items.append({"id": switch_id, "type": "switch", "location": sw_loc})
                    if DEBUG:
                        printDM(f"switch: {switch_id}, sw_loc: {sw_loc}", location="saiWebRoutes.list_device_locations")
            except Exception as e:
                printDM(f"switch_mgr error: {e}", location="saiWebRoutes.list_device_locations")

            items = sensor_items + switch_items

            if DEBUG:
                printDM(f"sensors={len(sensor_items)} switches={len(switch_items)} total={len(items)}", location="saiWebRoutes.DeviceLocations")

            return JSONResponse(items)

        except Exception as e:
            printDM(f"Failed to list device locations: {e}", location="saiWebRoutes.list_device_locations")
            # Return an empty list (200) rather than 500 so the modal stays open with a visible message
            # If you prefer to signal error, keep 500 — but then ensure the client does NOT auto-close the modal.
            return JSONResponse({"error": "failed"}, status_code=500)


    @router.post("/device-locations", tags=[LOCATIONS_ROUTE_TAG])
    async def save_device_locations(request: Request) -> JSONResponse:
        """
        Accepts: [{"id": "...", "type": "sensor"|"switch", "location": "..."}]
        (Also tolerates {"switch_location": "..."} for switches.)
        Saves LOCATION/SWITCH_LOCATION locally and updates live objects in memory.
        For remote Nodus devices, publishes MQTT config updates and waits for ack/result.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, list):
                return JSONResponse({"error": "invalid_payload"}, status_code=400)

            sensor_mgr = SensorSettingsManager("sensor_settings")
            switch_mgr = SwitchSettingsManager("switch_settings")
            SystemSettingsMgr = get_system_settings_manager()
            system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
            system_root = _resolve_system_settings_root(system_mgr)
            updated = {"sensor": 0, "switch": 0, "nodus_pushed": 0}

            sys_host_index = _build_system_hostname_index(system_root)
            if DEBUG:
                printDM(f"[save_device_locations] system hostname index: {sys_host_index}", location=MODULE)

            # Shared controller map supplied by the runtime context.
            def _find_switch_ctrl_by_id(switch_id: str):
                """Return the live SwitchController with matching switch_id, or None."""
                try:
                    sid = (switch_id or "").strip()
                    if not sid:
                        return None
                    if isinstance(services.switch_controllers, dict):
                        for cand in services.switch_controllers.values():
                            if getattr(cand, "switch_id", "") in {sid, sid.lower()}:
                                return cand
                    else:
                        if getattr(services.switch_controllers, "switch_id", "") in {sid, sid.lower()}:
                            return services.switch_controllers
                except Exception:
                    pass
                return None

            # --- make sensor_map accessible anywhere in this handler
            from collections.abc import Iterable
            def _get_sensor_map():
                sm = services.sensor_map
                return sm
            sensor_map = _get_sensor_map()

            # We’ll gather Nodus POSTs to run concurrently for speed
            nodus_tasks: list[tuple[dict[str, Any], Any]] = []

            for entry in payload:
                try:
                    dev_id   = (entry.get("id")   or "").strip()
                    dev_type = (entry.get("type") or "").strip().lower()

                    raw_loc = entry.get("location")
                    raw_sw  = entry.get("switch_location")
                    location = (raw_sw if (dev_type == "switch" and raw_sw is not None) else raw_loc) or ""
                    location = location.strip()

                    if not dev_id or dev_type not in {"sensor", "switch"}:
                        continue

                    if dev_type == "sensor":
                        # persist + live update
                        doc = sensor_mgr.load(dev_id) or {}
                        prev_location = str(((doc.get("Sensor", {}) or {}).get("LOCATION", "")) or "").strip()
                        if _nodus_values_match(prev_location, location):
                            if DEBUG:
                                printDM(
                                    f"[save_device_locations] sensor {dev_id} unchanged; skipping",
                                    location=MODULE,
                                )
                            continue
                        doc.setdefault("Sensor", {})["LOCATION"] = location
                        sensor_mgr.save(dev_id, doc)
                        updated["sensor"] += 1

                        try:
                            if isinstance(sensor_map, dict):
                                sobj = sensor_map.get(dev_id) or sensor_map.get(dev_id.lower())
                                if sobj is not None:
                                    setattr(sobj, "location", location)
                        except Exception as e:
                            printDM(f"[save_device_locations] sensor live update failed for {dev_id}: {e}",
                                    location=MODULE)

                        try:
                            type(sensor_mgr).invalidate_cache(dev_id, "sensor_settings")
                        except Exception:
                            pass

                        # enqueue remote push if this is a remote Nodus device
                        dev_kind = (sensor_mgr.get_setting(dev_id, "Sensor.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w", "nodus"):
                            sblk = (doc.get("Sensor") or {}) if isinstance(doc, dict) else {}
                            sensor_file_name = None
                            nodus_block = (
                                doc.get("Nodus")
                                if isinstance(doc.get("Nodus"), dict)
                                else {}
                            )
                            sensor_file_name = str(
                                nodus_block.get("CONFIG_FILE") or ""
                            ).strip() or None
                            if any(k in sblk for k in ("I2C_SCL","I2C_SDA","I2C_BUS","I2C_ADDR")):
                                sensor_file_name = sensor_file_name or "sensor_i2c.toml"
                            elif any(k in sblk for k in ("UART_TX","UART_RX","UART_BUS","RS485_DIR_PIN","MODBUS_ADDR")):
                                sensor_file_name = sensor_file_name or "sensor_soil.toml"

                            resolved_host = _read_hostname_from_system_settings(
                                dev_id,
                                system_mgr,
                                system_root,
                                device_type="sensor",
                                sys_host_index=sys_host_index,
                            )
                            if DEBUG:
                                printDM(f"[save_device_locations] sensor {dev_id} resolved host: {resolved_host}", location=MODULE)

                            nodus_tasks.append((
                                {
                                    "id": dev_id,
                                    "type": "sensor",
                                    "location": location,
                                    "target_host": str(resolved_host or dev_id or "").strip(),
                                    "file": sensor_file_name or "",
                                },
                                push_nodus_setting_simple(
                                    device_id=dev_id,
                                    device_type="sensor",
                                    setting_file_key="sensor",
                                    section="Sensor",
                                    key="LOCATION",
                                    value=location,
                                    sensor_file_name=sensor_file_name,
                                    system_mgr=system_mgr,
                                    system_root=system_root,
                                    sys_host_index=sys_host_index,
                                )
                            ))

                        try:
                            topic = f"sensor/{dev_id}/data"
                            if hasattr(services.mqtt_ingest, "device_location") and isinstance(services.mqtt_ingest.device_location, dict):
                                services.mqtt_ingest.device_location[topic] = location or "Unknown"
                        except Exception:
                            pass
                        route_state.sensor_locations.pop(dev_id, None)

                    else:  # switch
                        doc = switch_mgr.load(dev_id) or {}
                        prev_location = str(((doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "")) or "").strip()
                        if _nodus_values_match(prev_location, location):
                            if DEBUG:
                                printDM(
                                    f"[save_device_locations] switch {dev_id} unchanged; skipping",
                                    location=MODULE,
                                )
                            continue
                        doc.setdefault("Switch", {})["SWITCH_LOCATION"] = location
                        switch_mgr.save(dev_id, doc)
                        updated["switch"] += 1

                        try:
                            ctrl = _find_switch_ctrl_by_id(dev_id)
                            if ctrl and getattr(ctrl, "is_present", False):
                                setattr(ctrl, "location", location)
                        except Exception as e:
                            printDM(f"[save_device_locations] switch live update failed for {dev_id}: {e}",
                                    location=MODULE)

                        try:
                            type(switch_mgr).invalidate_cache(dev_id, "switch_settings")
                        except Exception:
                            pass


                        # enqueue remote push if this is a remote Nodus device
                        dev_kind = (switch_mgr.get_setting(dev_id, "Switch.TYPE", "") or "").strip().lower()
                        if dev_kind in ("picow", "pico2w", "nodus"):
                            resolved_host = _read_hostname_from_system_settings(
                                dev_id,
                                system_mgr,
                                system_root,
                                device_type="switch",
                                sys_host_index=sys_host_index,
                            )
                            if DEBUG:
                                printDM(f"[save_device_locations] switch {dev_id} resolved host: {resolved_host}", location=MODULE)

                            # Optional consistency hint: if host looks like 'switch-...' but a paired sensor exists with a different host
                            try:
                                serial = dev_id.rsplit("-", 1)[-1] if "-" in dev_id else dev_id
                                # If sensor_mgr exposes list_ids, check for paired sensor
                                paired_sensor_host = None
                                if hasattr(sensor_mgr, "list_ids"):
                                    for sid in (sensor_mgr.list_ids() or []):
                                        sid = str(sid)
                                        if sid.lower().startswith("switch-"):
                                            continue
                                        if sid.endswith(f"-{serial}"):
                                            paired_sensor_host = _read_hostname_from_system_settings(sid, system_mgr, system_root)
                                            break
                                if DEBUG and resolved_host and resolved_host.startswith("switch-") and paired_sensor_host and paired_sensor_host != resolved_host:
                                    printDM(
                                        f"[save_device_locations] WARNING: switch {dev_id} HOSTNAME='{resolved_host}' differs from paired sensor HOSTNAME='{paired_sensor_host}'. "
                                        f"Update system_settings/{dev_id}/settings.toml [Network].HOSTNAME to the shared Nodus hostname.",
                                        location=MODULE
                                    )
                            except Exception:
                                pass

                            nodus_tasks.append((
                                {
                                    "id": dev_id,
                                    "type": "switch",
                                    "location": location,
                                    "target_host": str(resolved_host or dev_id or "").strip(),
                                    "file": "switch.toml",
                                },
                                push_nodus_setting_simple(
                                    device_id=dev_id,
                                    device_type="switch",
                                    setting_file_key="switch",
                                    section="Switch",
                                    key="SWITCH_LOCATION",
                                    value=location,
                                    sensor_file_name=None,
                                    system_mgr=system_mgr,
                                    system_root=system_root,
                                    sys_host_index=sys_host_index,
                                )
                            ))

                    if DEBUG:
                        printDM(f"dev_type: {dev_type}, dev_id: {dev_id}, dev_loc: {location}",
                                location=f"{MODULE}.save_device_locations")

                except Exception as row_err:
                    printDM(f"[save_device_locations] row error: {row_err}", location=MODULE)

            _invalidate_dashboard_caches()

            # Run all remote pushes
            remote_failures = 0
            remote_results: list[dict[str, Any]] = []
            if nodus_tasks:
                try:
                    async def _run_nodus_push(meta: dict[str, Any], coro):
                        try:
                            ok = await coro
                            result = dict(meta)
                            result["ok"] = bool(ok is True)
                            if not result["ok"]:
                                result["error"] = "remote_apply_failed"
                            return result
                        except Exception as exc:
                            printDM(f"[save_device_locations] nodus push failed: {exc}", location=MODULE)
                            result = dict(meta)
                            result["ok"] = False
                            result["error"] = "remote_apply_failed"
                            return result

                    results = await asyncio.gather(
                        *[_run_nodus_push(meta, coro) for meta, coro in nodus_tasks],
                        return_exceptions=False,
                    )
                    remote_results = [dict(r) for r in results]
                    pushed = sum(1 for r in remote_results if r.get("ok") is True)
                    updated["nodus_pushed"] = pushed
                    remote_failures = max(len(remote_results) - pushed, 0)
                    if DEBUG:
                        failures = [r for r in remote_results if r.get("ok") is not True]
                        if failures:
                            printDM(f"[save_device_locations] nodus push failures: {failures}", location=MODULE)
                except Exception as e:
                    if DEBUG:
                        printDM(f"[save_device_locations] nodus push gather error: {e}", location=MODULE)
                    remote_failures = len(nodus_tasks)
                    remote_results = [
                        {
                            **meta,
                            "ok": False,
                            "error": "remote_apply_failed",
                        }
                        for meta, _coro in nodus_tasks
                    ]

            if remote_failures:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "nodus_remote_apply_failed",
                        "updated": updated,
                        "results": remote_results,
                    },
                    status_code=502,
                )
            return JSONResponse({"ok": True, "updated": updated, "results": remote_results})

        except Exception as e:
            printDM(f"Failed to save device locations: {e}", location=f"{MODULE}.save_device_locations")
            return JSONResponse({"error": "failed"}, status_code=500)


    def _normalize_dev_id(name: str | None) -> str | None:
        base = normalize_hostname_base(name)
        return base or None


    def _collect_ingest_ids() -> list[str]:
        """
        Aggregate IDs from MQTT discovery so the remove list reflects in-memory state.
        """
        ing = services.mqtt_ingest
        if not ing:
            return []

        ids: set[str] = set()
        try:
            ids.update(ing.get_known_devices() or [])
        except Exception:
            pass
        try:
            ids.update(ing.get_known_switch_devices() or [])
        except Exception:
            pass
        # Intentionally exclude raw mqtt_clients / host_to_peer_ids here.
        # Those caches can retain stale aliases and peer IDs after deletion, which
        # repopulates "Remove Device" with ghost entries.

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out


    def _enumerate_dirs(base_dir:str)->list[str]:
        try:
            p=_settings_base_path(base_dir)
            if not p.exists(): return []
            return [d.name for d in p.iterdir() if d.is_dir()]
        except Exception:
            return []


    def _collect_settings_ids() -> list[str]:
        ids: set[str] = set()
        try:
            sensor_mgr = SensorSettingsManager("sensor_settings")
            ids.update(sensor_mgr.list_ids() or [])
        except Exception:
            pass
        try:
            switch_mgr = SwitchSettingsManager("switch_settings")
            ids.update(switch_mgr.list_switches() or [])
        except Exception:
            pass
        try:
            ids.update(_enumerate_dirs(_SYS_BASE_DIR))
        except Exception:
            pass

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out


    def _collect_db_ids() -> list[str]:
        ids: set[str] = set()
        try:
            ids.update(services.data_logger.get_available_sensors() or [])
        except Exception:
            pass
        try:
            for row in (services.data_logger.get_switch_identities() or []):
                switch_id = str((row or {}).get("switch_id") or "").strip()
                if switch_id:
                    ids.add(switch_id)
        except Exception:
            pass

        out: list[str] = []
        seen: set[str] = set()
        for raw in ids:
            norm = _normalize_dev_id(raw)
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
        return out


    def _persisted_nodus_sensor_groups() -> tuple[dict[str, str], dict[str, list[str]]]:
        """Read child-to-physical Nodus identity from persisted sensor shadows."""
        child_hosts: dict[str, str] = {}
        host_children: dict[str, list[str]] = {}
        try:
            sensor_mgr = SensorSettingsManager("sensor_settings")
            for raw_sensor_id in sensor_mgr.list_ids() or []:
                sensor_id = _normalize_dev_id(raw_sensor_id)
                if not sensor_id:
                    continue
                try:
                    doc = sensor_mgr.load(raw_sensor_id) or {}
                except Exception:
                    continue
                nodus = doc.get("Nodus") if isinstance(doc.get("Nodus"), dict) else {}
                host = _normalize_dev_id(nodus.get("DEVICE_ID"))
                if not host:
                    continue
                child_hosts[sensor_id] = host
                children = host_children.setdefault(host, [])
                if sensor_id not in children:
                    children.append(sensor_id)
        except Exception:
            pass
        return child_hosts, host_children


    def _collect_removable_ids() -> list[str]:
        """
        Aggregate IDs that are candidates for removal from:
          - MQTT discovery (in-memory)
          - sensor, switch, and system settings
          - database-backed sensor and switch identities

        Excludes:
          - our own hub host folder
          - special folders like '__pycache__'
          - 'factory' metadata folders
        """
        ids: set[str] = set(_collect_ingest_ids())
        ids.update(_collect_settings_ids())
        ids.update(_collect_db_ids())

        # do not list hub host folder (our own hostname)
        try:
            hub_name = Path(get_hub_settings_path()).parent.name
            if hub_name:
                ids.discard(hub_name)
                ids.discard(_normalize_dev_id(hub_name) or hub_name)
        except Exception:
            pass

        # filter out known non-removable / meta folders
        banned = {"__pycache__", "factory"}
        filtered_ids = [
            dev_id
            for dev_id in ids
            if dev_id
            and dev_id.lower() not in banned
            and not dev_id.lower().startswith("factory")
            and not dev_id.lower().startswith("template")
        ]

        ingest = services.mqtt_ingest
        persisted_hosts, _ = _persisted_nodus_sensor_groups()
        sensor_hosts = dict(persisted_hosts)
        if ingest:
            sensor_hosts.update(getattr(ingest, "nodus_sensor_hosts", {}) or {})
        collapsed: set[str] = set()
        for dev_id in filtered_ids:
            physical = str(sensor_hosts.get(dev_id) or "").strip()
            collapsed.add(physical or dev_id)
        filtered_ids = list(collapsed)

        return sorted(filtered_ids)


    def _collect_removable_details(device_ids: list[str]) -> dict[str, dict[str, Any]]:
        ingest = services.mqtt_ingest

        _, persisted_groups = _persisted_nodus_sensor_groups()
        now_ts = time.time()
        details: dict[str, dict[str, Any]] = {}

        def _resolved_host(dev_id: str) -> str:
            base = normalize_hostname_base(dev_id) or str(dev_id or "").strip()
            if not ingest:
                return base
            resolver = getattr(ingest, "resolve_nodus_hostname", None)
            if callable(resolver):
                try:
                    resolved = resolver(base, device_type="switch" if base.startswith("switch-") else None)
                    host = normalize_hostname_base(str(resolved or ""))
                    if host:
                        return host
                except Exception:
                    pass
            return base

        def _last_seen_seconds(dev_id: str, host: str) -> float | None:
            latest = 0.0
            if ingest:
                seen = getattr(ingest, "last_mqtt_seen", {}) or {}
                candidates = {dev_id, host}
                base = normalize_hostname_base(host)
                if base:
                    candidates.add(base)
                    candidates.add(mdns_hostname(base))
                try:
                    for peer in (getattr(ingest, "host_to_peer_ids", {}) or {}).get(base or host, []) or []:
                        peer_base = normalize_hostname_base(str(peer or ""))
                        if peer_base:
                            candidates.add(peer_base)
                            candidates.add(mdns_hostname(peer_base))
                except Exception:
                    pass
                for key in candidates:
                    try:
                        latest = max(latest, float(seen.get(key, 0.0) or 0.0))
                    except Exception:
                        continue
                if latest:
                    return round(max(now_ts - latest, 0.0), 1)

            if _is_switch_id(dev_id):
                return None

            try:
                logger = _active_data_logger()
                last_packet = getattr(logger, "get_sensor_last_packet_epoch", None)
                if callable(last_packet):
                    aliases = [dev_id]
                    base = normalize_hostname_base(dev_id)
                    if base:
                        aliases.extend([base, mdns_hostname(base)])
                    epoch = last_packet(dev_id, aliases=aliases)
                    if epoch is not None:
                        return round(max(now_ts - float(epoch), 0.0), 1)
            except Exception:
                pass
            return None

        def _device_url(host: str) -> str:
            if not host:
                return ""
            ip = ""
            if ingest:
                base = normalize_hostname_base(host) or host
                for attr in ("_host_ipv4addr", "_host_ip_cache"):
                    try:
                        mapping = getattr(ingest, attr, {}) or {}
                        ip = str(mapping.get(base) or mapping.get(host) or mapping.get(mdns_hostname(base)) or "").strip()
                        if ip:
                            break
                    except Exception:
                        pass
            return f"http://{ip or mdns_hostname(host)}:8000"

        for raw in device_ids:
            dev_id = normalize_hostname_base(raw) or str(raw or "").strip()
            if not dev_id:
                continue
            host = _resolved_host(dev_id)
            details[dev_id] = {
                "url": _device_url(host),
                "last_seen_s": _last_seen_seconds(dev_id, host),
            }
            children = list(persisted_groups.get(host, []) or [])
            if ingest:
                live_children = (
                    (getattr(ingest, "nodus_host_sensors", {}) or {}).get(host, [])
                    or []
                )
                for child in live_children:
                    if child not in children:
                        children.append(child)
            if children:
                details[dev_id]["sensors"] = children
        return details


    def _toml_escape(s:str)->str:
        return s.replace("\\","\\\\").replace('"','\\"')


    def _toml_join_list(str_list:list[str])->str:
        inner=", ".join(f"\"{_toml_escape(s)}\"" for s in str_list)
        return f"[{inner}]"


    def _remove_client_from_hub_settings(device_id:str)->bool:
        # Clients list is no longer authoritative; nothing to remove.
        return True


    def _collect_related_device_ids(device_id: str, *, mqtt_ingest=None) -> list[str]:
        """
        Expand a remove target into related Nodus IDs (host + peer switch IDs).
        This ensures remove-device purges retained topics for the full Nodus identity set.
        """
        out: list[str] = []
        seen: set[str] = set()

        def _add(raw: str | None) -> None:
            val = str(raw or "").strip()
            if not val:
                return
            key = val.lower()
            if key in seen:
                return
            seen.add(key)
            out.append(val)

        def _alias_set(raw: str | None) -> set[str]:
            val = str(raw or "").strip()
            aliases: set[str] = set()
            if not val:
                return aliases
            aliases.add(val.lower())
            base = normalize_hostname_base(val)
            if base:
                aliases.add(base.lower())
                aliases.add(mdns_hostname(base).lower())
            return aliases

        def _add_nodus_suffix_peers(raw: str | None) -> None:
            """
            Nodus switch and channel IDs are derived from the same short suffix
            as the host sensor ID. If in-memory host_to_peer_ids is already gone,
            use that suffix to clear retained host, switch, and channel topics
            that could reseed the removed device.
            """
            val = str(raw or "").strip()
            if not val or "-" not in val:
                return
            prefix_l = val.split("-", 1)[0].lower()
            suffix = val.rsplit("-", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9]{5,32}", suffix):
                return
            sensor_prefixes = ("apvpd", "avpd", "aqi", "aht", "aht10", "ahtx0", "co2", "lux", "veml", "soil")
            is_switch_peer = prefix_l == "switch" or re.fullmatch(r"s\d+", prefix_l, flags=re.IGNORECASE)
            is_sensor_peer = prefix_l in sensor_prefixes
            if is_sensor_peer:
                _add(f"switch-{suffix}")
                for idx in range(1, 9):
                    _add(f"S{idx}-{suffix}")
                return
            if not is_switch_peer:
                return
            for sensor_prefix in sensor_prefixes:
                _add(f"{sensor_prefix}-{suffix}")
            _add(f"switch-{suffix}")
            for idx in range(1, 9):
                _add(f"S{idx}-{suffix}")

        _add(device_id)
        _add_nodus_suffix_peers(device_id)
        if not mqtt_ingest:
            return out

        wanted = _alias_set(device_id)
        try:
            norm = getattr(mqtt_ingest, "_normalize_host_key", None)
            if callable(norm):
                host_base = norm(device_id)
                if host_base:
                    wanted |= _alias_set(host_base)
                    _add(host_base)
        except Exception:
            pass

        mapping = getattr(mqtt_ingest, "host_to_peer_ids", None)
        if not isinstance(mapping, dict):
            mapping = {}

        for host, peers in list(mapping.items()):
            host_aliases = _alias_set(host)
            peer_aliases: set[str] = set()
            for peer in (peers or []):
                peer_aliases |= _alias_set(peer)
            if wanted & (host_aliases | peer_aliases):
                _add(host)
                for peer in (peers or []):
                    _add(peer)

        # Also include per-channel IDs (e.g. "S1-xxxxxx") tied to related
        # switch hosts so DB/topic cleanup can purge channel-keyed rows.
        for related in list(out):
            _add_nodus_suffix_peers(related)
            try:
                for row in (_collect_switch_channels(related, mqtt_ingest=mqtt_ingest) or []):
                    _add(str((row or {}).get("channel_id") or "").strip())
            except Exception:
                pass

        return out


    def _collect_switch_channels(device_id: str, mqtt_ingest=None) -> list[dict]:
        """
        Return switch channel dicts: [{"channel_id": "...", "label": "..."}].
        """
        channels: list[dict] = []
        try:
            mgr = SwitchSettingsManager("switch_settings")
            doc = mgr.load(device_id) or {}
            sw = doc.get("Switch", {}) or {}
            for key, val in sw.items():
                if not isinstance(key, str) or not key.startswith("SWITCH_"):
                    continue
                m = re.fullmatch(r"SWITCH_(\d+)_LABEL", str(key))
                if not m:
                    continue
                label = str(val or "").strip()
                suffix = m.group(1)
                id_key = f"SWITCH_{suffix}_CHANNEL_ID"
                channel_id = str(sw.get(id_key, "") or "").strip()
                if channel_id:
                    channels.append({"channel_id": channel_id, "label": label})
        except Exception:
            pass

        if mqtt_ingest and not channels:
            try:
                for info in (mqtt_ingest.nodus_switch_topic_map or {}).values():
                    if info and info.get("switch_id") == device_id:
                        ch_id = str(info.get("channel_id") or "").strip()
                        label = str(info.get("label") or "").strip()
                        if ch_id:
                            channels.append({"channel_id": ch_id, "label": label})
            except Exception:
                pass
            try:
                for (sw_id, ch_id), _topic in (mqtt_ingest.nodus_switch_command_topics or {}).items():
                    if sw_id == device_id and ch_id:
                        channels.append({"channel_id": ch_id, "label": ""})
            except Exception:
                pass

        # de-dupe, preserve order
        seen: set[str] = set()
        out: list[dict] = []
        for entry in channels:
            ch_id = str(entry.get("channel_id", "") or "").strip()
            if ch_id and ch_id not in seen:
                seen.add(ch_id)
                out.append(entry)
        return out


    def _collect_sensor_metrics(device_id: str, data_logger, mqtt_ingest=None) -> list[str]:
        metrics: list[str] = []
        try:
            from .saiSensorSettingsManager import SensorSettingsManager
            mgr = SensorSettingsManager("sensor_settings")
            metrics = mgr.get_display_metrics(device_id) or []
        except Exception:
            metrics = []

        if not metrics and mqtt_ingest:
            try:
                metrics = list(mqtt_ingest.expected_gauge_map.get(device_id) or [])
            except Exception:
                metrics = []

        if not metrics and data_logger:
            try:
                latest = data_logger.get_latest_values(device_id) or {}
                metrics = list(latest.keys())
            except Exception:
                metrics = []

        # de-dupe, preserve order, drop blanks
        seen: set[str] = set()
        out: list[str] = []
        for m in metrics:
            val = str(m or "").strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
        return out


    def _clear_ha_entities(device_id: str, *, mqtt_ingest=None, data_logger=None) -> dict:
        stats = {"topics_cleared": 0, "ids_expanded": []}
        if not mqtt_ingest:
            return stats
        try:
            from .saiHomeAssistantMqtt import slugify, HomeAssistantTopicMap
        except Exception:
            return stats

        topic_map = getattr(mqtt_ingest, "topic_map", None)
        if not topic_map:
            try:
                node_id = socket.gethostname()
                base_topic = getattr(mqtt_ingest, "base_topic", "sensorius")
                topic_map = HomeAssistantTopicMap(node_id=node_id, base_topic=base_topic, discovery_prefix="homeassistant")
            except Exception:
                topic_map = None
        if not topic_map:
            return stats

        topics: list[str] = []
        ids = _collect_related_device_ids(device_id, mqtt_ingest=mqtt_ingest)
        stats["ids_expanded"] = ids
        for dev_id in ids:
            metrics = _collect_sensor_metrics(dev_id, data_logger, mqtt_ingest)
            channels = _collect_switch_channels(dev_id, mqtt_ingest)
            for metric in metrics:
                object_id = f"{dev_id}__{slugify(metric)}"
                topics.append(topic_map.sensor_discovery_topic(object_id))
            for ch in channels:
                ch_id = str(ch.get("channel_id") or "").strip()
                label = str(ch.get("label") or "").strip()
                if ch_id:
                    topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{ch_id}"))
                if label:
                    topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{slugify(label)}"))

        if not topics:
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_sensor_metrics", None) or set()
                for key in list(known):
                    for dev_id in ids:
                        if key.startswith(f"{dev_id}::"):
                            metric_slug = key.split("::", 1)[1]
                            topics.append(topic_map.sensor_discovery_topic(f"{dev_id}__{metric_slug}"))
            except Exception:
                pass
            try:
                known = getattr(mqtt_ingest, "_ha_discovered_switch_channels", None) or set()
                for key in list(known):
                    for dev_id in ids:
                        if key.startswith(f"{dev_id}::"):
                            ch_id = key.split("::", 1)[1]
                            topics.append(topic_map.switch_discovery_topic(f"{dev_id}__{ch_id}"))
            except Exception:
                pass

        if not topics:
            return stats

        client = mqtt_ingest.ha_client or mqtt_ingest.client
        for topic in topics:
            try:
                info = client.publish(topic, payload="", qos=0, retain=True)
                if getattr(info, "rc", 0) == 0:
                    stats["topics_cleared"] += 1
            except Exception:
                pass

        return stats


    def _clear_retained_mqtt_topics(device_id: str, *, mqtt_ingest=None) -> dict:
        stats = {"topics_cleared": 0, "ids_expanded": []}
        if not mqtt_ingest:
            return stats
        client = mqtt_ingest.client
        base_topic = getattr(mqtt_ingest, "base_topic", "") or ""
        ids = _collect_related_device_ids(device_id, mqtt_ingest=mqtt_ingest)
        ids_l = {i.lower() for i in ids}
        stats["ids_expanded"] = ids

        topics: set[str] = set()
        try:
            for topic, dev in (mqtt_ingest.topic_dev_id_map or {}).items():
                if str(dev or "").strip().lower() in ids_l and topic:
                    topics.add(topic)
        except Exception:
            pass
        for dev_id in ids:
            for suffix in ("data", "state", "availability"):
                topics.add(f"nodus/{dev_id}/{suffix}")
                topics.add(f"switch/{dev_id}/{suffix}")
                if base_topic:
                    topics.add(f"{base_topic}/nodus/{dev_id}/{suffix}")
                    topics.add(f"{base_topic}/switch/{dev_id}/{suffix}")
            for suffix in (
                "meta",
                "meta/switch",
                "meta/patch",
                "status/heartbeat",
                "config/ack",
                "config/result",
                "fwupdate/result",
                "event/calibration_status",
                "event/calibration_progress",
                "event/calibration_sample",
                "event/calibration_result",
            ):
                topics.add(f"nodus/{dev_id}/{suffix}")
                if base_topic:
                    topics.add(f"{base_topic}/nodus/{dev_id}/{suffix}")
        try:
            for topic_map_name in (
                "nodus_switch_command_topics",
                "nodus_switch_state_topics",
                "nodus_switch_event_topics",
                "nodus_switch_ack_topics",
                "nodus_switch_result_topics",
                "nodus_switch_availability_topics",
            ):
                for (sw_id, _ch_id), topic in (getattr(mqtt_ingest, topic_map_name, {}) or {}).items():
                    if str(sw_id or "").strip().lower() in ids_l and topic:
                        topics.add(topic)
        except Exception:
            pass
        try:
            for topic, meta in (mqtt_ingest.nodus_switch_topic_map or {}).items():
                sw_id = str((meta or {}).get("switch_id") or "").strip().lower()
                if sw_id in ids_l and topic:
                    topics.add(topic)
        except Exception:
            pass
        try:
            for (sw_id, _label), base in (getattr(mqtt_ingest, "_set_base_by_label", {}) or {}).items():
                if str(sw_id or "").strip().lower() in ids_l and base:
                    topics.add(f"{base}/set")
        except Exception:
            pass

        for topic in topics:
            try:
                info = client.publish(topic, payload="", qos=0, retain=True)
                if getattr(info, "rc", 0) == 0:
                    stats["topics_cleared"] += 1
            except Exception:
                pass
        return stats


    def _purge_ingest_cache(device_id: str, *, mqtt_ingest=None, data_logger=None) -> dict:
        stats = {"ingest_keys_cleared": 0}
        ing = mqtt_ingest
        if not ing:
            return stats

        def _bump():
            stats["ingest_keys_cleared"] += 1

        ids = _collect_related_device_ids(device_id, mqtt_ingest=ing)
        ids_l = {str(i or "").strip().lower() for i in ids if str(i or "").strip()}
        expanded_keys: set[str] = set()
        for dev_id in ids:
            try:
                base = ing._normalize_host_key(dev_id) if hasattr(ing, "_normalize_host_key") else dev_id
            except Exception:
                base = dev_id
            for key in (dev_id, base, mdns_hostname(base)):
                if key:
                    expanded_keys.add(key)

        for key in expanded_keys:
            if not key:
                continue
            for dname in (
                "device_type",
                "expected_gauge_map",
                "latest_meta",
                "fwupdate_result_by_device",
                "calibration_status_by_sensor",
                "calibration_progress_by_sensor",
                "calibration_sample_by_sensor",
                "calibration_event_result_by_sensor",
                "nodus_firmware_versions",
                "nodus_board_types",
                "nodus_sensor_hardware",
                "_host_ip_cache",
                "_host_ipv4addr",
            ):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            for dname in ("device_status", "last_mqtt_seen", "last_nodus_report_seen", "retained_mqtt_seen", "nodus_availability", "last_heartbeat_ts", "last_heartbeat_payload", "heartbeat_interval_s_by_host", "heartbeat_stale"):
                d = getattr(ing, dname, None)
                if isinstance(d, dict) and key in d:
                    d.pop(key, None); _bump()
            s = getattr(ing, "mqtt_clients", None)
            if isinstance(s, set) and key in s:
                s.discard(key); _bump()
            d = getattr(ing, "discovery_cache", None)
            if isinstance(d, dict) and key in d:
                d.pop(key, None); _bump()

        # Remove from topic maps keyed by device_id
        try:
            for topic, dev in list((ing.topic_dev_id_map or {}).items()):
                if str(dev or "").strip().lower() in ids_l:
                    ing.topic_dev_id_map.pop(topic, None); _bump()
        except Exception:
            pass

        def _topic_matches_removed_id(topic: str) -> bool:
            parts = [str(part or "").strip().lower() for part in str(topic or "").split("/")]
            return bool(set(parts) & ids_l)

        try:
            locations = getattr(ing, "device_location", None)
            if isinstance(locations, dict):
                for topic in list(locations.keys()):
                    if _topic_matches_removed_id(topic):
                        locations.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            registered = getattr(ing, "registered_topics", None)
            if isinstance(registered, set):
                for topic in list(registered):
                    if "+" not in str(topic) and "#" not in str(topic) and _topic_matches_removed_id(topic):
                        registered.discard(topic); _bump()
        except Exception:
            pass

        try:
            for topic, meta in list((ing.switch_topic_meta or {}).items()):
                if str((meta or {}).get("switch_id") or "").strip().lower() in ids_l:
                    ing.switch_topic_meta.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_control_map or {}).keys()):
                if key and str((key[0] if len(key) > 0 else "") or "").strip().lower() in ids_l:
                    ing.switch_control_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for key in list((ing.switch_channel_map or {}).keys()):
                if key and str((key[0] if len(key) > 0 else "") or "").strip().lower() in ids_l:
                    ing.switch_channel_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            for topic, info in list((ing.nodus_switch_topic_map or {}).items()):
                if str((info or {}).get("switch_id") or "").strip().lower() in ids_l:
                    ing.nodus_switch_topic_map.pop(topic, None); _bump()
        except Exception:
            pass

        try:
            for map_name in (
                "nodus_switch_command_topics",
                "nodus_switch_state_topics",
                "nodus_switch_event_topics",
                "nodus_switch_availability_topics",
                "nodus_switch_ack_topics",
                "nodus_switch_result_topics",
            ):
                topic_map = getattr(ing, map_name, None)
                if not isinstance(topic_map, dict):
                    continue
                for key in list(topic_map.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel_id = str((key[1] if isinstance(key, tuple) and len(key) > 1 else "") or "").strip().lower()
                    if switch_id in ids_l or channel_id in ids_l:
                        topic_map.pop(key, None); _bump()
        except Exception:
            pass

        try:
            channel_topics = getattr(ing, "nodus_channel_command_topics", None)
            if isinstance(channel_topics, dict):
                for channel_id in list(channel_topics.keys()):
                    if str(channel_id or "").strip().lower() in ids_l:
                        channel_topics.pop(channel_id, None); _bump()
        except Exception:
            pass

        try:
            sensor_topics = getattr(ing, "nodus_sensor_topics", None)
            if isinstance(sensor_topics, dict):
                for sensor_id in list(sensor_topics.keys()):
                    if str(sensor_id or "").strip().lower() in ids_l:
                        sensor_topics.pop(sensor_id, None); _bump()
        except Exception:
            pass

        for map_name in ("nodus_sensor_hosts", "nodus_sensor_config_files"):
            try:
                mapping = getattr(ing, map_name, None)
                if isinstance(mapping, dict):
                    for sensor_id in list(mapping.keys()):
                        if str(sensor_id or "").strip().lower() in ids_l:
                            mapping.pop(sensor_id, None)
                            _bump()
            except Exception:
                pass
        try:
            mapping = getattr(ing, "nodus_host_sensors", None)
            if isinstance(mapping, dict):
                for host in list(mapping.keys()):
                    host_l = str(host or "").strip().lower()
                    children = {
                        str(child or "").strip().lower()
                        for child in (mapping.get(host) or [])
                    }
                    if host_l in ids_l or children & ids_l:
                        mapping.pop(host, None)
                        _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_known_switch_ids", None)
            if isinstance(s, set):
                for sid in list(s):
                    if str(sid or "").strip().lower() in ids_l:
                        s.discard(sid); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_switch_state_cache", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    if str(key or "").strip().lower() in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_pending_set", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    sid = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    if sid in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "_set_base_by_label", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    if switch_id in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            d = getattr(ing, "nodus_label_to_channel", None)
            if isinstance(d, dict):
                for key in list(d.keys()):
                    sid = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel = str(d.get(key) or "").strip().lower()
                    if sid in ids_l or channel in ids_l:
                        d.pop(key, None); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_sensor_metrics", None)
            if isinstance(s, set):
                for key in list(s):
                    key_l = str(key or "").strip().lower()
                    if any(key_l.startswith(f"{dev}::") for dev in ids_l):
                        s.discard(key); _bump()
        except Exception:
            pass

        try:
            s = getattr(ing, "_ha_discovered_switch_channels", None)
            if isinstance(s, set):
                for key in list(s):
                    key_l = str(key or "").strip().lower()
                    if any(key_l.startswith(f"{dev}::") for dev in ids_l):
                        s.discard(key); _bump()
        except Exception:
            pass

        try:
            mapping = getattr(ing, "host_to_peer_ids", None)
            if isinstance(mapping, dict):
                for dev_id in ids:
                    dev_base = normalize_hostname_base(dev_id) or dev_id
                    if dev_base in mapping:
                        mapping.pop(dev_base, None); _bump()
                for host, peers in list(mapping.items()):
                    peer_set_l = {str(p or "").strip().lower() for p in (peers or [])}
                    if peer_set_l & {i.lower() for i in ids}:
                        peers[:] = [p for p in peers if str(p or "").strip().lower() not in {i.lower() for i in ids}]
                        _bump()
        except Exception:
            pass

        for set_name in ("_live_sensor_shadow_seeded", "_dashboard_inventory_notified_sensors"):
            try:
                values = getattr(ing, set_name, None)
                if isinstance(values, set):
                    for value in list(values):
                        if str(value or "").strip().lower() in ids_l:
                            values.discard(value); _bump()
            except Exception:
                pass

        try:
            attempts = getattr(ing, "_live_sensor_shadow_attempt_at", None)
            if isinstance(attempts, dict):
                for key in list(attempts.keys()):
                    if str(key or "").strip().lower() in ids_l:
                        attempts.pop(key, None); _bump()
        except Exception:
            pass

        try:
            persisted = getattr(ing, "_last_persisted_switch_state", None)
            if isinstance(persisted, dict):
                for key in list(persisted.keys()):
                    switch_id = str((key[0] if isinstance(key, tuple) and key else "") or "").strip().lower()
                    channel_id = str((key[1] if isinstance(key, tuple) and len(key) > 1 else "") or "").strip().lower()
                    if switch_id in ids_l or channel_id in ids_l:
                        persisted.pop(key, None); _bump()
        except Exception:
            pass

        try:
            if data_logger:
                for dev_id in ids:
                    if dev_id in data_logger.sensor_values:
                        data_logger.sensor_values.pop(dev_id, None); _bump()
                    if dev_id in getattr(data_logger, "sensor_stats", {}):
                        data_logger.sensor_stats.pop(dev_id, None); _bump()
        except Exception:
            pass

        return stats


    def _purge_switch_controller_state(device_ids: list[str]) -> dict:
        stats = {"switch_controllers_cleared": 0, "switch_monitors_cancelled": 0}
        ids_l = {str(i or "").strip().lower() for i in (device_ids or []) if str(i or "").strip()}
        if not ids_l:
            return stats

        def _purge_map(sc_map) -> None:
            if not isinstance(sc_map, dict):
                return
            for key, ctrl in list(sc_map.items()):
                key_l = str(key or "").strip().lower()
                sid_l = str(getattr(ctrl, "switch_id", "") or "").strip().lower()
                channel_ids = set()
                try:
                    channel_ids.update(str(v or "").strip().lower() for v in (getattr(ctrl, "channel_id_for_label", {}) or {}).values())
                except Exception:
                    pass
                if key_l in ids_l or sid_l in ids_l or bool(channel_ids & ids_l):
                    sc_map.pop(key, None)
                    stats["switch_controllers_cleared"] += 1

        _purge_map(services.switch_controllers)

        for key, task in list(route_state.dynamic_switch_monitor_tasks.items()):
            if str(key or "").strip().lower() not in ids_l:
                continue
            route_state.dynamic_switch_monitor_tasks.pop(key, None)
            try:
                if task and not task.done():
                    task.cancel()
                stats["switch_monitors_cancelled"] += 1
            except Exception:
                pass

        return stats


    def _delete_device_dirs(device_id:str)->dict:
        removed={"sensor":False,"switch":False,"system":False, "ids_deleted":[]}
        targets = [
            ("sensor", _safe_child_path(_settings_base_path(_SENSOR_BASE_DIR), device_id)),
            ("switch", _safe_child_path(_settings_base_path(_SWITCH_BASE_DIR), device_id)),
            ("system", _safe_child_path(_settings_base_path(_SYS_BASE_DIR), device_id)),
        ]
        for key, path in targets:
            try:
                if path is None:
                    continue
                if path.exists():
                    shutil.rmtree(path)
                    removed[key]=True
                    removed["ids_deleted"].append(device_id)
                    if key == "sensor":
                        SensorSettingsManager.invalidate_cache(device_id, str(_settings_base_path(_SENSOR_BASE_DIR)))
                    elif key == "switch":
                        SwitchSettingsManager.invalidate_cache(device_id, str(_settings_base_path(_SWITCH_BASE_DIR)))
            except Exception as e:
                printDM(f"[remove-device] rmtree {path}: {e}", location=MODULE)
        return removed


    def _delete_device_dirs_many(device_ids: list[str])->dict:
        removed={"sensor":False,"switch":False,"system":False, "ids_deleted":[]}
        seen: set[str] = set()
        for did in device_ids:
            dev = str(did or "").strip()
            if not dev or dev.lower() in seen:
                continue
            seen.add(dev.lower())
            one = _delete_device_dirs(dev)
            for key in ("sensor", "switch", "system"):
                removed[key] = bool(removed.get(key)) or bool(one.get(key))
            for deleted_id in (one.get("ids_deleted") or []):
                if deleted_id not in removed["ids_deleted"]:
                    removed["ids_deleted"].append(deleted_id)
        return removed


    def _active_data_logger():
        return services.data_logger


    def _get_db_path()->str:
        try:
            active_logger = _active_data_logger()
            db_path = str(getattr(active_logger, "db_path", "") or "").strip()
            if db_path:
                return db_path
        except Exception:
            pass
        try:
            from .saiDataLogger import saiDataLogger  # type: ignore
            if hasattr(saiDataLogger,"DB_PATH"): return getattr(saiDataLogger,"DB_PATH")
            if hasattr(saiDataLogger,"get_db_path"): return saiDataLogger.get_db_path()  # type: ignore
        except Exception:
            pass
        if Path("sensorius_data.db").exists(): return "sensorius_data.db"
        if Path("data/sensorius_data.db").exists(): return "data/sensorius_data.db"
        return "sensorius_data.db"


    def _table_has_column(cur:sqlite3.Cursor, table:str, column:str)->bool:
        try:
            cur.execute(f'PRAGMA table_info("{table}")')
            return column in [r[1] for r in cur.fetchall()]
        except Exception:
            return False


    def _purge_device_from_db(device_id:str)->dict:
        db_path=_get_db_path()
        stats={"db_path":db_path,"rows_deleted":0,"tables":[]}
        purged_tables: set[str] = set()
        try:
            active_logger = _active_data_logger()
            purge_sensor_data = getattr(active_logger, "purge_sensor_data", None)
            if callable(purge_sensor_data):
                sensor_stats = purge_sensor_data(device_id)
                stats["rows_deleted"] += int(sensor_stats.get("rows_deleted", 0) or 0)
                for entry in (sensor_stats.get("tables", []) or []):
                    if isinstance(entry, dict):
                        purged_tables.update(str(k) for k in entry.keys())
                    stats["tables"].append(entry)
                logger_db_path = str(getattr(active_logger, "db_path", "") or "").strip()
                if logger_db_path:
                    db_path = logger_db_path
                    stats["db_path"] = logger_db_path
        except Exception as e:
            printDM(f"[remove-device] active logger sensor purge failed: {e}", location=MODULE)
        if not Path(db_path).exists(): return stats
        try:
            conn = _sqlite_connect_with_recovery(db_path, source="remove_device_purge")
            cur=conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables=[r[0] for r in cur.fetchall()]
            target_cols=["sensor_id","device_id","client_id","switch_id","switch_key"]
            like_cols=["topic","source","channel","switch_key"]
            for t in tables:
                if t in purged_tables:
                    continue
                deleted=0
                for col in target_cols:
                    if _table_has_column(cur,t,col):
                        cur.execute(f'DELETE FROM "{t}" WHERE "{col}"=?',(device_id,))
                        deleted+=cur.rowcount
                for col in like_cols:
                    if _table_has_column(cur,t,col):
                        cur.execute(f'DELETE FROM "{t}" WHERE "{col}" LIKE ?',(f"%{device_id}%",))
                        deleted+=cur.rowcount
                if deleted:
                    stats["tables"].append({t:deleted})
                    stats["rows_deleted"]+=deleted
            conn.commit(); conn.close()
        except Exception as e:
            printDM(f"[remove-device] DB purge failed: {e}", location=MODULE)
        return stats


    def build_sensor_locations_map() -> dict[str, str]:
        from .saiSensorSettingsManager import SensorSettingsManager
        mgr = SensorSettingsManager("sensor_settings")
        locations: dict[str, str] = {}
        for sid in mgr.list_ids():
            loc = mgr.get_setting(sid, "Sensor.LOCATION", "Unknown")
            locations[sid] = loc or "Unknown"
        return locations


    @router.get("/halt-device-list")
    async def halt_device_list(request: Request):
        """List physical devices with explicit MQTT halt capability."""
        _require_protected_access(request, require_csrf=True)
        from .saiNodusHalt import halt_device_ids
        ingest = services.mqtt_ingest
        devices = [device for device in halt_device_ids(ingest) if _is_valid_device_id(device)]
        return JSONResponse({"devices": devices}, headers={"Cache-Control": "no-store"})


    @router.post("/halt-device")
    async def halt_device_post(request: Request):
        """Halt acknowledged selections without removing their settings or data."""
        _require_protected_access(request, require_csrf=True)
        from .saiNodusHalt import halt_device_ids, halt_device
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict) or body.get("acknowledged") is not True:
            return JSONResponse({"error": "Halt acknowledgement required"}, status_code=400)
        ids = body.get("device_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(d, str) or not _is_valid_device_id(d) for d in ids):
            return JSONResponse({"error": "Invalid device_ids"}, status_code=400)
        ids = list(dict.fromkeys(ids))
        ingest = services.mqtt_ingest
        supported = set(halt_device_ids(ingest))
        if any(d not in supported for d in ids):
            return JSONResponse({"error": "Device does not advertise halt support"}, status_code=400)
        if not all(callable(getattr(ingest, name, None)) for name in ("publish_nodus_halt", "wait_for_config_result")):
            return JSONResponse({"error": "MQTT halt unavailable"}, status_code=503)
        results = []
        for device in ids:
            try:
                results.append(await halt_device(ingest, device))
            except Exception:
                results.append({"device_id": device, "halted": False, "error": "Halt failed; device state is unconfirmed"})
        return JSONResponse({"results": results})


    @router.get("/remove-device-list")
    async def remove_device_list(request: Request):
        _require_protected_access(request, require_csrf=True)
        devices = await asyncio.to_thread(_collect_removable_ids)
        device_details = await asyncio.to_thread(_collect_removable_details, devices)
        return JSONResponse({"devices": devices, "device_details": device_details})


    @router.get("/remove-device")
    async def remove_device_modal_hint(request: Request):
        """
        Kept for compatibility in case someone navigates to /remove-device.
        We just return a tiny page that instructs to use the modal button.
        """
        _require_protected_access(request, require_csrf=True)
        return HTMLResponse("<html><body><p>Use the Halt or Remove Device button to open the modal.</p></body></html>")


    @router.post("/remove-device")
    async def remove_device_post(request: Request):
        """
        Accepts JSON: {"device_ids": ["id1","id2",...]} or form with multiple 'device_ids'
        Executes removal for each device; returns JSON summary.
        """
        _require_protected_access(request, require_csrf=True)
        device_ids: list[str] = []
        ctype = request.headers.get("content-type","")
        if "application/json" in ctype.lower():
            body = await request.json()
            device_ids = list(body.get("device_ids", []) or [])
        else:
            form = await request.form()
            # supports checkboxes named device_ids
            raw = form.getlist("device_ids")
            device_ids = [x for x in raw if x]

        device_ids = [d.strip() for d in device_ids if (d or "").strip()]
        if not device_ids:
            return JSONResponse({"error": "No device_ids provided"}, status_code=400)
        invalid_ids = [d for d in device_ids if not _is_valid_device_id(d)]
        if invalid_ids:
            return JSONResponse({"error": "invalid_device_id", "count": len(invalid_ids)}, status_code=400)

        mqtt_ingest = services.mqtt_ingest

        requested_ids = list(dict.fromkeys(device_ids))
        related_ids: list[str] = []
        seen_related: set[str] = set()
        cleanup_targets: list[str] = []
        for dev in requested_ids:
            expanded = _collect_related_device_ids(dev, mqtt_ingest=mqtt_ingest)
            if dev not in expanded:
                expanded.insert(0, dev)
            expanded_keys = {
                str(value or "").strip().lower()
                for value in expanded
                if str(value or "").strip()
            }
            if not (expanded_keys & seen_related):
                cleanup_targets.append(dev)
            for raw in expanded:
                value = str(raw or "").strip()
                key = value.lower()
                if value and key not in seen_related:
                    seen_related.add(key)
                    related_ids.append(value)

        known_remote_ids: set[str] = set()
        if mqtt_ingest:
            try:
                known_remote_ids.update(str(value or "").strip().lower() for value in (mqtt_ingest.get_known_devices() or []))
            except Exception:
                pass
            try:
                known_remote_ids.update(str(value or "").strip().lower() for value in (mqtt_ingest.get_known_switch_devices() or []))
            except Exception:
                pass
        remote_remove = bool(known_remote_ids & seen_related)
        if not remote_remove:
            remote_remove = any(
                (not is_direct_local_sensor_id(dev)) and _sensor_shadow_is_remote_nodus(dev)
                for dev in requested_ids
                if not _is_switch_id(dev)
            )
        if not remote_remove:
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                for dev in requested_ids:
                    if not _is_switch_id(dev):
                        continue
                    doc = switch_mgr.load(dev) or {}
                    switch_type = str(((doc.get("Switch") or {}).get("TYPE") or "")).strip().lower()
                    if switch_type in {"nodus", "picow", "pico2w", "remote", "mqtt"}:
                        remote_remove = True
                        break
            except Exception:
                pass

        suppression = {"active": False, "persisted": False, "persistence_supported": False, "added": []}
        if remote_remove and mqtt_ingest:
            suppress = getattr(mqtt_ingest, "suppress_nodus_devices", None)
            if callable(suppress):
                suppression = suppress(related_ids, persist=True) or suppression
                if suppression.get("persistence_supported") and not suppression.get("persisted"):
                    return JSONResponse(
                        {"error": "removed_device_suppression_persist_failed", "device_ids": requested_ids},
                        status_code=500,
                    )

        def _purge_db_many(ids: list[str]) -> dict:
            merged = {"db_path": _get_db_path(), "rows_deleted": 0, "tables": [], "ids_purged": list(ids)}
            for did in ids:
                one = _purge_device_from_db(did)
                merged["rows_deleted"] += int(one.get("rows_deleted", 0) or 0)
                merged["tables"].extend(one.get("tables", []) or [])
            return merged

        active_logger = _active_data_logger()
        ha_stats = {"topics_cleared": 0, "ids_expanded": []}
        mqtt_stats = {"topics_cleared": 0, "ids_expanded": []}
        for dev in cleanup_targets:
            one_ha = await asyncio.to_thread(
                _clear_ha_entities,
                dev,
                mqtt_ingest=mqtt_ingest,
                data_logger=active_logger,
            )
            one_mqtt = await asyncio.to_thread(_clear_retained_mqtt_topics, dev, mqtt_ingest=mqtt_ingest)
            ha_stats["topics_cleared"] += int(one_ha.get("topics_cleared", 0) or 0)
            mqtt_stats["topics_cleared"] += int(one_mqtt.get("topics_cleared", 0) or 0)
            ha_stats["ids_expanded"].extend(one_ha.get("ids_expanded", []) or [])
            mqtt_stats["ids_expanded"].extend(one_mqtt.get("ids_expanded", []) or [])

        removed_dirs, db_stats = await asyncio.gather(
            asyncio.to_thread(_delete_device_dirs_many, related_ids),
            asyncio.to_thread(_purge_db_many, related_ids),
        )
        ok_settings = all(
            await asyncio.gather(
                *[asyncio.to_thread(_remove_client_from_hub_settings, dev) for dev in requested_ids]
            )
        )
        ingest_stats = {"ingest_keys_cleared": 0}
        for dev in cleanup_targets:
            one_ingest = _purge_ingest_cache(dev, mqtt_ingest=mqtt_ingest, data_logger=active_logger)
            ingest_stats["ingest_keys_cleared"] += int(one_ingest.get("ingest_keys_cleared", 0) or 0)
        controller_stats = _purge_switch_controller_state(related_ids)

        _invalidate_dashboard_caches()
        route_state.switch_status_payload = None
        route_state.switch_status_until = 0.0

        remaining = sorted(
            seen_related
            & {str(value or "").strip().lower() for value in await asyncio.to_thread(_collect_removable_ids)}
        )
        summary = (
            f"dirs(sensor={removed_dirs.get('sensor')},switch={removed_dirs.get('switch')},system={removed_dirs.get('system')}), "
            f"db_rows={db_stats.get('rows_deleted',0)}, clients_updated={ok_settings}, "
            f"ha_topics={ha_stats.get('topics_cleared',0)}, mqtt_topics={mqtt_stats.get('topics_cleared',0)}, "
            f"ingest_keys={ingest_stats.get('ingest_keys_cleared',0)}, "
            f"switch_controllers={controller_stats.get('switch_controllers_cleared',0)}, "
            f"switch_monitors={controller_stats.get('switch_monitors_cancelled',0)}, "
            f"suppressed={len(suppression.get('added', []) or [])}"
        )
        printDM(f"[remove-device] {requested_ids}: {summary}", location=MODULE)
        result = {
            "requested_ids": requested_ids,
            "cleanup_targets": cleanup_targets,
            "related_ids": related_ids,
            "dirs": removed_dirs,
            "db": db_stats,
            "clients_updated": ok_settings,
            "ha": ha_stats,
            "mqtt": mqtt_stats,
            "ingest": ingest_stats,
            "controllers": controller_stats,
            "suppression": suppression,
            "remaining_ids": remaining,
            "summary": summary,
        }
        results = {dev: result for dev in requested_ids}
        if remaining:
            return JSONResponse(
                {"error": "remove_device_verification_failed", "remaining_ids": remaining, "results": results},
                status_code=409,
            )

        if remote_remove and mqtt_ingest:
            allow = getattr(mqtt_ingest, "allow_nodus_devices", None)
            if callable(allow):
                released = await asyncio.to_thread(allow, related_ids, persist=True)
                result["suppression_release"] = released
                if suppression.get("persistence_supported") and not released.get("persisted"):
                    return JSONResponse(
                        {"error": "removed_device_suppression_clear_failed", "results": results},
                        status_code=500,
                    )
                suppression["active"] = False

        overall = f"Removed {len(requested_ids)} device(s)."
        return JSONResponse({"ok": True, "results": results, "summary": overall})


    @router.post("/retry-discovery")
    async def retry_discovery(request: Request):
        data = await request.json()
        hostname = data.get("host")
        if hostname in services.mqtt_ingest.discovery_failures:
            del services.mqtt_ingest.discovery_failures[hostname]
            services.mqtt_ingest.device_status[hostname] = "unknown"
            return JSONResponse(content={"status": "retrying"})
        return JSONResponse(content={"status": "unknown host"}, status_code=400)


    return DeviceRouteHelpers(
        build_device_network_info=_build_device_network_info,
        build_system_hostname_index=_build_system_hostname_index,
        display_nodus_board_type=_display_nodus_board_type,
        display_nodus_sensor_hardware=_display_nodus_sensor_hardware,
        nodus_values_match=_nodus_values_match,
        read_hostname_from_system_settings=_read_hostname_from_system_settings,
        request_nodus_device_restart=_request_nodus_device_restart,
        resolve_system_settings_root=_resolve_system_settings_root,
        push_nodus_settings_batch=push_nodus_settings_batch,
    )
