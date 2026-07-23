"""Main application entrypoint for Sensorius, an IoT sensor hub and automation system.

Sensorius orchestrates local sensor acquisition, switch/relay control, data
logging, MQTT publish/ingest, Home Assistant integration, and the web UI/API.
This module wires together the runtime services (supervisor, watchdog, MQTT,
ingest/HA bridge, web server) and starts the async event loop that powers the
hub on Raspberry Pi hardware.
"""

import asyncio
import importlib.util
from threading import Thread
import socket
from datetime import datetime
from . import __version__
from .saiUtils import printDM, debug_enabled, configure_logging
from .saiSensor import SensorController
from .saiMQTTClient import saiMQTTClient, set_mqtt_client, get_all_mqtt_clients
from .saiTaskSupervisor import TaskSupervisor
from .saiGarbageCollection import GCManager
from .saiWebServer import WebServerController, launch_webview
from .saiWatchdog import WatchdogMonitor
from .saiMQTTIngest import saiMQTTIngest
from .saiFarmOSBridge import saiFarmOSBridge
from .saiDailySummary import DailySummaryService
from .saiWeeWX import WeeWXArchiveIngest
from .saiSettings import saiSettings
from .saiSensorSettingsManager import SensorSettingsManager
from .saiUtils import SettingsWrapper
from .saiSensorFactory import find_sensors
from .saiSwitchFactory import detect_relay_board
from .saiTimeSync import TimeSyncService
from .saiNodusAutomationStatus import NodusAutomationStatusPublisher
from .saiEmailNotifications import AutomationNotificationService

MODULE = "Sensorius"
DEBUG = debug_enabled(MODULE)


def local_sensor_runtime_available() -> bool:
    """Return True when the local host has the Pi sensor runtime available."""
    return importlib.util.find_spec("board") is not None

def is_self_broker(broker: str | None, *, hostnames: set[str] | None = None) -> bool:
    """Return True when broker points at this host or is unset."""
    b = str(broker or "").strip().lower()
    if not b or b in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True

    names = {str(name or "").strip().lower() for name in (hostnames or set()) if str(name or "").strip()}
    try:
        sock_host = (socket.gethostname() or "").strip().lower()
        if sock_host:
            names.add(sock_host)
            names.add(f"{sock_host}.local")
    except Exception:
        pass
    try:
        from .saiNet import rPiNetManager
        hn = str(getattr(rPiNetManager(), "hostname", "") or "").strip().lower()
        if hn:
            names.add(hn)
            names.add(f"{hn}.local")
    except Exception:
        pass
    return b in names


def is_remote_sensor_settings(config_dict: dict | None) -> bool:
    """Return True when the sensor settings describe an MQTT-backed remote device."""
    try:
        sensor_block = config_dict.get("Sensor", {}) if isinstance(config_dict, dict) else {}
        sensor_type = str(sensor_block.get("TYPE", "") or "").strip().lower()
        return sensor_type in {"nodus", "picow", "pico2w", "remote", "mqtt", "weewx", "station"}
    except Exception:
        return False

