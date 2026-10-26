"""Register Nodus onboarding sessions, bootstrap, and progress routes.

Session tokens, MQTT hello/config correlation, retry timing, and Wi-Fi restoration
retain their existing behavior. Each registration owns its session monitors and
WebSocket clients and receives the owning runtime explicitly.
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
from .saiWebContracts import SettingsFactory, SensorSettingsFactory, SwitchSettingsFactory
from .saiUtils import printDM, normalize_hostname_base, mdns_hostname, normalize_sensor_id

MODULE = "saiWebRoutes"

import hashlib
import json
import platform
import plistlib
import subprocess
from .saiOnboardingStore import OnboardingSessionStore, OnboardingStates
from .saiOnboardingToken import OnboardingTokenManager


def register_onboarding_routes(
    router: APIRouter, *, services: RuntimeServices, settings, main_loop,
    settings_factory: SettingsFactory, sensor_settings_factory: SensorSettingsFactory,
    switch_settings_factory: SwitchSettingsFactory,
    deobfuscate_secret: Callable[[str], str], system_base_dir, sensor_base_dir, switch_base_dir,
    debug: bool = False,
) -> Callable[[], bool]:
    """Register onboarding endpoints and return the feature-availability check."""
    saiSettings = settings_factory
    SensorSettingsManager = sensor_settings_factory
    SwitchSettingsManager = switch_settings_factory
    _SENSOR_BASE_DIR = sensor_base_dir
    _SWITCH_BASE_DIR = switch_base_dir
    DEBUG = debug
    onboarding_store = OnboardingSessionStore(base_dir=system_base_dir)
    onboarding_tokens = OnboardingTokenManager(onboarding_store, default_ttl_sec=600)
    _v2_session_tasks: Dict[str, asyncio.Task] = {}
    _ONBOARD_SOCKETS: Dict[str, Set[WebSocket]] = {}

    def _scan_for_ssid(target_ssid: str) -> tuple[bool, str, list[str]]:
        ssid = str(target_ssid or "").strip()
        from . import saiAddDevice as _add_device

        def _matching_ssids(values: list[str]) -> list[str]:
            unique = {str(value or "").strip() for value in values if str(value or "").strip()}
            if ssid:
                return [ssid] if ssid in unique else []
            return sorted(value for value in unique if _add_device.is_nodus_setup_ssid(value))

        sys_name = platform.system().lower()
        try:
            if sys_name == "linux":
                iface = ""
                try:
                    from . import saiAddDevice as _add_device
                    iface = str(getattr(_add_device, "_wifi_interface_name", lambda: "")() or "").strip()
                except Exception:
                    iface = ""

                errors: list[str] = []
                if iface:
                    rescan = subprocess.run(
                        ["nmcli", "dev", "wifi", "rescan", "ifname", iface],
                        capture_output=True, text=True, timeout=12
                    )
                    if rescan.returncode != 0:
                        errors.append((rescan.stderr or rescan.stdout or "nmcli rescan failed").strip())

                list_commands = []
                if iface:
                    list_commands.append(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface])
                list_commands.append(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"])

                for cmd in list_commands:
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                    if p.returncode != 0:
                        errors.append((p.stderr or p.stdout or "nmcli list failed").strip())
                        continue
                    lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
                    matches = _matching_ssids(lines)
                    if matches:
                        return True, "ok", matches

                detail = "; ".join(x for x in errors if x).strip()
                return False, detail or "ok", []

            if sys_name == "darwin":
                airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
                if not Path(airport).exists():
                    return False, "airport tool not found", []
                # Prefer plist output (-x) for robust parsing across spacing/alignment changes.
                p_xml = subprocess.run([airport, "-s", "-x"], capture_output=True, timeout=8)
                if p_xml.returncode == 0 and p_xml.stdout:
                    try:
                        rows = plistlib.loads(p_xml.stdout)
                        scanned_ssids: list[str] = []
                        for row in (rows or []):
                            if not isinstance(row, dict):
                                continue
                            candidate = str(row.get("SSID_STR") or row.get("SSID") or "").strip()
                            if candidate:
                                scanned_ssids.append(candidate)
                        matches = _matching_ssids(scanned_ssids)
                        return bool(matches), "ok", matches
                    except Exception:
                        pass

                # Fallback to text output if plist parsing isn't available on this host.
                p = subprocess.run([airport, "-s"], capture_output=True, text=True, timeout=8)
                if p.returncode != 0:
                    err = p.stderr or p.stdout
                    if not err and p_xml.returncode != 0:
                        err = (p_xml.stderr or b"").decode(errors="ignore")
                    return False, (err or "airport scan failed").strip(), []
                scanned_ssids = []
                for ln in (p.stdout or "").splitlines():
                    line = ln.rstrip()
                    if not line or line.lstrip().startswith("SSID "):
                        continue
                    bssid = re.search(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", line)
                    candidate = line[:bssid.start()].strip() if bssid else line.strip()
                    if candidate:
                        scanned_ssids.append(candidate)
                matches = _matching_ssids(scanned_ssids)
                return bool(matches), "ok", matches

            if sys_name == "windows":
                p = subprocess.run(
                    ["netsh", "wlan", "show", "networks", "mode=bssid"],
                    capture_output=True, text=True, timeout=10
                )
                if p.returncode != 0:
                    return False, (p.stderr or p.stdout or "netsh scan failed").strip(), []
                scanned_ssids = []
                for ln in (p.stdout or "").splitlines():
                    m = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*)$", ln, flags=re.IGNORECASE)
                    if m and m.group(1).strip():
                        scanned_ssids.append(m.group(1).strip())
                matches = _matching_ssids(scanned_ssids)
                return bool(matches), "ok", matches
        except Exception as ex:
            printDM(f"[onboard] Wi-Fi scan failed: {ex}", location=MODULE)
            return False, "Wi-Fi scan failed. Check the Sensorius log.", []
        return False, f"unsupported platform: {platform.system()}", []


    def _get_ws_set(job_id: str) -> Set[WebSocket]:
        return _ONBOARD_SOCKETS.setdefault(job_id, set())


    async def _broadcast(job_id: str, payload: Dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in list(_get_ws_set(job_id)):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            try: _get_ws_set(job_id).discard(ws)
            except Exception: pass


    async def _emit(job_id: str, step: int, ok: bool, label: str, detail: str = ""):
        await _broadcast(job_id, {
            "step": step,          # 1..3
            "ok": ok,              # True/False
            "label": label,        # short title
            "detail": detail,      # optional
        })


    def _build_v2_time_payload() -> Dict[str, Any]:
        time_doc: Dict[str, Any] = {}
        for key in ("TZ", "TZ_NAME", "NTP_SERVER", "NTP_SERVER_IP"):
            try:
                raw = settings.get_setting("Time", key, None)
            except Exception:
                raw = None
            if raw is not None:
                time_doc[key] = str(raw or "")
        try:
            raw_offset = settings.get_setting("Time", "TZ_OFFSET", None)
        except Exception:
            raw_offset = None
        if raw_offset is not None:
            try:
                time_doc["TZ_OFFSET"] = int(raw_offset)
            except Exception:
                time_doc["TZ_OFFSET"] = 0
        return time_doc


    def _build_v2_bootstrap_payload(
        *,
        onboard_token: str,
        ssid: str,
        password: str,
        hostname: str,
        broker_host_override: str | None = None,
    ) -> Dict[str, Any]:
        def _normalize_broker_host(raw_host: str, fallback_host: str) -> str:
            from . import saiAddDevice
            return saiAddDevice._hub_broker_hostname(raw_host, fallback_host)

        instance_id = (
            str(settings.get_setting("Network", "HOSTNAME", "") or "").strip()
            or socket.gethostname().strip()
            or "sensorius"
        )
        fallback_broker_host = instance_id
        broker_host = _normalize_broker_host(
            broker_host_override if broker_host_override is not None else settings.get_setting("SensorNetwork", "BROKER", ""),
            fallback_broker_host,
        )
        broker_port_raw = settings.get_setting("SensorNetwork", "MQTTPORT", 1883)
        try:
            broker_port = int(broker_port_raw)
        except Exception:
            broker_port = 1883
        if broker_port <= 0:
            broker_port = 1883
        payload: Dict[str, Any] = {
            "onboard_token": onboard_token,
            "ssid": ssid,
            "password": password,
            "hostname": hostname,
            "mqtt": {
                "broker_host": broker_host,
                "broker_port": broker_port,
            },
        }
        time_doc = _build_v2_time_payload()
        if time_doc:
            payload["time"] = time_doc
        return payload


    def _onboarding_v2_enabled() -> bool:
        raw = settings.get_setting("Onboarding", "ONBOARDING_V2_MQTT", None)
        if raw is None:
            raw = settings.get_setting("Onboarding", "onboarding_v2_mqtt", True)
        if isinstance(raw, bool):
            return raw
        text = str(raw or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return True


    def _onboarding_timeouts() -> Dict[str, float]:
        hello_timeout = float(settings.get_setting("Onboarding", "HELLO_TIMEOUT_SEC", 240) or 240)
        ack_timeout = float(settings.get_setting("Onboarding", "ACK_TIMEOUT_SEC", 10) or 10)
        result_timeout = float(settings.get_setting("Onboarding", "RESULT_TIMEOUT_SEC", 30) or 30)
        retry_backoff = float(settings.get_setting("Onboarding", "CONFIG_SET_BACKOFF_MS", 1500) or 1500) / 1000.0
        max_attempts = int(settings.get_setting("Onboarding", "CONFIG_SET_MAX_ATTEMPTS", 3) or 3)
        return {
            "hello_timeout_sec": max(5.0, hello_timeout),
            "ack_timeout_sec": max(2.0, ack_timeout),
            "result_timeout_sec": max(5.0, result_timeout),
            "retry_backoff_sec": max(0.25, retry_backoff),
            "max_attempts": max(1, max_attempts),
        }


    def _emit_onboarding_event(event_name: str, *, session_id: str = "", device_id: str = "", detail: str = "") -> None:
        payload = {
            "event": str(event_name or "").strip(),
            "session_id": str(session_id or "").strip(),
            "device_id": str(device_id or "").strip(),
            "detail": str(detail or "").strip(),
            "ts": time.time(),
        }
        try:
            printDM(f"[onboarding_event] {json.dumps(payload, separators=(',', ':'))}", location=MODULE)
        except Exception:
            pass


    def _build_v2_config_payload(device_id: str) -> Dict[str, Any]:
        """
        Build full config payload from hub-side settings managers.
        Falls back to empty section dicts when per-device docs do not exist yet.
        """
        def _to_jsonable(value: Any) -> Any:
            if isinstance(value, dict):
                return {str(k): _to_jsonable(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_to_jsonable(v) for v in value]
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            try:
                return str(value)
            except Exception:
                return None

        network_doc: Dict[str, Any] = {}
        sensor_doc: Dict[str, Any] = {}
        switch_doc: Dict[str, Any] = {}
        mqtt_doc: Dict[str, Any] = {}
        display_doc: Dict[str, Any] = {}
        calibration_doc: Dict[str, Any] = {}
        time_doc: Dict[str, Any] = {}

        try:
            sys_settings = saiSettings(apply_live=False, device_id=device_id)
            network_doc = dict(sys_settings.get_section("Network") or {})
            mqtt_doc = dict(sys_settings.get_section("MQTT") or {})
            display_doc = dict(sys_settings.get_section("Display") or {})
            calibration_doc = dict(sys_settings.get_section("Calibration") or {})
        except Exception:
            pass
        time_doc = _build_v2_time_payload()
        try:
            sensor_doc = dict(SensorSettingsManager(base_dir_name=_SENSOR_BASE_DIR).load(device_id) or {})
        except Exception:
            sensor_doc = {}
        try:
            switch_doc = dict(SwitchSettingsManager(base_dir=_SWITCH_BASE_DIR).load(device_id) or {})
        except Exception:
            switch_doc = {}

        settings_map: Dict[str, Any] = {}
        if isinstance(network_doc, dict) and network_doc:
            settings_map["Network"] = _to_jsonable(network_doc)
        if isinstance(mqtt_doc, dict) and mqtt_doc:
            settings_map["MQTT"] = _to_jsonable(mqtt_doc)
        if isinstance(time_doc, dict) and time_doc:
            settings_map["Time"] = _to_jsonable(time_doc)
        if isinstance(display_doc, dict) and display_doc:
            settings_map["Display"] = _to_jsonable(display_doc)
        if isinstance(calibration_doc, dict) and calibration_doc:
            settings_map["Calibration"] = _to_jsonable(calibration_doc)
        if isinstance(sensor_doc, dict) and sensor_doc:
            settings_map["Sensor"] = _to_jsonable(sensor_doc)
        if isinstance(switch_doc, dict) and switch_doc:
            settings_map["Switch"] = _to_jsonable(switch_doc)
        return {"settings": settings_map}


    def _publish_config_set_for_session(session_id: str, device_id: str) -> bool:
        session = onboarding_store.get_session(session_id)
        if not session:
            return False
        policy = _onboarding_timeouts()
        message_id = str(session.get("message_id", "") or "").strip()
        config_version = int(session.get("config_version", 1) or 1)
        payload_block = session.get("config_payload")
        if not isinstance(payload_block, dict):
            payload_block = _build_v2_config_payload(device_id)

        payload_bytes = json.dumps(payload_block, sort_keys=True, separators=(",", ":")).encode("utf-8")
        checksum = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
        if not message_id:
            message_id = f"cfg-{int(time.time())}-{uuid4().hex[:8]}"
        token_secret = str(session.get("onboard_token_secret", "") or "")
        onboard_token = deobfuscate_secret(token_secret).strip() if token_secret else ""
        if not onboard_token:
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_onboard_token")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, device_id=device_id, detail="missing_onboard_token")
            return False

        envelope = {
            "message_id": message_id,
            "onboard_token": onboard_token,
            "config_version": config_version,
            "checksum": checksum,
            "payload": payload_block,
        }
        topic = f"nodus/{device_id}/config/set"
        ok = bool(services.mqtt_ingest.publish_json(topic, envelope, qos=1, retain=False, use_ha_client=False))
        now = time.time()
        if not ok:
            _emit_onboarding_event("onboarding_failed", session_id=session_id, device_id=device_id, detail="config_publish_failed")
            return False
        current_retry = int(session.get("retry_count", 0) or 0)
        onboarding_store.update_session(
            session_id,
            state=OnboardingStates.WAITING_CONFIG_ACK,
            device_id=device_id,
            message_id=message_id,
            config_version=config_version,
            config_payload=payload_block,
            retry_count=current_retry + 1,
            last_config_sent_at=now,
            ack_deadline_at=now + float(policy["ack_timeout_sec"]),
        )
        _emit_onboarding_event("onboarding_config_sent", session_id=session_id, device_id=device_id, detail=message_id)
        return True


    async def _v2_session_monitor(session_id: str) -> None:
        policy = _onboarding_timeouts()
        while True:
            await asyncio.sleep(0.5)
            session = onboarding_store.get_session(session_id)
            if not session:
                return
            state = str(session.get("state", "") or "").strip()
            if state in {OnboardingStates.ONLINE, OnboardingStates.FAILED}:
                return

            now = time.time()
            token_exp = float(session.get("token_expires_at", 0.0) or 0.0)
            if token_exp and now > token_exp and state != OnboardingStates.ONLINE:
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="token_expired")
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="token_expired")
                return

            if state == OnboardingStates.WAITING_MQTT_HELLO:
                hello_deadline = float(session.get("hello_deadline_at", 0.0) or 0.0)
                if hello_deadline and now > hello_deadline:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="hello_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="hello_timeout")
                    return
                continue

            if state == OnboardingStates.WAITING_CONFIG_ACK:
                ack_deadline = float(session.get("ack_deadline_at", 0.0) or 0.0)
                if not ack_deadline or now <= ack_deadline:
                    continue
                retries = int(session.get("retry_count", 0) or 0)
                if retries >= int(policy["max_attempts"]):
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="config_ack_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="config_ack_timeout")
                    return
                await asyncio.sleep(float(policy["retry_backoff_sec"]))
                device_id = str(session.get("device_id", "") or session.get("expected_device_id", "") or "").strip()
                if not device_id:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_device_id")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="missing_device_id")
                    return
                _publish_config_set_for_session(session_id, device_id)
                continue

            if state == OnboardingStates.WAITING_CONFIG_RESULT:
                result_deadline = float(session.get("result_deadline_at", 0.0) or 0.0)
                if result_deadline and now > result_deadline:
                    onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="config_result_timeout")
                    _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="config_result_timeout")
                    return


    def _ensure_v2_session_monitor(session_id: str) -> None:
        def _spawn() -> None:
            existing = _v2_session_tasks.get(session_id)
            if existing and not existing.done():
                return
            _v2_session_tasks[session_id] = asyncio.create_task(_v2_session_monitor(session_id))

        existing = _v2_session_tasks.get(session_id)
        if existing and not existing.done():
            return
        try:
            running = asyncio.get_running_loop()
            if running is main_loop:
                _spawn()
                return
        except Exception:
            pass
        try:
            main_loop.call_soon_threadsafe(_spawn)
        except Exception:
            pass


    def _handle_onboarding_event(event: Dict[str, Any]) -> None:
        event_type = str(event.get("event_type", "") or "").strip()
        device_id = str(event.get("device_id", "") or "").strip()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not event_type or not device_id:
            return

        if event_type == "onboarding_hello":
            token = str(payload.get("onboard_token", "") or "").strip()
            if not token:
                return

            # Always resolve by token validation first so parallel/restarted sessions
            # with the same device id do not fail the wrong session.
            session = None
            for candidate in onboarding_store.list_active_sessions():
                sid = str(candidate.get("session_id", "") or "").strip()
                if not sid:
                    continue
                ok, _ = onboarding_tokens.validate_for_session(
                    session_id=sid,
                    token=token,
                    device_id=device_id,
                )
                if ok:
                    session = candidate
                    break
            if not session:
                return

            sid = str(session.get("session_id", "") or "").strip()
            ok, reason, _updated = onboarding_tokens.consume_for_session(
                session_id=sid,
                token=token,
                device_id=device_id,
            )
            if not ok:
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)
                return
            try:
                allow_removed = getattr(services.mqtt_ingest, "allow_nodus_devices", None)
                if callable(allow_removed):
                    allow_removed([device_id], persist=True)
                refresh_retained = getattr(services.mqtt_ingest, "refresh_nodus_retained_metadata", None)
                if callable(refresh_retained):
                    refresh_result = refresh_retained(device_id)
                    if not bool((refresh_result or {}).get("ok", False)):
                        printDM(
                            f"[onboarding] retained metadata refresh incomplete for {device_id}: {refresh_result}",
                            location=MODULE,
                        )
            except Exception as exc:
                printDM(f"[onboarding] failed to allow and refresh re-onboarded device {device_id}: {exc}", location=MODULE)
            onboarding_store.set_device_id(sid, device_id)
            onboarding_store.update_session(sid, last_hello_at=time.time(), device_id=device_id)
            _emit_onboarding_event("onboarding_hello_received", session_id=sid, device_id=device_id)
            onboarding_store.set_state(sid, OnboardingStates.CONFIG_SENDING)
            if not _publish_config_set_for_session(sid, device_id):
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason="config_publish_failed")
                return
            _ensure_v2_session_monitor(sid)
            return

        if event_type == "onboarding_config_ack":
            msg_id = str(payload.get("message_id", "") or "").strip()
            if not msg_id:
                return
            session = onboarding_store.find_active_by_device_and_message(device_id, msg_id)
            if not session:
                return
            sid = str(session.get("session_id", "") or "").strip()
            if not sid:
                return
            accepted = bool(payload.get("accepted", True))
            if not accepted:
                reason = str(payload.get("error", "") or "config_rejected")
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)
                return
            result_deadline = time.time() + float(_onboarding_timeouts()["result_timeout_sec"])
            onboarding_store.update_session(sid, state=OnboardingStates.WAITING_CONFIG_RESULT, result_deadline_at=result_deadline)
            _emit_onboarding_event("onboarding_config_ack", session_id=sid, device_id=device_id, detail=msg_id)
            return

        if event_type == "onboarding_config_result":
            msg_id = str(payload.get("message_id", "") or "").strip()
            applied = bool(payload.get("applied", False))
            if not msg_id:
                return
            session = onboarding_store.find_active_by_device_and_message(device_id, msg_id)
            if not session:
                return
            sid = str(session.get("session_id", "") or "").strip()
            if not sid:
                return
            _emit_onboarding_event("onboarding_config_result", session_id=sid, device_id=device_id, detail=msg_id)
            if applied:
                onboarding_store.update_session(sid, state=OnboardingStates.ONLINE, last_seen=time.time())
                onboarding_tokens.invalidate_session_token(sid)
                _emit_onboarding_event("onboarding_online", session_id=sid, device_id=device_id, detail=msg_id)
            else:
                reason = str(payload.get("error", "") or "config_apply_failure")
                onboarding_store.set_state(sid, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=sid, device_id=device_id, detail=reason)


    @router.post("/onboard-device/v2/prepare")
    async def onboard_prepare_v2(request: Request):
        """Create a resumable onboarding session before Wi-Fi is switched."""
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)

        form = await request.form()
        requested_device_id = str(form.get("device_id", "") or "").strip()
        session_id = uuid4().hex
        issued = onboarding_tokens.issue_token(
            session_id=session_id,
            expected_device_id=requested_device_id,
            ttl_sec=600,
        )
        onboarding_store.set_state(session_id, OnboardingStates.AP_DISCOVERED)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "state": OnboardingStates.AP_DISCOVERED,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": requested_device_id,
            }
        )


    @router.post("/onboard-device/v2/start")
    async def onboard_start_v2(request: Request):
        """
        V2 bootstrap start:
        1) Connect to Nodus AP
        2) Issue short-lived single-use token (stored hashed)
        3) POST /itaot-init with minimal bootstrap payload
        4) Persist WAITING_MQTT_HELLO state for resume/correlation
        """
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)

        from . import saiAddDevice
        form = await request.form()
        factory_target_ap = (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
        target_ap = str(form.get("target_ap", "") or "").strip() or factory_target_ap
        target_ap_password_raw = form.get("target_ap_password")
        if target_ap_password_raw is None and saiAddDevice.is_nodus_setup_ssid(target_ap):
            target_ap_password = str(saiAddDevice.PICOW_AP_PASSWORD or "")
        else:
            target_ap_password = str(target_ap_password_raw or "")
        local_ssid = str(form.get("local_ssid", "") or "").strip()
        local_password = str(form.get("local_password", "") or "")
        requested_device_id = str(form.get("device_id", "") or "").strip()
        hostname = requested_device_id or f"nodus-{uuid4().hex[:8]}"
        sys_name = platform.system().lower()

        prepared_session_id = str(form.get("session_id", "") or "").strip()
        prepared_session = onboarding_store.get_session(prepared_session_id) if prepared_session_id else None
        if prepared_session_id:
            if not prepared_session:
                return JSONResponse({"ok": False, "error": "session_not_found"}, status_code=404)
            prepared_state = str(prepared_session.get("state", "") or "").strip()
            if prepared_state != OnboardingStates.AP_DISCOVERED:
                return JSONResponse(
                    {"ok": False, "session_id": prepared_session_id, "error": "session_not_ready", "state": prepared_state},
                    status_code=409,
                )
            session_id = prepared_session_id
            token_secret = str(prepared_session.get("onboard_token_secret", "") or "")
            token = deobfuscate_secret(token_secret).strip() if token_secret else ""
            if not token:
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_onboard_token")
                return JSONResponse(
                    {"ok": False, "session_id": session_id, "error": "missing_onboard_token"},
                    status_code=409,
                )
            if not requested_device_id:
                requested_device_id = str(prepared_session.get("expected_device_id", "") or "").strip()
                if requested_device_id:
                    hostname = requested_device_id
            issued = {"expires_at": prepared_session.get("token_expires_at")}
        else:
            session_id = uuid4().hex
            issued = onboarding_tokens.issue_token(
                session_id=session_id,
                expected_device_id=requested_device_id,
                ttl_sec=600,
            )
            token = issued["token"]
            onboarding_store.set_state(session_id, OnboardingStates.AP_DISCOVERED)

        if not local_ssid and sys_name != "darwin":
            try:
                resolved_ssid, resolved_password = await asyncio.get_event_loop().run_in_executor(
                    None,
                    saiAddDevice.resolve_pi_wifi_credentials,
                )
                local_ssid = (resolved_ssid or "").strip()
                if not local_password:
                    local_password = resolved_password or ""
            except Exception:
                pass

        if sys_name == "darwin" and saiAddDevice.is_nodus_setup_ssid(local_ssid):
            # A manual macOS join happens before Add is clicked, so resolving
            # credentials now can only discover the setup AP, not the network
            # that the Nodus must ultimately join.
            local_ssid = ""
            local_password = ""

        async def _restore_local_wifi_on_failure() -> None:
            if sys_name == "darwin":
                return
            restore_ssid = (local_ssid or "").strip()
            if not restore_ssid:
                return
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.reconnect_to_network(restore_ssid, local_password, max_attempts=3, delay_sec=1.5),
                )
            except Exception as e:
                printDM(f"[onboard-v2] failed to restore Wi-Fi '{restore_ssid}': {e}", location="saiWebRoutes")

        async def _restore_local_wifi_on_success() -> tuple[bool, str]:
            restore_ssid = (local_ssid or "").strip()
            if not restore_ssid:
                return False, ""
            try:
                ok_restore, resolved_ssid = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.reconnect_to_network(restore_ssid, local_password, max_attempts=5, delay_sec=2.0),
                )
                return bool(ok_restore), str(resolved_ssid or restore_ssid).strip()
            except Exception as e:
                printDM(f"[onboard-v2] failed to restore Wi-Fi '{restore_ssid}' after bootstrap: {e}", location="saiWebRoutes")
                return False, restore_ssid

        ok_ap = False
        current_ap_ssid = ""
        meta_result: dict[str, Any] | None = None
        if sys_name == "darwin":
            # networksetup returns -3900 and stale association state on current
            # macOS even when the host has joined the Nodus AP. Treat the Wi-Fi
            # address plus the Nodus HTTP endpoint as the source of truth.
            on_nodus_subnet = await asyncio.get_event_loop().run_in_executor(
                None,
                saiAddDevice.mac_wifi_is_on_nodus_subnet,
            )
            if on_nodus_subnet:
                meta_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.get_itaot_meta(timeout_sec=3.0),
                )
                ok_ap = bool(meta_result.get("ok", False))
                if ok_ap:
                    current_ap_ssid = target_ap
        else:
            try:
                current_ap_ssid = await asyncio.get_event_loop().run_in_executor(
                    None,
                    getattr(saiAddDevice, "_get_current_ssid"),
                )
            except Exception:
                current_ap_ssid = ""

        if sys_name == "linux":
            can_control_network, permission_detail = await asyncio.get_event_loop().run_in_executor(
                None,
                saiAddDevice.linux_network_control_permission_status,
            )
            if not can_control_network:
                reason = "network_control_not_authorized"
                detail = (
                    "Sensorius is not authorized to control NetworkManager "
                    f"({permission_detail}). Grant NetworkManager control to the sensorius.service user, "
                    "then restart Sensorius."
                )
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=reason)
                return JSONResponse(
                    {
                        "ok": False,
                        "session_id": session_id,
                        "state": OnboardingStates.FAILED,
                        "error": reason,
                        "detail": detail,
                    },
                    status_code=403,
                )

        ok_ap = ok_ap or (current_ap_ssid or "").strip() == target_ap
        if not ok_ap:
            if sys_name == "darwin":
                reason = "manual_ap_join_required"
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason=reason)
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=reason)
                return JSONResponse(
                    {
                        "ok": False,
                        "session_id": session_id,
                        "state": OnboardingStates.FAILED,
                        "error": reason,
                        "detail": "Join a Nodus setup network in macOS Wi-Fi, then click Add again.",
                    },
                    status_code=409,
                )
            try:
                ok_ap = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.connect_to_sensor_ap(target_ap, target_ap_password, attempts=3),
                )
            except Exception as e:
                await _restore_local_wifi_on_failure()
                printDM(f"[onboard-v2] AP connection failed: {e}", location=MODULE)
                onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="ap_connect_error")
                _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="ap_connect_error")
                return JSONResponse(
                    {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "ap_connect_error"},
                    status_code=502,
                )

        if not ok_ap:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="ap_connect_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="ap_connect_failed")
            detail = ""
            if sys_name == "darwin":
                detail = f"Could not join {target_ap} automatically. Join it from Other Networks, then click Add again."
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "ap_connect_failed",
                    "detail": detail,
                },
                status_code=502,
            )

        if not local_ssid:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="missing_local_ssid")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail="missing_local_ssid")
            return JSONResponse(
                {"ok": False, "session_id": session_id, "state": OnboardingStates.FAILED, "error": "missing_local_ssid"},
                status_code=400,
            )

        if meta_result is None:
            meta_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: saiAddDevice.get_itaot_meta(timeout_sec=5.0),
            )
        meta_body = meta_result.get("body") if isinstance(meta_result, dict) else None
        if not bool(meta_result.get("ok", False)):
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="meta_fetch_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"meta_fetch_failed:{meta_result.get('error', '')}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "meta_fetch_failed",
                    "detail": str(meta_result.get("error", "")),
                },
                status_code=502,
            )

        meta_device_id = saiAddDevice._extract_device_id_from_meta(meta_body or {}, requested_device_id or hostname)
        if meta_device_id:
            hostname = meta_device_id
        if not requested_device_id:
            requested_device_id = meta_device_id

        onboarding_store.set_state(session_id, OnboardingStates.INIT_SENDING)
        _emit_onboarding_event("onboarding_init_sent", session_id=session_id, detail=hostname)
        init_payload = _build_v2_bootstrap_payload(
            onboard_token=token,
            ssid=local_ssid,
            password=local_password,
            hostname=hostname,
        )
        init_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: saiAddDevice.post_itaot_init(init_payload, timeout_sec=11.0),
        )
        init_response_indeterminate = bool(init_result.get("indeterminate", False))
        if not bool(init_result.get("ok", False)) and not init_response_indeterminate:
            await _restore_local_wifi_on_failure()
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="INIT_FAILED")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"INIT_FAILED:{init_result.get('error', '')}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "INIT_FAILED",
                    "detail": str(init_result.get("error", "")),
                },
                status_code=502,
            )

        onboarding_store.update_session(
            session_id,
            local_ssid=local_ssid,
            init_response_indeterminate=init_response_indeterminate,
            init_response_error=(
                str(init_result.get("error", "")) if init_response_indeterminate else ""
            ),
        )
        onboarding_store.set_state(session_id, OnboardingStates.INIT_SENT)
        _emit_onboarding_event(
            "onboarding_init_response_indeterminate" if init_response_indeterminate else "onboarding_init_ack",
            session_id=session_id,
            detail=hostname,
        )
        restore_pending = False
        if sys_name == "darwin":
            ok_restore, _restored_ip = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: saiAddDevice.wait_for_macos_local_network(timeout_sec=15.0),
            )
            restored_ssid = local_ssid
            restore_pending = not ok_restore
        else:
            ok_restore, restored_ssid = await _restore_local_wifi_on_success()
        if not ok_restore and sys_name != "darwin":
            onboarding_store.set_state(session_id, OnboardingStates.FAILED, failure_reason="local_wifi_restore_failed")
            _emit_onboarding_event("onboarding_failed", session_id=session_id, detail=f"local_wifi_restore_failed:{restored_ssid or local_ssid}")
            return JSONResponse(
                {
                    "ok": False,
                    "session_id": session_id,
                    "state": OnboardingStates.FAILED,
                    "error": "local_wifi_restore_failed",
                    "detail": str(restored_ssid or local_ssid or ""),
                },
                status_code=502,
            )

        onboarding_store.update_session(
            session_id,
            state=OnboardingStates.WAITING_REBOOT,
            local_ssid=str(restored_ssid or local_ssid or "").strip(),
            local_wifi_restored_at=time.time() if ok_restore else None,
            local_wifi_restore_pending=restore_pending,
        )
        hello_deadline = time.time() + float(_onboarding_timeouts()["hello_timeout_sec"])
        onboarding_store.update_session(session_id, state=OnboardingStates.WAITING_MQTT_HELLO, hello_deadline_at=hello_deadline)
        _ensure_v2_session_monitor(session_id)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "state": OnboardingStates.WAITING_MQTT_HELLO,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": requested_device_id,
                "local_ssid": str(restored_ssid or local_ssid or "").strip(),
                "local_wifi_restore_pending": restore_pending,
                "init_response_indeterminate": init_response_indeterminate,
            }
        )


    @router.get("/onboard-device/v2/session/{session_id}")
    async def onboard_session_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"error": "onboarding_v2_disabled"}, status_code=409)
        session = onboarding_store.get_session(session_id)
        if not session:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        safe = dict(session)
        safe.pop("onboard_token_hash", None)
        safe.pop("onboard_token_secret", None)
        return JSONResponse(safe)


    @router.get("/onboard-device/v2/sessions")
    async def onboard_sessions_v2(active_only: bool = Query(True)):
        if not _onboarding_v2_enabled():
            return JSONResponse({"error": "onboarding_v2_disabled"}, status_code=409)
        sessions = onboarding_store.list_active_sessions() if active_only else onboarding_store.list_sessions()
        redacted: list[Dict[str, Any]] = []
        for s in sessions:
            one = dict(s)
            one.pop("onboard_token_hash", None)
            one.pop("onboard_token_secret", None)
            redacted.append(one)
        return JSONResponse({"count": len(redacted), "items": redacted})


    @router.post("/onboard-device/v2/retry/{session_id}")
    async def onboard_retry_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)
        session = onboarding_store.get_session(session_id)
        if not session:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        state = str(session.get("state", "") or "").strip()
        device_id = str(session.get("device_id", "") or session.get("expected_device_id", "") or "").strip()
        if state == OnboardingStates.WAITING_MQTT_HELLO:
            hello_deadline = time.time() + float(_onboarding_timeouts()["hello_timeout_sec"])
            onboarding_store.update_session(session_id, hello_deadline_at=hello_deadline)
            _ensure_v2_session_monitor(session_id)
            return JSONResponse({"ok": True, "session_id": session_id, "state": state})
        if state in {OnboardingStates.WAITING_CONFIG_ACK, OnboardingStates.CONFIG_SENDING} and device_id:
            if not _publish_config_set_for_session(session_id, device_id):
                return JSONResponse({"ok": False, "error": "config_publish_failed"}, status_code=502)
            _ensure_v2_session_monitor(session_id)
            return JSONResponse({"ok": True, "session_id": session_id, "state": OnboardingStates.WAITING_CONFIG_ACK})
        return JSONResponse({"ok": False, "error": "retry_not_allowed_for_state", "state": state}, status_code=409)


    @router.post("/onboard-device/v2/restart/{session_id}")
    async def onboard_restart_v2(session_id: str):
        if not _onboarding_v2_enabled():
            return JSONResponse({"ok": False, "error": "onboarding_v2_disabled"}, status_code=409)
        old = onboarding_store.get_session(session_id)
        if not old:
            return JSONResponse({"error": "session_not_found"}, status_code=404)
        expected_device_id = str(old.get("expected_device_id", "") or old.get("device_id", "") or "").strip()
        new_id = uuid4().hex
        issued = onboarding_tokens.issue_token(
            session_id=new_id,
            expected_device_id=expected_device_id,
            ttl_sec=600,
        )
        onboarding_store.update_session(
            new_id,
            state=OnboardingStates.AP_DISCOVERED,
            restart_of=session_id,
        )
        return JSONResponse(
            {
                "ok": True,
                "session_id": new_id,
                "state": OnboardingStates.AP_DISCOVERED,
                "token_expires_at": issued.get("expires_at"),
                "expected_device_id": expected_device_id,
            }
        )


    @router.websocket("/ws/onboard/{job_id}")
    async def ws_onboard_progress(websocket: WebSocket, job_id: str):
        await websocket.accept()
        _get_ws_set(job_id).add(websocket)
        try:
            # greet (lets UI mark “connected to progress channel” if desired)
            await websocket.send_json({"hello": True, "job_id": job_id})
            # keep alive until client disconnects
            while True:
                # optional ping/pong or just sleep
                await asyncio.sleep(30)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            printDM(f"[ws_onboard_progress] {e}", location="saiWebRoutes")
        finally:
            try: _get_ws_set(job_id).discard(websocket)
            except Exception: pass


    @router.get("/onboard-device", response_class=HTMLResponse)
    async def onboard_device_page():
        # serve a tiny page that only contains the modal+script block from step (2)
        # (You can inline the exact same HTML/JS or import a renderer if you prefer)
        html = """
        <html><head><title>Add Device</title></head><body style="background:#F5FFFA;">
        <!-- Paste the same modal + <script> from step (2) here -->
        <script>window.onload = function(){ openOnboardModal(); };</script>
        </body></html>
        """
        return HTMLResponse(html)


    @router.post("/onboard-device")
    async def onboard_start(request: Request):
        """
        Starts the Pico2 W (or switch) onboarding flow and returns a job_id.
        Frontend then connects to /ws/onboard/{job_id} to receive step updates.
        """
        from . import saiAddDevice
        form = await request.form()
        # Pull what your System Setup dialog already posts.
        # These names are examples; keep them aligned with your current form fields:
        sensor_type = form.get("sensor_type", "")
        location    = form.get("location", "Unknown")
        local_ssid  = form.get("local_ssid", "Unknown")
        factory_target_ap = (saiAddDevice.PICOW_AP_SSID or "Nodus_Setup").strip() or "Nodus_Setup"
        target_ap = str(form.get("target_ap", "") or "").strip() or factory_target_ap
        target_ap_password_raw = form.get("target_ap_password")
        if target_ap_password_raw is None and saiAddDevice.is_nodus_setup_ssid(target_ap):
            target_ap_password = str(saiAddDevice.PICOW_AP_PASSWORD or "")
        else:
            target_ap_password = str(target_ap_password_raw or "")
        # You may already assemble a richer onboarding payload; pass it through:
        payload_json = form.get("payload_json")  # optional richer JSON blob

        job_id = uuid4().hex
        printDM(f"[onboard-start] job_id={job_id} sensor_type={sensor_type} location={location}", location="saiWebRoutes")

        async def run_flow():
            # Step 1: AP connect
            label1 = f"{target_ap} connection established"
            ok1 = False
            try:
                ok1 = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: saiAddDevice.connect_to_sensor_ap(
                        target_ap,
                        target_ap_password,
                        attempts=3
                    )
                )
            except Exception as e:
                printDM(f"[onboard] connect_to_sensor_ap failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 1, bool(ok1), label1)

            sensor_id_for_step = "Unknown"

            # Step 2: configure + reboot (no reconnect here)
            label2 = "configured and rebooting"
            ok2 = False
            if ok1:
                try:
                    ok2, maybe_sensor_id = await asyncio.get_event_loop().run_in_executor(
                        None, saiAddDevice.perform_picow_configure_and_reboot
                    )
                    if maybe_sensor_id:
                        sensor_id_for_step = maybe_sensor_id
                except Exception as e:
                    printDM(f"[onboard] perform_picow_configure_and_reboot failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 2, bool(ok2), f"{sensor_id_for_step} {label2}")

            # Step 3: reconnect Pi to its local SSID (and update CLIENTS if step 2 succeeded)
            ok3 = False
            conn_ssid = "Unknown"
            label3 = f"Reconnecting to {conn_ssid}"
            try:
                ok3, conn_ssid = await asyncio.get_event_loop().run_in_executor(None, saiAddDevice.reconnect_to_pi)
                label3 = f"Reconnecting to {conn_ssid}"
            except Exception as e:
                printDM(f"[onboard] reconnect_to_pi failed: {e}", location="saiWebRoutes")
            await _emit(job_id, 3, bool(ok3), label3)

            if ok2 and sensor_id_for_step:
                try:
                    saiAddDevice.update_hub_clients(saiAddDevice.get_hub_settings_path(), sensor_id_for_step)
                except Exception as e:
                    printDM(f"[onboard] update_hub_clients failed: {e}", location="saiWebRoutes")
                # Nudge ingest discovery immediately (no restart required)
                try:
                    allow_removed = getattr(services.mqtt_ingest, "allow_nodus_devices", None)
                    if callable(allow_removed):
                        allow_removed([sensor_id_for_step], persist=True)
                    # match the form you persist in CLIENTS (you showed ".local")
                    host_for_ingest = mdns_hostname(sensor_id_for_step)
                    services.mqtt_ingest.add_client(host_for_ingest)
                    printDM(f"[onboard] nudged discovery for {host_for_ingest}", location="saiWebRoutes")
                except Exception as e:
                    printDM(f"[onboard] add_client nudge failed: {e}", location="saiWebRoutes")


            await _broadcast(job_id, {"done": True})

        asyncio.create_task(run_flow())
        return JSONResponse({"job_id": job_id})


    @router.get("/scan-nodus-setup")
    async def scan_nodus_setup(ssid: str = Query(None)):
        target_ssid = (ssid or "").strip()
        factory_target_ssid = "Nodus_Setup"
        ap_password = ""
        current_ssid = ""
        current_is_nodus_setup = False
        try:
            from . import saiAddDevice
            factory_target_ssid = (getattr(saiAddDevice, "PICOW_AP_SSID", "") or "").strip() or "Nodus_Setup"
            ap_password = str(getattr(saiAddDevice, "PICOW_AP_PASSWORD", "") or "")
            if platform.system().lower() != "darwin":
                current_ssid = await asyncio.to_thread(getattr(saiAddDevice, "_get_current_ssid", lambda: ""))
            current_is_nodus_setup = saiAddDevice.is_nodus_setup_ssid(current_ssid)
        except Exception:
            pass
        found, msg, matching_ssids = await asyncio.to_thread(_scan_for_ssid, target_ssid)
        if not target_ssid and current_is_nodus_setup and current_ssid not in matching_ssids:
            matching_ssids = sorted([*matching_ssids, current_ssid])
            found = True
        if not target_ssid and matching_ssids:
            target_ssid = matching_ssids[0]
        elif not target_ssid:
            target_ssid = factory_target_ssid
        sys_name = platform.system()
        manual_join_required = False
        if sys_name.lower() == "darwin":
            manual_join_required = True
            already_connected = bool(target_ssid) and (current_ssid or "").strip() == target_ssid
            found = bool(found) or already_connected
            if already_connected:
                msg = f"macOS connected to {target_ssid}"
            elif found:
                msg = f"{target_ssid} found. Join it in macOS Wi-Fi, then click Add."
            else:
                detail = str(msg or "").strip()
                if not detail or detail == "ok":
                    msg = "Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
                elif "airport tool not found" in detail.lower():
                    msg = "Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
                else:
                    msg = f"{detail}. Enter the home Wi-Fi credentials below, join a Nodus setup network in macOS Wi-Fi, then click Add."
        return JSONResponse({
            "ssid": target_ssid,
            "ssids": matching_ssids,
            "password": ap_password,
            "found": bool(found),
            "platform": sys_name,
            "current_ssid": str(current_ssid or "").strip(),
            "manual_join_required": manual_join_required,
            "message": msg,
        })


    try:
        services.mqtt_ingest.set_onboarding_event_handler(_handle_onboarding_event)
    except Exception as e:
        if DEBUG:
            printDM(f"Failed to set onboarding MQTT callback: {e}", location=MODULE)
    for _session in onboarding_store.list_active_sessions():
        _sid = str(_session.get("session_id", "") or "").strip()
        if _sid:
            _ensure_v2_session_monitor(_sid)

    return _onboarding_v2_enabled
