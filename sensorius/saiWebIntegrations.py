"""Register integration settings, status, and connection-test routes.

The registrar uses the owning app's runtime services and explicit settings and
UI callbacks. Existing integration protocols, configuration formats, and response
contracts remain unchanged; no runtime services are created at import time.
"""

import asyncio
import re
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .saiEcowitt import EcowittError
from .saiRuntimeServices import RuntimeServices
from .saiUtils import printDM
from .sensor_modules.station_ecowitt import (
    DEFAULT_POLL_INTERVAL_SEC as ECOWITT_DEFAULT_POLL_INTERVAL_SEC,
    MIN_POLL_INTERVAL_SEC, MAX_POLL_INTERVAL_SEC,
)
from .sensor_modules.station_weewx import (
    DEFAULT_DB_PATH as WEEWX_DEFAULT_DB_PATH,
    DEFAULT_MQTT_TOPIC as WEEWX_DEFAULT_MQTT_TOPIC,
    DEFAULT_SENSOR_ID as WEEWX_DEFAULT_SENSOR_ID,
    DEFAULT_UPDATE_PERIOD_SEC as WEEWX_DEFAULT_UPDATE_PERIOD_SEC,
)

MODULE = "saiWebRoutes"


def _ecowitt_public_error(exc: EcowittError) -> str:
    """Return trusted validation text without forwarding arbitrary exception details."""
    validation_messages = (
        "Ecowitt gateway URL is required.",
        "Ecowitt gateway URL is invalid.",
        "Ecowitt gateway URL must use plain HTTP.",
        "Ecowitt gateway URL must contain a host and no credentials.",
        "Enter only the gateway base URL without a path, query, or fragment.",
        "Ecowitt gateway URL contains an invalid port.",
        "Retrieval interval must be a whole number of seconds.",
        f"Retrieval interval must be between {MIN_POLL_INTERVAL_SEC} and {MAX_POLL_INTERVAL_SEC} seconds.",
        "Smart plug query interval must be a whole number from 15 to 60 seconds.",
        "Gateway response schema is not supported.",
        "The device does not identify itself as an Ecowitt gateway.",
    )
    for message in validation_messages:
        if str(exc) == message:
            return message
    return "Ecowitt gateway operation failed. Check the gateway connection and Sensorius log."