# helpers for determining all (directly and/or remote mqtt clients) devices
async def ensure_local_sensor_configs(settings) -> list[str]:
    """
    Probe hardware via find_sensors(), ensure per-sensor TOMLs exist,
    and return the configured local sensor IDs.

    ID format: <kind>-<bus>-<hostname>
      e.g., avpd-i2c-1-sensoria-hub-0

    Behavior:
      - Start from any existing sensor_settings entries.
      - Scan I2C busses for known sensor types.
      - For each detected descriptor, ensure sensor_settings/<sensor_id>/sensor.toml exists.
      - If nothing is detected, return the existing configured IDs.
    """
    if not local_sensor_runtime_available():
        if DEBUG:
            printDM(
                "Pi sensor runtime unavailable; skipping local sensor discovery/config materialization.",
                location="ensure_local_sensor_configs",
            )
        return []

    sensor_mgr = SensorSettingsManager("sensor_settings")
    existing_ids = sensor_mgr.list_ids()
    existing_ids = [str(sid).strip() for sid in existing_ids if str(sid).strip()]
    for sid in existing_ids:
        try:
            sensor_mgr.ensure_direct_local_type(sid)
        except Exception:
            pass
        try:
            sensor_mgr.ensure_local_serial_num(sid)
        except Exception:
            pass
    seen_ids: set[str] = set(existing_ids)

    host = (
        settings.get_setting("Network", "HOSTNAME")
        or socket.gethostname()
        or "pi"
    ).strip().lower()

    # One pass detect that returns descriptors with .kind and .bus
    known_used = {"i2c-1": set(), "i2c-0": set()}
    try:
        descriptors = find_sensors(known_used)  # -> list[DeviceDescriptor]
    except Exception as e:
        printDM(f"find_sensors failed: {e}", location="ensure_local_sensor_configs")
        # fall back to whatever we already had
        return existing_ids

    if not descriptors:
        if not existing_ids:
            printDM(
                "No local sensors detected; no local sensor configs present.",
                location="ensure_local_sensor_configs",
            )
        else:
            printDM(
                "No new local sensors detected; keeping existing local sensor configs.",
                location="ensure_local_sensor_configs",
            )
        return existing_ids

    # Deterministic order (nice for diffs)
    descriptors.sort(key=lambda d: (d.kind or "", d.bus or ""))

    updated_ids: list[str] = list(existing_ids)

    for desc in descriptors:
        kind = (desc.kind or "").strip().lower()
        bus = (desc.bus or "").strip().lower()
        if not bus:
            # Defensive fallback: skip malformed descriptors instead of generating bad IDs.
            printDM(f"Skipping descriptor with empty bus: kind={kind}", location="ensure_local_sensor_configs")
            continue
        sid = f"{kind}-{bus}-{host}"

        # Always ensure sensor.toml exists (idempotent if already present)
        try:
            sensor_mgr.seed_from_factory(
                sensor_id=sid,
                device=kind,        # -> [Sensor].DEVICE
                location="Unknown", # UI can edit later
            )
        except Exception as e:
            printDM(
                f"seed_from_factory failed for {sid}: {e}",
                location="ensure_local_sensor_configs",
            )
            # keep going for other sensors
            continue

        if sid not in seen_ids:
            seen_ids.add(sid)
            updated_ids.append(sid)

        try:
            sensor_mgr.ensure_local_serial_num(sid)
        except Exception as e:
            printDM(
                f"ensure_local_serial_num failed for {sid}: {e}",
                location="ensure_local_sensor_configs",
            )

    return updated_ids
    
async def build_sensor_controllers(sensor_ids, supervisor, gc_mgr, data_logger):
    sensor_mgr = SensorSettingsManager("sensor_settings")
    sensors = []
    for sid in sensor_ids:
        config_dict = sensor_mgr.load(sid)
        if not config_dict:
            printDM(f"Sensor config for '{sid}' not found", location=f"{MODULE}:bsc")
            continue
        if is_remote_sensor_settings(config_dict):
            if DEBUG:
                printDM(f"Skipping remote sensor runtime for '{sid}'", location=f"{MODULE}:bsc")
            continue
        config = SettingsWrapper(config_dict)
        if DEBUG:
            printDM(f"sid {sid}, config: {config_dict}", location=f"{MODULE}:bsc")
        try:
            sensor = SensorController(config, supervisor, gc_mgr, data_logger=data_logger)
        except Exception as e:
            printDM(f"Sensor init skipped for '{sid}': {e}", location=f"{MODULE}:bsc")
            continue
        sensors.append(sensor)
    if DEBUG:
        printDM(f"sensor_id: {sensors}", location=f"{MODULE}:bsc")
    return sensors

