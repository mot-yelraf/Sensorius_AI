"""Register local and Nodus calibration workflows and shared settings helpers.

The handlers preserve calibration payloads, acknowledgement timing, settings
writes, and runtime notifications. App-owned state keeps progress independent
between hubs; no web-route imports or process-wide service lookups are used.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Callable
import time

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from .saiRuntimeServices import RuntimeServices
from .saiWebRouteState import RouteState
from .saiUtils import printDM, normalize_sensor_id

MODULE = "saiWebRoutes"

from dataclasses import dataclass
from .saiInputUnits import calibration_input_offsets, calibration_presentation
from .saiSensorSettingsManager import is_direct_local_sensor_id

_SOIL_TEMP_CAL_KEYS = ("SOIL_TEMP_CAL_VAL",)
_SOIL_MOISTURE_CAL_KEYS = ("SOIL_MOIST_CAL_VAL", "SOIL_TEMP_MOIST_VAL")
_SOIL_PH_CAL_KEYS = ("SOIL_PH_CAL_VAL",)
_SOIL_EC_CAL_KEYS = ("SOIL_EC_CAL_VAL",)


@dataclass(frozen=True)
class CalibrationHelpers:
    """Share calibration presentation with the existing sensor settings pane."""

    soil_offset_value: Callable
    soil_device_offsets: Callable
    append_system_altitude_calibration: Callable


def register_calibration_routes(
    router: APIRouter, *, services: RuntimeServices, route_state: RouteState,
    settings, settings_factory: Callable, sensor_settings_factory: Callable,
    get_host_lock: Callable, ack_timeout: float, result_timeout: float,
    debug: bool = False,
) -> CalibrationHelpers:
    """Register calibration routes and return helpers used by sensor settings."""
    saiSettings = settings_factory
    SensorSettingsManager = sensor_settings_factory
    _get_host_lock = get_host_lock
    _NODUS_CALIBRATION_ACK_TIMEOUT_SEC = ack_timeout
    _NODUS_CALIBRATION_RESULT_TIMEOUT_SEC = result_timeout
    DEBUG = debug

    def _is_remote_nodus_type(sensor_type: str | None) -> bool:
        return str(sensor_type or "").strip().lower() in ("picow", "pico2w", "nodus", "remote")


    def _load_sensor_settings_with_direct_local_repair(manager: SensorSettingsManager, sensor_id: str) -> dict:
        doc = manager.load(sensor_id) or {}
        if is_direct_local_sensor_id(sensor_id):
            try:
                if manager.ensure_direct_local_type(sensor_id):
                    doc = manager.load(sensor_id) or {}
            except Exception as exc:
                if DEBUG:
                    printDM(f"Direct local sensor type repair failed for {sensor_id}: {exc}", location=MODULE)
        return doc


    def _sensor_id_looks_direct_local(sensor_id: str | None) -> bool:
        return is_direct_local_sensor_id(sensor_id)


    def _active_local_sensor_controller(sensor_id: str | None):
        sid_norm = normalize_sensor_id(str(sensor_id or ""))
        if not sid_norm:
            return None

        def _controller_id(candidate) -> str:
            sensor_obj = getattr(candidate, "sensor", None)
            for obj in (candidate, sensor_obj):
                if obj is None:
                    continue
                value = getattr(obj, "sensor_id", None)
                if value:
                    return normalize_sensor_id(str(value))
            return ""

        maps = [services.sensor_map]
        for smap in maps:
            if not smap:
                continue
            if isinstance(smap, dict):
                direct = smap.get(sid_norm) or smap.get(sid_norm.lower()) or smap.get(sid_norm.upper())
                if direct:
                    return direct
                iterable = list(smap.values())
            else:
                try:
                    iterable = list(smap)
                except TypeError:
                    iterable = []
            for candidate in iterable:
                if _controller_id(candidate).lower() == sid_norm.lower():
                    return candidate
        return None


    def _sensor_uses_remote_calibration(sensor_id: str | None, sensor_block: dict | None) -> bool:
        if _active_local_sensor_controller(sensor_id) is not None:
            return False
        if _sensor_id_looks_direct_local(sensor_id):
            return False
        block = sensor_block if isinstance(sensor_block, dict) else {}
        sensor_type = str(block.get("TYPE", block.get("type", "")) or "").strip().lower()
        return _is_remote_nodus_type(sensor_type)


    def _soil_offset_value(section: dict, keys: tuple[str, ...], _get_float, default: float = 0.0) -> float:
        if not isinstance(section, dict):
            return default
        for key in keys:
            if key in section:
                return _get_float(section, key, default)
        return default


    def _soil_device_offsets(device_section: dict, _get_float) -> list[dict]:
        return [
            {
                "key": "soil_moisture_offset",
                "label": "Soil Moisture",
                "unit": "%",
                "value": _soil_offset_value(device_section, _SOIL_MOISTURE_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_temp_offset",
                "label": "Soil Temperature",
                "unit": "°C",
                "value": _soil_offset_value(device_section, _SOIL_TEMP_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_ph_offset",
                "label": "Soil pH",
                "unit": "pH",
                "value": _soil_offset_value(device_section, _SOIL_PH_CAL_KEYS, _get_float),
            },
            {
                "key": "soil_ec_offset",
                "label": "Soil EC",
                "unit": "",
                "value": _soil_offset_value(device_section, _SOIL_EC_CAL_KEYS, _get_float),
            },
        ]


    def _system_altitude_meters() -> float | None:
        def _coerce_altitude(raw) -> float | None:
            try:
                if raw is None or str(raw).strip() == "":
                    return None
                altitude = float(raw)
                return altitude if -500.0 <= altitude <= 10000.0 else None
            except Exception:
                return None

        for source in (
            lambda: saiSettings(apply_live=False).get_setting("Astral", "ALTITUDE", ""),
            lambda: settings.get_setting("Astral", "ALTITUDE", ""),
        ):
            try:
                altitude = _coerce_altitude(source())
                if altitude is not None:
                    return altitude
            except Exception:
                continue
        return None


    def _device_supports_altitude_calibration(device_kind: str) -> bool:
        return str(device_kind or "").strip().lower() in {
            "aqi",
            "bme680",
            "bme688",
            "co2",
            "scd30",
            "scd4x",
            "vpd",
            "avpd",
            "bme280",
            "apvpd",
        }


    def _append_system_altitude_calibration(device_offsets: list[dict], device_kind: str) -> None:
        altitude = _system_altitude_meters()
        if altitude is None or not _device_supports_altitude_calibration(device_kind):
            return
        device_offsets.append(
            {
                "key": "Calibration.Device.ALTITUDE_METERS",
                "label": "System Altitude",
                "unit": "m",
                "value": altitude,
                "readonly": True,
                "force_send": True,
                "title": "Altitude is edited in General Settings.",
            }
        )


    def _apply_device_offsets_shadow(sensor_id: str, device_kind: str, offsets: list[dict]) -> list[str]:
        from collections import OrderedDict
        from collections import OrderedDict as _OD

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = mgr.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            doc = OrderedDict()

        calib = doc.get("Calibration")
        if not isinstance(calib, dict):
            calib = _OD()
            doc["Calibration"] = calib

        def _set_path(path: str, value: float) -> None:
            parts = [p for p in path.split(".") if p]
            if not parts:
                return
            cur = doc
            for seg in parts[:-1]:
                sub = cur.get(seg)
                if not isinstance(sub, dict):
                    sub = _OD()
                    cur[seg] = sub
                cur = sub
            cur[parts[-1]] = value

        applied_keys: list[str] = []
        for item in offsets or []:
            key = str(item.get("key") or "").strip()
            raw_val = item.get("value", 0)
            try:
                val = float(raw_val)
            except Exception:
                continue

            if device_kind == "apvpd" and key in ("ambient_temp_offset", "ambient_rh_offset"):
                if key == "ambient_temp_offset":
                    calib["APVPD_TEMP_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_TEMP_CAL_VAL")
                elif key == "ambient_rh_offset":
                    calib["APVPD_RH_CAL_VAL"] = val
                    applied_keys.append("Calibration.APVPD_RH_CAL_VAL")
                continue

            if device_kind == "soil" and key in (
                "soil_moisture_offset",
                "soil_temp_offset",
                "soil_ph_offset",
                "soil_ec_offset",
            ):
                dev = calib.get("Device")
                if not isinstance(dev, dict):
                    dev = _OD()
                    calib["Device"] = dev

                if key == "soil_moisture_offset":
                    dev["SOIL_MOIST_CAL_VAL"] = val
                    dev["SOIL_TEMP_MOIST_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_MOIST_CAL_VAL")
                elif key == "soil_temp_offset":
                    dev["SOIL_TEMP_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_TEMP_CAL_VAL")
                elif key == "soil_ph_offset":
                    dev["SOIL_PH_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_PH_CAL_VAL")
                elif key == "soil_ec_offset":
                    dev["SOIL_EC_CAL_VAL"] = val
                    applied_keys.append("Calibration.Device.SOIL_EC_CAL_VAL")
                continue

            if not key:
                continue
            _set_path(key, val)
            applied_keys.append(key)

        mgr.save(sensor_id, doc)
        return applied_keys


    def _get_current_device_offset_value(doc: dict, device_kind: str, key: str) -> float | None:
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        calib = doc.get("Calibration") if isinstance(doc, dict) else {}
        if not isinstance(calib, dict):
            calib = {}

        if device_kind == "apvpd" and key in ("ambient_temp_offset", "ambient_rh_offset"):
            if key == "ambient_temp_offset":
                return _to_float(calib.get("APVPD_TEMP_CAL_VAL"))
            if key == "ambient_rh_offset":
                return _to_float(calib.get("APVPD_RH_CAL_VAL"))

        device_cal = calib.get("Device") if isinstance(calib.get("Device"), dict) else {}
        if device_kind == "soil" and key in (
            "soil_moisture_offset",
            "soil_temp_offset",
            "soil_ph_offset",
            "soil_ec_offset",
        ):
            soil_key_map = {
                "soil_moisture_offset": _SOIL_MOISTURE_CAL_KEYS,
                "soil_temp_offset": _SOIL_TEMP_CAL_KEYS,
                "soil_ph_offset": _SOIL_PH_CAL_KEYS,
                "soil_ec_offset": _SOIL_EC_CAL_KEYS,
            }
            for soil_key in soil_key_map[key]:
                current = _to_float(device_cal.get(soil_key))
                if current is not None:
                    return current
            return None

        cur = doc if isinstance(doc, dict) else {}
        for seg in [p for p in str(key or "").split(".") if p]:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(seg)
        return _to_float(cur)


    def _filter_changed_device_offsets(doc: dict, device_kind: str, offsets: list[dict]) -> list[dict]:
        changed: list[dict] = []
        for item in offsets or []:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            force_send = key == "Calibration.Device.ALTITUDE_METERS" and bool(
                item.get("force") or item.get("force_send") or item.get("always_send")
            )
            if key == "Calibration.Device.ALTITUDE_METERS":
                system_altitude = _system_altitude_meters()
                if system_altitude is None:
                    continue
                new_val = system_altitude
            else:
                try:
                    new_val = float(item.get("value", 0))
                except Exception:
                    continue
            current = _get_current_device_offset_value(doc, device_kind, key)
            if not force_send and current is not None and abs(new_val - current) < 1e-9:
                continue
            changed.append({"key": key, "value": new_val})
        return changed


    def _mqtt_calibration_payload_from_offsets(offsets: list[dict]) -> dict:
        return {"offsets": [dict(item) for item in (offsets or [])]}


    def _apply_remote_calibration_patch_shadow(sensor_id: str, patch: dict | None) -> list[str]:
        from collections import OrderedDict

        if not isinstance(patch, dict):
            return []

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = mgr.load(sensor_id) or OrderedDict()
        except FileNotFoundError:
            doc = OrderedDict()

        def _ensure_block(parent: dict, name: str) -> dict:
            block = parent.get(name)
            if not isinstance(block, dict):
                block = OrderedDict()
                parent[name] = block
            return block

        changed = False
        applied_keys: list[str] = []
        for update in (patch.get("updates") or []):
            if not isinstance(update, dict):
                continue
            section = str(update.get("section") or "").strip()
            key = str(update.get("key") or "").strip()
            if not key:
                continue
            section_norm = section.lower()
            if section_norm != "calibration" and not section_norm.startswith("calibration."):
                continue

            current = doc
            for segment in [seg for seg in section.split(".") if seg]:
                current = _ensure_block(current, segment)
            value = update.get("value")
            if current.get(key) != value:
                current[key] = value
                changed = True
            applied_keys.append(f"{section}.{key}" if section else key)

        if changed:
            mgr.save(sensor_id, doc)
        return applied_keys


    async def _sync_remote_calibration_shadow(sensor_id: str, message_id: str, *, timeout: float = 3.0) -> tuple[list[str], bool]:
        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "wait_for_nodus_meta_patch"):
            return [], False
        patch = await ingest.wait_for_nodus_meta_patch(
            message_id,
            source="calibration_set",
            timeout=timeout,
        )
        if not isinstance(patch, dict):
            return [], False
        return _apply_remote_calibration_patch_shadow(sensor_id, patch), True


    async def _remote_calibration_meta_patch_fallback(message_id: str, action: str, *, timeout: float = 3.0) -> dict | None:
        if str(action or "").strip().lower() not in {"apply", "set", "update"}:
            return None
        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "wait_for_nodus_meta_patch"):
            return None
        patch = await ingest.wait_for_nodus_meta_patch(
            message_id,
            source="calibration_set",
            timeout=timeout,
        )
        if not isinstance(patch, dict):
            return None
        return {
            "message_id": message_id,
            "applied": True,
            "updated": len(patch.get("updates") or []),
            "error": "",
            "meta_patch_fallback": True,
        }


    def _resolve_remote_sensor_target(sensor_id: str) -> dict[str, str]:
        sid = str(sensor_id or "").strip()
        ingest = services.mqtt_ingest
        resolver = getattr(ingest, "resolve_nodus_sensor_target", None)
        target = resolver(sid) if callable(resolver) else {}
        device_id = str((target or {}).get("device_id") or "").strip()
        config_file = str((target or {}).get("config_file") or "").strip()

        try:
            doc = SensorSettingsManager("sensor_settings").load(sid) or {}
        except Exception:
            doc = {}
        nodus_block = doc.get("Nodus") if isinstance(doc.get("Nodus"), dict) else {}
        sensor_block = doc.get("Sensor") if isinstance(doc.get("Sensor"), dict) else {}
        device_id = (
            device_id
            or str(nodus_block.get("DEVICE_ID") or "").strip()
            or sid
        )
        config_file = config_file or str(
            nodus_block.get("CONFIG_FILE") or ""
        ).strip()
        if not config_file:
            sensor_kind = str(sensor_block.get("DEVICE") or "").strip().lower()
            config_file = (
                "sensor_soil.toml"
                if sensor_kind.startswith("soil")
                or sensor_kind in {"rs485", "modbus"}
                else "sensor_i2c.toml"
            )
        return {
            "sensor_id": sid,
            "device_id": device_id,
            "config_file": config_file,
        }


    async def _publish_remote_calibration_command(
        sensor_id: str,
        *,
        action: str,
        payload: dict | None = None,
        ack_timeout: float = _NODUS_CALIBRATION_ACK_TIMEOUT_SEC,
        result_timeout: float = _NODUS_CALIBRATION_RESULT_TIMEOUT_SEC,
    ) -> tuple[bool, str, dict | None, dict | None]:
        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_calibration"):
            return False, "MQTT ingest unavailable", None, None

        target = _resolve_remote_sensor_target(sensor_id)
        publish_result = ingest.publish_nodus_calibration(
            target["device_id"],
            action=action,
            payload=payload,
            sensor_id=target["sensor_id"],
            name=target["config_file"],
        )
        if not bool(publish_result.get("ok", False)):
            return False, "Failed to publish calibration command", None, None

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_calibration_ack(message_id, timeout=ack_timeout)
        if not ack or not bool(ack.get("accepted", False)):
            fallback = await _remote_calibration_meta_patch_fallback(message_id, action)
            if fallback is not None:
                return True, "", ack, fallback
            return False, "Calibration command was not acknowledged", ack, None

        result = await ingest.wait_for_calibration_result(message_id, timeout=result_timeout)
        if result is None:
            fallback = await _remote_calibration_meta_patch_fallback(message_id, action)
            if fallback is not None:
                return True, "", ack, fallback
            return False, "Timed out waiting for calibration result", ack, None
        return True, "", ack, result


    async def _publish_remote_device_calibration_offsets(sensor_id: str, offsets: list[dict]) -> tuple[bool, str, int, list[str], bool]:
        target = _resolve_remote_sensor_target(sensor_id)
        target_device = str(target.get("device_id") or "").strip()
        if not target_device:
            return False, "Missing sensor_id.", 400, [], False

        applied_keys: list[str] = []
        shadow_synced = True
        completed_count = 0
        total_count = len(offsets or [])

        async with _get_host_lock(target_device):
            for offset in offsets or []:
                ok, err, ack, result = await _publish_remote_calibration_command(
                    sensor_id,
                    action="apply",
                    payload=_mqtt_calibration_payload_from_offsets([offset]),
                    ack_timeout=_NODUS_CALIBRATION_ACK_TIMEOUT_SEC,
                    result_timeout=_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC,
                )
                if not ok:
                    if completed_count:
                        err = f"{err} after applying {completed_count} of {total_count} calibration value(s)."
                    return False, err, 502, applied_keys, shadow_synced
                if not bool((result or {}).get("applied", False)):
                    err = str((result or {}).get("error") or "Calibration update was rejected.")
                    if completed_count:
                        err = f"{err} after applying {completed_count} of {total_count} calibration value(s)."
                    return False, err, 400, applied_keys, shadow_synced

                completed_count += 1
                message_id = str((result or {}).get("message_id") or (ack or {}).get("message_id") or "").strip()
                one_applied, one_shadow_synced = await _sync_remote_calibration_shadow(sensor_id, message_id)
                applied_keys.extend(one_applied)
                if not one_shadow_synced:
                    shadow_synced = False

        return True, "", 200, applied_keys, shadow_synced


    def _extract_soil_ph_from_sample(sample: dict, fallback_offset: float = 0.0) -> float | None:
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        raw_ph = _to_float(sample.get("raw_ph"))
        if raw_ph is not None:
            return raw_ph

        corrected_ph = _to_float(sample.get("corrected_ph"))
        if corrected_ph is None:
            values = sample.get("values") if isinstance(sample.get("values"), dict) else {}
            corrected_ph = _to_float(values.get("Soil-pH"))
        if corrected_ph is None:
            return None

        offset = _to_float(sample.get("soil_ph_offset"))
        if offset is None:
            offset = fallback_offset
        return corrected_ph - float(offset or 0.0)


    async def _run_remote_soil_ph_session(sensor_id: str, *, reference_ph: float, sample_count: int = 6, sample_interval_s: float = 10.0) -> tuple[bool, str, dict]:
        ingest = services.mqtt_ingest
        if not ingest or not hasattr(ingest, "publish_nodus_calibration"):
            return False, "MQTT ingest unavailable", {}
        if not hasattr(ingest, "wait_for_calibration_samples"):
            return False, "MQTT ingest does not support calibration sample collection", {}

        session_payload = {
            "reference_ph": float(reference_ph),
            "sample_count": int(sample_count),
            "sample_interval_s": float(sample_interval_s),
        }
        publish_result = ingest.publish_nodus_calibration(
            sensor_id,
            action="soil_ph_session_start",
            payload=session_payload,
        )
        if not bool(publish_result.get("ok", False)):
            return False, "Failed to publish soil pH session start", {}

        message_id = str(publish_result.get("message_id") or "").strip()
        ack = await ingest.wait_for_calibration_ack(message_id, timeout=_NODUS_CALIBRATION_ACK_TIMEOUT_SEC)
        if not ack or not bool(ack.get("accepted", False)):
            return False, "Calibration command was not acknowledged", {"ack": ack, "message_id": message_id}

        start_result = await ingest.wait_for_calibration_result(message_id, timeout=_NODUS_CALIBRATION_RESULT_TIMEOUT_SEC)
        if start_result is None:
            return False, "Timed out waiting for soil pH session start result", {"ack": ack, "message_id": message_id}
        if not bool(start_result.get("applied", False)) or not bool(start_result.get("started", False)):
            return False, str(start_result.get("error") or "Soil pH session was rejected."), {
                "ack": ack,
                "message_id": message_id,
                "result": start_result,
            }

        expected_count_raw = start_result.get("sample_count", session_payload["sample_count"])
        interval_raw = start_result.get("sample_interval_s", session_payload["sample_interval_s"])
        try:
            expected_count = max(int(expected_count_raw), 1)
        except Exception:
            expected_count = int(session_payload["sample_count"])
        try:
            sample_interval = max(float(interval_raw), 0.0)
        except Exception:
            sample_interval = float(session_payload["sample_interval_s"])

        sample_timeout = max(15.0, (expected_count * sample_interval) + 10.0)
        samples = await ingest.wait_for_calibration_samples(
            message_id,
            expected_count=expected_count,
            timeout=sample_timeout,
        )
        if len(samples) < expected_count:
            return False, f"Timed out waiting for soil pH samples ({len(samples)}/{expected_count})", {
                "ack": ack,
                "message_id": message_id,
                "result": start_result,
                "samples": samples,
            }

        return True, "", {
            "ack": ack,
            "message_id": message_id,
            "result": start_result,
            "samples": samples,
            "sample_count": expected_count,
            "sample_interval_s": sample_interval,
        }


    @router.post("/calibrate")
    async def calibrate_sensor(sensor_id: str = Query(...)):
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager
        import asyncio

        def _get_sensor_map():
            sm = services.sensor_map
            return sm

        def _resolve_sensor(smap, sid: str):
            if smap is None:
                return None
            sid_norm = normalize_sensor_id(sid)
            if hasattr(smap, "get"):
                hit = smap.get(sid_norm) or smap.get(sid_norm.lower()) or smap.get(sid_norm.upper())
                if hit:
                    return hit
                try:
                    for k, v in getattr(smap, "items")():
                        if str(k).lower() == sid_norm.lower():
                            return v
                except Exception:
                    pass
            try:
                for item in smap:
                    cand = None
                    controller = None
                    if hasattr(item, "sensor") and hasattr(item.sensor, "sensor_id"):
                        cand = getattr(item.sensor, "sensor_id", None)
                        controller = item
                    elif hasattr(item, "sensor_id"):
                        cand = getattr(item, "sensor_id", None)
                        controller = item
                    elif isinstance(item, (tuple, list)) and item:
                        maybe = item[0]
                        if hasattr(maybe, "sensor") and hasattr(maybe.sensor, "sensor_id"):
                            cand = getattr(maybe.sensor, "sensor_id", None)
                            controller = maybe
                        elif hasattr(maybe, "sensor_id"):
                            cand = getattr(maybe, "sensor_id", None)
                            controller = maybe
                    if cand and normalize_sensor_id(str(cand)) == sid_norm:
                        return controller
            except TypeError:
                pass
            return None

        # ---------- 1) Local controller (Pi-attached) ----------
        sensor_map = _get_sensor_map()
        controller = _resolve_sensor(sensor_map, sensor_id)

        if controller and hasattr(controller, "sensor") and hasattr(controller.sensor, "calibrate_plant_sensor"):
            try:
                # Don’t block the HTTP POST for ~60s; fire-and-forget
                _ = asyncio.create_task(controller.sensor.calibrate_plant_sensor())
                return JSONResponse({"status": "started", "source": "local"})
            except Exception as e:
                printDM(f"[calibrate_sensor] local exception: {e}", location="saiWebRoutes")
                return JSONResponse({"status": "error", "message": "Failed to start calibration"}, status_code=500)

        # ---------- 2) Remote Nodus over MQTT ----------
        try:
            mgr = SensorSettingsManager("sensor_settings")
            sid_norm = normalize_sensor_id(sensor_id)
            settings_dict = _load_sensor_settings_with_direct_local_repair(mgr, sid_norm)
            sensor_block = (settings_dict.get("Sensor") or settings_dict.get("sensor") or {})
            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            if _sensor_uses_remote_calibration(sid_norm, sensor_block):
                ok, err, _ack, result = await _publish_remote_calibration_command(
                    sid_norm,
                    action="start",
                    payload=None,
                    ack_timeout=3.0,
                    result_timeout=6.0,
                )
                if not ok:
                    return JSONResponse({"status": "error", "message": err}, status_code=502)

                if bool(result.get("started", False)) or bool(result.get("applied", False)):
                    return JSONResponse({"status": "started", "source": "mqtt", "message_id": result.get("message_id")})

                return JSONResponse(
                    {
                        "status": "error",
                        "message": str(result.get("error") or "Calibration did not start"),
                    },
                    status_code=400,
                )

        except Exception as e:
            printDM(f"[calibrate_sensor] mqtt lookup exception: {e}", location="saiWebRoutes")

        # ---------- 3) Unknown ----------
        return JSONResponse({"status": "error", "message": f"Unknown or unsupported sensor_id: {sensor_id}"}, status_code=404)


    @router.get("/calibration-status")
    async def get_calibration_status(sensor_id: str = Query(...)):
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager

        try:
            sid_norm = normalize_sensor_id(sensor_id)
            mgr = SensorSettingsManager("sensor_settings")
            settings = _load_sensor_settings_with_direct_local_repair(mgr, sid_norm)

            sensor_block = (settings.get("Sensor") or settings.get("sensor") or {})
            cal_block = (settings.get("Calibration") or settings.get("calibration") or {})

            dev_type = str(sensor_block.get("TYPE", sensor_block.get("type", ""))).strip().lower()
            uses_remote_calibration = _sensor_uses_remote_calibration(sid_norm, sensor_block)
            hostname = (sensor_block.get("HOSTNAME") or sensor_block.get("hostname") or "").strip()
            if not hostname:
                # fallback: keep full SENSOR_ID as host hint
                host_src = str(sensor_block.get("SENSOR_ID", sensor_block.get("sensor_id", "") ) or "")
                hostname = host_src if host_src else ""

            # Small helper to pull offsets in a way that matches your APVPD schema
            def _extract_offsets():
                temp_offset_val = cal_block.get("APVPD_TEMP_CAL_VAL")
                rh_offset_val = cal_block.get("APVPD_RH_CAL_VAL")

                # fallback to generic keys, then legacy sensor-level keys
                if temp_offset_val is None:
                    temp_offset_val = cal_block.get("TEMP_OFFSET")
                if rh_offset_val is None:
                    rh_offset_val = cal_block.get("RH_OFFSET")

                if temp_offset_val is None:
                    temp_offset_val = sensor_block.get("THP280_PLANT_TEMP_CAL")
                if rh_offset_val is None:
                    rh_offset_val = sensor_block.get("THP280_PLANT_RH_CAL")

                try:
                    temp_offset = float(temp_offset_val or 0.0)
                except Exception:
                    temp_offset = 0.0
                try:
                    rh_offset = float(rh_offset_val or 0.0)
                except Exception:
                    rh_offset = 0.0

                return temp_offset, rh_offset

            # ───────────────── Pi-attached sensors ─────────────────
            if not uses_remote_calibration:
                ctrl = _active_local_sensor_controller(sid_norm)
                if ctrl and hasattr(ctrl, "sensor"):
                    state = getattr(ctrl.sensor, "is_calibrated", "Not Calibrated")
                    toff, roff = _extract_offsets()
                    return JSONResponse(
                        {
                            "status": "ok",
                            "calibrated": state,
                            "temp_offset": toff,
                            "rh_offset": roff,
                        }
                    )

            # ───────────────── Nodus / remote sensors ─────────────────
            ingest = services.mqtt_ingest
            mqtt_state = None
            if uses_remote_calibration and ingest and hasattr(ingest, "get_nodus_calibration_state"):
                mqtt_state = ingest.get_nodus_calibration_state(sid_norm)
            if mqtt_state:
                progress_state = mqtt_state.get("progress") if isinstance(mqtt_state.get("progress"), dict) else None
                status_state = mqtt_state.get("status") if isinstance(mqtt_state.get("status"), dict) else None
                result_state = mqtt_state.get("result") if isinstance(mqtt_state.get("result"), dict) else None
                live = progress_state or status_state or result_state or {}

                temp_offset = live.get("temp_offset")
                rh_offset = live.get("rh_offset")
                if temp_offset is None or rh_offset is None:
                    temp_offset, rh_offset = _extract_offsets()

                status_text = str(live.get("status") or "").strip().lower()
                calibrated_raw = live.get("calibrated")
                if status_text == "in_progress":
                    body = {
                        "status": "ok",
                        "calibrated": "Calibrating",
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                    if live.get("sample_index") is not None and live.get("sample_total") is not None:
                        body["sample_index"] = live.get("sample_index")
                        body["sample_total"] = live.get("sample_total")
                    return JSONResponse(body)

                if isinstance(calibrated_raw, bool):
                    calibrated_label = "Calibrated" if calibrated_raw else "Not Calibrated"
                elif status_text in {"calibrated", "not_calibrated", "idle", "unavailable"}:
                    calibrated_label = "Calibrated" if status_text == "calibrated" else "Not Calibrated"
                else:
                    calibrated_label = "Not Calibrated"

                response = {
                    "status": "ok",
                    "calibrated": calibrated_label,
                    "temp_offset": temp_offset,
                    "rh_offset": rh_offset,
                }
                err_text = str((result_state or {}).get("error") or live.get("error") or "").strip()
                if err_text:
                    response["error"] = err_text
                return JSONResponse(response)

            # 1) Prefer in-memory state; it carries phase + sample counts.
            state = route_state.calibration_progress.get(sid_norm)
            if state:
                phase = str(state.get("phase", "") or "").strip().lower()
                sample_index = state.get("sample_index")
                sample_total = state.get("sample_total")
                cached_calibrated = state.get("calibrated")

                temp_offset, rh_offset = _extract_offsets()

                # While in progress, always report "Calibrating"
                if phase == "in_progress":
                    response_body = {
                        "status": "ok",
                        "calibrated": "Calibrating",
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                    if sample_index is not None and sample_total is not None:
                        response_body["sample_index"] = sample_index
                        response_body["sample_total"] = sample_total
                    return JSONResponse(response_body)

                # After completion (phase == "done"), prefer the in-memory flag
                if phase == "done":
                    if isinstance(cached_calibrated, bool):
                        status_label = "Calibrated" if cached_calibrated else "Not Calibrated"
                    else:
                        # Fallback to TOML if flag not set for some reason
                        temp_offset, rh_offset = _extract_offsets()
                        cal_flag = cal_block.get("CALIBRATED", cal_block.get("calibrated"))
                        if cal_flag is None:
                            cal_flag = sensor_block.get("CALIBRATED", sensor_block.get("calibrated", False))
                        cal_flag = bool(cal_flag)
                        status_label = "Calibrated" if cal_flag else "Not Calibrated"

                    return JSONResponse(
                        {
                            "status": "ok",
                            "calibrated": status_label,
                            "temp_offset": temp_offset,
                            "rh_offset": rh_offset,
                        }
                    )

            # 2) No in-memory state → use final persisted calibration info only
            has_cal_section = bool(cal_block)
            has_cal_flag = (
                "CALIBRATED" in cal_block
                or "calibrated" in cal_block
                or "CALIBRATED" in sensor_block
                or "calibrated" in sensor_block
            )

            if has_cal_section or has_cal_flag:
                temp_offset, rh_offset = _extract_offsets()

                calibrated_flag = cal_block.get("CALIBRATED", cal_block.get("calibrated"))
                if calibrated_flag is None:
                    calibrated_flag = sensor_block.get("CALIBRATED", sensor_block.get("calibrated", False))
                calibrated_flag = bool(calibrated_flag)

                status_label = "Calibrated" if calibrated_flag else "Not Calibrated"

                return JSONResponse(
                    {
                        "status": "ok",
                        "calibrated": status_label,
                        "temp_offset": temp_offset,
                        "rh_offset": rh_offset,
                    }
                )

            return JSONResponse(
                {"status": "error", "message": "Unable to determine calibration status"},
                status_code=502,
            )
        except Exception as e:
            printDM(f"[/calibration-status] error: {e}", location="saiWebRoutes")
            return JSONResponse({"status": "error", "message": "Internal error"}, status_code=500)


    @router.post("/sensor-event")
    async def sensor_event(event: dict):
        """
        Handles push events (MQTT/HTTP) from Nodus or local controllers.

        Expected payload from Nodus (APVPDSensor):

        Progress:
        {
            "event": "calibration_progress",
            "payload": {
                "status": "in_progress",
                "sensor_id": "<id>",
                "timestamp": <any>,          # may be ISO string or numeric
                "sample_index": <int>,
                "sample_total": <int>
            }
        }

        Result:
        {
            "event": "calibration_result",
            "payload": {
                "status": "success" | "failed",
                "sensor_id": "<id>",
                "timestamp": <any>,          # may be ISO string or numeric
                "calibrated": true|false,
                "temp_offset": <float>,
                "rh_offset": <float>,
                "error": "<optional>"
            }
        }
        """
        from .saiUtils import normalize_sensor_id, printDM
        from .saiSensorSettingsManager import SensorSettingsManager
        from collections import OrderedDict
        import time

        evt_name = str((event or {}).get("event", "")).strip().lower()
        payload = (event or {}).get("payload", {}) or {}

        printDM(
            f"[sensor-event] received event='{evt_name}' for sensor_id='{payload.get('sensor_id', '')}'",
            location="saiWebRoutes",
        )

        # ── normalize sensor_id ──────────────────────────────────────────────────
        sid_raw = str(payload.get("sensor_id", "") or "").strip()
        if not sid_raw:
            return JSONResponse({"status": "error", "message": "payload.sensor_id required"}, status_code=400)
        sensor_id = normalize_sensor_id(sid_raw)

        # Small helpers
        def _to_float(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        def _safe_epoch(_value):
            """Always use local epoch time; ignore device-provided timestamps."""
            return time.time()

        mgr = SensorSettingsManager("sensor_settings")

        # =========================================================================
        # 1) calibration_progress  (IN-MEMORY ONLY, no TOML writes)
        # =========================================================================
        if evt_name == "calibration_progress":
            status_text = str(payload.get("status", "") or "").strip().lower()
            sample_index = payload.get("sample_index")
            sample_total = payload.get("sample_total")
            updated_ts = _safe_epoch(payload.get("timestamp"))

            try:
                sample_index_int = int(sample_index) if sample_index is not None else None
            except Exception:
                sample_index_int = None
            try:
                sample_total_int = int(sample_total) if sample_total is not None else None
            except Exception:
                sample_total_int = None

            updated: dict[str, object] = OrderedDict()

            # In-memory scratchpad only; do not touch TOML here
            try:
                route_state.calibration_progress[sensor_id] = {
                    "phase": "in_progress",
                    "calibrated": None,  # not known yet
                    "sample_index": sample_index_int,
                    "sample_total": sample_total_int,
                    "updated_at": updated_ts,
                }
            except Exception as e:
                printDM(f"[sensor-event] progress cache update failed: {e}", location="saiWebRoutes")

            # Live controller nudge so the UI can show "Calibrating" without a reload
            try:
                sensor_map = services.sensor_map
                ctrl = sensor_map.get(sensor_id) if isinstance(sensor_map, dict) else None
                if ctrl and hasattr(ctrl, "sensor") and hasattr(ctrl.sensor, "is_calibrated"):
                    try:
                        ctrl.sensor.is_calibrated = "Calibrating"
                    except Exception:
                        pass
            except Exception as e:
                printDM(f"[sensor-event] progress live controller update skipped: {e}", location="saiWebRoutes")

            return JSONResponse(
                {
                    "status": "ok",
                    "kind": "progress",
                    "sensor_id": sensor_id,
                    "sample_index": sample_index_int,
                    "sample_total": sample_total_int,
                    "updated": updated,
                }
            )

        # =========================================================================
        # 2) calibration_result  (FINAL; persist to TOML)
        # =========================================================================
        if evt_name != "calibration_result":
            # Unknown / unhandled event type
            return JSONResponse({"status": "ignored"})

        status_text = str(payload.get("status", "") or "").strip().lower()  # "success" | "failed"
        calibrated_flag = bool(payload.get("calibrated", status_text == "success"))
        error_text = str(payload.get("error", "") or "").strip()

        temp_offset = _to_float(payload.get("temp_offset"))
        rh_offset = _to_float(payload.get("rh_offset"))
        updated_ts = _safe_epoch(payload.get("timestamp"))

        # Canonical boolean
        calib_status_bool = bool(calibrated_flag)

        # Update in-memory state to "done" (we no longer pop immediately)
        try:
            route_state.calibration_progress[sensor_id] = {
                "phase": "done",
                "calibrated": calib_status_bool,
                "sample_index": None,
                "sample_total": None,
                "updated_at": updated_ts,
            }
        except Exception as e:
            printDM(f"[sensor-event] result cache update failed: {e}", location="saiWebRoutes")

        updated: dict[str, object] = OrderedDict()

        # ── decide which keys to use based on existing schema ────────────────────
        existing = mgr.load(sensor_id) or {}
        sensor_block = (existing.get("Sensor") or existing.get("sensor") or {})
        cal_block = (existing.get("Calibration") or existing.get("calibration") or {})

        # APVPD sensors: prefer APVPD_TEMP_CAL_VAL / APVPD_RH_CAL_VAL
        temp_key = "Calibration.TEMP_OFFSET"
        rh_key = "Calibration.RH_OFFSET"
        if "APVPD_TEMP_CAL_VAL" in cal_block or "APVPD_RH_CAL_VAL" in cal_block:
            temp_key = "Calibration.APVPD_TEMP_CAL_VAL"
            rh_key = "Calibration.APVPD_RH_CAL_VAL"

        # ── persist to local TOML (FINAL ONLY) ───────────────────────────────────
        try:
            mgr.update_setting(sensor_id, "Calibration.CALIBRATED", calib_status_bool)
            updated["Calibration.CALIBRATED"] = calib_status_bool

            if temp_offset is not None:
                mgr.update_setting(sensor_id, temp_key, temp_offset)
                updated[temp_key] = temp_offset
            if rh_offset is not None:
                mgr.update_setting(sensor_id, rh_key, rh_offset)
                updated[rh_key] = rh_offset

            # We do *not* write STATUS, LAST_EVENT, UPDATED_AT, SAMPLE_*, RESULT, ERROR, etc.

        except Exception as e:
            printDM(f"[sensor-event] settings write failed: {e}", location="saiWebRoutes")
            return JSONResponse({"status": "error", "message": "Calibration settings could not be saved."}, status_code=500)

        # ── live controller nudge so UI updates instantly (best-effort) ───────────
        try:
            sensor_map = services.sensor_map
            ctrl = sensor_map.get(sensor_id) if isinstance(sensor_map, dict) else None
            if ctrl and hasattr(ctrl, "sensor"):
                if temp_offset is not None and hasattr(ctrl.sensor, "thp280_plant_temp_cal"):
                    setattr(ctrl.sensor, "thp280_plant_temp_cal", float(temp_offset))
                if rh_offset is not None and hasattr(ctrl.sensor, "thp280_plant_rh_cal"):
                    setattr(ctrl.sensor, "thp280_plant_rh_cal", float(rh_offset))
                if hasattr(ctrl.sensor, "is_calibrated"):
                    try:
                        ctrl.sensor.is_calibrated = "Calibrated" if calib_status_bool else "Not Calibrated"
                    except Exception:
                        pass
        except Exception as e:
            printDM(f"[sensor-event] live controller update skipped: {e}", location="saiWebRoutes")

        return JSONResponse(
            {
                "status": "ok",
                "kind": "result",
                "sensor_id": sensor_id,
                "result": status_text,
                "calibrated": calib_status_bool,
                "updated": updated,
            }
        )


    @router.post("/calibration/device/apply", response_class=JSONResponse)
    async def device_calibration_apply(request: Request):
        """
        Apply per-device calibration offsets for a single sensor.

        Expected JSON body from JS:

            {
              "sensor_id": "co2-i2c-1-sensoria-hub-0",
              "device_kind": "co2",
              "offsets": [
                {"key": "Calibration.Device.TEMP_OFFSET", "value": -0.5},
                {"key": "Calibration.Device.RH_OFFSET",   "value":  1.2},
                {"key": "Calibration.Device.CO2_OFFSET",  "value": -700.0}
              ]
            }

        For non-APVPD devices, each 'key' is a dotted path we apply
        into the sensor's TOML (e.g., Calibration.Device.CO2_OFFSET).

        For APVPD, we also accept the simple keys:
          - ambient_temp_offset -> Calibration.APVPD_TEMP_CAL_VAL
          - ambient_rh_offset   -> Calibration.APVPD_RH_CAL_VAL

        For SoilModbusSensor ("soil" device_kind), we accept soil-specific
        short keys which map into [Calibration.Device]:

          - soil_moisture_offset -> Calibration.Device.SOIL_MOIST_CAL_VAL
                                      (legacy SOIL_TEMP_MOIST_VAL is still read)
          - soil_temp_offset     -> Calibration.Device.SOIL_TEMP_CAL_VAL
          - soil_ph_offset       -> Calibration.Device.SOIL_PH_CAL_VAL
          - soil_ec_offset       -> Calibration.Device.SOIL_EC_CAL_VAL
        """
        from .saiUtils import printDM
        from .saiCalibration import notify_sensor_runtime_of_calibration

        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON payload."},
                status_code=400,
            )

        sensor_id = str(payload.get("sensor_id") or "").strip()
        device_kind = str(payload.get("device_kind") or "").strip().lower()
        offsets = payload.get("offsets") or []

        if not sensor_id:
            return JSONResponse(
                {"status": "error", "message": "Missing sensor_id."},
                status_code=400,
            )
        if not offsets:
            return JSONResponse(
                {"status": "error", "message": "No offsets provided."},
                status_code=400,
            )

        try:
            offsets = calibration_input_offsets(offsets)
        except (ValueError, TypeError, AttributeError) as exc:
            printDM(f"Invalid calibration offsets: {exc}", location=MODULE)
            return JSONResponse({"status": "error", "message": "Invalid calibration offsets. Check numeric values and units."}, status_code=400)

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = _load_sensor_settings_with_direct_local_repair(mgr, sensor_id)
        except FileNotFoundError:
            doc = {}
        offsets = _filter_changed_device_offsets(doc, device_kind, offsets)
        if not offsets:
            return JSONResponse(
                {"status": "error", "message": "No calibration changes detected."},
                status_code=400,
            )

        sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
        sensor_type = str(sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()
        uses_remote_calibration = _sensor_uses_remote_calibration(sensor_id, sensor_blk)

        if uses_remote_calibration:
            ok, err, status_code, applied_keys, shadow_synced = await _publish_remote_device_calibration_offsets(
                sensor_id,
                offsets,
            )
            if not ok:
                body = {"status": "error", "message": err, "shadow_synced": shadow_synced}
                if applied_keys:
                    body["applied"] = applied_keys
                return JSONResponse(body, status_code=status_code)
        else:
            try:
                applied_keys = _apply_device_offsets_shadow(sensor_id, device_kind, offsets)
            except Exception as exc:
                printDM(f"[{MODULE}] device_calibration_apply save error: {exc}", location=MODULE)
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"Failed to save device calibration for {sensor_id}.",
                    },
                    status_code=500,
                )
            shadow_synced = True

        if not uses_remote_calibration:
            try:
                supervisor = services.supervisor
                notify_sensor_runtime_of_calibration(supervisor, sensor_id)
            except Exception as exc:
                printDM(
                    f"[{MODULE}] device_calibration_apply reload error for {sensor_id}: {exc}",
                    location=MODULE,
                )


        if uses_remote_calibration and not shadow_synced:
            msg = f"Accepted calibration update for {sensor_id}; awaiting meta patch shadow sync."
        else:
            msg = f"Updated {len(applied_keys)} device calibration value(s) for {sensor_id}."
        return JSONResponse(
            {
                "status": "success",
                "message": msg,
                "applied": applied_keys,
                "shadow_synced": shadow_synced,
            }
        )


    @router.post("/calibration/soil/ph-buffer", response_class=JSONResponse)
    async def soil_ph_buffer_calibration(request: Request):
        """
        Calibrate a soil pH sensor against a known buffer solution by deriving
        the required pH offset from the latest Soil-pH reading.
        """
        from .saiCalibration import notify_sensor_runtime_of_calibration

        try:
            payload = await request.json()
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "message": "Invalid JSON payload."},
                status_code=400,
            )

        sensor_id = normalize_sensor_id(str(payload.get("sensor_id") or ""))
        buffer_raw = payload.get("buffer_ph")

        if not sensor_id:
            return JSONResponse(
                {"status": "error", "message": "Missing sensor_id."},
                status_code=400,
            )

        try:
            buffer_ph = float(buffer_raw)
        except Exception:
            return JSONResponse(
                {"status": "error", "message": "buffer_ph must be numeric."},
                status_code=400,
            )

        if buffer_ph not in (4.0, 7.0, 10.0):
            return JSONResponse(
                {"status": "error", "message": "buffer_ph must be one of 4.0, 7.0, or 10.0."},
                status_code=400,
            )

        mgr = SensorSettingsManager("sensor_settings")
        try:
            doc = _load_sensor_settings_with_direct_local_repair(mgr, sensor_id)
        except FileNotFoundError:
            return JSONResponse(
                {"status": "error", "message": f"Unknown sensor_id '{sensor_id}'."},
                status_code=404,
            )

        sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
        device_kind = str(sensor_blk.get("DEVICE") or sensor_blk.get("device") or "").strip().lower()
        sensor_type = str(sensor_blk.get("TYPE") or sensor_blk.get("type") or "").strip().lower()
        uses_remote_calibration = _sensor_uses_remote_calibration(sensor_id, sensor_blk)
        if device_kind != "soil":
            return JSONResponse(
                {"status": "error", "message": f"Sensor '{sensor_id}' is not a soil sensor."},
                status_code=400,
            )

        if uses_remote_calibration:
            current_offset = 0.0
            try:
                current_offset = float(
                    ((doc.get("Calibration") or {}).get("Device") or {}).get("SOIL_PH_CAL_VAL", 0.0) or 0.0
                )
            except Exception:
                current_offset = 0.0

            ok, err, session = await _run_remote_soil_ph_session(sensor_id, reference_ph=buffer_ph)
            if not ok:
                return JSONResponse({"status": "error", "message": err}, status_code=502)

            samples = session.get("samples") if isinstance(session.get("samples"), list) else []
            raw_values = [
                value
                for value in (_extract_soil_ph_from_sample(sample, fallback_offset=current_offset) for sample in samples)
                if value is not None
            ]
            if not raw_values:
                return JSONResponse(
                    {
                        "status": "error",
                        "message": "No valid soil pH samples were collected.",
                    },
                    status_code=502,
                )
            current_ph = round(sum(raw_values) / len(raw_values), 4)
            new_offset = round(buffer_ph - current_ph, 4)
            offsets = [{"key": "soil_ph_offset", "value": new_offset}]

            ok, err, _ack, result = await _publish_remote_calibration_command(
                sensor_id,
                action="apply",
                payload=_mqtt_calibration_payload_from_offsets(offsets),
                ack_timeout=3.0,
                result_timeout=8.0,
            )
            if not ok:
                return JSONResponse({"status": "error", "message": err}, status_code=502)
            if not bool(result.get("applied", False)):
                return JSONResponse(
                    {
                        "status": "error",
                        "message": str(result.get("error") or "Calibration update was rejected."),
                    },
                    status_code=400,
                )
            message_id = str((result or {}).get("message_id") or (_ack or {}).get("message_id") or "").strip()
            applied_keys, shadow_synced = await _sync_remote_calibration_shadow(sensor_id, message_id)
        else:
            try:
                latest = services.data_logger.get_latest_values(sensor_id) or {}
            except Exception as exc:
                printDM(f"Failed to read latest values for {sensor_id}: {exc}", location=MODULE)
                return JSONResponse(
                    {"status": "error", "message": "Failed to read latest sensor values."},
                    status_code=500,
                )

            current_raw = latest.get("Soil-pH")
            try:
                current_ph = float(current_raw)
            except Exception:
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"No recent Soil-pH reading is available for {sensor_id}.",
                    },
                    status_code=409,
                )

            new_offset = round(buffer_ph - current_ph, 4)
            offsets = [{"key": "soil_ph_offset", "value": new_offset}]
            try:
                applied_keys = _apply_device_offsets_shadow(sensor_id, device_kind, offsets)
            except Exception as exc:
                printDM(f"[{MODULE}] soil_ph_buffer_calibration save error: {exc}", location=MODULE)
                return JSONResponse(
                    {
                        "status": "error",
                        "message": f"Failed to save soil pH calibration for {sensor_id}.",
                    },
                    status_code=500,
                )
            shadow_synced = True

        if not uses_remote_calibration:
            try:
                supervisor = services.supervisor
                notify_sensor_runtime_of_calibration(supervisor, sensor_id)
            except Exception as exc:
                printDM(
                    f"[{MODULE}] soil_ph_buffer_calibration reload error for {sensor_id}: {exc}",
                    location=MODULE,
                )

        return JSONResponse(
            {
                "status": "success",
                "message": (
                    f"Calibrated {sensor_id} to pH {buffer_ph:.1f}."
                    if shadow_synced
                    else f"Calibrated {sensor_id} to pH {buffer_ph:.1f}; awaiting meta patch shadow sync."
                ),
                "sensor_id": sensor_id,
                "buffer_ph": buffer_ph,
                "measured_ph": current_ph,
                "soil_ph_offset": new_offset,
                "applied": applied_keys,
                "shadow_synced": shadow_synced,
            }
        )


    @router.get("/ui/modal/system-calibration", response_class=HTMLResponse)
    async def modal_system_calibration(
        request: Request,
        sensor_id: str = Query(""),
    ):
        """
        Serve the Device & System Calibration modal as a Jinja template fragment.
        """
        from .saiCalibration import CalibrationManager

        templates = request.app.state.templates

        # All known sensors (for system-calibration list)
        all_sensor_ids = settings.get_all_sensor_ids() or []

        # Which sensor is "in focus" for device calibration?
        start_sensor_id = sensor_id or (all_sensor_ids[0] if all_sensor_ids else "")

        # Load per-sensor TOML via SensorSettingsManager
        sensor_mgr = SensorSettingsManager("sensor_settings")
        sensor_doc = {}
        if start_sensor_id:
            try:
                sensor_doc = sensor_mgr.load(start_sensor_id)
            except FileNotFoundError:
                sensor_doc = {}

        sensor_section = (sensor_doc.get("Sensor") or {}) or {}
        calib_section  = (sensor_doc.get("Calibration") or {}) or {}
        device_section = (calib_section.get("Device") or calib_section.get("device") or {}) or {}

        # Basic identity for left-side panel
        raw_device   = str(sensor_section.get("DEVICE", "") or "")
        device_kind  = raw_device.strip().lower()
        device_label = raw_device or device_kind or "Unknown"
        sensor_location = str(sensor_section.get("LOCATION", "") or "")

        is_apvpd = (device_kind == "apvpd")

        def _get_float(section: dict, key: str, default: float = 0.0) -> float:
            try:
                return float(section.get(key, default) or default)
            except Exception:
                return default

        # APVPD-specific ambient offsets, fallback to 0.0 if missing
        ambient_temp_offset = _get_float(calib_section, "APVPD_TEMP_CAL_VAL", 0.0)
        ambient_rh_offset   = _get_float(calib_section, "APVPD_RH_CAL_VAL", 0.0)
        soil_ph_offset      = _soil_offset_value(device_section, _SOIL_PH_CAL_KEYS, _get_float)

        # ---- Build per-device offsets for non-APVPD devices -----------------
        device_offsets: list[dict] = []

        def _add_offset(key_path: str, label: str, unit: str, field_key: str, default_val: float = 0.0) -> None:
            value = _get_float(device_section, field_key, default_val)
            device_offsets.append(
                {
                    "key": key_path,   # e.g. "Calibration.Device.CO2_OFFSET"
                    "label": label,    # e.g. "CO₂"
                    "unit": unit,      # e.g. "ppm"
                    "value": value,    # numeric value
                }
            )

        # CO2 devices (e.g. SCD30/SCD4x → DEVICE="co2")
        if device_kind in ("co2", "scd30", "scd4x"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.CO2_OFFSET",  "CO₂", "ppm", "CO2_OFFSET")

        # AQI devices (BME680/BME688 → DEVICE="aqi")
        elif device_kind in ("aqi", "bme680", "bme688"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
            _add_offset("Calibration.Device.AQI_OFFSET",  "AQI", "", "AQI_OFFSET")
            _add_offset("Calibration.Device.GAS_OFFSET",  "Gas resistance", "kΩ", "GAS_OFFSET")

        # LUX devices (VEML7700 → DEVICE="veml"; allow "lux" synonym)
        elif device_kind in ("veml", "lux"):
            # User-visible metrics that should be adjustable:
            #   - "Light Intensity" (lux)  → LUX_OFFSET
            #   - "Estimated PPFD" (µmol/m²/s) → PPFD_OFFSET
            _add_offset(
                "Calibration.Device.LUX_OFFSET",
                "Light Intensity",
                "lux",
                "LUX_OFFSET",
            )
            _add_offset(
                "Calibration.Device.PPFD_OFFSET",
                "Estimated PPFD",
                "µmol/m²/s",
                "PPFD_OFFSET",
            )

        # Non-APVPD VPD sensors (DEVICE="vpd" or "avpd")
        elif device_kind in ("vpd", "avpd", "bme280", "aht", "aht10", "ahtx0"):
            _add_offset("Calibration.Device.TEMP_OFFSET", "Temperature", "°C", "TEMP_OFFSET")
            _add_offset("Calibration.Device.RH_OFFSET",   "Rel-Humidity", "%", "RH_OFFSET")
        elif device_kind in ("soil",):
            device_offsets.extend(_soil_device_offsets(device_section, _get_float))
        _append_system_altitude_calibration(device_offsets, device_kind)

        # NOTE: APVPD still uses the dedicated is_apvpd branch in the template.
        # If you later want APVPD to also use generic device_offsets, you can
        # add "apvpd" to the elif above, but right now we preserve existing behavior.

        # System calibration candidates via CalibrationManager (unchanged)
        cal_mgr = CalibrationManager(services.data_logger, sensor_mgr)
        candidate_sensors = cal_mgr.get_calibratable_sensors() or []

        context = {
            "request": request,
            "start_sensor_id": start_sensor_id,
            "sensor_location": sensor_location,
            "device_kind": device_kind,
            "device_label": device_label,
            "is_apvpd": is_apvpd,
            "is_soil": (device_kind == "soil"),
            **calibration_presentation(device_offsets, ambient_temp_offset,
                saiSettings(apply_live=False).get_setting("Display", "unit_system", "Imperial")),
            "ambient_rh_offset": ambient_rh_offset,
            "soil_ph_offset": soil_ph_offset,
            "device_offsets": device_offsets,
            "candidate_sensors": candidate_sensors,
            "default_range_hours": 24,
        }

        return templates.TemplateResponse(
            "modals/system_calibration.html",
            context,
        )


    @router.post("/system-calibration/preview", response_class=JSONResponse)
    async def system_calibration_preview(
        reference_id: str = Form(...),
        range_hours: int = Form(24),
        sensor_ids: str = Form(""),   # comma-separated list; empty => all calibratable
    ):
        """
        Compute *system calibration* preview values for selected sensors.

        Returns, per sensor:
        - raw & adjusted Temperature
        - raw & adjusted Rel-Humidity
        - sigma (RMS error) for Temperature & RH
        """
        import time
        from .saiCalibration import CalibrationManager
        from .saiSensorSettingsManager import SensorSettingsManager

        # Parse/normalize inputs
        try:
            hours = int(range_hours)
        except Exception:
            hours = 24
        if hours <= 0:
            hours = 24

        selected_ids = [
            s.strip()
            for s in (sensor_ids or "").split(",")
            if s.strip()
        ]
        selected_set = set(selected_ids) if selected_ids else None

        end_ts = time.time()
        start_ts = end_ts - (hours * 3600.0)

        # Build helpers
        sensor_mgr = SensorSettingsManager("sensor_settings")
        cal_mgr = CalibrationManager(services.data_logger, sensor_mgr)

        # Compute offsets + sigmas (no disk writes)
        try:
            results = cal_mgr.compute_system_calibration(
                reference_id=reference_id,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        except Exception as exc:
            if DEBUG:
                printDM(
                    f"[SystemCal] preview compute failed: ref={reference_id}, "
                    f"hours={hours}, err={exc}",
                    location=MODULE,
                )
            return JSONResponse(
                {"ok": False, "error": "Unable to compute calibration. Check the selected reference, time window, and sensor data."},
                status_code=400,
            )

        # If compute_system_calibration returned nothing, tell the UI explicitly.
        if not results:
            msg = (
                "No sensors had sufficient overlapping Temperature/Rel-Humidity data "
                f"in the last {hours} hours to compute a system calibration preview."
            )
            if DEBUG:
                printDM(f"[SystemCal] preview -> empty results: {msg}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": msg},
                status_code=200,
            )

        # Shape response
        payload: dict[str, Any] = {
            "ok": True,
            "reference_id": reference_id,
            "range_hours": hours,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "sensors": [],
        }

        for sid, res in results.items():
            # Optional: restrict to subset chosen by user
            if selected_set and sid not in selected_set:
                continue

            latest = services.data_logger.get_latest_values(sid) or {}

            # Prefer ambient, fall back to plant metrics
            raw_temp = None
            for key in ("Temperature", "Plant Temperature"):
                if key in latest:
                    try:
                        raw_temp = float(latest[key])
                    except Exception:
                        raw_temp = None
                    break

            raw_rh = None
            for key in ("Rel-Humidity", "Plant Rel-Humidity"):
                if key in latest:
                    try:
                        raw_rh = float(latest[key])
                    except Exception:
                        raw_rh = None
                    break

            # Adjusted values based on system offsets
            adj_temp = (raw_temp + res.temp_offset) if raw_temp is not None else None
            adj_rh   = (raw_rh + res.rh_offset)     if raw_rh is not None else None

            payload["sensors"].append(
                {
                    "sensor_id": sid,
                    "raw_temp": raw_temp,
                    "adj_temp": adj_temp,
                    "temp_offset": res.temp_offset,
                    "temp_sigma": res.temp_rms,   # σ for temperature

                    "raw_rh": raw_rh,
                    "adj_rh": adj_rh,
                    "rh_offset": res.rh_offset,
                    "rh_sigma": res.rh_rms,       # σ for RH

                    "n_pairs": res.n_pairs,
                }
            )

        # If filtering by selected sensors removed everything, call that out.
        if not payload["sensors"]:
            msg = (
                "Selected sensors had no valid calibration data in the chosen window. "
                "Try a different reference sensor or time range."
            )
            if DEBUG:
                printDM(f"[SystemCal] preview -> no sensors after filter: {msg}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": msg},
                status_code=200,
            )

        if DEBUG:
            printDM(
                f"[SystemCal] preview ok: ref={reference_id}, "
                f"sensors={len(payload['sensors'])}, hours={hours}",
                location=MODULE,
            )

        return JSONResponse(payload)


    @router.post("/system-calibration/apply", response_class=JSONResponse)
    async def system_calibration_apply(request: Request):
        """
        Apply previously-computed SYSTEM calibration offsets for selected sensors.

        Expects JSON body from the UI like:

        {
          "reference_id": "co2-i2c-1-sensoria-hub-0",
          "start_ts": 1733050000.0,
          "end_ts":   1733136400.0,
          "sensors": [
            {
              "sensor_id": "apvpd-luvk44",
              "temp_offset": 3.129,
              "temp_sigma": 0.260,
              "rh_offset": -6.257,
              "rh_sigma": 0.389,
              "n_pairs": 4152
            },
            ...
          ]
        }
        """
        from .saiCalibration import CalibrationManager, SystemCalResult

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON payload."},
                status_code=400,
            )

        reference_id = str(payload.get("reference_id") or "").strip()
        sensors_data = payload.get("sensors") or []
        start_ts = payload.get("start_ts")
        end_ts = payload.get("end_ts")

        if not sensors_data:
            return JSONResponse(
                {"ok": False, "error": "No sensors provided for calibration."},
                status_code=400,
            )

        # Fallbacks for start/end if omitted
        import time as _time
        now = _time.time()
        if start_ts is None or end_ts is None:
            # default to last 24h if window missing
            end_ts = now
            start_ts = end_ts - 24.0 * 3600.0

        try:
            start_ts = float(start_ts)
            end_ts = float(end_ts)
        except Exception:
            end_ts = now
            start_ts = end_ts - 24.0 * 3600.0

        sensor_mgr = SensorSettingsManager("sensor_settings")
        cal_mgr = CalibrationManager(services.data_logger, sensor_mgr)

        applied: list[str] = []
        failures: list[dict] = []

        for entry in sensors_data:
            sensor_id = str(entry.get("sensor_id") or "").strip()
            if not sensor_id:
                continue

            try:
                temp_offset = float(entry.get("temp_offset"))
                rh_offset = float(entry.get("rh_offset"))
            except Exception as exc:
                failures.append(
                    {
                        "sensor_id": sensor_id,
                        "error": "Bad offsets in payload. Provide numeric temperature and humidity offsets.",
                    }
                )
                continue

            try:
                temp_sigma = float(entry.get("temp_sigma") or entry.get("temp_rms") or 0.0)
            except Exception:
                temp_sigma = 0.0
            try:
                rh_sigma = float(entry.get("rh_sigma") or entry.get("rh_rms") or 0.0)
            except Exception:
                rh_sigma = 0.0
            try:
                n_pairs = int(entry.get("n_pairs") or 0)
            except Exception:
                n_pairs = 0

            result = SystemCalResult(
                temp_offset=temp_offset,
                rh_offset=rh_offset,
                temp_rms=temp_sigma,
                rh_rms=rh_sigma,
                n_pairs=n_pairs,
                ref_sensor_id=reference_id,
                start_ts=start_ts,
                end_ts=end_ts,
            )

            try:
                doc = _load_sensor_settings_with_direct_local_repair(sensor_mgr, sensor_id)
                sensor_blk = doc.get("Sensor", {}) if isinstance(doc, dict) else {}
                sensor_type = (
                    sensor_blk.get("TYPE")
                    or sensor_blk.get("type")
                    or ""
                )
                sensor_type = str(sensor_type).strip().lower()
                if _sensor_uses_remote_calibration(sensor_id, sensor_blk):
                    ok, err, _ack, mqtt_result = await _publish_remote_calibration_command(
                        sensor_id,
                        action="apply",
                        payload={
                            "calibration": {
                                "system": {
                                    "TEMP_OFFSET": round(temp_offset, 3),
                                    "RH_OFFSET": round(rh_offset, 3),
                                    "REF_SENSOR_ID": reference_id,
                                    "REF_RANGE_HOURS": int((end_ts - start_ts) / 3600.0),
                                    "REF_START_TS": int(start_ts),
                                    "REF_END_TS": int(end_ts),
                                }
                            }
                        },
                        ack_timeout=3.0,
                        result_timeout=8.0,
                    )
                    if not ok:
                        failures.append({"sensor_id": sensor_id, "error": err})
                        continue
                    if not bool(mqtt_result.get("applied", False)):
                        failures.append(
                            {
                                "sensor_id": sensor_id,
                                "error": str(mqtt_result.get("error") or "Calibration update was rejected."),
                            }
                        )
                        continue
                    message_id = str((mqtt_result or {}).get("message_id") or (_ack or {}).get("message_id") or "").strip()
                    await _sync_remote_calibration_shadow(sensor_id, message_id)
                else:
                    cal_mgr.apply_system_calibration(sensor_id, result)
                applied.append(sensor_id)

            except Exception as exc:
                printDM(f"Calibration apply failed for {sensor_id}: {exc}", location=MODULE)
                failures.append(
                    {
                        "sensor_id": sensor_id,
                        "error": "Calibration could not be applied. Check the Sensorius log.",
                    }
                )
                continue

        return JSONResponse(
            {
                "ok": True,
                "reference_id": reference_id,
                "applied": applied,
                "failures": failures,
            }
        )


    @router.post("/update-calibration-values")
    async def update_calibration_values(request: Request):
        """
        Body:
        {
            "sensor_id": "co2-i2c-1-sensoria-hub-0",
            "offsets": [...],
            "calibration": {...},    # optional nested form
            "is_remote": false       # optional hint
        }
        """
        from .saiCalibration import apply_calibration_updates_local, notify_sensor_runtime_of_calibration

        payload = await request.json()
        sensor_id = (payload.get("sensor_id") or "").strip()
        if not sensor_id:
            return JSONResponse({"error": "sensor_id required"}, status_code=400)

        offsets = payload.get("offsets")
        calib   = payload.get("calibration")
        if DEBUG:
            printDM(f"/update-calibration-values called with payload={payload}", location=MODULE)

        # 1) persist to local sensor_settings/<sensor_id>/sensor.toml
        if offsets is not None:
            apply_calibration_updates_local(sensor_id, offsets)
        elif calib is not None:
            apply_calibration_updates_local(sensor_id, calib)
        else:
            return JSONResponse({"error": "no offsets provided"}, status_code=400)

        # 2) notify local runtime (if this is a local Pi sensor)
        supervisor = services.supervisor if hasattr(request.app.state, "supervisor") else None
        notify_sensor_runtime_of_calibration(supervisor, sensor_id)

        return JSONResponse({"ok": True})


    return CalibrationHelpers(
        _soil_offset_value, _soil_device_offsets, _append_system_altitude_calibration,
    )