def register_integration_routes(
    router: APIRouter, *, services: RuntimeServices, settings_factory: Callable,
    ecowitt_factory: Callable, ensure_weewx_settings: Callable,
    evaluate_weewx_status: Callable, dashboard_broadcast: Callable,
    invalidate_dashboard_caches: Callable, debug: bool = False,
) -> None:
    """Register existing integration endpoints with app-owned services."""
    @router.post("/submit-homeassistant-settings")
    async def submit_homeassistant_settings(request: Request):
        settings = settings_factory()
        try:
            data = await request.json()
        except Exception:
            data = {}

        enabled = bool(data.get("enabled", False))
        use_tls = bool(data.get("use_tls", False))
        broker = str(data.get("broker", "") or "").strip()
        username = str(data.get("username", "") or "").strip()
        password = str(data.get("password", "") or "").strip()
        try:
            port = int(data.get("port", 1883) or 1883)
        except Exception:
            port = 1883
        if port < 1 or port > 65535:
            port = 1883

        settings.replace_setting("HomeAssistant", "ENABLED", enabled)
        settings.replace_setting("HomeAssistant", "USE_TLS", use_tls)
        settings.replace_setting("HomeAssistant", "HA_BROKER", broker)
        settings.replace_setting("HomeAssistant", "HA_MQTTPORT", port)
        settings.replace_setting("HomeAssistant", "HA_USERNAME", username)
        settings.replace_setting("HomeAssistant", "HA_PASSWORD", settings_factory.obfuscate_secret(password))

        return JSONResponse({"status": "ok"})

    def _ecowitt_service(request: Request):
        service = services.ecowitt_service
        if service is None:
            service = ecowitt_factory(
                settings=settings_factory(),
                data_logger=services.data_logger,
                supervisor=services.supervisor,
            )
            services.ecowitt_service = service
            request.app.state.ecowitt_service = service
            service.switch_controllers = services.switch_controllers
            service.dashboard_broadcast = dashboard_broadcast
        return service

    @router.post("/ecowitt/discover")
    async def ecowitt_discover(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            result = await _ecowitt_service(request).discover(payload.get("gateway_url", ""))
            return JSONResponse(result)
        except EcowittError as exc:
            printDM(f"Ecowitt operation failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": _ecowitt_public_error(exc)}, status_code=400)
        except Exception as exc:
            if debug:
                printDM(f"Ecowitt discovery failed: {exc}", location=MODULE)
            return JSONResponse({"ok": False, "error": "Ecowitt discovery failed."}, status_code=502)

    @router.post("/ecowitt/save")
    async def ecowitt_save(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        service = _ecowitt_service(request)
        try:
            discovery = await service.discover(payload.get("gateway_url", ""))
            await asyncio.to_thread(
                service.save_configuration,
                discovery,
                payload.get("poll_interval_sec", ECOWITT_DEFAULT_POLL_INTERVAL_SEC),
                payload.get("smart_plug_interval_sec", 30),
            )
            service.dashboard_broadcast = dashboard_broadcast
            service.switch_controllers = services.switch_controllers
            await service.activate_smart_plugs()
            invalidate_dashboard_caches()
            return JSONResponse({"ok": True, **discovery, "poll_interval_sec": service.poll_interval_sec,
                                 "smart_plug_interval_sec": service.smart_plug_interval_sec})
        except EcowittError as exc:
            printDM(f"Ecowitt operation failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": _ecowitt_public_error(exc)}, status_code=400)
        except Exception as exc:
            printDM(f"Ecowitt settings save failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": "Failed to save Ecowitt settings."}, status_code=500)

    @router.get("/ecowitt/status")
    async def ecowitt_status(request: Request):
        return JSONResponse(_ecowitt_service(request).status())

    @router.post("/ecowitt/disable")
    async def ecowitt_disable(request: Request):
        try:
            await asyncio.to_thread(_ecowitt_service(request).disable)
            return JSONResponse({"ok": True})
        except Exception as exc:
            printDM(f"Ecowitt disable failed: {exc}", location=MODULE, level="warning")
            return JSONResponse({"ok": False, "error": "Failed to disable Ecowitt integration."}, status_code=500)

    @router.post("/submit-weewx-settings")
    async def submit_weewx_settings(request: Request):
        settings = settings_factory()
        try:
            data = await request.json()
        except Exception:
            data = {}

        mqtt_enabled = bool(data.get("mqtt_enabled", False))
        db_path = str(data.get("db_path", WEEWX_DEFAULT_DB_PATH) or WEEWX_DEFAULT_DB_PATH).strip()
        sensor_id = str(data.get("sensor_id", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID).strip()
        mqtt_topic = str(data.get("mqtt_topic", WEEWX_DEFAULT_MQTT_TOPIC) or WEEWX_DEFAULT_MQTT_TOPIC).strip()
        try:
            update_period_sec = int(data.get("update_period_sec", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        except Exception:
            update_period_sec = int(WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        update_period_sec = max(15, min(3600, update_period_sec))

        if not db_path.startswith("/"):
            return JSONResponse({"ok": False, "error": "WeeWX database path must be absolute."}, status_code=400)
        if not re.match(r"^[A-Za-z0-9._-]+$", sensor_id):
            return JSONResponse({"ok": False, "error": "WeeWX sensor ID may contain only letters, numbers, dot, underscore, and dash."}, status_code=400)
        if not mqtt_topic:
            return JSONResponse({"ok": False, "error": "WeeWX MQTT topic filter is required."}, status_code=400)

        settings.replace_setting("WeeWX", "MQTT_ENABLED", mqtt_enabled)
        settings.replace_setting("WeeWX", "MQTT_TOPIC", mqtt_topic)
        settings.replace_setting("WeeWX", "SENSOR_ID", sensor_id)
        settings.replace_setting("WeeWX", "DB_PATH", db_path)
        settings.replace_setting("WeeWX", "UPDATE_PERIOD_SEC", update_period_sec)
        settings.replace_setting("WeeWX", "ENABLED", False)
        settings.replace_setting("WeeWX", "AUTO_DISCOVER", False)

        try:
            ensure_weewx_settings(sensor_id)
        except Exception as exc:
            printDM(f"WeeWX sensor settings materialization failed: {exc}", location=MODULE)
            return JSONResponse(
                {"ok": False, "error": "Failed to create WeeWX sensor settings."},
                status_code=500,
            )

        applied_live = False
        try:
            ingest = services.mqtt_ingest
            if ingest is not None and hasattr(ingest, "configure_weewx_mqtt"):
                applied_live = bool(
                    ingest.configure_weewx_mqtt(
                        enabled=mqtt_enabled,
                        topic_filter=mqtt_topic,
                        sensor_id=sensor_id,
                        update_period_sec=update_period_sec,
                    )
                )
        except Exception as exc:
            printDM(f"WeeWX MQTT live reconfigure skipped: {exc}", location=MODULE)

        return JSONResponse({"status": "ok", "restart_required": not applied_live})

    @router.get("/weewx/status")
    async def weewx_status(request: Request):
        settings = settings_factory(apply_live=False)
        sensor_id = str(settings.get_setting("WeeWX", "SENSOR_ID", WEEWX_DEFAULT_SENSOR_ID) or WEEWX_DEFAULT_SENSOR_ID).strip() or WEEWX_DEFAULT_SENSOR_ID
        db_path = str(settings.get_setting("WeeWX", "DB_PATH", WEEWX_DEFAULT_DB_PATH) or WEEWX_DEFAULT_DB_PATH).strip()
        mqtt_enabled = bool(settings.get_setting("WeeWX", "MQTT_ENABLED", False))
        sqlite_enabled = bool(settings.get_setting("WeeWX", "ENABLED", False))
        auto_discover = bool(settings.get_setting("WeeWX", "AUTO_DISCOVER", False))
        try:
            update_period_sec = float(settings.get_setting("WeeWX", "UPDATE_PERIOD_SEC", WEEWX_DEFAULT_UPDATE_PERIOD_SEC) or WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        except Exception:
            update_period_sec = float(WEEWX_DEFAULT_UPDATE_PERIOD_SEC)
        update_period_sec = max(15.0, update_period_sec)
        offline_after_sec = update_period_sec * 3.0

        latest_timestamp = None
        latest_age_sec = None
        latest_values = {}
        try:
            latest_timestamp = services.data_logger.get_latest_timestamp(sensor_id)
            latest_values = services.data_logger.get_latest_values(sensor_id) or {}
            tz_name = str(settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver")
            _status, latest_age_sec = evaluate_weewx_status(
                latest_timestamp=latest_timestamp,
                latest_values=latest_values,
                update_period_sec=update_period_sec,
                tz_name=tz_name,
            )
        except Exception:
            latest_timestamp = None
            latest_age_sec = None
            latest_values = {}

        configured = mqtt_enabled or sqlite_enabled or auto_discover
        weewx_measure_status, latest_age_sec = evaluate_weewx_status(
            latest_timestamp=latest_timestamp,
            latest_values=latest_values,
            update_period_sec=update_period_sec,
            tz_name=str(settings.get_setting("Time", "TZ", "America/Denver") or "America/Denver"),
        )
        receiving = weewx_measure_status == "online"
        stale = weewx_measure_status == "offline"

        if mqtt_enabled:
            mode = "MQTT configured"
        elif sqlite_enabled or auto_discover:
            mode = "SQLite fallback configured"
        elif latest_timestamp:
            mode = "Data present, integration switch disabled"
        else:
            mode = "Not configured"

        if receiving:
            state = "online"
            label = "Receiving WeeWX weather data"
        elif stale:
            state = "offline"
            label = "WeeWX data is stale"
        elif not configured and not latest_timestamp:
            state = "disabled"
            label = "WeeWX integration disabled"
        else:
            state = "unknown"
            label = "Waiting for WeeWX weather data"

        note = ""
        if latest_timestamp and not configured:
            note = "The MQTT interface is disabled, but Sensorius has stored WeeWX station data."
        elif mqtt_enabled:
            note = "MQTT availability is inferred from weather data updates."

        return JSONResponse({
            "state": state,
            "label": label,
            "mode": mode,
            "note": note,
            "sensor_id": sensor_id,
            "db_path": db_path,
            "mqtt_enabled": mqtt_enabled,
            "sqlite_enabled": sqlite_enabled,
            "auto_discover": auto_discover,
            "latest_timestamp": latest_timestamp or "",
            "latest_age_sec": latest_age_sec,
            "offline_after_sec": offline_after_sec,
            "latest_metric_count": len(latest_values or {}),
        })

    @router.post("/submit-farmos-settings")
    async def submit_farmos_settings(request: Request):
        settings = settings_factory()
        try:
            data = await request.json()
        except Exception:
            data = {}

        enabled = bool(data.get("enabled", False))
        base_url = str(data.get("base_url", "") or "").strip().rstrip("/")
        verify_tls = bool(data.get("verify_tls", True))
        access_token = str(data.get("access_token", "") or "").strip()
        client_id = str(data.get("client_id", "farm") or "farm").strip() or "farm"
        client_secret = str(data.get("client_secret", "") or "").strip()
        username = str(data.get("username", "") or "").strip()
        password = str(data.get("password", "") or "").strip()
        log_bundle = str(data.get("log_bundle", "observation") or "observation").strip().lower() or "observation"

        settings.replace_setting("FarmOS", "ENABLED", enabled)
        settings.replace_setting("FarmOS", "BASE_URL", base_url)
        settings.replace_setting("FarmOS", "VERIFY_TLS", verify_tls)
        settings.replace_setting("FarmOS", "ACCESS_TOKEN", settings_factory.obfuscate_secret(access_token))
        settings.replace_setting("FarmOS", "CLIENT_ID", client_id)
        settings.replace_setting("FarmOS", "CLIENT_SECRET", settings_factory.obfuscate_secret(client_secret))
        settings.replace_setting("FarmOS", "USERNAME", username)
        settings.replace_setting("FarmOS", "PASSWORD", settings_factory.obfuscate_secret(password))
        settings.replace_setting("FarmOS", "LOG_BUNDLE", log_bundle)

        return JSONResponse({"status": "ok"})

    @router.get("/farmos/status")
    async def farmos_status(request: Request):
        bridge = services.farmos_bridge
        if bridge and hasattr(bridge, "status_snapshot"):
            try:
                return JSONResponse(bridge.status_snapshot())
            except Exception as exc:
                printDM(f"FarmOS status failed: {exc}", location=MODULE, level="warning")
                return JSONResponse({"ok": False, "error": "Unable to read FarmOS status."}, status_code=500)

        settings = settings_factory(apply_live=False)
        return JSONResponse({
            "enabled": bool(settings.get_setting("FarmOS", "ENABLED", False)),
            "base_url": str(settings.get_setting("FarmOS", "BASE_URL", "") or "").strip(),
            "verify_tls": bool(settings.get_setting("FarmOS", "VERIFY_TLS", True)),
            "log_bundle": str(settings.get_setting("FarmOS", "LOG_BUNDLE", "observation") or "observation").strip(),
            "queue_depth": 0,
            "has_static_token": bool(str(settings.get_setting("FarmOS", "ACCESS_TOKEN", "") or "").strip()),
            "has_runtime_token": False,
            "last_error": "FarmOS bridge not attached",
        })

    @router.post("/farmos/test")
    async def farmos_test(request: Request):
        bridge = services.farmos_bridge
        if bridge and hasattr(bridge, "test_connection"):
            try:
                result = await bridge.test_connection()
                code = 200 if bool(result.get("ok")) else 502
                return JSONResponse(result, status_code=code)
            except Exception as exc:
                printDM(f"FarmOS connection test failed: {exc}", location=MODULE, level="warning")
                return JSONResponse({"ok": False, "error": "FarmOS connection test failed."}, status_code=500)

        return JSONResponse({"ok": False, "error": "FarmOS bridge not attached"}, status_code=503)