async def build_switch_controllers(sensors, supervisor, data_logger):
    switch_controllers = {}

    from .saiSwitchSettingsManager import SwitchSettingsManager
    from .saiSwitch import build_switch_controller, is_remote_switch_settings

    # Materialize local host switch settings only when local relay hardware is present.
    switch_mgr = SwitchSettingsManager(base_dir="switch_settings")
    local_relay_present = bool(detect_relay_board())
    if local_relay_present:
        device_id = saiSettings().device_id  # this class already resolves hostname
        switch_mgr.ensure_host_switch(device_id, template_id="factory", switch_loc="Unknown")

    switch_ids = switch_mgr.list_switches()
    if DEBUG:
        printDM(f"Switch IDs: {switch_ids}", location=f"{MODULE}:bswc")

    for switch_id in switch_ids:
        switch_mgr.ensure_channel_ids_for_switch(switch_id)

    for sw_id in switch_ids:
        sw_config = SettingsWrapper(switch_mgr.load(sw_id))
        switch_id = (sw_config.get_setting("Switch", "SWITCH_DEVICE_ID", "") or sw_id).strip().lower()
        sw_location = sw_config.get_setting("Switch", "SWITCH_LOCATION", "").lower()
        sw_type = str(sw_config.get_setting("Switch", "TYPE", "") or "").strip().lower()

        match_sensor = next((s for s in sensors if s.location.lower() == sw_location), None)
        if DEBUG:
            printDM(
                f"sw_id: {switch_id}, type: {sw_type or 'pi'}, location: {sw_location}, matched: {match_sensor}",
                location=f"{MODULE}:build_switch",
            )

        switch_ctrl_temp = build_switch_controller(
            switch_settings=sw_config,
            supervisor=supervisor,
            sensor=match_sensor,
            data_logger=data_logger,
        )
        if switch_ctrl_temp.is_present:
            switch_controllers[switch_id] = switch_ctrl_temp
            if DEBUG:
                kind = "remote" if is_remote_switch_settings(sw_config) else "local"
                printDM(f"Build Switch Initialized: {switch_id} ({kind})", location=f"{MODULE}:bswc")
        else:
            printDM(f"No relay hardware detected for {switch_id} — skipping", location=f"{MODULE}:bswc")

        for k, v in switch_controllers.items():
            printDM(f"{k} → {len(v.switches)} switches", location=f"{MODULE}:bswc")
            
    if DEBUG:
        printDM(f"switch controllers built: {switch_controllers}", location=f"{MODULE}:bswc")

    try:
        key_migrations: dict[str, str] = {}
        for ctrl in (switch_controllers or {}).values():
            sid = str(getattr(ctrl, "switch_id", "") or "").strip()
            if not sid:
                continue
            channel_map = dict(getattr(ctrl, "channel_id_for_label", {}) or {})
            for label, channel_id in channel_map.items():
                label_text = str(label or "").strip()
                channel_text = str(channel_id or "").strip()
                if not label_text or not channel_text:
                    continue
                idx = None
                try:
                    idx = ctrl.get_channel_index(label_text)
                except Exception:
                    idx = None
                if idx:
                    key_migrations[f"S{idx}-::{label_text}"] = f"{sid}::{channel_text}"
                key_migrations[f"{sid}::{label_text}"] = f"{sid}::{channel_text}"
        if key_migrations:
            migrated = int(data_logger.migrate_switch_keys(key_migrations) or 0)
            if DEBUG and migrated:
                printDM(f"migrated {migrated} local switch event row(s) forward", location=f"{MODULE}:bswc")
    except Exception as e:
        printDM(f"switch key migration failed: {e}", location=f"{MODULE}:bswc")

    return switch_controllers

def seed_switch_state_history_once(data_logger, switch_controllers):
    """
    If sw_events is empty for a given switch, write one event from the
    controller’s current state so the DB and hardware are aligned.

    Uses SwitchController._switch_key(...) so the DB key always matches the
    canonical "<channel_id>::<label>" form when SWITCH_n_CHANNEL_ID is defined.
    """
    import time

    try:
        for ctrl in (switch_controllers or {}).values():
            sid = getattr(ctrl, "switch_id", None)
            if not sid:
                continue

            sensor_lineage = f"Switch_{sid}"

            # prefer the controller's own canonical key builder if present
            has_key_builder = hasattr(ctrl, "_switch_key") and callable(getattr(ctrl, "_switch_key"))

            for label in (ctrl.get_switch_names() or []):
                if has_key_builder:
                    db_key = ctrl._switch_key(label)
                else:
                    # very old fallback, should not normally be used
                    db_key = f"{sid}::{label}"

                # Only seed if no events exist yet for this canonical key
                latest = data_logger.get_latest_switch_state(db_key, sensor_id=sensor_lineage)
                if latest is not None:
                    continue

                try:
                    live_on = bool(ctrl.get_state(label))
                except Exception:
                    # fall back to controller cache if provided
                    live_on = bool((getattr(ctrl, "last_state", {}) or {}).get(label, False))

                state_text = "On" if live_on else "Off"

                data_logger.log_switch_event(
                    switch_key=db_key,
                    is_on=live_on,
                    source="seed",
                    sensor_id=sensor_lineage,
                )

                # Log both the canonical key and the human label for clarity
                printDM(
                    f"[seed_switch_state_history_once] seeded {db_key} "
                    f"(label={sid}::{label}) = {state_text}",
                    location="Sensorius",
                )

    except Exception as e:
        printDM(f"[seed_switch_state_history_once] failed: {e}", location="Sensorius")
        
