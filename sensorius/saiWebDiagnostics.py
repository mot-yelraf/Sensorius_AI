"""Register statistics, runtime diagnostics, and service restart routes.

Services are supplied explicitly from the app's RuntimeServices context. Shared
statistics builders also supply existing settings panes, preserving their payload
and label formats. Network, settings, and platform probes run off the event loop;
this module never imports the main route module or creates runtime services.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any, Callable
from .saiWebContracts import ProtectedAccess, SwitchSettingsFactory

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from .saiEcowitt import ecowitt_sensor_health
from .saiServiceRestart import find_restart_service, restart_service
from .saiRuntimeServices import RuntimeServices
from .saiUtils import mdns_hostname, normalize_hostname_base, normalize_sensor_id, printDM

MODULE = "saiWebRoutes"
_DB_RETENTION_MIN_DAYS = 30
_DB_RETENTION_MAX_DAYS = 365
_DB_RETENTION_DEFAULT_DAYS = 90

def _clamp_db_retention_days(raw: object, default: int = _DB_RETENTION_DEFAULT_DAYS) -> int:
    try:
        days = int(str(raw if raw is not None else default))
    except Exception:
        days = int(default)
    return max(_DB_RETENTION_MIN_DAYS, min(_DB_RETENTION_MAX_DAYS, days))


def _format_stats_duration(seconds: float | int | None) -> str:
    """Format a compact uptime duration for statistics panes."""
    if seconds is None:
        return "No offline events"
    try:
        total = max(0, int(float(seconds)))
    except Exception:
        return "No offline events"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _format_stats_age(epoch: float | int | None, *, empty: str = "No events") -> str:
    """Format a compact age from an epoch timestamp."""
    if epoch is None:
        return empty
    try:
        return f"{_format_stats_duration(time.time() - float(epoch))} ago"
    except Exception:
        return empty


def _format_stats_timestamp(epoch: float | int | None, *, empty: str = "No events") -> str:
    """Format a local timestamp for statistics panes."""
    if epoch is None:
        return empty
    try:
        dt = datetime.fromtimestamp(float(epoch)).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return empty


def _format_switch_state_label(state: object) -> str:
    """Normalize a persisted switch state into a short UI label."""
    if isinstance(state, bool):
        return "On" if state else "Off"
    text = str(state if state is not None else "").strip().lower()
    if text in {"1", "true", "on", "yes"}:
        return "On"
    if text in {"0", "false", "off", "no"}:
        return "Off"
    return str(state or "").strip() or "Unknown"


def _is_true_text(v: str | None, default: bool = False) -> bool:
    """Parse an environment boolean using the existing accepted values."""
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _switch_channel_display_name(channel: dict) -> str:
    """Return the existing short display name for a physical channel."""
    channel_id = str((channel or {}).get("channel_id") or "").strip()
    index = str((channel or {}).get("index") or "").strip()
    label = str((channel or {}).get("label") or "").strip()
    if channel_id and "-" in channel_id:
        prefix = channel_id.split("-", 1)[0].strip()
        if prefix:
            return prefix.upper()
    if index:
        return f"S{index}"
    return label or "Switch"


def _switch_channels_from_settings(settings_dict: dict) -> list[dict]:
    """Select installed channels using the existing settings rules."""
    sw_section = (settings_dict or {}).get("Switch", {}) or {}

    def _has_install_marker(val) -> bool:
        if val is None:
            return False
        if isinstance(val, bool):
            return val
        return str(val).strip() != ""

    sw_type = str(sw_section.get("TYPE", "") or "").strip().lower()
    has_en_keys = (
        ("SWITCH_1_ENABLE_PIN" in sw_section) or ("SWITCH_2_ENABLE_PIN" in sw_section)
        or ("SWITCH_1_EN" in sw_section) or ("SWITCH_2_EN" in sw_section)
    )

    def _enable_value(i: int):
        return sw_section.get(f"SWITCH_{i}_ENABLE_PIN", sw_section.get(f"SWITCH_{i}_EN", ""))

    indices_found: set[int] = set()
    for key in sw_section.keys():
        m = re.match(r"^SWITCH_(\d+)(?:_LABEL|_ENABLE_PIN|_EN|_PIN|_Trigger)?$", str(key))
        if m:
            indices_found.add(int(m.group(1)))
    if not indices_found:
        indices_found.add(1)

    render_indices: list[int] = []
    for i in sorted(indices_found):
        label_key = f"SWITCH_{i}_LABEL"
        pin_key = f"SWITCH_{i}_PIN"
        if sw_type in ("picow", "pico2w") or has_en_keys:
            if _has_install_marker(_enable_value(i)):
                render_indices.append(i)
        else:
            if str(sw_section.get(label_key, "")).strip() and str(sw_section.get(pin_key, "")).strip():
                render_indices.append(i)
    channel_indices = render_indices or [1]
    return [
        {
            "index": idx,
            "label": str(sw_section.get(f"SWITCH_{idx}_LABEL", "") or ""),
            "channel_id": str(sw_section.get(f"SWITCH_{idx}_CHANNEL_ID", "") or ""),
        }
        for idx in channel_indices
    ]


class DeviceStatistics:
    """Build sensor and switch statistics from the app's shared logger."""

    def __init__(self, services: RuntimeServices):
        """Bind the runtime context without opening storage or starting tasks."""
        self.services = services

    async def sensor_payload(self, sensor_id_value: str) -> dict[str, Any]:
        """Build the existing sensor statistics response and settings-pane values."""
        data_logger = self.services.data_logger
        normalized_id = normalize_sensor_id(str(sensor_id_value or ""))
        offline_events_24h = 0
        last_offline_epoch = None
        data_packets_received = 0
        last_packet_epoch = None
        stats_now = time.time()
        base_id = normalize_hostname_base(normalized_id)
        sensor_stat_aliases = [normalized_id]
        if base_id:
            sensor_stat_aliases.extend([base_id, mdns_hostname(base_id)])

        try:
            counter = getattr(data_logger, "get_sensor_offline_event_count", None)
            if callable(counter):
                offline_events_24h = await asyncio.to_thread(
                    lambda: counter(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            offline_events_24h = 0
        try:
            last_offline = getattr(data_logger, "get_sensor_last_offline_event_epoch", None)
            if callable(last_offline):
                last_offline_epoch = await asyncio.to_thread(
                    lambda: last_offline(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            last_offline_epoch = None
        try:
            packet_counter = getattr(data_logger, "get_sensor_packet_count", None)
            if callable(packet_counter):
                data_packets_received = await asyncio.to_thread(
                    lambda: packet_counter(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            data_packets_received = 0
        try:
            last_packet = getattr(data_logger, "get_sensor_last_packet_epoch", None)
            if callable(last_packet):
                last_packet_epoch = await asyncio.to_thread(
                    lambda: last_packet(normalized_id, aliases=sensor_stat_aliases)
                )
        except Exception:
            last_packet_epoch = None

        return {
            "is_ecowitt": normalized_id.startswith("ecowitt-"),
            **(await asyncio.to_thread(ecowitt_sensor_health, data_logger, normalized_id)),
            "offline_events_24h": offline_events_24h,
            "last_offline_epoch": last_offline_epoch,
            "uptime_since_last_offline_label": _format_stats_duration(
                (stats_now - float(last_offline_epoch))
                if last_offline_epoch is not None
                else None
            ),
            "last_offline_event_label": _format_stats_timestamp(
                last_offline_epoch,
                empty="No offline events",
            ),
            "data_packets_received": data_packets_received,
            "last_packet_epoch": last_packet_epoch,
            "last_packet_received_label": _format_stats_age(
                last_packet_epoch,
                empty="No packets",
            ),
        }

    async def switch_payload(self, switch_id_value: str, channels: list[dict]) -> dict[str, Any]:
        """Build the existing switch statistics response and settings-pane values."""
        data_logger = self.services.data_logger
        sid = str(switch_id_value or "").strip()
        offline_events_24h = 0
        last_offline_epoch = None
        last_packet_epoch = None
        stats_now = time.time()
        base_id = normalize_hostname_base(sid)
        switch_stat_aliases = [sid]
        if base_id:
            switch_stat_aliases.extend([base_id, mdns_hostname(base_id)])
        for ch in channels or []:
            channel_id = str((ch or {}).get("channel_id") or "").strip()
            if channel_id:
                switch_stat_aliases.append(channel_id)
                channel_base = normalize_hostname_base(channel_id)
                if channel_base:
                    switch_stat_aliases.extend([channel_base, mdns_hostname(channel_base)])

        try:
            counter = getattr(data_logger, "get_sensor_offline_event_count", None)
            if callable(counter):
                offline_events_24h = await asyncio.to_thread(
                    lambda: counter(sid, aliases=switch_stat_aliases)
                )
        except Exception:
            offline_events_24h = 0
        try:
            last_offline = getattr(data_logger, "get_sensor_last_offline_event_epoch", None)
            if callable(last_offline):
                last_offline_epoch = await asyncio.to_thread(
                    lambda: last_offline(sid, aliases=switch_stat_aliases)
                )
        except Exception:
            last_offline_epoch = None

        channel_state_stats: list[dict[str, Any]] = []
        channel_last_event = getattr(data_logger, "get_switch_last_event_for_channel", None)
        for ch in channels or []:
            channel_id = str((ch or {}).get("channel_id") or "").strip()
            display_name = _switch_channel_display_name(ch)
            event = None
            if channel_id and callable(channel_last_event):
                try:
                    event = await asyncio.to_thread(lambda cid=channel_id: channel_last_event(cid))
                except Exception:
                    event = None
            event_epoch = event.get("ts_epoch") if isinstance(event, dict) else None
            state_value = _format_switch_state_label(event.get("state")) if isinstance(event, dict) else ""
            if event_epoch is not None:
                try:
                    last_packet_epoch = max(float(last_packet_epoch or 0.0), float(event_epoch))
                except Exception:
                    pass
            channel_state_stats.append(
                {
                    "channel_id": channel_id,
                    "label": str((ch or {}).get("label") or "").strip(),
                    "display_name": display_name,
                    "row_label": f"{display_name} current state, age:",
                    "state": state_value,
                    "state_epoch": event_epoch,
                    "state_age_label": (
                        f"{state_value}, {_format_stats_duration(stats_now - float(event_epoch))}"
                        if event_epoch is not None
                        else "No state changes"
                    ),
                }
            )

        return {
            "offline_events_24h": offline_events_24h,
            "last_offline_epoch": last_offline_epoch,
            "uptime_since_last_offline_label": _format_stats_duration(
                (stats_now - float(last_offline_epoch))
                if last_offline_epoch is not None
                else None
            ),
            "last_offline_event_label": _format_stats_timestamp(
                last_offline_epoch,
                empty="No offline events",
            ),
            "switch_last_packet_epoch": last_packet_epoch,
            "switch_last_packet_received_label": _format_stats_age(
                last_packet_epoch,
                empty="No packets",
            ),
            "switch_channel_state_stats": channel_state_stats,
        }


@dataclass(frozen=True)
class AdvancedStatusConfiguration:
    """Supply the existing environment and platform helpers to diagnostics."""

    read_environment: Callable[[], dict[str, str]]
    autostart_is_enabled: Callable[[str], bool]
    debug_module_choices: tuple[str, ...]
    default_log_file: str


def register_statistics_diagnostics_routes(
    router, *, services: RuntimeServices, statistics: DeviceStatistics, net_mgr,
    require_protected_access: ProtectedAccess, advanced: AdvancedStatusConfiguration,
    switch_settings_manager: SwitchSettingsFactory,
) -> None:
    """Register existing statistics and diagnostics URLs with explicit dependencies."""
    @router.get("/hayd", response_class=JSONResponse)
    async def how_are_you_doing():
        """Report the legacy hub readiness status."""
        _status = "ok"
        return {"STATUS": _status}

    def _switch_controller_snapshot():
        out = []
        sc = services.switch_controllers
        if isinstance(sc, dict):
            for k, ctrl in sc.items():
                out.append({
                    "key": k,
                    "switch_id": getattr(ctrl, "switch_id", None),
                    "location": getattr(ctrl, "location", None),
                    "labels": list(getattr(ctrl, "get_switch_names", lambda: [])() or []),
                    "is_present": bool(getattr(ctrl, "is_present", False)),
                })
        elif sc:
            out.append({
                "key": None,
                "switch_id": getattr(sc, "switch_id", None),
                "location": getattr(sc, "location", None),
                "labels": list(getattr(sc, "get_switch_names", lambda: [])() or []),
                "is_present": bool(getattr(sc, "is_present", False)),
            })
        return {"count": len(out), "items": out}

    @router.get("/debug/switch-controllers", response_class=JSONResponse)
    async def debug_switch_controllers(request: Request):
        """
        Return the currently registered switch controllers (debug only).
        """
        try:
            require_protected_access(request)
            return await asyncio.to_thread(_switch_controller_snapshot)
        except HTTPException:
            raise
        except Exception as e:
            printDM(f"[debug_switch_controllers] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    def _automation_state_snapshot(switch_id, label):
        from .saiAutomationManager import AutomationManager
        sid = (switch_id or "").strip()
        lbl = (label or "").strip()
        switch_key = f"{sid}::{lbl}" if sid and lbl else ""
        mgr = AutomationManager("automation_settings")
        path = mgr._path_for_hostname(sid) if sid else None
        data = mgr.load(sid) if sid else {}
        adv = (data.get("Advanced") or {}) if isinstance(data, dict) else {}

        state = mgr.get_advanced_state_for_switch_key(sid, switch_key) if switch_key else {}

        return {
            "switch_id": sid,
            "label": lbl,
            "switch_key": switch_key,
            "path": str(path) if path else None,
            "path_exists": bool(path and path.exists()),
            "mtime": path.stat().st_mtime if path and path.exists() else None,
            "advanced_rules": adv,
            "computed_state": state,
        }

    @router.get("/debug/automation-state", response_class=JSONResponse)
    async def debug_automation_state(
        request: Request,
        switch_id: str = Query(...),
        label: str = Query(...),
    ):
        """
        Debug helper to inspect Advanced automation enabled state for a switch/label.
        """
        try:
            require_protected_access(request)
            return await asyncio.to_thread(_automation_state_snapshot, switch_id, label)
        except HTTPException:
            raise
        except Exception as e:
            printDM(f"[debug_automation_state] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.get("/advanced/status")
    async def advanced_status(request: Request):
        """Return logging, autostart, retention, and runtime health settings."""
        env_map = await asyncio.to_thread(advanced.read_environment)
        log_level = str(env_map.get("SENSORIUS_LOG_LEVEL", "DEBUG") or "DEBUG").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            log_level = "DEBUG"

        file_log = _is_true_text(env_map.get("SENSORIUS_FILE_LOG"), default=False)
        log_file = str(env_map.get("SENSORIUS_LOG_FILE", advanced.default_log_file) or advanced.default_log_file).strip()
        log_file_path = str(Path(log_file).expanduser().resolve())
        dbg_raw = str(env_map.get("SENSORIUS_DEBUG_MODULES", "") or "")
        debug_modules = [m.strip() for m in dbg_raw.split(",") if m.strip()]

        retention_days = _clamp_db_retention_days(env_map.get("SENSORIUS_DB_RETENTION_DAYS", _DB_RETENTION_DEFAULT_DAYS))

        autostart_scope = str(env_map.get("SENSORIUS_AUTOSTART_SCOPE", "user") or "user").strip().lower()
        if autostart_scope not in {"user", "system"}:
            autostart_scope = "user"
        autostart_enabled = await asyncio.to_thread(advanced.autostart_is_enabled, autostart_scope)
        restart_target = await asyncio.to_thread(find_restart_service)
        supervisor = services.supervisor
        mqtt_ingest = services.mqtt_ingest
        runtime_health = None
        if supervisor and hasattr(supervisor, "runtime_status_snapshot"):
            try:
                runtime_health = supervisor.runtime_status_snapshot()
            except Exception:
                runtime_health = None
        mqtt_ingest_health = None
        if mqtt_ingest and hasattr(mqtt_ingest, "mqtt_callback_health_snapshot"):
            try:
                mqtt_ingest_health = mqtt_ingest.mqtt_callback_health_snapshot()
                if hasattr(mqtt_ingest, "mqtt_connection_health_snapshot"):
                    mqtt_ingest_health["connections"] = mqtt_ingest.mqtt_connection_health_snapshot()
            except Exception:
                mqtt_ingest_health = None

        return JSONResponse({
            "platform": platform.system(),
            "autostart_scope": autostart_scope,
            "autostart_enabled": bool(autostart_enabled),
            "restart_enabled": restart_target is not None,
            "restart_message": (f"Restart the {restart_target.name}." if restart_target else
                                "Restart is available when this instance is running under an enabled automatic service or scheduled task."),
            "log_level": log_level,
            "file_log": bool(file_log),
            "log_file_path": log_file_path,
            "debug_module_choices": list(advanced.debug_module_choices),
            "debug_modules": debug_modules,
            "db_retention_days": retention_days,
            "runtime_health": runtime_health,
            "mqtt_ingest_health": mqtt_ingest_health,
            "runtime_instance_id": request.app.state.ui_runtime_instance_id,
            "autostart_note": "If you manually run 'python Sensorius.py', stop that instance before enabling auto-start to avoid duplicate instances.",
            "autostart_scope_note": "macOS user-level launchctl is default. System-level may require admin privileges.",
        })

    @router.get("/advanced/restart/status")
    async def restart_status(request: Request):
        """Let the browser distinguish a new process from a failed restart."""
        require_protected_access(request, require_csrf=True)
        return JSONResponse({
            "runtime_instance_id": request.app.state.ui_runtime_instance_id,
            "error": getattr(request.app.state, "service_restart_error", ""),
        }, headers={"Cache-Control": "no-store"})

    @router.post("/advanced/restart")
    async def request_service_restart(request: Request):
        """Recheck service ownership and acknowledge before restarting the app."""
        require_protected_access(request, require_csrf=True)
        state = request.app.state
        if getattr(state, "service_restart_until", 0) > time.monotonic():
            return JSONResponse({"error": "A restart is already in progress."}, status_code=409)
        state.service_restart_until = time.monotonic() + 120
        service = await asyncio.to_thread(find_restart_service)
        if service is None:
            state.service_restart_until = 0
            return JSONResponse({"error": "This instance is not running under an enabled automatic service."}, status_code=409)
        state.service_restart_error = ""

        async def perform_restart():
            await asyncio.sleep(0.5)
            try:
                await asyncio.to_thread(restart_service, service)
            except Exception as exc:
                state.service_restart_error = str(exc) or "Service restart failed."
                state.service_restart_until = 0
                printDM(f"Service restart failed: {exc}", location=MODULE)

        return JSONResponse({
            "status": "restarting", "runtime_instance_id": state.ui_runtime_instance_id,
        }, status_code=202, background=BackgroundTask(perform_restart))

    @router.get("/sensor-settings/statistics", response_class=JSONResponse)
    async def sensor_settings_statistics(
        request: Request,
        sensor_id: str = Query(...),
    ):
        """Return the statistics payload used by the sensor settings pane."""
        require_protected_access(request, require_csrf=True)
        normalized_id = normalize_sensor_id(sensor_id)
        if not normalized_id:
            return JSONResponse({"ok": False, "error": "Missing sensor_id"}, status_code=400)
        payload = await statistics.sensor_payload(normalized_id)
        payload["ok"] = True
        payload["sensor_id"] = normalized_id
        return JSONResponse(payload)

    @router.get("/switch-settings/statistics", response_class=JSONResponse)
    async def switch_settings_statistics(
        request: Request,
        switch_id: str = Query(...),
    ):
        """Return the statistics payload used by the switch settings pane."""
        require_protected_access(request, require_csrf=True)
        sid = str(switch_id or "").strip()
        if not sid:
            return JSONResponse({"ok": False, "error": "Missing switch_id"}, status_code=400)
        manager = switch_settings_manager("switch_settings")
        settings_dict = await asyncio.to_thread(lambda: manager.load(sid) or {})
        channels = _switch_channels_from_settings(settings_dict)
        payload = await statistics.switch_payload(sid, channels)
        payload["ok"] = True
        payload["switch_id"] = sid
        return JSONResponse(payload)

    def _network_snapshot():
        connected = net_mgr.is_connected()
        current_ssid = net_mgr.get_current_ssid()
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False)
        ip_output = result.stdout.strip()
        ip_addr = ip_output.split()[0] if ip_output else "N/A"

        return {
            "connected": connected,
            "ssid": current_ssid,
            "ip": ip_addr,
            "mode": "AP" if not connected and ip_addr.startswith("192.168.4.") else "Client"
        }

    @router.get("/network-status", response_class=JSONResponse)
    async def network_status_api():
        """Return network connection, SSID, address, and AP/client mode."""
        try:
            return JSONResponse(await asyncio.to_thread(_network_snapshot))
        except Exception as e:
            printDM(f"[network_status] {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @router.get("/debug/mqtt-retained-commands", response_class=JSONResponse)
    async def debug_mqtt_retained_commands(
        timeout: float = Query(1.0, ge=0.2, le=5.0),
        limit: int = Query(64, ge=1, le=200),
    ):
        """Return the existing read-only retained-command diagnostic scan."""
        ing = services.mqtt_ingest
        scanner = getattr(ing, "scan_retained_command_topics", None)
        if not ing or not callable(scanner):
            return JSONResponse({"ok": False, "error": "mqtt_ingest_unavailable"}, status_code=503)
        try:
            result = await asyncio.to_thread(scanner, timeout=timeout, limit=limit)
            status_code = 200 if bool(result.get("ok", False)) else 503
            return JSONResponse(result, status_code=status_code)
        except Exception as e:
            printDM(f"[debug_mqtt_retained_commands] {e}", location=MODULE)
            return JSONResponse({"ok": False, "error": "internal_error"}, status_code=500)

    @router.get("/debug", response_class=HTMLResponse)
    async def debug_page():
        """Return the diagnostic landing response."""
        return HTMLResponse("<h2>Debug route loaded</h2>")

    @router.get("/keep-alive", response_class=JSONResponse)
    async def keep_alive():
        """Report readiness for existing browser keep-alive requests."""
        return {"ready": True}