async def configure_mqtt_clients(sensors, settings, supervisor):
    clients = []
    for sensor in sensors:
        client = saiMQTTClient(sensor, settings)
        client.supervisor = supervisor
        set_mqtt_client(sensor.sensor_id, client)
        clients.append(client)
    if DEBUG:
        printDM(f"The Clients List: {clients}", location=f"{MODULE}:cmc")
    return clients

async def ensure_mqtt_ready(client, retries=3):
    for _ in range(retries):
        await client.mqtt_reconnect()
        if await client.ensure_connected():
            if DEBUG:
                printDM(f"MQTT connected and ready", location=f"{MODULE}:emr")
            
            return True
        await asyncio.sleep(2)
    printDM(f"[ERROR] MQTT failed to connect after retries", location=f"{MODULE}:emr")
    return False


async def bootstrap_astral_auto_location(settings, *, attempts: int = 1, initial_delay_sec: float = 0.0, delay_sec: float = 30.0):
    """Resolve and persist Astral IP geolocation when manual coordinates are empty."""
    last_error = ""
    if initial_delay_sec > 0:
        await asyncio.sleep(initial_delay_sec)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            astral_loc = await asyncio.to_thread(
                settings.resolve_astral_location,
                persist_if_auto=True,
                timeout_sec=5.0,
            )
            lat = astral_loc.get("lat")
            lon = astral_loc.get("lon")
            if lat is not None and lon is not None:
                if astral_loc.get("source") == "ip":
                    provider = str(astral_loc.get("provider") or "ip").strip()
                    printDM(
                        f"Astral auto-location persisted via {provider}: lat={lat:.6f}, lon={lon:.6f}",
                        location=f"{MODULE}:main",
                    )
                return astral_loc
            last_error = str(astral_loc.get("error") or "location unavailable")
        except Exception as e:
            last_error = str(e)

        if attempt < attempts:
            await asyncio.sleep(delay_sec)

    if last_error:
        printDM(f"Astral auto-location unavailable after {attempts} attempt(s): {last_error}", location=f"{MODULE}:main")
    return None


# Sensorius main
async def main():
    printDM(f"Sensorius startup... version={__version__}", location=f"{MODULE}:main")

    supervisor = TaskSupervisor()
    gc_mgr = GCManager(interval_sec=31, supervisor=supervisor)
    settings = saiSettings()
    astral_loc = await bootstrap_astral_auto_location(settings, attempts=1)
    if not astral_loc:
        asyncio.create_task(
            bootstrap_astral_auto_location(settings, attempts=6, initial_delay_sec=5.0, delay_sec=30.0)
        )

    from .saiNet import rPiNetManager
    net_mgr = rPiNetManager()

    from .saiDataLogger import saiDataLogger
    data_logger = saiDataLogger()

    # Build local sensors only if we actually have any
    sensor_ids = await ensure_local_sensor_configs(settings)
    sensor_map = []
    if sensor_ids:
        sensor_map = await build_sensor_controllers(sensor_ids, supervisor, gc_mgr, data_logger)
        if DEBUG:
            printDM(f"sensor_map: {sensor_map}", location=f"{MODULE}:main")
    else:
        printDM("No directly connected sensors detected or configured; skipping local sensor build.", location=f"{MODULE}:main")

    sensor_map: Dict[str, SensorController] | list[SensorController]

    mqtt_clients = []
        
    switch_controllers: Dict[str, SwitchController] or SwitchController

    # --- Build switch controllers (exactly once) ---
    # Determine hub hostname (used for dedupe key if needed)
    try:
        hub_hostname = settings.get_setting("Network", "HOSTNAME", "") or ""
        if not hub_hostname:
            from .saiNet import rPiNetManager
            hub_hostname = rPiNetManager().hostname or ""
        hub_hostname = hub_hostname.strip().lower()
    except Exception:
        hub_hostname = ""

    try:
        # Build once (some implementations rely on sensors)
        switch_controllers = await build_switch_controllers(sensor_map, supervisor, data_logger)
        if DEBUG:
            printDM(f"switch_controllers: {switch_controllers}", location=f"{MODULE}:main")
    except Exception as e:
        switch_controllers = {}
        printDM(f"build_switch_controllers skipped/failed with no local sensors: {e}", location=f"{MODULE}:main")

    # --- Dedupe: keep a single controller per logical switch id/hub ---
    # If your builder already returns only one, this is a no-op.
    if isinstance(switch_controllers, dict) and switch_controllers:
        seen_ids = set()
        to_delete = []
        for sid in list(switch_controllers.keys()):
            norm = (sid or "").strip().lower()
            # collapse empty/misreported ids to the hub host, so duplicates merge
            norm = norm or hub_hostname or "hub"
            if norm in seen_ids:
                to_delete.append(sid)
            else:
                seen_ids.add(norm)
        for sid in to_delete:
            try:
                del switch_controllers[sid]
            except Exception:
                pass

    # log what we ended up with
    try:
        count = len(switch_controllers) if isinstance(switch_controllers, dict) else 0
        printDM(f"Total switch controllers built: {count}", location=f"{MODULE}:main")
    except Exception:
        pass

    from . import saiWebRoutes
     

    # make them available to the routes module
    saiWebRoutes.sensor_map = sensor_map
    saiWebRoutes.switch_controllers = switch_controllers
    saiWebRoutes.data_logger = data_logger

    # (optional but nice) also put them on the FastAPI app for future-proof access
    try:
        from .saiWebRoutes import app  # if your app lives there
        app.state.sensor_map = sensor_map
        app.state.switch_controllers = switch_controllers
    except Exception:
        pass

    # Configure MQTT publishers only for locally connected sensors (optional)
    broker = settings.get_setting("SensorNetwork", "BROKER") or ""
    publish_to_mqtt = bool(broker) and not is_self_broker(broker)
    if sensor_map:
        mqtt_clients = await configure_mqtt_clients(sensor_map, settings, supervisor)

        from .saiUtils import supervised_task
        for client in mqtt_clients:
            if not publish_to_mqtt:
                if DEBUG:
                    printDM(
                        f"Skipping MQTT publisher for {client.sensor.sensor_id} "
                        f"(broker='{broker or ''}' treated as self/unset)",
                        location=f"{MODULE}:main",
                    )
                client.broker = ""
                continue
            await ensure_mqtt_ready(client)
            await asyncio.sleep(3)

            if client.broker:
                supervisor.add(
                    lambda c=client: supervised_task(f"{c.sensor.sensor_id} MQTT Publisher", c.mqtt_publish_data, supervisor),
                    name=f"{client.sensor.sensor_id} MQTT Publisher",
                    fatal_on_timeout=False,
                    fatal_on_error=False,
                )
                supervisor.add(
                    lambda c=client: supervised_task(f"{c.sensor.sensor_id} MQTT Loop", c.mqtt_loop, supervisor),
                    name=f"{client.sensor.sensor_id} MQTT Loop",
                    fatal_on_timeout=False,
                    fatal_on_error=False,
                )
                if DEBUG:
                    printDM(f"MQTT client coroutines added: {client.sensor.sensor_id}", location=f"{MODULE}:main")
    else:
        if DEBUG:
            printDM("No local sensors → no local MQTT publishers configured.", location=f"{MODULE}:main")

    # --- MQTT Ingest (always allowed; discovers/subscribes to remote devices) ---
    mqtt_ingest_clients = saiMQTTIngest(
        broker,
        mqtt_clients=settings.get_all_clients(),  # hostnames for discovery; ok if empty
        supervisor=supervisor,
        settings=settings,
        data_logger = data_logger,
    )
    # Make it globally discoverable for switch fallbacks / web routes / etc.
    try:
        from .saiMQTTIngest import set_current_ingest
        set_current_ingest(mqtt_ingest_clients)
    except Exception:
        pass

    if broker:
        asyncio.create_task(mqtt_ingest_clients.start())
        supervisor.add(
            mqtt_ingest_clients.mqtt_discovery_loop,
            name="MQTT Discovery Loop",
            fatal_on_timeout=False,
            fatal_on_error=False,
        )
        
        ha_enabled = bool(settings.get_setting("HomeAssistant", "ENABLED", False))

        if ha_enabled and broker:
            from .saiHomeAssistantMqtt import rPiHomeAssistantBridge, HomeAssistantTopicMap

            topic_map = HomeAssistantTopicMap(
                node_id=(settings.get_setting("Network", "HOSTNAME") or socket.gethostname() or "sensorius").strip().lower(),
                discovery_prefix=settings.get_setting("HomeAssistant", "DISCOVERY_PREFIX", "homeassistant"),
                base_topic=settings.get_setting("HomeAssistant", "BASE_TOPIC", "sensorius"),
            )

            ha_bridge = rPiHomeAssistantBridge(
                mqtt_clients=mqtt_ingest_clients,
                settings=settings,
                topic_map=topic_map,
                switch_controllers=switch_controllers,
                data_logger=data_logger,
            )
            try:
                mqtt_ingest_clients.register_liveness_callback(ha_bridge.handle_nodus_liveness_change)
            except Exception:
                pass

            async def _ha_bootstrap():
                ok = await mqtt_ingest_clients.wait_until_ha_connected(timeout=10.0)
                if not ok:
                    printDM("[HA] MQTT ingest never became connected; skipping discovery", location=MODULE)
                    return
                ha_bridge.install_command_handlers()
                await ha_bridge.publish_all_discovery()

            asyncio.create_task(_ha_bootstrap())
        
    else:
        if DEBUG:
            printDM("No SensorNetwork.BROKER configured — MQTT ingest not started.", location=f"{MODULE}:main")

    # --- Always-on supervisors ---
    weewx_ingest = WeeWXArchiveIngest(settings=settings, data_logger=data_logger, supervisor=supervisor)
    farmos_bridge = saiFarmOSBridge(settings=settings, data_logger=data_logger, supervisor=supervisor)
    daily_summary_service = DailySummaryService(settings=settings, data_logger=data_logger, supervisor=supervisor)
    automation_notifications = AutomationNotificationService(data_logger=data_logger, supervisor=supervisor)
    time_sync_service = TimeSyncService(settings=settings, mqtt_ingest=mqtt_ingest_clients, supervisor=supervisor)
    nodus_automation_status = NodusAutomationStatusPublisher(
        mqtt_ingest_clients,
        supervisor=supervisor,
    )
    supervisor.add(weewx_ingest.run, name="WeeWX Archive Ingest", fatal_on_timeout=False, fatal_on_error=False)
    supervisor.add(farmos_bridge.run, name="FarmOS Bridge", fatal_on_timeout=False, fatal_on_error=False)
    supervisor.add(daily_summary_service.run, name="Daily Summary Writer", fatal_on_timeout=False, fatal_on_error=False)
    supervisor.add(
        automation_notifications.run,
        name="Automation Notifications",
        fatal_on_timeout=False,
        fatal_on_error=False,
    )
    supervisor.add(time_sync_service.run, name="Time Sync Manager", fatal_on_timeout=False, fatal_on_error=False)
    if broker:
        supervisor.add(
            nodus_automation_status.run,
            name="Nodus Automation Status",
            fatal_on_timeout=False,
            fatal_on_error=False,
        )
    supervisor.add(WatchdogMonitor, supervisor, name="Watchdog Monitor")
    supervisor.add(gc_mgr.run, name="GC Manager", fatal_on_timeout=False, fatal_on_error=False)
    
    from .saiUtils import loop_lag_monitor
    asyncio.create_task(loop_lag_monitor())

    # --- Local sensor data collection (optional) ---
    for sensor in sensor_map:
        await asyncio.sleep(1)
        supervisor.add(
            sensor.data_collection,
            name=f"{sensor.sensor_id} Data Collection",
            fatal_on_timeout=False,
            fatal_on_error=False,
        )
        if DEBUG:
            printDM(f"sensor.sensor_id {sensor.sensor_id}.", location=f"{MODULE}:main")

    # --- Switch controllers (optional) ---
    seed_switch_state_history_once(data_logger, switch_controllers)
    for ctrl in (switch_controllers.values() if isinstance(switch_controllers, dict) else []):
        await asyncio.sleep(1)
        if DEBUG:
            try:
                printDM(
                    f"Queueing switch monitor: {ctrl.switch_id} remote={int(bool(getattr(ctrl, 'is_remote', False)))} "
                    f"labels={list(getattr(ctrl, 'get_switch_names', lambda: [])() or [])}",
                    location=f"{MODULE}:main",
                )
            except Exception:
                pass
        supervisor.add(
            ctrl.run_controladora_monitor,
            ctrl.sensor,
            name=f"{ctrl.switch_id} Controladora Monitor",
            fatal_on_timeout=False,
            fatal_on_error=False,
        )
        if DEBUG:
            printDM(f"switch.switch_id {ctrl.switch_id}.", location=f"{MODULE}:main")

    asyncio.create_task(supervisor.start())

    # Web server can run with zero local sensors; it will still show MQTT-discovered devices via ingest
    web_server = WebServerController(settings, net_mgr, supervisor, gc_mgr, mqtt_ingest_clients)
    # make ingest available to request.app.state for /retry-discovery
    web_server.app.state.mqtt_ingest = mqtt_ingest_clients
    web_server.app.state.farmos_bridge = farmos_bridge
    web_server.app.state.supervisor = supervisor
    web_server.app.state.data_logger = data_logger
    web_server.app.state.sensor_map = sensor_map
    web_server.app.state.switch_controllers = switch_controllers
    
    await web_server.initialize_server()
    await web_server.run_async()

    return supervisor

def run_main_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        supervisor = loop.run_until_complete(main())
        if DEBUG:
            printDM("All tasks registered. Launching supervisor.", location=f"{MODULE}:rmt")
        loop.run_until_complete(supervisor.run_forever())
    except Exception as e:
        printDM(f"Fatal error in run_main_thread: {e}", location=f"{MODULE}:rmt")
    finally:
        for client in get_all_mqtt_clients():
            try:
                client.close()
            except Exception as close_e:
                printDM(f"MQTT client close failed: {close_e}", location=f"{MODULE}:rmt")

def run_application():
    """Start the Sensorius backend and optional desktop webview."""
    import os
    import sys

    try:
        configure_logging()

        # Start backend system in a daemon thread
        main_thread = Thread(target=run_main_thread, daemon=True)
        main_thread.start()

        platform = sys.platform
        is_macos = platform == "darwin"
        is_linux = platform.startswith("linux")

        gui_env = os.environ.get("SENSORIUS_GUI")
        gui_env_normalized = gui_env.strip().lower() if gui_env else ""
        gui_env_force_on = gui_env_normalized in {"1", "true", "yes", "on"}
        gui_env_force_off = gui_env_normalized in {"0", "false", "no", "off"}

        # On macOS, DISPLAY is not typically set; allow GUI by default.
        # On Linux, require an active X11 or Wayland display to avoid TTY/headless sessions.
        want_gui = is_macos or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if gui_env_force_on:
            want_gui = True
        elif gui_env_force_off:
            want_gui = False

        if want_gui:
            try:
                window = asyncio.run(launch_webview(url="http://127.0.0.1:8000/", retries=10, delay=7.0))
                if window:
                    try:
                        import webview
                        gui_backend = "cocoa" if is_macos else ("gtk" if is_linux else None)
                        if gui_backend:
                            webview.start(gui=gui_backend)
                        else:
                            webview.start()
                    except Exception as e:
                        printDM(f"webview.start failed: {e} — continuing headless", location=f"{MODULE}:__main__")
                else:
                    printDM("No webview window created — continuing headless", location=f"{MODULE}:__main__")
            except Exception as e:
                printDM(f"Webview launch failed: {e} — continuing headless", location=f"{MODULE}:__main__")
        else:
            printDM("DISPLAY not set; running headless (no GUI).", location=f"{MODULE}:__main__")

        main_thread.join()

    except KeyboardInterrupt:
        printDM("Keyboard interrupt received. Exiting.", location=f"{MODULE}:__main__")
    except Exception as e:
        printDM(f"Fatal error in __main__: {e}", location=f"{MODULE}:__main__")


if __name__ == "__main__":
    run_application()
