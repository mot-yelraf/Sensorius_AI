"""Coordinate startup, recovery, and steady-state runtime behavior.

This module wires together configuration loading, feature initialization,
network setup, MQTT lifecycle management, web runtime startup, and bounded
recovery decisions for the main cPyNodus_II application loop.
"""

import asyncio
import gc
import os
import time
from dataclasses import replace

from cpynodus_ii import __version__
from cpynodus_ii.core.network import (
    build_network_stack,
    network_error_signature,
    network_link_is_ready,
    reconnect_network_stack,
    refresh_network_socket_artifacts,
    refresh_network_stack,
    stop_network_mdns,
    teardown_network_stack,
    verify_station_connectivity,
)
from cpynodus_ii.core.ntp import DEFAULT_NTP_SERVER, NTPState, maybe_sync_ntp
from cpynodus_ii.core.plan import StartupPlan
from cpynodus_ii.core.reboot_log import append_reboot_reason_traceback
from cpynodus_ii.core.recovery import (
    RecoveryPolicy,
    RecoveryState,
    advance_recovery_state,
)
from cpynodus_ii.core.settings import Settings
from cpynodus_ii.ota.state import FwUpdateState, load_ota_state, save_ota_state

MQTT_REBUILD_VERIFY_WINDOW_S = 90.0
MQTT_REBUILD_VERIFY_PHASE_S = 10.0
MQTT_LONG_RECOVERY_REBOOT_S = 900.0
MQTT_BROKER_OUTAGE_BACKOFF_AFTER_S = 300.0
MQTT_BROKER_OUTAGE_RETRY_INTERVAL_S = 30.0
MQTT_MEMORY_FAILURE_REBOOT_S = 20.0
MQTT_MEMORY_FAILURE_REBOOT_MIN_COUNT = 5
MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S = 60.0
MQTT_REPEATED_CONNECT_FAILURE_MIN_COUNT = 3
MQTT_PLAIN_CONNECT_STATION_RESET_S = 180.0
MQTT_PLAIN_CONNECT_STATION_RESET_INTERVAL_S = 180.0
MQTT_PREFLIGHT_RETRIES = 3
MQTT_PREFLIGHT_RETRY_DELAY_S = 0.5
MQTT_PREFLIGHT_CONNECT_DELAY_S = 5.0
MQTT_PREFLIGHT_CONNECT_DELAY_STEP_S = 3.0
MQTT_PREFLIGHT_CONNECT_DELAY_MAX_S = 14.0
MQTT_STARTUP_CONDITIONING_ENABLED = False
MQTT_STARTUP_CONDITIONING_SOFT_RELOAD_ENABLED = True
MQTT_STARTUP_CONDITIONING_TIMEOUT_S = 3.0
MQTT_STARTUP_CONDITIONING_RETRIES = 1
NTP_DEFER_UNTIL_MQTT_STARTUP_CLEAR = False
SENSOR_NOT_FOUND_REBOOT_S = 60.0
SENSOR_NOT_FOUND_REBOOT_MIN_COUNT = 6
SENSOR_NOT_FOUND_REINIT_MAX_ATTEMPTS = 2
SOFT_REBOOT_SETTLE_S = 1.0
WARM_START_RADIO_SETTLE_S = 1.0
WIFI_AFTER_READY_FAILURE_SIGNATURES = (
    "station_scan_miss_after_ready",
    "station_unknown_after_ready",
)
WIFI_AFTER_READY_STATION_RESET_FAILURES = 2
WIFI_AFTER_READY_REBOOT_S = 90.0
WIFI_AFTER_READY_REBOOT_MIN_FAILURES = 3
WIFI_BEFORE_READY_RESET_SIGNATURES = (
    "station_scan_miss",
    "station_unknown",
)
WIFI_BEFORE_READY_STATION_RESET_FAILURES = 2
STATION_RESET_RECONNECT_ATTEMPTS = 3
STATION_RESET_RECONNECT_DELAY_S = 2.0
HARD_RECOVERY_REBOOT_REASONS = (
    "ap_idle_timeout",
    "mqtt_memory_allocation_failures",
    "mqtt_recovery_timeout",
    "mqtt_repeated_connect_failures",
    "sensor_not_found",
    "wifi_after_ready_failure",
    "wifi_recovery_timeout",
)
RECOVERY_HARD_RESET_NVM_INDEX = 1
RECOVERY_HARD_RESET_MQTT_MARKER = 77
SOFT_RELOAD_CLEANUP_NVM_INDEX = 2
SOFT_RELOAD_CLEANUP_MARKER = 31

_SOFT_RELOAD_PREPARED = False


class _InactiveMQTTAdapter:
    def __init__(self, runtime_config):
        mqtt_config = getattr(runtime_config, "mqtt", None)
        self.phase = "inactive"
        self.broker = str(getattr(mqtt_config, "broker", "") or "")
        self.active_broker = ""
        self.port = int(getattr(mqtt_config, "port", 1883) or 1883)
        self.errors = ()
        self.broker_targets = ()
        self.published_index = 0
        self.subscription_index = 0


class _InactiveMQTTTransport:
    connected = False
    connection_generation = 0
    published_messages = ()
    subscriptions = ()
    received_messages = ()
    last_disconnect_reason = ""
    last_success_at = -1.0

    def mark_disconnected(self, reason=""):
        self.last_disconnect_reason = str(reason or "")

    def mark_connect_requested(self):
        return None

    def drain_received(self):
        return ()


class _InactiveRuntimeIteration:
    def __init__(self, state, runtime_config):
        self.state = state
        self.runtime_config = runtime_config
        self.subscribed_topics = ()
        self.command_results = ()
        self.errors = ()
        self.sensor_publish_phase = "skipped"
        self.sensor_published_count = 0


class _InactiveSensorSnapshot:
    def __init__(self):
        self.phase = "inactive"
        self.sensor_id = ""
        self.device = ""
        self.metrics = {}
        self.errors = ()


def _build_mqtt_transport(runtime_config):
    from cpynodus_ii.core.mqtt import MQTTTransport

    return MQTTTransport(
        runtime_config.mqtt.preferred_host,
        runtime_config.mqtt.port,
    )


def build_mqtt_client_adapter(*args, **kwargs):
    """Build an MQTT adapter after the active profile requires MQTT."""
    from cpynodus_ii.core.mqtt_client import build_mqtt_client_adapter as build

    return build(*args, **kwargs)


def close_mqtt_client(*args, **kwargs):
    """Close MQTT resources after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import close_mqtt_client as close

    return close(*args, **kwargs)


def connect_mqtt_client(*args, **kwargs):
    """Connect MQTT after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import connect_mqtt_client as connect

    return connect(*args, **kwargs)


def disconnect_mqtt_client(*args, **kwargs):
    """Disconnect MQTT after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import disconnect_mqtt_client as disconnect

    return disconnect(*args, **kwargs)


def poll_mqtt_client(*args, **kwargs):
    """Poll MQTT after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import poll_mqtt_client as poll

    return poll(*args, **kwargs)


def preflight_mqtt_broker_connect(*args, **kwargs):
    """Probe MQTT connect readiness after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import (
        preflight_mqtt_broker_connect as preflight_connect,
    )

    return preflight_connect(*args, **kwargs)


def preflight_mqtt_broker_tcp(*args, **kwargs):
    """Probe MQTT TCP readiness after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import preflight_mqtt_broker_tcp as preflight_tcp

    return preflight_tcp(*args, **kwargs)


def raw_mqtt_connect_enabled(*args, **kwargs):
    """Return raw MQTT connect enablement after MQTT support is imported."""
    from cpynodus_ii.core.mqtt_client import raw_mqtt_connect_enabled as enabled

    return enabled(*args, **kwargs)


def sync_transport_to_client(*args, **kwargs):
    """Synchronize queued MQTT work after MQTT support has been imported."""
    from cpynodus_ii.core.mqtt_client import sync_transport_to_client as sync

    return sync(*args, **kwargs)


def _build_switch_stack(runtime_config):
    if not getattr(getattr(runtime_config, "switch", None), "present", False):
        return None, None, None, None

    from cpynodus_ii.features.switch import plan_switch_initialization
    from cpynodus_ii.features.switch_runtime import build_switch_runtime
    from cpynodus_ii.features.switch_service import start_switch_service
    from cpynodus_ii.hardware.switch_adapter import bind_switch_hardware

    switch_init = plan_switch_initialization(runtime_config)
    switch_runtime = build_switch_runtime(switch_init)
    switch_adapter = bind_switch_hardware(switch_runtime)
    switch_service = start_switch_service(switch_runtime, switch_adapter)
    return switch_init, switch_runtime, switch_adapter, switch_service


def _stop_switch_service_for_shutdown(switch_service):
    from cpynodus_ii.features.switch_service import stop_switch_service

    stop_switch_service(switch_service)


def _subscribe_switch_runtime_topics(transport, runtime_config):
    from cpynodus_ii.features.command_intake import subscribe_switch_runtime_topics

    return subscribe_switch_runtime_topics(transport, runtime_config)


def _inactive_steady_state_iteration(state, runtime_config):
    return _InactiveRuntimeIteration(state, runtime_config)


def bind_sensor_hardware(*args, **kwargs):
    """Bind sensor hardware after the active hardware plan requires it."""
    from cpynodus_ii.hardware.sensor_adapter import bind_sensor_hardware as bind

    return bind(*args, **kwargs)


def start_sensor_service(*args, **kwargs):
    """Start sensor service after the active hardware plan requires it."""
    from cpynodus_ii.features.sensor_service import start_sensor_service as start

    return start(*args, **kwargs)


def stop_sensor_service(*args, **kwargs):
    """Stop sensor service after sensor support has been imported."""
    from cpynodus_ii.features.sensor_service import stop_sensor_service as stop

    return stop(*args, **kwargs)


def read_sensor_snapshot(*args, **kwargs):
    """Read a sensor snapshot after sensor support has been imported."""
    from cpynodus_ii.features.sensor_service import read_sensor_snapshot as read

    return read(*args, **kwargs)


def _build_sensor_stack(runtime_config):
    if not getattr(getattr(runtime_config, "sensor", None), "present", False):
        return None, None, None, _InactiveSensorSnapshot(), None

    from cpynodus_ii.features.sensor import plan_sensor_initialization
    from cpynodus_ii.features.sensor_runtime import build_sensor_runtime

    sensor_init = plan_sensor_initialization(runtime_config)
    sensor_runtime = build_sensor_runtime(sensor_init, runtime_config)
    sensor_adapter = bind_sensor_hardware(sensor_runtime, runtime_config)
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )
    sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
    return sensor_init, sensor_runtime, sensor_adapter, sensor_snapshot, sensor_service


def _stop_sensor_service_for_shutdown(sensor_service):
    stop_sensor_service(sensor_service)


def _path_exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _seconds_stamp(start_monotonic):
    try:
        elapsed = max(0, int(time.monotonic() - float(start_monotonic)))
    except Exception:
        elapsed = 0
    return "{}s".format(elapsed)


def _datetime_stamp():
    try:
        current = time.localtime()
    except Exception:
        return ""
    try:
        if int(current[0]) < 2023:
            return ""
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            int(current[0]),
            int(current[1]),
            int(current[2]),
            int(current[3]),
            int(current[4]),
            int(current[5]),
        )
    except Exception:
        return ""


def _log_stamp(start_monotonic):
    stamp = _datetime_stamp()
    if stamp:
        return stamp
    return _seconds_stamp(start_monotonic)


def _print_log(prefix, message, *, start_monotonic):
    print("{} {} {}".format(_log_stamp(start_monotonic), prefix, message))


def _memory_summary():
    free_mem = "unknown"
    mem_alloc = "unknown"
    try:
        free_mem = str(gc.mem_free())
    except Exception:
        pass
    try:
        mem_alloc = str(gc.mem_alloc())
    except Exception:
        pass
    return "free_mem={} mem_alloc={}".format(free_mem, mem_alloc)


def _transport_queue_summary(transport):
    """Return compact queue depth counters for MQTT transport buffers."""
    try:
        published = len(getattr(transport, "published_messages", ()))
    except Exception:
        published = -1
    try:
        subscriptions = len(getattr(transport, "subscriptions", ()))
    except Exception:
        subscriptions = -1
    try:
        received = len(getattr(transport, "received_messages", ()))
    except Exception:
        received = -1
    return "queues pub={} sub={} rx={}".format(
        published,
        subscriptions,
        received,
    )


def _defer_switch_subscriptions_until_after_startup_publish(runtime_config):
    """Return True when switch command topics should use the normal deferred path."""
    switch_config = getattr(runtime_config, "switch", None)
    return bool(getattr(switch_config, "present", False))


def _stop_mqtt_mdns(network_stack, *, reason="", start_monotonic=None):
    """Stop any unexpected MQTT-profile mDNS before recovery touches sockets."""
    if getattr(network_stack, "mdns_server", None) is None:
        return network_stack
    network_stack = stop_network_mdns(network_stack)
    _print_log(
        "network",
        "mdns phase=stopped reason={}".format(reason or "mqtt_recovery"),
        start_monotonic=start_monotonic,
    )
    return network_stack


def _mqtt_sync_operation_summary(sync_result):
    """Return compact MQTT sync operation metadata for diagnostics."""
    operation = str(getattr(sync_result, "operation", "") or "none")
    topic = str(getattr(sync_result, "topic", "") or "none")
    retain = 1 if bool(getattr(sync_result, "retain", False)) else 0
    try:
        payload_bytes = int(getattr(sync_result, "payload_bytes", -1))
    except Exception:
        payload_bytes = -1
    try:
        pending_count = int(getattr(sync_result, "pending_count", 0))
    except Exception:
        pending_count = 0
    try:
        elapsed_ms = int(getattr(sync_result, "elapsed_ms", -1))
    except Exception:
        elapsed_ms = -1
    return "op={} topic={} retain={} bytes={} pending={} elapsed_ms={}".format(
        operation,
        topic,
        retain,
        payload_bytes,
        pending_count,
        elapsed_ms,
    )


def _mqtt_sync_summary(sync_result, transport, *, source, before_queues):
    """Return a compact MQTT sync diagnostic string."""
    errors = ",".join(sync_result.errors) if sync_result.errors else "none"
    diagnostic = str(getattr(sync_result, "diagnostic", "") or "").strip()
    diagnostic_text = ""
    if diagnostic:
        diagnostic_text = " {}".format(diagnostic)
    return (
        "sync source={} phase={} published={} subscribed={} {}{} errors={}"
    ).format(
        str(source or "unknown"),
        sync_result.phase,
        sync_result.published_count,
        sync_result.subscribed_count,
        _mqtt_sync_operation_summary(sync_result),
        diagnostic_text,
        errors,
    )


def _mqtt_sync_should_log_success(sync_result):
    """Return True when a successful sync result carries startup diagnostics."""
    if getattr(sync_result, "phase", "") != "synced":
        return False
    if (
        str(getattr(sync_result, "operation", "") or "") == "subscribe"
        and int(getattr(sync_result, "subscribed_count", 0) or 0) > 0
    ):
        return True
    return False


def _sensor_error_text(*parts):
    """Return a compact sensor error string for startup and poll logs."""
    errors = []
    for part in parts:
        for error in tuple(getattr(part, "errors", ()) or ()):
            text = str(error or "").strip()
            if text and text not in errors:
                errors.append(text)
    return ",".join(errors) if errors else "none"


def _sensor_target_addr(sensor_runtime):
    """Return a compact sensor target address for startup logs."""
    target = str(getattr(sensor_runtime, "transport_target", "") or "").strip()
    if not target:
        return "none"
    if "@" in target:
        address = target.rsplit("@", 1)[-1].strip()
        if address:
            return address.lower() if address.lower().startswith("0x") else address
    return target


def _sensor_issue_text(errors):
    """Return sensor-specific poll errors, omitting normal skipped cadences."""
    issue_errors = []
    for error in tuple(errors or ()):
        text = str(error or "").strip()
        if not text or text == "sensor_poll_interval_not_elapsed":
            continue
        if text.startswith("sensor_") and text not in issue_errors:
            issue_errors.append(text)
    return ",".join(issue_errors) if issue_errors else ""


def _sensor_errors_indicate_not_found(errors):
    """Return True when sensor errors report a missing I2C device."""
    for error in tuple(errors or ()):
        text = str(error or "").strip()
        if text == "sensor_not_found" or text.startswith("sensor_not_found:"):
            return True
    return False


def _sensor_driver_start_deferred(sensor_service):
    """Return True when sensor startup was intentionally deferred."""
    errors = getattr(sensor_service, "errors", ())
    for error in tuple(errors or ()):
        if str(error or "").strip() == "sensor_driver_start_deferred":
            return True
    return False


def _update_sensor_not_found_window(
    errors,
    failure_count,
    first_failure_at,
    now_monotonic,
):
    """Update the repeated sensor-not-found failure window."""
    if not _sensor_errors_indicate_not_found(errors):
        return 0, -1.0
    if int(failure_count or 0) <= 0:
        first_failure_at = float(now_monotonic or 0.0)
    return int(failure_count or 0) + 1, float(first_failure_at)


def _should_reboot_sensor_not_found(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=SENSOR_NOT_FOUND_REBOOT_S,
    min_count=SENSOR_NOT_FOUND_REBOOT_MIN_COUNT,
):
    """Return True when repeated sensor-not-found errors need reboot recovery."""
    elapsed_s = max(0.0, float(now_monotonic or 0.0) - float(first_failure_at))
    return (
        int(failure_count or 0) >= int(min_count or 0)
        and float(first_failure_at) >= 0.0
        and elapsed_s >= float(timeout_s or 0.0)
    )


def _restart_sensor_stack(sensor_runtime, sensor_service, runtime_config):
    """Rebind sensor hardware and return a fresh adapter, service, and snapshot."""
    stop_sensor_service(sensor_service)
    _collect_garbage()
    sensor_adapter = bind_sensor_hardware(sensor_runtime, runtime_config)
    sensor_service = start_sensor_service(
        sensor_runtime, sensor_adapter, runtime_config
    )
    sensor_snapshot = read_sensor_snapshot(sensor_service, runtime_config)
    return sensor_adapter, sensor_service, sensor_snapshot


def _dns_health_text(network_stack, mqtt_adapter, ntp_state):
    """Return compact DNS health from existing MQTT and NTP resolver state."""
    if getattr(network_stack, "phase", "") != "ready":
        return "unavailable"
    if getattr(network_stack, "socket_pool", None) is None:
        return "unavailable"
    errors = tuple(getattr(mqtt_adapter, "errors", ()) or ()) + tuple(
        getattr(ntp_state, "errors", ()) or ()
    )
    for error in errors:
        text = str(error or "")
        if "resolve_failed" in text or "dns_unready" in text:
            return "error"
    return "ok"


def _ntp_health_text(ntp_state):
    """Return compact NTP health for periodic runtime logs."""
    phase = str(getattr(ntp_state, "phase", "") or "").strip()
    return phase or "idle"


def _command_results_request_ntp_resync(command_results):
    """Return True when applied commands changed runtime time settings."""
    for result in command_results or ():
        if bool(getattr(result, "ntp_resync_requested", False)):
            return True
    return False


def _command_result_reboot_request(command_results):
    """Return the first command result that requested a runtime reboot."""
    for result in command_results or ():
        if bool(getattr(result, "reboot_requested", False)):
            return result
    return None


def _ntp_state_forced_resync(ntp_state):
    """Return NTP state reset so the next allowed pass attempts a sync."""
    return NTPState(
        phase="idle",
        server="",
        datetime_text=str(getattr(ntp_state, "datetime_text", "") or ""),
        last_attempt_at=-1.0,
        last_sync_at=-1.0,
        failure_count=0,
        failure_window=1,
        cooldown_until=-1.0,
        errors=(),
    )


def _is_recoverable_mqtt_poll_error(errors):
    """Return True when poll errors match known recoverable MiniMQTT noise."""
    for error in tuple(errors or ()):
        text = str(error or "").strip()
        if text.startswith("mqtt_poll_failed:minimqtt_socket:"):
            return True
    return False


def _collect_garbage():
    """Run best-effort garbage collection for constrained heap recovery."""
    try:
        gc.collect()
    except Exception:
        return False
    return True


def _log_memory_checkpoint(start_monotonic, phase):
    """Log a compact memory checkpoint for startup and diagnostics."""
    _print_log(
        "memory",
        "phase={} {}".format(str(phase or "unknown"), _memory_summary()),
        start_monotonic=start_monotonic,
    )


def _filesystem_mode_label(fs_writable):
    """Return a compact filesystem mode label for logs."""
    if fs_writable is True:
        return "RWFS"
    if fs_writable is False:
        return "ROFS"
    return "FS?"


def _should_preflight_broker(adapter):
    targets = tuple(getattr(adapter, "broker_targets", ()) or ())
    return len(targets) > 1


def _mqtt_connect_errors_are_repeated_failures(
    errors,
    *,
    had_mqtt_success=False,
    count_plain_connect_failures=False,
):
    for error in tuple(errors or ()):
        text = str(error or "").strip().lower()
        if "repeated connect failures" in text:
            return True
        if text.startswith("mqtt_connect_failed:") and (
            bool(had_mqtt_success) or bool(count_plain_connect_failures)
        ):
            return True
    return False


def _mqtt_connect_attempt_is_connack_timeout_pattern(
    *,
    tcp_preflight_error,
    connect_probe_error,
    connect_probe_code,
    connect_errors,
):
    """Return True for the warm-start TCP-OK/CONNACK-timeout failure pattern."""
    if _mqtt_connect_probe_is_connack_timeout(
        tcp_preflight_error=tcp_preflight_error,
        connect_probe_error=connect_probe_error,
        connect_probe_code=connect_probe_code,
    ):
        return _mqtt_connect_errors_are_repeated_failures(
            connect_errors,
            count_plain_connect_failures=True,
        )
    if tcp_preflight_error or connect_probe_error:
        return False
    return _mqtt_connect_errors_indicate_raw_connack_timeout(connect_errors)


def _mqtt_connect_errors_indicate_raw_connack_timeout(errors):
    """Return True when raw MQTT connect times out after TCP opened."""
    for error in tuple(errors or ()):
        text = str(error or "").strip().lower()
        if not text.startswith("mqtt_connect_failed:") or ":raw:" not in text:
            continue
        if (
            "errno 116" in text
            or "etimedout" in text
            or "timedout" in text
            or "timed out" in text
            or "timeout" in text
            or "errno 119" in text
            or "einprogress" in text
            or "operation now in progress" in text
        ):
            return True
    return False


def _mqtt_connect_probe_is_connack_timeout(
    *,
    tcp_preflight_error,
    connect_probe_error,
    connect_probe_code,
):
    """Return True when TCP opens but raw MQTT CONNACK times out."""
    if tcp_preflight_error:
        return False
    if int(connect_probe_code if connect_probe_code is not None else -1) != -1:
        return False
    probe_text = str(connect_probe_error or "").strip().lower()
    if "mqtt_connect_probe_failed:" not in probe_text:
        return False
    if (
        "errno 116" not in probe_text
        and "etimedout" not in probe_text
        and "timedout" not in probe_text
        and "timed out" not in probe_text
        and "timeout" not in probe_text
    ):
        return False
    return True


def _mqtt_error_indicates_socket_progress(error):
    """Return True when an MQTT socket is stuck in EINPROGRESS."""
    text = str(error or "").strip().lower()
    if not text:
        return False
    return (
        "errno 119" in text
        or "[errno 119]" in text
        or "einprogress" in text
        or "operation now in progress" in text
    )


def _mqtt_preconnect_has_socket_progress(
    *,
    tcp_preflight_error,
    connect_probe_error,
):
    """Return True when preconnect probes hit the warm-start 119 pattern."""
    return bool(
        _mqtt_error_indicates_socket_progress(tcp_preflight_error)
        or _mqtt_error_indicates_socket_progress(connect_probe_error)
    )


def _mqtt_preconnect_pattern_label(
    *,
    connack_timeout_candidate,
    socket_progress_candidate,
):
    """Return a compact label for the MQTT preconnect failure pattern."""
    if socket_progress_candidate:
        return "socket_progress"
    if connack_timeout_candidate:
        return "connack_timeout"
    return "none"


def _mqtt_preconnect_probe(
    adapter,
    network_stack,
    *,
    start_monotonic,
    connect_delay_s=None,
):
    """Run bounded TCP and raw MQTT probes before startup MQTT connect."""
    max_attempts = max(1, int(MQTT_PREFLIGHT_RETRIES or 1))
    tcp_preflight_error = ""
    tcp_preflight_target = ""
    tcp_preflight_finished_at = time.monotonic()
    broker = adapter.active_broker or adapter.broker or "none"
    socket_source = getattr(network_stack, "socket_artifact_source", "") or "unknown"

    for retry_index in range(1, max_attempts + 1):
        tcp_preflight_started_at = time.monotonic()
        tcp_preflight_error, tcp_preflight_target = preflight_mqtt_broker_tcp(adapter)
        tcp_preflight_finished_at = time.monotonic()
        _print_log(
            "mqtt",
            (
                "preflight phase={} broker={} target={} port={} "
                "elapsed_s={:.1f} socket_source={} try={} errors={}"
            ).format(
                "tcp_error" if tcp_preflight_error else "tcp_ok",
                broker,
                tcp_preflight_target or "none",
                adapter.port,
                tcp_preflight_finished_at - tcp_preflight_started_at,
                socket_source,
                retry_index,
                tcp_preflight_error or "none",
            ),
            start_monotonic=start_monotonic,
        )
        if not (
            tcp_preflight_error
            and retry_index < max_attempts
            and _mqtt_error_indicates_socket_progress(tcp_preflight_error)
        ):
            break
        _mqtt_preconnect_retry_sleep(
            "preflight",
            retry_index,
            tcp_preflight_error,
            start_monotonic=start_monotonic,
        )

    connect_probe_error = ""
    connect_probe_target = tcp_preflight_target
    connect_probe_client_id = ""
    connect_probe_code = -1
    connect_probe_finished_at = tcp_preflight_finished_at
    if tcp_preflight_error:
        connect_probe_error = "mqtt_connect_probe_skipped:tcp_preflight_error"
        _print_log(
            "mqtt",
            (
                "connect_probe phase=skipped broker={} target={} port={} "
                "elapsed_s={:.1f} client_id={} connack={} errors={}"
            ).format(
                broker,
                connect_probe_target or "none",
                adapter.port,
                0.0,
                "none",
                connect_probe_code,
                connect_probe_error,
            ),
            start_monotonic=start_monotonic,
        )
        return (
            tcp_preflight_error,
            tcp_preflight_target,
            tcp_preflight_finished_at,
            connect_probe_error,
            connect_probe_target,
            connect_probe_client_id,
            connect_probe_code,
            connect_probe_finished_at,
        )

    if raw_mqtt_connect_enabled(adapter):
        _print_log(
            "mqtt",
            (
                "connect_probe phase=skipped broker={} target={} port={} "
                "elapsed_s={:.1f} client_id={} connack={} reason={} errors={}"
            ).format(
                broker,
                connect_probe_target or "none",
                adapter.port,
                0.0,
                "none",
                connect_probe_code,
                "raw_connect",
                "none",
            ),
            start_monotonic=start_monotonic,
        )
        return (
            tcp_preflight_error,
            tcp_preflight_target,
            tcp_preflight_finished_at,
            connect_probe_error,
            connect_probe_target,
            connect_probe_client_id,
            connect_probe_code,
            connect_probe_finished_at,
        )

    for retry_index in range(1, max_attempts + 1):
        connect_probe_started_at = time.monotonic()
        (
            connect_probe_error,
            connect_probe_target,
            connect_probe_client_id,
            connect_probe_code,
        ) = preflight_mqtt_broker_connect(adapter)
        connect_probe_finished_at = time.monotonic()
        _print_log(
            "mqtt",
            (
                "connect_probe phase={} broker={} target={} port={} "
                "elapsed_s={:.1f} client_id={} connack={} try={} errors={}"
            ).format(
                "connack_error" if connect_probe_error else "connack_ok",
                broker,
                connect_probe_target or "none",
                adapter.port,
                connect_probe_finished_at - connect_probe_started_at,
                connect_probe_client_id or "none",
                connect_probe_code,
                retry_index,
                connect_probe_error or "none",
            ),
            start_monotonic=start_monotonic,
        )
        if not (
            connect_probe_error
            and retry_index < max_attempts
            and _mqtt_error_indicates_socket_progress(connect_probe_error)
        ):
            break
        _mqtt_preconnect_retry_sleep(
            "connect_probe",
            retry_index,
            connect_probe_error,
            start_monotonic=start_monotonic,
        )

    if not tcp_preflight_error and not connect_probe_error:
        _mqtt_preconnect_success_sleep(
            start_monotonic,
            delay_s=connect_delay_s,
        )

    return (
        tcp_preflight_error,
        tcp_preflight_target,
        tcp_preflight_finished_at,
        connect_probe_error,
        connect_probe_target,
        connect_probe_client_id,
        connect_probe_code,
        connect_probe_finished_at,
    )


def _mqtt_preconnect_retry_sleep(phase, retry_index, error, *, start_monotonic):
    """Wait briefly before retrying a socket-progress preconnect probe."""
    delay_s = max(0.0, float(MQTT_PREFLIGHT_RETRY_DELAY_S or 0.0))
    _print_log(
        "mqtt",
        (
            "{} phase=retry reason=socket_progress try={} delay_s={:.1f} errors={}"
        ).format(
            str(phase or "preconnect"),
            int(retry_index or 0),
            delay_s,
            str(error or "none"),
        ),
        start_monotonic=start_monotonic,
    )
    if delay_s > 0.0:
        try:
            time.sleep(delay_s)
        except Exception:
            pass


def _mqtt_preconnect_success_sleep(start_monotonic, *, delay_s=None):
    """Wait briefly after successful probes before MiniMQTT connect."""
    if delay_s is None:
        delay_s = MQTT_PREFLIGHT_CONNECT_DELAY_S
    delay_s = max(0.0, float(delay_s or 0.0))
    if delay_s <= 0.0:
        return False
    _print_log(
        "mqtt",
        "connect_delay phase=pre_minimqtt delay_s={:.1f}".format(delay_s),
        start_monotonic=start_monotonic,
    )
    try:
        time.sleep(delay_s)
    except Exception:
        return False
    return True


def _startup_run_reason_text():
    """Return the CircuitPython supervisor run reason, when available."""
    try:
        import supervisor  # type: ignore

        runtime = getattr(supervisor, "runtime", None)
        reason = getattr(runtime, "run_reason", None)
    except Exception:
        reason = None
    return str(reason or "").strip().lower()


def _run_reason_indicates_soft_reload(run_reason_text=None):
    """Return True when the current run looks like a VM reload."""
    text = str(run_reason_text or _startup_run_reason_text() or "").lower()
    return bool(text and "reload" in text and "startup" not in text)


def _startup_conditioning_enabled_for_current_run(
    soft_reload_cleanup_requested=False,
):
    """Return True when startup MQTT conditioning should run now."""
    if MQTT_STARTUP_CONDITIONING_ENABLED:
        return True
    if not MQTT_STARTUP_CONDITIONING_SOFT_RELOAD_ENABLED:
        return False
    return bool(
        soft_reload_cleanup_requested or _run_reason_indicates_soft_reload()
    )


def _condition_startup_mqtt_socket(
    runtime_config,
    network_stack,
    mqtt_adapter,
    *,
    start_monotonic,
    soft_reload_cleanup_requested=False,
):
    """Run one direct MiniMQTT pass before the app's raw preflight."""
    if not _startup_conditioning_enabled_for_current_run(
        soft_reload_cleanup_requested
    ):
        return mqtt_adapter
    if not getattr(runtime_config, "mqtt_enabled", False):
        return mqtt_adapter
    if not network_link_is_ready(network_stack):
        _print_log(
            "mqtt",
            "startup_conditioning phase=skipped reason=network_not_ready",
            start_monotonic=start_monotonic,
        )
        return mqtt_adapter
    if getattr(mqtt_adapter, "phase", "") != "ready":
        _print_log(
            "mqtt",
            "startup_conditioning phase=skipped reason=adapter_{}".format(
                getattr(mqtt_adapter, "phase", "") or "unavailable"
            ),
            start_monotonic=start_monotonic,
        )
        return mqtt_adapter

    _run_startup_direct_mqtt_probe(
        runtime_config,
        network_stack,
        mqtt_adapter,
        start_monotonic=start_monotonic,
    )
    _collect_garbage()
    rebuilt_adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
    )
    if getattr(rebuilt_adapter, "phase", "") != "ready":
        _print_log(
            "mqtt",
            "startup_conditioning rebuild phase={} errors={}".format(
                getattr(rebuilt_adapter, "phase", "") or "unknown",
                ",".join(getattr(rebuilt_adapter, "errors", ()) or ())
                or "none",
            ),
            start_monotonic=start_monotonic,
        )
    return rebuilt_adapter


def _run_startup_direct_mqtt_probe(
    runtime_config,
    network_stack,
    mqtt_adapter,
    *,
    start_monotonic,
):
    client = None
    broker = (
        getattr(mqtt_adapter, "active_broker", "")
        or getattr(mqtt_adapter, "broker", "")
        or getattr(runtime_config.mqtt, "preferred_host", "")
    )
    broker = str(broker or "").strip()
    port = int(getattr(mqtt_adapter, "port", 1883) or 1883)
    _print_log(
        "mqtt",
        "startup_conditioning phase=start broker={} port={}".format(
            broker or "none",
            port,
        ),
        start_monotonic=start_monotonic,
    )
    try:
        client = _build_startup_direct_mqtt_client(
            runtime_config,
            network_stack,
            mqtt_adapter,
            broker,
            port,
        )
        started = time.monotonic()
        client.connect()
        _print_log(
            "mqtt",
            "startup_conditioning phase=connected elapsed_s={:.1f}".format(
                time.monotonic() - started,
            ),
            start_monotonic=start_monotonic,
        )
        return True
    except Exception as exc:
        _print_log(
            "mqtt",
            "startup_conditioning phase=error type={} error={}".format(
                type(exc).__name__,
                exc,
            ),
            start_monotonic=start_monotonic,
        )
    finally:
        _close_startup_direct_mqtt_client(
            client,
            start_monotonic=start_monotonic,
        )
    return False


def _build_startup_direct_mqtt_client(
    runtime_config,
    network_stack,
    mqtt_adapter,
    broker,
    port,
):
    client_class = getattr(mqtt_adapter, "client_class", None)
    if client_class is None:
        raise RuntimeError("mqtt_client_class_unavailable")
    socket_pool = getattr(network_stack, "socket_pool", None)
    if socket_pool is None:
        raise RuntimeError("socket_pool_unavailable")
    kwargs = {
        "broker": broker,
        "port": int(port or 1883),
        "socket_pool": socket_pool,
        "keep_alive": 60,
        "socket_timeout": MQTT_STARTUP_CONDITIONING_TIMEOUT_S,
        "connect_retries": MQTT_STARTUP_CONDITIONING_RETRIES,
    }
    if (
        bool(getattr(runtime_config.mqtt, "use_tls", False))
        or int(port or 1883) == 8883
    ):
        kwargs["ssl_context"] = getattr(network_stack, "ssl_context", None)
    if str(getattr(runtime_config.mqtt, "username", "") or ""):
        kwargs["username"] = runtime_config.mqtt.username
    if str(getattr(runtime_config.mqtt, "password", "") or ""):
        kwargs["password"] = runtime_config.mqtt.password
    try:
        return client_class(**kwargs)
    except TypeError as exc:
        if not _mqtt_type_error_is_optional_kwarg(exc):
            raise
        _drop_startup_direct_optional_kwargs(kwargs)
    return client_class(**kwargs)


def _mqtt_type_error_is_optional_kwarg(exc):
    text = str(exc or "").strip().lower()
    return "keyword" in text and ("unexpected" in text or "invalid" in text)


def _drop_startup_direct_optional_kwargs(kwargs):
    for key in ("socket_timeout", "connect_retries"):
        try:
            del kwargs[key]
        except Exception:
            pass


def _close_startup_direct_mqtt_client(client, *, start_monotonic):
    if client is None:
        return
    try:
        client.disconnect()
        _print_log(
            "mqtt",
            "startup_conditioning disconnect phase=ok",
            start_monotonic=start_monotonic,
        )
    except Exception as exc:
        _print_log(
            "mqtt",
            "startup_conditioning disconnect phase=error error={}".format(exc),
            start_monotonic=start_monotonic,
        )
    _close_startup_direct_socket(client)


def _close_startup_direct_socket(client):
    try:
        sock = getattr(client, "_sock", None)
    except Exception:
        sock = None
    if sock is None:
        return
    close = getattr(sock, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _reset_mqtt_preflight_connect_delay(current_delay_s, *, start_monotonic):
    """Reset the adaptive post-preflight MiniMQTT delay after success."""
    default_delay_s = max(0.0, float(MQTT_PREFLIGHT_CONNECT_DELAY_S or 0.0))
    if abs(float(current_delay_s or 0.0) - default_delay_s) > 0.01:
        _print_log(
            "mqtt",
            "connect_delay phase=reset delay_s={:.1f}".format(default_delay_s),
            start_monotonic=start_monotonic,
        )
    return default_delay_s


def _increase_mqtt_preflight_connect_delay(current_delay_s, *, start_monotonic):
    """Increase the post-preflight MiniMQTT delay after connect failure."""
    default_delay_s = max(0.0, float(MQTT_PREFLIGHT_CONNECT_DELAY_S or 0.0))
    step_s = max(0.0, float(MQTT_PREFLIGHT_CONNECT_DELAY_STEP_S or 0.0))
    max_delay_s = max(
        default_delay_s,
        float(MQTT_PREFLIGHT_CONNECT_DELAY_MAX_S or 0.0),
    )
    next_delay_s = min(
        max_delay_s,
        max(default_delay_s, float(current_delay_s or 0.0)) + step_s,
    )
    if abs(next_delay_s - float(current_delay_s or 0.0)) > 0.01:
        _print_log(
            "mqtt",
            ("connect_delay phase=increase reason=connect_error delay_s={:.1f}").format(
                next_delay_s
            ),
            start_monotonic=start_monotonic,
        )
    return next_delay_s


def _elapsed_since_s(started_at, now_monotonic):
    """Return elapsed seconds for diagnostic counters."""
    if float(started_at or -1.0) < 0.0:
        return 0
    return int(max(0.0, float(now_monotonic or 0.0) - float(started_at)))


def _mqtt_client_socket_present(adapter):
    """Return True when the bound MiniMQTT client still has a socket."""
    client = getattr(adapter, "client", None)
    if client is None:
        return False
    try:
        return getattr(client, "_sock", None) is not None
    except Exception:
        return False


def _mqtt_repeated_failure_elapsed_s(first_failure_at, now_monotonic):
    """Return elapsed seconds since the first repeated MQTT failure."""
    try:
        first = float(first_failure_at)
        now = float(now_monotonic or 0.0)
    except Exception:
        return 0
    if first < 0.0:
        return 0
    return int(max(0.0, now - first))


def _mqtt_repeated_failure_budget_exhausted(
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S,
):
    """Return True once a repeated MQTT failure window has exceeded budget."""
    try:
        first = float(first_failure_at)
    except Exception:
        return False
    if first < 0.0:
        return False
    return _mqtt_repeated_failure_elapsed_s(first, now_monotonic) >= int(
        float(timeout_s or 0.0)
    )


def _should_fast_reboot_mqtt_connect_failures(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S,
    min_count=MQTT_REPEATED_CONNECT_FAILURE_MIN_COUNT,
):
    return (
        int(failure_count or 0) >= int(min_count or 0)
        and float(first_failure_at) >= 0.0
        and _mqtt_repeated_failure_budget_exhausted(
            first_failure_at,
            now_monotonic,
            timeout_s=timeout_s,
        )
    )


def _mqtt_repeated_failure_reboot_gate_tripped(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_REPEATED_CONNECT_FAILURE_REBOOT_S,
    min_count=MQTT_REPEATED_CONNECT_FAILURE_MIN_COUNT,
):
    """Return True when repeated MQTT failures reached reboot budget."""
    try:
        count = int(failure_count or 0)
        first = float(first_failure_at)
    except Exception:
        count = 0
        first = -1.0
    if first < 0.0:
        return False
    if count >= int(min_count or 0):
        return True
    return _mqtt_repeated_failure_budget_exhausted(
        first,
        now_monotonic,
        timeout_s=timeout_s,
    ) or _should_fast_reboot_mqtt_connect_failures(
        count,
        first,
        now_monotonic,
        timeout_s=timeout_s,
        min_count=min_count,
    )


def _should_stage_mqtt_station_reset_before_reboot(
    failure_count,
    first_failure_at,
    now_monotonic,
    last_station_reset_at,
):
    """Return True when repeated MQTT failures should try station reset first."""
    try:
        if float(last_station_reset_at) >= 0.0:
            return False
    except Exception:
        pass
    return _mqtt_repeated_failure_reboot_gate_tripped(
        failure_count,
        first_failure_at,
        now_monotonic,
    )


def _should_cycle_mqtt_radio_for_socket_progress(station_verify, last_station_reset_at):
    """Return True when MQTT socket-progress recovery should power-cycle Wi-Fi."""
    try:
        if float(last_station_reset_at) >= 0.0:
            return False
    except Exception:
        pass
    if bool(getattr(station_verify, "reset_needed", False)):
        return False
    if not bool(getattr(station_verify, "station_ready", False)):
        return False
    if str(getattr(station_verify, "status", "") or "") != "tcp_failed":
        return False
    for error in tuple(getattr(station_verify, "errors", ()) or ()):
        if _mqtt_error_indicates_socket_progress(error):
            return True
    return False


def _mqtt_client_init_memory_failed(errors):
    """Return True when MQTT client construction failed from heap pressure."""
    text = " ".join(str(error or "") for error in tuple(errors or ())).lower()
    return "mqtt_client_init_failed" in text and (
        "memory allocation failed" in text or "allocating " in text
    )


def _update_mqtt_memory_failure_window(
    errors,
    failure_count,
    first_failure_at,
    now_monotonic,
):
    """Update consecutive MQTT client memory-init failure counters."""
    if not _mqtt_client_init_memory_failed(errors):
        return 0, -1.0
    if int(failure_count or 0) <= 0:
        first_failure_at = float(now_monotonic or 0.0)
    return int(failure_count or 0) + 1, float(first_failure_at)


def _should_reboot_mqtt_memory_failures(
    failure_count,
    first_failure_at,
    now_monotonic,
    *,
    timeout_s=MQTT_MEMORY_FAILURE_REBOOT_S,
    min_count=MQTT_MEMORY_FAILURE_REBOOT_MIN_COUNT,
):
    """Return True when repeated MQTT memory failures need reboot escalation."""
    elapsed_s = max(0.0, float(now_monotonic or 0.0) - float(first_failure_at))
    return (
        int(failure_count or 0) >= int(min_count or 0)
        and float(first_failure_at) >= 0.0
        and elapsed_s >= float(timeout_s or 0.0)
    )


def _should_reboot_long_mqtt_recovery(
    recovery_state,
    now_monotonic,
    *,
    timeout_s=MQTT_LONG_RECOVERY_REBOOT_S,
):
    """Return True when MQTT recovery has exceeded the bounded outage window."""
    return _mqtt_recovery_elapsed_s(recovery_state, now_monotonic) >= float(
        timeout_s or 0.0
    )


def _is_plain_mqtt_connect_failure(reason):
    """Return True for broker connect failures that do not imply socket poison."""
    text = str(reason or "").strip().lower()
    if not text.startswith("mqtt_connect_failed:"):
        return False
    return "repeated connect failures" not in text


def _mqtt_connect_retry_interval_s(
    recovery_state,
    now_monotonic,
    last_disconnect_reason,
):
    """Return the current MQTT connect retry interval."""
    if (
        _is_plain_mqtt_connect_failure(last_disconnect_reason)
        and _mqtt_recovery_elapsed_s(recovery_state, now_monotonic)
        >= MQTT_BROKER_OUTAGE_BACKOFF_AFTER_S
    ):
        return MQTT_BROKER_OUTAGE_RETRY_INTERVAL_S
    return 5.0


def _should_reset_mqtt_station_for_plain_connect_failure(
    recovery_state,
    now_monotonic,
    last_disconnect_reason,
    last_station_reset_at,
    *,
    reset_after_s=MQTT_PLAIN_CONNECT_STATION_RESET_S,
    reset_interval_s=MQTT_PLAIN_CONNECT_STATION_RESET_INTERVAL_S,
):
    """Return True when plain MQTT connect failures need station reset."""
    if not _is_plain_mqtt_connect_failure(last_disconnect_reason):
        return False
    now_value = float(now_monotonic or 0.0)
    if _mqtt_recovery_elapsed_s(recovery_state, now_value) < float(reset_after_s):
        return False
    if float(last_station_reset_at or -1.0) < 0.0:
        return True
    return (now_value - float(last_station_reset_at)) >= float(reset_interval_s)


def _recovery_hard_reset_marker_value(nvm=None):
    """Return the recovery hard-reset marker byte, or -1 when unavailable."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= RECOVERY_HARD_RESET_NVM_INDEX:
            return -1
        return int(nvm[RECOVERY_HARD_RESET_NVM_INDEX] or 0)
    except Exception:
        return -1


def _recovery_hard_reset_marker_is_set(reboot_reason, nvm=None):
    """Return True when a persistent MQTT hard reset already ran."""
    if str(reboot_reason or "").strip() != "mqtt_repeated_connect_failures":
        return False
    return _recovery_hard_reset_marker_value(nvm) == RECOVERY_HARD_RESET_MQTT_MARKER


def _recovery_hard_reset_marker_should_suppress(
    reboot_reason,
    first_failure_at,
    now_monotonic,
    nvm=None,
):
    """Return True when the marker should suppress only a fast repeat reset."""
    if nvm is None:
        marker_is_set = _recovery_hard_reset_marker_is_set(reboot_reason)
    else:
        marker_is_set = _recovery_hard_reset_marker_is_set(reboot_reason, nvm)
    if not marker_is_set:
        return False
    return not _mqtt_repeated_failure_budget_exhausted(
        first_failure_at,
        now_monotonic,
    )


def _mark_recovery_hard_reset_requested(reboot_reason, nvm=None):
    """Mark a persistent MQTT hard reset before resetting the MCU."""
    if str(reboot_reason or "").strip() != "mqtt_repeated_connect_failures":
        return False
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= RECOVERY_HARD_RESET_NVM_INDEX:
            return False
        nvm[RECOVERY_HARD_RESET_NVM_INDEX] = RECOVERY_HARD_RESET_MQTT_MARKER
        return True
    except Exception:
        return False


def _clear_recovery_hard_reset_marker(reboot_reason, nvm=None):
    """Clear the persistent MQTT hard-reset marker after MQTT recovers."""
    if str(reboot_reason or "").strip() != "mqtt_repeated_connect_failures":
        return False
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= RECOVERY_HARD_RESET_NVM_INDEX:
            return False
        if int(nvm[RECOVERY_HARD_RESET_NVM_INDEX] or 0) != 0:
            nvm[RECOVERY_HARD_RESET_NVM_INDEX] = 0
        return True
    except Exception:
        return False


def _should_rebuild_mqtt_adapter_for_recovery(mqtt_adapter, last_disconnect_reason):
    """Return True when MQTT recovery should replace the current adapter."""
    if getattr(mqtt_adapter, "phase", "") != "ready":
        return True
    return _mqtt_disconnect_reason_requires_rebuild(last_disconnect_reason)


def _is_after_ready_wifi_failure(signature):
    """Return True for Wi-Fi failures seen after a known-good station link."""
    return str(signature or "") in WIFI_AFTER_READY_FAILURE_SIGNATURES


def _should_fast_reset_wifi_station(signature, failure_count):
    """Return True when post-ready Wi-Fi failure should reset station state."""
    return (
        _is_after_ready_wifi_failure(signature)
        and int(failure_count or 0) >= WIFI_AFTER_READY_STATION_RESET_FAILURES
    )


def _should_reset_wifi_station_before_ready(signature, failure_count, wifi_was_ready):
    """Return True when startup Wi-Fi recovery needs a station reset."""
    return (
        not bool(wifi_was_ready)
        and str(signature or "") in WIFI_BEFORE_READY_RESET_SIGNATURES
        and int(failure_count or 0) >= WIFI_BEFORE_READY_STATION_RESET_FAILURES
    )


def _wifi_station_reset_reason(fast_station_reset, pre_ready_station_reset):
    if fast_station_reset:
        return "after_ready_failure"
    if pre_ready_station_reset:
        return "before_ready_failure"
    return "backoff"


def _verify_station_reset_needed_for_mqtt(
    runtime_config,
    network_stack,
    mqtt_adapter,
    *,
    reason,
    start_monotonic,
):
    """Return station connectivity status before an MQTT-triggered reset."""
    broker = (
        getattr(mqtt_adapter, "active_broker", "")
        or getattr(mqtt_adapter, "broker", "")
        or getattr(getattr(runtime_config, "mqtt", None), "preferred_host", "")
    )
    port = int(getattr(mqtt_adapter, "port", 1883) or 1883)
    result = verify_station_connectivity(
        runtime_config,
        network_stack,
        host=broker,
        port=port,
        probe="tcp",
    )
    _print_log(
        "recovery",
        (
            "wifi verify reason={} status={} station_ready={} "
            "reset_needed={} probe={} target={} errors={}"
        ).format(
            str(reason or "mqtt"),
            result.status or "unknown",
            1 if result.station_ready else 0,
            1 if result.reset_needed else 0,
            result.probe or "none",
            result.target or "none",
            ",".join(result.errors) if result.errors else "none",
        ),
        start_monotonic=start_monotonic,
    )
    return result


def _station_reset_reason_from_mqtt_flags(
    connack_timeout_station_reset,
    plain_station_reset,
    mqtt_disconnect_reason,
):
    if connack_timeout_station_reset:
        return "connack_timeout"
    if plain_station_reset:
        return "plain_connect_failure"
    text = str(mqtt_disconnect_reason or "").strip()
    if text.startswith("mqtt_poll_failed:"):
        return "mqtt_poll_failed"
    if text.startswith("mqtt_publish_failed:"):
        return "mqtt_publish_failed"
    if text.startswith("mqtt_subscribe_failed:"):
        return "mqtt_subscribe_failed"
    if text.startswith("mqtt_poll_callback_failed:"):
        return "mqtt_poll_callback_failed"
    if text.startswith("mqtt_client_on_disconnect"):
        return "mqtt_client_on_disconnect"
    return "mqtt_rebuild"


def _should_fast_reboot_wifi_after_ready(
    signature,
    failure_count,
    recovery_elapsed_s,
):
    """Return True when post-ready Wi-Fi recovery should reload runtime."""
    return (
        _is_after_ready_wifi_failure(signature)
        and int(failure_count or 0) >= WIFI_AFTER_READY_REBOOT_MIN_FAILURES
        and float(recovery_elapsed_s or 0.0) >= WIFI_AFTER_READY_REBOOT_S
    )


def _recovery_reconnect_attempts(reset_station):
    return STATION_RESET_RECONNECT_ATTEMPTS if reset_station else 1


def _recovery_reconnect_delay_s(reset_station):
    return STATION_RESET_RECONNECT_DELAY_S if reset_station else 0.0


def _looks_like_ip_literal(value):
    text = str(value or "").strip()
    if not text:
        return False
    parts = text.split(".")
    if len(parts) == 4:
        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False
    return ":" in text


def _wifi_recovery_elapsed_s(recovery_state, now_monotonic):
    started_at = float(getattr(recovery_state, "phase_started_at", -1.0))
    if started_at < 0.0:
        return 0.0
    return max(0.0, float(now_monotonic or 0.0) - started_at)


def _mqtt_recovery_elapsed_s(recovery_state, now_monotonic):
    if getattr(recovery_state, "phase", "") != "mqtt":
        return 0.0
    started_at = float(getattr(recovery_state, "phase_started_at", -1.0))
    if started_at < 0.0:
        return 0.0
    return max(0.0, float(now_monotonic or 0.0) - started_at)


def _should_log_wifi_failure_signature(signature, count, reset_station):
    """Return True when a Wi-Fi failure signature should be emitted."""
    if not signature or signature == "none":
        return False
    if count in (1, 3):
        return True
    return bool(reset_station and (int(count or 0) % 5) == 0)


def _wifi_signature_text(network_stack, *, had_ready_link=False):
    """Return the health-log Wi-Fi signature text for the network state."""
    signature = network_error_signature(network_stack, had_ready_link=had_ready_link)
    if signature == "none" and getattr(network_stack, "phase", "") == "ready":
        return "ready"
    return signature or "none"


def _runtime_device_id(runtime_config):
    """Return the preferred device identifier for runtime logs."""
    sensor = getattr(runtime_config, "sensor", None)
    switch = getattr(runtime_config, "switch", None)
    network = getattr(runtime_config, "network", None)
    for value in (
        getattr(sensor, "sensor_id", ""),
        getattr(switch, "device_id", ""),
        getattr(network, "hostname", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "cPyNodus_II"


def _should_verify_mqtt_before_rebuild(
    transport,
    recovery_state,
    now_monotonic,
    *,
    success_window_s=MQTT_REBUILD_VERIFY_WINDOW_S,
    phase_window_s=MQTT_REBUILD_VERIFY_PHASE_S,
):
    """Return True when recent MQTT success should get one reconnect attempt."""
    if _mqtt_disconnect_reason_requires_rebuild(
        getattr(transport, "last_disconnect_reason", "")
    ):
        return False
    last_success_at = float(getattr(transport, "last_success_at", -1.0))
    if last_success_at < 0.0:
        return False
    now_value = float(now_monotonic or 0.0)
    if now_value - last_success_at > float(success_window_s or 0.0):
        return False
    return _mqtt_recovery_elapsed_s(recovery_state, now_value) <= float(
        phase_window_s or 0.0
    )


def _mqtt_disconnect_reason_requires_rebuild(reason):
    """Return True when the MQTT client/socket should be rebuilt immediately."""
    text = str(reason or "").strip()
    if not text:
        return False
    if text.startswith("mqtt_connect_failed:"):
        return "repeated connect failures" in text.lower()
    hard_prefixes = (
        "mqtt_poll_failed:",
        "mqtt_publish_failed:",
        "mqtt_subscribe_failed:",
        "mqtt_poll_callback_failed:",
        "mqtt_client_on_disconnect",
    )
    return any(text.startswith(prefix) for prefix in hard_prefixes)


def _mqtt_disconnect_reason_indicates_socket_poison(reason):
    """Return True when MQTT failure suggests stale lower-level socket state."""
    text = str(reason or "").strip().lower()
    if not text:
        return False
    return (
        "ebadf" in text
        or "[errno 9]" in text
        or "bad file descriptor" in text
        or "socket not managed" in text
    )


def _should_keep_mqtt_station_reset_after_verify(mqtt_disconnect_reason, result):
    """Return True when MQTT recovery should still reset station after verify."""
    if bool(getattr(result, "reset_needed", False)):
        return True
    return _mqtt_disconnect_reason_indicates_socket_poison(mqtt_disconnect_reason)


def _is_mqtt_subscription_failure(sync_result):
    """Return True when MQTT sync failed while subscribing."""
    if getattr(sync_result, "phase", "") != "error":
        return False
    operation = str(getattr(sync_result, "operation", "") or "")
    if operation and operation != "subscribe":
        return False
    for error in tuple(getattr(sync_result, "errors", ()) or ()):
        if "mqtt_subscribe_failed:" in str(error or ""):
            return True
    return False


def _ntp_allowed_for_startup(*, mqtt_enabled, transport, ntp_state=None):
    """Return True when startup NTP may attempt a sync."""
    if str(getattr(ntp_state, "phase", "") or "") == "synced":
        return True
    if not bool(NTP_DEFER_UNTIL_MQTT_STARTUP_CLEAR):
        return True
    if not bool(mqtt_enabled):
        return True
    if not bool(getattr(transport, "connected", False)):
        return False
    try:
        pending_subscriptions = len(getattr(transport, "subscriptions", ()) or ())
        pending_publishes = len(getattr(transport, "published_messages", ()) or ())
        return pending_subscriptions == 0 and pending_publishes == 0
    except Exception:
        return False


def _startup_subscription_recovery_drained(sync_result, transport):
    """Return True when recovery drained queued MQTT subscriptions."""
    if getattr(sync_result, "phase", "") != "synced":
        return False
    if int(getattr(sync_result, "subscribed_count", 0) or 0) <= 0:
        return False
    try:
        return len(getattr(transport, "subscriptions", ()) or ()) == 0
    except Exception:
        return False


def _recover_mqtt_subscription_failure(
    *,
    runtime_config,
    network_stack,
    mqtt_adapter,
    transport,
    sync_result=None,
    fs_writable=False,
    start_monotonic,
):
    """Close and rebuild MQTT after a subscribe timeout poisons the session."""
    topic = str(getattr(sync_result, "topic", "") or "none")
    detail = "reason=subscribe_failure topic={} {}".format(
        topic,
        _transport_queue_summary(transport),
    )
    _print_log(
        "recovery",
        "action=mqtt_rebuild {}".format(detail),
        start_monotonic=start_monotonic,
    )
    _log_recovery_event(
        "mqtt_rebuild",
        detail,
        fs_writable=fs_writable,
        device_id=_runtime_device_id(runtime_config),
    )
    close_result = close_mqtt_client(mqtt_adapter, transport)
    if close_result.errors:
        _print_log(
            "recovery",
            "mqtt close errors={}".format(",".join(close_result.errors)),
            start_monotonic=start_monotonic,
        )
    rebuilt_adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
    )
    _print_log(
        "recovery",
        "mqtt action=rebuild broker={} socket_pool={} station_reset=0".format(
            rebuilt_adapter.active_broker or rebuilt_adapter.broker or "none",
            "ready" if network_stack.socket_pool is not None else "none",
        ),
        start_monotonic=start_monotonic,
    )
    return rebuilt_adapter


def _resolve_broker_ip_from_hostname(runtime_config, network_stack):
    """Resolve configured MQTT broker hostname to the primary IP literal."""
    broker = str(getattr(runtime_config.mqtt, "broker", "") or "").strip()
    if not broker:
        return "", ()
    if _looks_like_ip_literal(broker):
        return broker, ()
    socket_pool = getattr(network_stack, "socket_pool", None)
    if socket_pool is None:
        return "", ("mqtt_resolve_failed:{}:socket_pool_unavailable".format(broker),)
    getaddrinfo = getattr(socket_pool, "getaddrinfo", None)
    if not callable(getaddrinfo):
        return "", ("mqtt_resolve_failed:{}:getaddrinfo_unavailable".format(broker),)
    try:
        resolved = getaddrinfo(broker, runtime_config.mqtt.port)
    except Exception as exc:
        return "", ("mqtt_resolve_failed:{}:{}".format(broker, exc),)
    resolved_ip = _ip_from_getaddrinfo_result(resolved)
    if not resolved_ip:
        return "", ("mqtt_resolve_failed:{}:empty_result".format(broker),)
    return resolved_ip, ()


def _refresh_broker_ip_from_hostname(
    runtime_config,
    network_stack,
    *,
    settings_root=None,
):
    """Refresh runtime MQTT.BROKER_IP before MQTT connects."""
    resolved_ip, errors = _resolve_broker_ip_from_hostname(
        runtime_config,
        network_stack,
    )
    if errors:
        return runtime_config, "error", tuple(errors)
    if not resolved_ip:
        return runtime_config, "skipped", ()
    resolved_ip = str(resolved_ip or "").strip()
    current_ip = str(getattr(runtime_config.mqtt, "broker_ip", "") or "").strip()
    if current_ip == resolved_ip:
        return runtime_config, "unchanged", ()
    resolved_runtime = replace(
        runtime_config,
        mqtt=replace(
            runtime_config.mqtt,
            broker_ip=resolved_ip,
        ),
    )
    return resolved_runtime, "resolved_volatile", ()


def _broker_ip_refresh_needed(runtime_config, *, settings_root=None):
    """Return True when MQTT.BROKER can refresh the literal broker IP."""
    broker = str(getattr(runtime_config.mqtt, "broker", "") or "").strip()
    return bool(broker)


def _refresh_broker_ip_for_mqtt(
    runtime_config,
    network_stack,
    *,
    settings_root=None,
    start_monotonic=None,
):
    """Refresh MQTT broker IPs and report whether the active target changed."""
    if not _broker_ip_refresh_needed(runtime_config, settings_root=settings_root):
        return runtime_config, False
    previous_targets = tuple(getattr(runtime_config.mqtt, "connection_targets", ()))
    runtime_config, broker_ip_phase, broker_ip_errors = (
        _refresh_broker_ip_from_hostname(
            runtime_config,
            network_stack,
            settings_root=settings_root,
        )
    )
    if broker_ip_phase != "skipped":
        _print_log(
            "mqtt",
            "broker_ip phase={} host={} ip={} errors={}".format(
                broker_ip_phase,
                runtime_config.mqtt.broker or "none",
                runtime_config.mqtt.broker_ip or "none",
                ",".join(broker_ip_errors) if broker_ip_errors else "none",
            ),
            start_monotonic=start_monotonic,
        )
    current_targets = tuple(getattr(runtime_config.mqtt, "connection_targets", ()))
    return runtime_config, bool(current_targets and current_targets != previous_targets)


def _persist_broker_ip_after_mqtt_connect(
    runtime_config,
    *,
    settings_root=None,
    start_monotonic=None,
):
    """Persist resolved broker IPs after MQTT has connected successfully."""
    if settings_root is None:
        return False
    broker_ip = str(getattr(runtime_config.mqtt, "broker_ip", "") or "").strip()
    if not broker_ip:
        return False
    updates = (
        {
            "section": "MQTT",
            "key": "BROKER_IP",
            "value": broker_ip,
        },
    )
    _persisted_config, persisted_updates, persistence_errors = (
        Settings.apply_updates_to_directory(
            settings_root,
            runtime_config,
            updates,
            reload_runtime=False,
        )
    )
    _ = _persisted_config
    phase = "persisted" if persisted_updates and not persistence_errors else "volatile"
    _print_log(
        "mqtt",
        "broker_ip_persist phase={} ip={} errors={}".format(
            phase,
            broker_ip,
            ",".join(persistence_errors) if persistence_errors else "none",
        ),
        start_monotonic=start_monotonic,
    )
    return bool(persisted_updates and not persistence_errors)


def _ip_from_getaddrinfo_result(resolved):
    try:
        for item in resolved or ():
            try:
                sockaddr = item[-1]
                ip = str(sockaddr[0] or "").strip()
            except Exception:
                continue
            if not ip or not _looks_like_ip_literal(ip):
                continue
            return ip
    except Exception:
        pass
    return ""


def _should_fallback_to_ap(runtime_config, network_stack):
    if runtime_config.ap_mode:
        return False
    if getattr(network_stack, "phase", "") == "ap":
        return False
    if not runtime_config.network.ssid or not runtime_config.network.password:
        return True
    if runtime_config.active_profile != "nodusweb":
        return False
    if runtime_config.network.ssid:
        if _station_ip_looks_recoverable(runtime_config, network_stack):
            return False
        return getattr(network_stack, "phase", "") in {"error", "unavailable"}
    return True


def _station_ip_looks_recoverable(runtime_config, network_stack):
    """Return True when station has a non-AP IP and recovery should proceed."""
    if getattr(network_stack, "phase", "") not in {"error", "unavailable"}:
        return False
    ip_address = str(getattr(network_stack, "ip_address", "") or "").strip()
    if not ip_address:
        return False
    if (
        ip_address.startswith("192.168.4.")
        and runtime_config.network.ssid != runtime_config.network.ap_ssid
    ):
        return False
    return True


def _startup_ap_fallback_reason(station_errors):
    """Return the original station failure reason for AP fallback logs."""
    errors = tuple(station_errors or ())
    if errors:
        return ",".join(str(error) for error in errors)
    return "network_startup_failed"


def _enter_ap_recovery_mode(runtime_config):
    return replace(runtime_config, active_profile="nodusweb", ap_mode=True)


def _mark_soft_reload_prepared():
    global _SOFT_RELOAD_PREPARED
    _SOFT_RELOAD_PREPARED = True


def _consume_soft_reload_prepared():
    global _SOFT_RELOAD_PREPARED
    prepared = bool(_SOFT_RELOAD_PREPARED)
    _SOFT_RELOAD_PREPARED = False
    return prepared


def _soft_reload_cleanup_marker_value(nvm=None):
    """Return the soft-reload cleanup marker byte, or -1 when unavailable."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= SOFT_RELOAD_CLEANUP_NVM_INDEX:
            return -1
        return int(nvm[SOFT_RELOAD_CLEANUP_NVM_INDEX] or 0)
    except Exception:
        return -1


def _mark_soft_reload_cleanup_requested(nvm=None):
    """Mark that the next boot should run warm-start radio cleanup."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= SOFT_RELOAD_CLEANUP_NVM_INDEX:
            return False
        nvm[SOFT_RELOAD_CLEANUP_NVM_INDEX] = SOFT_RELOAD_CLEANUP_MARKER
        return True
    except Exception:
        return False


def _consume_soft_reload_cleanup_marker(nvm=None):
    """Clear and return whether warm-start radio cleanup was requested."""
    if nvm is None:
        try:
            import microcontroller  # type: ignore

            nvm = getattr(microcontroller, "nvm", None)
        except ImportError:
            nvm = None
    try:
        if nvm is None or len(nvm) <= SOFT_RELOAD_CLEANUP_NVM_INDEX:
            return False
        requested = (
            int(nvm[SOFT_RELOAD_CLEANUP_NVM_INDEX] or 0) == SOFT_RELOAD_CLEANUP_MARKER
        )
        if int(nvm[SOFT_RELOAD_CLEANUP_NVM_INDEX] or 0) != 0:
            nvm[SOFT_RELOAD_CLEANUP_NVM_INDEX] = 0
        return requested
    except Exception:
        return False


def _call_runtime_method(handle, name):
    method = getattr(handle, name, None)
    if not callable(method):
        return False
    try:
        method()
        return True
    except Exception:
        return False


def _cycle_wifi_radio_for_warm_start(start_monotonic):
    """Reset station/AP state before normal warm-start initialization."""
    try:
        import wifi  # type: ignore
    except ImportError:
        _print_log(
            "runtime",
            "warm_start_cleanup phase=skipped reason=wifi_unavailable",
            start_monotonic=start_monotonic,
        )
        return False
    radio = getattr(wifi, "radio", None)
    if radio is None:
        _print_log(
            "runtime",
            "warm_start_cleanup phase=skipped reason=radio_unavailable",
            start_monotonic=start_monotonic,
        )
        return False
    _print_log(
        "runtime",
        "warm_start_cleanup phase=start",
        start_monotonic=start_monotonic,
    )
    stopped_ap = _call_runtime_method(radio, "stop_ap")
    disconnected = _call_runtime_method(radio, "disconnect")
    stopped_station = _call_runtime_method(radio, "stop_station")
    started_station = False
    if stopped_station:
        started_station = _call_runtime_method(radio, "start_station")
    _collect_garbage()
    try:
        time.sleep(max(0.0, float(WARM_START_RADIO_SETTLE_S)))
    except Exception:
        pass
    _print_log(
        "runtime",
        (
            "warm_start_cleanup phase=done disconnect={} stop_station={} "
            "stop_ap={} cycle_radio={} start_station={}"
        ).format(
            1 if disconnected else 0,
            1 if stopped_station else 0,
            1 if stopped_ap else 0,
            0,
            1 if started_station else 0,
        ),
        start_monotonic=start_monotonic,
    )
    return bool(disconnected or stopped_station or stopped_ap or started_station)


def _soft_reboot(*, reason="soft_reboot", start_monotonic=None, settle_s=0.0):
    marker_status = "set" if _mark_soft_reload_cleanup_requested() else "unavailable"
    try:
        delay_s = max(0.0, float(settle_s or 0.0))
    except Exception:
        delay_s = 0.0
    if start_monotonic is not None:
        _print_log(
            "runtime",
            "action=reload_prepare reason={} marker={}".format(
                str(reason or "soft_reboot"),
                marker_status,
            ),
            start_monotonic=start_monotonic,
        )
    if delay_s > 0.0:
        if start_monotonic is not None:
            _print_log(
                "runtime",
                "action=reload_wait reason={} delay_s={:.1f}".format(
                    str(reason or "soft_reboot"),
                    delay_s,
                ),
                start_monotonic=start_monotonic,
            )
        try:
            time.sleep(delay_s)
        except Exception:
            pass
    if start_monotonic is not None:
        _print_log(
            "runtime",
            "action=reload reason={}".format(str(reason or "soft_reboot")),
            start_monotonic=start_monotonic,
        )
    try:
        import supervisor  # type: ignore
    except ImportError as exc:
        raise RuntimeError("supervisor_unavailable") from exc
    reload_runtime = getattr(supervisor, "reload", None)
    if not callable(reload_runtime):
        raise RuntimeError("supervisor_reload_unavailable")
    reload_runtime()


def _hard_reboot(*, reason="hard_reboot", start_monotonic=None):
    if start_monotonic is not None:
        _print_log(
            "runtime",
            "action=reset reason={}".format(str(reason or "hard_reboot")),
            start_monotonic=start_monotonic,
        )
    try:
        import microcontroller  # type: ignore
    except ImportError as exc:
        raise RuntimeError("microcontroller_unavailable") from exc
    reset = getattr(microcontroller, "reset", None)
    if not callable(reset):
        raise RuntimeError("microcontroller_reset_unavailable")
    reset()


def _recovery_reboot_kind(reboot_reason, *, fs_writable=False):
    """Return the reload depth needed for a recovery escalation."""
    reason = str(reboot_reason or "").strip()
    if reason in HARD_RECOVERY_REBOOT_REASONS:
        return "hard"
    if fs_writable is not True:
        return "soft"
    return "soft"


def _runtime_restart_kind(runtime_config, requested_kind="soft"):
    """Return the reboot kind for an app-requested runtime restart."""
    requested = str(requested_kind or "soft").strip().lower()
    if requested == "hard":
        return "hard"
    if bool(getattr(runtime_config, "mqtt_enabled", False)):
        return "hard"
    return "soft"


def _log_recovery_reboot(
    reboot_reason,
    *,
    fs_writable,
    reboot_kind="soft",
    device_id="",
):
    """Persist a recovery reboot traceback when RWFS is available."""
    if fs_writable is not True:
        return False
    kind = str(reboot_kind or "soft").strip() or "soft"
    return append_reboot_reason_traceback(
        reboot_reason,
        header="recovery {} reboot: {}".format(
            kind,
            str(reboot_reason or "unknown"),
        ),
        device_id=device_id,
    )


def _log_recovery_soft_reboot(reboot_reason, *, fs_writable, device_id=""):
    """Persist a legacy soft-reboot traceback when RWFS is available."""
    return _log_recovery_reboot(
        reboot_reason,
        fs_writable=fs_writable,
        reboot_kind="soft",
        device_id=device_id,
    )


def _log_recovery_event(event, detail="", *, fs_writable, device_id=""):
    """Persist a bounded recovery event when RWFS is available."""
    if fs_writable is not True:
        return False
    from cpynodus_ii.core.recovery_log import append_recovery_event

    return append_recovery_event(event, detail, device_id=device_id)


def _teardown_network_for_shutdown(
    network_stack,
    *,
    start_monotonic,
    log_prefix="runtime",
    cycle_radio=False,
):
    """Tear down Wi-Fi networking before leaving the runtime."""
    if network_stack is None:
        return False
    try:
        torn_down = teardown_network_stack(
            network_stack,
            cycle_radio=bool(cycle_radio),
        )
        _print_log(
            log_prefix,
            "network action=teardown result={} cycle_radio={}".format(
                1 if torn_down else 0,
                1 if cycle_radio else 0,
            ),
            start_monotonic=start_monotonic,
        )
        return bool(torn_down)
    except Exception as exc:
        _print_log(
            log_prefix,
            "network teardown errors={}".format(str(exc)),
            start_monotonic=start_monotonic,
        )
    return False


def _stop_web_runtime_for_shutdown(
    web_runtime,
    *,
    start_monotonic,
    log_prefix="runtime",
):
    """Stop the web runtime before closing station networking."""
    if web_runtime is None:
        return False
    stop = getattr(web_runtime, "stop", None)
    if not callable(stop):
        return False
    try:
        stopped = bool(stop())
        _print_log(
            log_prefix,
            "web action=stop result={}".format(1 if stopped else 0),
            start_monotonic=start_monotonic,
        )
        return stopped
    except Exception as exc:
        _print_log(
            log_prefix,
            "web stop errors={}".format(str(exc)),
            start_monotonic=start_monotonic,
        )
    return False


def _stop_feature_services_for_shutdown(
    *,
    sensor_service=None,
    switch_service=None,
    start_monotonic,
    log_prefix="runtime",
):
    """Deinitialize sensor and switch services before a reload."""
    stopped_sensor = False
    stopped_switch = False
    if sensor_service is not None:
        try:
            _stop_sensor_service_for_shutdown(sensor_service)
            stopped_sensor = True
        except Exception as exc:
            _print_log(
                log_prefix,
                "sensor stop errors={}".format(str(exc)),
                start_monotonic=start_monotonic,
            )
    if switch_service is not None:
        try:
            _stop_switch_service_for_shutdown(switch_service)
            stopped_switch = True
        except Exception as exc:
            _print_log(
                log_prefix,
                "switch stop errors={}".format(str(exc)),
                start_monotonic=start_monotonic,
            )
    if stopped_sensor or stopped_switch:
        _print_log(
            log_prefix,
            "services action=stop sensor={} switch={}".format(
                1 if stopped_sensor else 0,
                1 if stopped_switch else 0,
            ),
            start_monotonic=start_monotonic,
        )
    return bool(stopped_sensor or stopped_switch)


def _mark_unprepared_shutdown_cleanup(start_monotonic, *, reason="unprepared"):
    """Request warm-start cleanup after an external or fatal shutdown."""
    marker_status = "set" if _mark_soft_reload_cleanup_requested() else "unavailable"
    _print_log(
        "runtime",
        "shutdown_prepare reason={} marker={}".format(
            str(reason or "unprepared"),
            marker_status,
        ),
        start_monotonic=start_monotonic,
    )
    return marker_status == "set"


def _prepare_soft_recovery_reboot(
    *,
    mqtt_adapter=None,
    transport=None,
    runtime_config=None,
    network_stack=None,
    web_runtime=None,
    sensor_service=None,
    switch_service=None,
    start_monotonic,
):
    """Close MQTT and station networking before a recovery reload."""
    _stop_web_runtime_for_shutdown(
        web_runtime,
        start_monotonic=start_monotonic,
        log_prefix="recovery",
    )
    if (
        runtime_config is not None
        and bool(getattr(runtime_config, "mqtt_enabled", False))
        and mqtt_adapter is not None
        and transport is not None
    ):
        try:
            if (
                bool(getattr(transport, "connected", False))
                and runtime_config is not None
            ):
                mqtt_result = disconnect_mqtt_client(
                    mqtt_adapter,
                    transport,
                    runtime_config,
                )
                action = "disconnect"
            else:
                mqtt_result = close_mqtt_client(mqtt_adapter, transport)
                action = "close"
            if mqtt_result.errors:
                _print_log(
                    "recovery",
                    "mqtt {} errors={}".format(
                        action,
                        ",".join(mqtt_result.errors),
                    ),
                    start_monotonic=start_monotonic,
                )
        except Exception as exc:
            _print_log(
                "recovery",
                "mqtt close errors={}".format(str(exc)),
                start_monotonic=start_monotonic,
            )
    _collect_garbage()
    _stop_feature_services_for_shutdown(
        sensor_service=sensor_service,
        switch_service=switch_service,
        start_monotonic=start_monotonic,
        log_prefix="recovery",
    )
    _collect_garbage()
    _teardown_network_for_shutdown(
        network_stack,
        start_monotonic=start_monotonic,
        log_prefix="recovery",
        cycle_radio=False,
    )
    _collect_garbage()


def _perform_recovery_reboot(
    reboot_reason,
    reboot_kind,
    *,
    fs_writable,
    start_monotonic,
    mqtt_adapter=None,
    transport=None,
    runtime_config=None,
    network_stack=None,
    web_runtime=None,
    sensor_service=None,
    switch_service=None,
):
    """Persist recovery reboot context, then perform the selected reboot."""
    _log_recovery_reboot(
        reboot_reason,
        fs_writable=fs_writable,
        reboot_kind=reboot_kind,
        device_id=_runtime_device_id(runtime_config),
    )
    _log_recovery_event(
        "{}_reboot".format(str(reboot_kind or "soft")),
        "reason={}".format(str(reboot_reason or "unknown")),
        fs_writable=fs_writable,
        device_id=_runtime_device_id(runtime_config),
    )
    reason = "recovery:{}".format(
        reboot_reason or "{}_reboot".format(reboot_kind or "soft")
    )
    if reboot_kind == "hard":
        _hard_reboot(reason=reason, start_monotonic=start_monotonic)
    else:
        _prepare_soft_recovery_reboot(
            mqtt_adapter=mqtt_adapter,
            transport=transport,
            runtime_config=runtime_config,
            network_stack=network_stack,
            web_runtime=web_runtime,
            sensor_service=sensor_service,
            switch_service=switch_service,
            start_monotonic=start_monotonic,
        )
        _mark_soft_reload_prepared()
        _soft_reboot(
            reason=reason,
            start_monotonic=start_monotonic,
            settle_s=SOFT_REBOOT_SETTLE_S,
        )


def _maybe_reboot_for_mqtt_failure_budget(
    *,
    failure_count,
    first_failure_at,
    now_monotonic,
    fs_writable,
    start_monotonic,
    mqtt_adapter=None,
    transport=None,
    runtime_config=None,
    network_stack=None,
    web_runtime=None,
    sensor_service=None,
    switch_service=None,
):
    """Escalate repeated MQTT preconnect failures by count or elapsed budget."""
    if not _mqtt_repeated_failure_reboot_gate_tripped(
        failure_count,
        first_failure_at,
        now_monotonic,
    ):
        return "none"

    reboot_reason = "mqtt_repeated_connect_failures"
    if _recovery_hard_reset_marker_should_suppress(
        reboot_reason,
        first_failure_at,
        now_monotonic,
    ):
        _print_log(
            "recovery",
            (
                "action=hard_reboot_deferred reason={} marker=nvm "
                "count={} elapsed_s={}"
            ).format(
                reboot_reason,
                int(failure_count or 0),
                _mqtt_repeated_failure_elapsed_s(first_failure_at, now_monotonic),
            ),
            start_monotonic=start_monotonic,
        )
        return "none"

    marker_status = (
        "set" if _mark_recovery_hard_reset_requested(reboot_reason) else "unavailable"
    )
    reboot_kind = _recovery_reboot_kind(reboot_reason, fs_writable=fs_writable)
    _print_log(
        "recovery",
        "action={}_reboot reason={} count={} elapsed_s={} marker={}".format(
            reboot_kind,
            reboot_reason,
            int(failure_count or 0),
            _mqtt_repeated_failure_elapsed_s(first_failure_at, now_monotonic),
            marker_status,
        ),
        start_monotonic=start_monotonic,
    )
    _perform_recovery_reboot(
        reboot_reason,
        reboot_kind,
        fs_writable=fs_writable,
        start_monotonic=start_monotonic,
        mqtt_adapter=mqtt_adapter,
        transport=transport,
        runtime_config=runtime_config,
        network_stack=network_stack,
        web_runtime=web_runtime,
        sensor_service=sensor_service,
        switch_service=switch_service,
    )
    return "reboot"


def _stage_mqtt_station_reset_before_reboot(
    runtime_config,
    network_stack,
    mqtt_adapter,
    transport,
    *,
    failure_count,
    first_failure_at,
    now_monotonic,
    start_monotonic,
):
    """Force a station/socket rebuild before repeated MQTT failures reboot."""
    previous_reason = str(getattr(transport, "last_disconnect_reason", "") or "")
    close_result = close_mqtt_client(mqtt_adapter, transport)
    if previous_reason:
        transport.mark_disconnected(reason=previous_reason)
    if close_result.errors:
        _print_log(
            "recovery",
            "mqtt close errors={}".format(",".join(close_result.errors)),
            start_monotonic=start_monotonic,
        )
    _collect_garbage()
    _print_log(
        "recovery",
        (
            "mqtt action=station_reset reason=mqtt_repeated_connect_failures "
            "count={} elapsed_s={} stage=pre_reboot"
        ).format(
            int(failure_count or 0),
            _mqtt_repeated_failure_elapsed_s(first_failure_at, now_monotonic),
        ),
        start_monotonic=start_monotonic,
    )
    network_stack = reconnect_network_stack(
        runtime_config,
        network_stack,
        max_attempts=_recovery_reconnect_attempts(True),
        retry_delay_s=_recovery_reconnect_delay_s(True),
        rebuild_socket_artifacts=True,
        reset_station=True,
        cycle_radio=True,
        force_station_reset=True,
        log_start_monotonic=start_monotonic,
    )
    mqtt_adapter = build_mqtt_client_adapter(
        runtime_config,
        socket_pool=network_stack.socket_pool,
        ssl_context=network_stack.ssl_context,
    )
    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
        _collect_garbage()
    _print_log(
        "recovery",
        (
            "mqtt action=rebuild broker={} socket_pool={} "
            "station_reset=1 reason=mqtt_repeated_connect_failures"
        ).format(
            mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
            "ready" if network_stack.socket_pool is not None else "none",
        ),
        start_monotonic=start_monotonic,
    )
    return network_stack, mqtt_adapter


def _should_log_command_result(result):
    """Return True when a command result should be emitted to the serial log."""
    if result is None:
        return False
    if getattr(result, "phase", "") == "ignored":
        return False
    return getattr(result, "command_type", "") != "switch"


def _resolve_startup_plan(runtime_config, startup_plan_override=None):
    """Build the startup plan, applying an optional test override."""
    plan = StartupPlan.from_runtime_config(runtime_config)
    if callable(startup_plan_override):
        overridden = startup_plan_override(plan)
        if overridden is not None:
            return overridden
    return plan


def _load_settings_for_startup(settings_root):
    """Load settings while skipping write-based bootstrap on ROFS."""
    fs_writable = Settings.filesystem_writable(settings_root)
    if fs_writable is not False:
        if Settings.apply_factory_profile_reset_if_requested(settings_root):
            return None, fs_writable, True
        Settings.bootstrap_factory_defaults(settings_root)
    settings = Settings.from_working_directory()
    return settings, fs_writable, False


def _ota_state_path(root):
    """Return the private OTA state path under the settings root."""
    root_text = str(root or ".")
    if root_text == "/":
        return "/_ota/state.json"
    if root_text.endswith("/"):
        return "{}_ota/state.json".format(root_text)
    return "{}/_ota/state.json".format(root_text)


def _load_startup_ota_state(settings_root):
    """Load private OTA state for startup branch selection."""
    if not settings_root:
        return None
    return load_ota_state(_ota_state_path(settings_root))


def _should_enter_ota_mode(ota_state, fs_writable):
    """Return True when startup should use the temporary OTA runtime."""
    if fs_writable is not True or ota_state is None:
        return False
    if getattr(ota_state, "mode", "") != "ota":
        return False
    return getattr(ota_state, "phase", "") in {"requested", "ready"}


def _mark_ota_applied_after_boot(ota_state, settings_root, fs_writable):
    """Mark an OTA update applied once normal startup resumes."""
    if fs_writable is not True or ota_state is None or not settings_root:
        return ota_state
    if getattr(ota_state, "mode", "") != "ota":
        return ota_state
    if getattr(ota_state, "phase", "") != "applied_pending_boot":
        return ota_state
    applied_state = FwUpdateState(
        prior_profile=getattr(ota_state, "prior_profile", "") or "",
        package_id=getattr(ota_state, "package_id", "") or "",
        phase="applied",
    )
    return save_ota_state(applied_state, _ota_state_path(settings_root))


async def main(*, startup_plan_override=None):
    """Run the current scaffold runtime."""
    start_monotonic = time.monotonic()
    settings_root = "."
    settings, fs_writable, profile_reset_requested = _load_settings_for_startup(
        settings_root
    )
    if profile_reset_requested:
        _print_log(
            "factory_reset",
            "phase=profile_reset profile=nodusweb action=hard_reboot",
            start_monotonic=start_monotonic,
        )
        _hard_reboot()
        return
    fs_mode = _filesystem_mode_label(fs_writable)
    persistence_mode = (
        "persisted"
        if fs_writable
        else "volatile"
        if fs_writable is False
        else "unknown"
    )
    writable_settings_root = (
        settings_root
        if fs_writable is True and _path_exists(Settings.SETTINGS_FILE)
        else None
    )
    runtime_config = settings.runtime_config()
    soft_reload_cleanup_requested = _consume_soft_reload_cleanup_marker()
    if soft_reload_cleanup_requested:
        _cycle_wifi_radio_for_warm_start(start_monotonic)
    ota_state = _load_startup_ota_state(writable_settings_root)
    ota_state = _mark_ota_applied_after_boot(
        ota_state,
        writable_settings_root,
        fs_writable,
    )
    if _should_enter_ota_mode(ota_state, fs_writable):
        from cpynodus_ii.ota.runtime import run_ota_mode

        await run_ota_mode(
            runtime_config,
            ota_state,
            settings_root=writable_settings_root,
            version=__version__,
            log_fn=lambda prefix, message: _print_log(
                prefix,
                message,
                start_monotonic=start_monotonic,
            ),
            log_start_monotonic=start_monotonic,
            reboot_callback=lambda: _hard_reboot(
                reason="ota:applied_pending_boot",
                start_monotonic=start_monotonic,
            )
            if _runtime_restart_kind(runtime_config) == "hard"
            else _soft_reboot(
                reason="ota:applied_pending_boot",
                start_monotonic=start_monotonic,
            ),
            idle_s=None,
        )
        return
    network_stack = build_network_stack(
        runtime_config,
        preconnect_scan=True,
        log_start_monotonic=start_monotonic,
    )
    startup_ap_fallback = _should_fallback_to_ap(runtime_config, network_stack)
    startup_ap_fallback_errors = tuple(network_stack.errors)
    if startup_ap_fallback:
        runtime_config = _enter_ap_recovery_mode(runtime_config)
        network_stack = build_network_stack(
            runtime_config, log_start_monotonic=start_monotonic
        )
    plan = _resolve_startup_plan(
        runtime_config,
        startup_plan_override=startup_plan_override,
    )
    broker_ip_persist_pending = False
    if plan.mqtt_enabled:
        runtime_config, _broker_ip_changed = _refresh_broker_ip_for_mqtt(
            runtime_config,
            network_stack,
            settings_root=writable_settings_root,
            start_monotonic=start_monotonic,
        )
        if _broker_ip_changed and writable_settings_root is not None:
            broker_ip_persist_pending = True
    if plan.mqtt_enabled:
        from cpynodus_ii.features.steady_state import (
            SteadyState,
            run_steady_state_iteration,
        )

        transport = _build_mqtt_transport(runtime_config)
        mqtt_adapter = build_mqtt_client_adapter(
            runtime_config,
            socket_pool=network_stack.socket_pool,
            ssl_context=network_stack.ssl_context,
        )
        mqtt_adapter = _condition_startup_mqtt_socket(
            runtime_config,
            network_stack,
            mqtt_adapter,
            start_monotonic=start_monotonic,
            soft_reload_cleanup_requested=soft_reload_cleanup_requested,
        )
        steady_state = SteadyState()
    else:
        transport = _InactiveMQTTTransport()
        mqtt_adapter = _InactiveMQTTAdapter(runtime_config)
        run_steady_state_iteration = None
        steady_state = None
    _sensor_init, sensor_runtime, sensor_adapter, sensor_snapshot, sensor_service = (
        _build_sensor_stack(runtime_config)
    )
    switch_init, switch_runtime, switch_adapter, switch_service = _build_switch_stack(
        runtime_config
    )
    ntp_state = NTPState()
    ntp_startup_defer_logged = False
    recovery_policy = RecoveryPolicy()
    recovery_state = RecoveryState(
        phase="ap" if network_stack.phase == "ap" else "idle",
        phase_started_at=float(start_monotonic)
        if network_stack.phase == "ap"
        else -1.0,
    )
    web_runtime = None
    next_health_at = float(start_monotonic) + 300.0
    next_periodic_gc_at = float(start_monotonic) + 60.0
    periodic_gc_count = 0
    mqtt_connect_attempt_count = 0
    last_mqtt_connect_attempt_at = float(start_monotonic)
    repeated_mqtt_connect_failure_count = 0
    repeated_mqtt_connect_failure_started_at = -1.0
    mqtt_client_memory_failure_count = 0
    mqtt_client_memory_failure_started_at = -1.0
    mqtt_preflight_connect_delay_s = max(
        0.0,
        float(MQTT_PREFLIGHT_CONNECT_DELAY_S or 0.0),
    )
    mqtt_subscribe_recovery_pending = False
    mqtt_connack_timeout_recovery_pending = False
    deferred_switch_subscription_generation = 0
    last_ntp_defer_detail = ""
    wifi_was_ready = network_link_is_ready(network_stack)
    last_wifi_failure_signature = ""
    wifi_failure_signature_count = 0
    wifi_after_ready_failure_count = 0
    sensor_not_found_failure_count = 0
    sensor_not_found_failure_started_at = -1.0
    sensor_reinit_attempt_count = 0
    last_mqtt_station_reset_at = -1.0
    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
        mqtt_client_memory_failure_count = 1
        mqtt_client_memory_failure_started_at = float(start_monotonic)
        _collect_garbage()

    connect_phase = "deferred" if plan.mqtt_enabled else "skipped"

    _print_log(
        "cPyNodus_II",
        "boot version={} profile={} ap_mode={} fs={} persistence_mode={}".format(
            __version__,
            plan.profile,
            plan.ap_mode,
            fs_mode,
            persistence_mode,
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "runtime network_phase={} network_errors={} mqtt_client={} mqtt_connect={}"
        ).format(
            network_stack.phase,
            ",".join(network_stack.errors) if network_stack.errors else "none",
            mqtt_adapter.phase,
            "{}:{}".format(connect_phase, mqtt_adapter.active_broker or "none"),
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "network",
        "socket_artifacts source={} socket_pool={} ssl_context={}".format(
            getattr(network_stack, "socket_artifact_source", "") or "unknown",
            "ready"
            if getattr(network_stack, "socket_pool", None) is not None
            else "none",
            "ready"
            if getattr(network_stack, "ssl_context", None) is not None
            else "none",
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "sensor enabled={} family={} interface={} file={} phase={} target_addr={} "
            "adapter={} service={} metrics={} errors={}"
        ).format(
            plan.sensor_enabled,
            plan.sensor_family or "none",
            plan.sensor_interface or "none",
            plan.active_sensor_file or "none",
            getattr(sensor_runtime, "phase", "inactive"),
            _sensor_target_addr(sensor_runtime),
            getattr(sensor_adapter, "phase", "inactive"),
            getattr(sensor_service, "phase", "inactive"),
            len((sensor_snapshot.metrics or {})),
            _sensor_error_text(
                sensor_runtime,
                sensor_adapter,
                sensor_service,
                sensor_snapshot,
            ),
        ),
        start_monotonic=start_monotonic,
    )
    _print_log(
        "cPyNodus_II",
        (
            "switch enabled={} channels={} phase={} adapter={} "
            "service={} mqtt={} web={} ntp={}"
        ).format(
            plan.switch_enabled,
            int(getattr(switch_init, "channel_count", 0) or 0),
            getattr(switch_runtime, "phase", "inactive"),
            getattr(switch_adapter, "phase", "inactive"),
            getattr(switch_service, "phase", "inactive"),
            plan.mqtt_enabled,
            plan.web_enabled,
            plan.ntp_enabled,
        ),
        start_monotonic=start_monotonic,
    )
    _collect_garbage()
    _print_log(
        "cPyNodus_II",
        "network ssid={} ipv4={} hostname={}".format(
            network_stack.ssid or "none",
            network_stack.ip_address or "none",
            network_stack.hostname or "none",
        ),
        start_monotonic=start_monotonic,
    )

    def _request_runtime_soft_reboot(reason="web:restart", requested_kind="soft"):
        restart_kind = _runtime_restart_kind(runtime_config, requested_kind)
        if restart_kind == "hard":
            _print_log(
                "runtime",
                "action=hard_reboot reason={} requested={} profile={}".format(
                    str(reason or "soft_reboot"),
                    str(requested_kind or "soft"),
                    runtime_config.active_profile,
                ),
                start_monotonic=start_monotonic,
            )
            _hard_reboot(
                reason=reason,
                start_monotonic=start_monotonic,
            )
            return
        _prepare_soft_recovery_reboot(
            mqtt_adapter=mqtt_adapter,
            transport=transport,
            runtime_config=runtime_config,
            network_stack=network_stack,
            web_runtime=web_runtime,
            sensor_service=sensor_service,
            switch_service=switch_service,
            start_monotonic=start_monotonic,
        )
        _mark_soft_reload_prepared()
        _soft_reboot(
            reason=reason,
            start_monotonic=start_monotonic,
            settle_s=SOFT_REBOOT_SETTLE_S,
        )

    if plan.web_enabled:
        from cpynodus_ii.features.web_runtime import WebRuntimeController

        def _web_event_log(message):
            _print_log(
                "web",
                str(message or ""),
                start_monotonic=start_monotonic,
            )

        web_runtime = WebRuntimeController(
            runtime_config,
            network_stack,
            version=__version__,
            sensor_service=sensor_service,
            switch_service=switch_service,
            settings_root=writable_settings_root,
            reboot_callbacks={
                "soft": _request_runtime_soft_reboot,
                "hard": _hard_reboot,
            },
            event_logger=_web_event_log,
        ).start()
        _print_log(
            "web",
            "phase={} routes={} port={} errors={}".format(
                web_runtime.phase,
                ",".join(web_runtime.route_paths)
                if web_runtime.route_paths
                else "none",
                runtime_config.network.http_port,
                ",".join(web_runtime.errors) if web_runtime.errors else "none",
            ),
            start_monotonic=start_monotonic,
        )
        _collect_garbage()
    if startup_ap_fallback:
        _print_log(
            "recovery",
            "startup action=ap_fallback reason={}".format(
                _startup_ap_fallback_reason(startup_ap_fallback_errors),
            ),
            start_monotonic=start_monotonic,
        )
    _print_log(
        "cPyNodus_II",
        "switch_channels ids={} labels={}".format(
            ",".join(
                channel.channel_id or "none"
                for channel in runtime_config.switch.channels
            )
            or "none",
            ",".join(
                channel.label or channel.key or "none"
                for channel in runtime_config.switch.channels
            )
            or "none",
        ),
        start_monotonic=start_monotonic,
    )

    loop_error = None
    try:
        while True:
            now_monotonic = time.monotonic()
            network_stack = refresh_network_stack(network_stack)
            if network_link_is_ready(network_stack):
                wifi_was_ready = True
                last_wifi_failure_signature = ""
                wifi_failure_signature_count = 0
                wifi_after_ready_failure_count = 0
            if web_runtime is not None:
                web_runtime.update_context(
                    runtime_config=runtime_config,
                    network_stack=network_stack,
                    sensor_service=sensor_service,
                    switch_service=switch_service,
                    version=__version__,
                )
                web_runtime.poll()
                runtime_config = web_runtime.runtime_config
            previous_phase = recovery_state.phase
            recovery_decision = advance_recovery_state(
                recovery_state,
                now_monotonic=now_monotonic,
                policy=recovery_policy,
                ap_mode=(network_stack.phase == "ap"),
                mqtt_enabled=bool(plan.mqtt_enabled),
                wifi_link_ready=network_link_is_ready(network_stack),
                transport_connected=transport.connected,
            )
            recovery_state = recovery_decision.state
            if recovery_state.phase != previous_phase:
                if recovery_state.phase == "wifi" and transport.connected:
                    transport.mark_disconnected(reason="wifi_link_lost")
                phase_detail = (
                    "previous={} phase={} wifi_ready={} mqtt_connected={}".format(
                        previous_phase,
                        recovery_state.phase,
                        network_link_is_ready(network_stack),
                        transport.connected,
                    )
                )
                _print_log(
                    "recovery",
                    phase_detail,
                    start_monotonic=start_monotonic,
                )
                _log_recovery_event(
                    "phase_change",
                    phase_detail,
                    fs_writable=fs_writable,
                    device_id=_runtime_device_id(runtime_config),
                )
                if not transport.connected and transport.last_disconnect_reason:
                    _print_log(
                        "recovery",
                        "mqtt disconnected reason={}".format(
                            transport.last_disconnect_reason
                        ),
                        start_monotonic=start_monotonic,
                    )
            if plan.mqtt_enabled and (
                recovery_state.phase != "idle" or not transport.connected
            ):
                network_stack = _stop_mqtt_mdns(
                    network_stack,
                    reason="mqtt_not_idle",
                    start_monotonic=start_monotonic,
                )
            if (
                plan.mqtt_enabled
                and network_link_is_ready(network_stack)
                and not transport.connected
                and _should_reboot_long_mqtt_recovery(
                    recovery_state,
                    now_monotonic,
                )
            ):
                reboot_reason = "mqtt_recovery_timeout"
                reboot_kind = _recovery_reboot_kind(
                    reboot_reason,
                    fs_writable=fs_writable,
                )
                _print_log(
                    "recovery",
                    "action={}_reboot reason={} elapsed_s={:.0f}".format(
                        reboot_kind,
                        reboot_reason,
                        _mqtt_recovery_elapsed_s(recovery_state, now_monotonic),
                    ),
                    start_monotonic=start_monotonic,
                )
                _perform_recovery_reboot(
                    reboot_reason,
                    reboot_kind,
                    fs_writable=fs_writable,
                    start_monotonic=start_monotonic,
                    mqtt_adapter=mqtt_adapter,
                    transport=transport,
                    runtime_config=runtime_config,
                    network_stack=network_stack,
                    web_runtime=web_runtime,
                    sensor_service=sensor_service,
                    switch_service=switch_service,
                )
            if recovery_decision.request_soft_reboot:
                reboot_kind = _recovery_reboot_kind(
                    recovery_decision.reboot_reason,
                    fs_writable=fs_writable,
                )
                _print_log(
                    "recovery",
                    "action={}_reboot reason={}".format(
                        reboot_kind, recovery_decision.reboot_reason
                    ),
                    start_monotonic=start_monotonic,
                )
                _perform_recovery_reboot(
                    recovery_decision.reboot_reason,
                    reboot_kind,
                    fs_writable=fs_writable,
                    start_monotonic=start_monotonic,
                    mqtt_adapter=mqtt_adapter,
                    transport=transport,
                    runtime_config=runtime_config,
                    network_stack=network_stack,
                    web_runtime=web_runtime,
                    sensor_service=sensor_service,
                    switch_service=switch_service,
                )
            if recovery_decision.attempt_wifi_reconnect:
                wifi_recovery_elapsed_s = _wifi_recovery_elapsed_s(
                    recovery_state,
                    now_monotonic,
                )
                fast_station_reset = _should_fast_reset_wifi_station(
                    last_wifi_failure_signature,
                    wifi_after_ready_failure_count,
                )
                pre_ready_station_reset = _should_reset_wifi_station_before_ready(
                    last_wifi_failure_signature,
                    wifi_failure_signature_count,
                    wifi_was_ready,
                )
                reset_station = recovery_state.phase == "wifi" and (
                    fast_station_reset
                    or pre_ready_station_reset
                    or wifi_recovery_elapsed_s
                    >= float(recovery_policy.wifi_backoff_after_s)
                )
                cycle_radio = bool(reset_station and recovery_state.phase == "wifi")
                if reset_station:
                    reset_count = (
                        wifi_after_ready_failure_count
                        if fast_station_reset
                        else wifi_failure_signature_count
                    )
                    _print_log(
                        "recovery",
                        (
                            "wifi action=station_reset reason={} count={} "
                            "radio_cycle={}"
                        ).format(
                            _wifi_station_reset_reason(
                                fast_station_reset,
                                pre_ready_station_reset,
                            ),
                            reset_count,
                            1 if cycle_radio else 0,
                        ),
                        start_monotonic=start_monotonic,
                    )
                reconnect_result = reconnect_network_stack(
                    runtime_config,
                    network_stack,
                    max_attempts=_recovery_reconnect_attempts(reset_station),
                    retry_delay_s=_recovery_reconnect_delay_s(reset_station),
                    rebuild_socket_artifacts=reset_station,
                    reset_station=reset_station,
                    cycle_radio=cycle_radio,
                    log_start_monotonic=start_monotonic,
                )
                network_stack = reconnect_result
                if network_link_is_ready(network_stack):
                    wifi_was_ready = True
                    last_wifi_failure_signature = ""
                    wifi_failure_signature_count = 0
                    wifi_after_ready_failure_count = 0
                    wifi_detail = "phase=recovered ipv4={}".format(
                        network_stack.ip_address or "none"
                    )
                    _print_log(
                        "recovery",
                        "wifi {}".format(wifi_detail),
                        start_monotonic=start_monotonic,
                    )
                    _log_recovery_event(
                        "wifi_recovered",
                        wifi_detail,
                        fs_writable=fs_writable,
                        device_id=_runtime_device_id(runtime_config),
                    )
                    if plan.mqtt_enabled:
                        runtime_config, broker_ip_changed = (
                            _refresh_broker_ip_for_mqtt(
                                runtime_config,
                                network_stack,
                                settings_root=writable_settings_root,
                                start_monotonic=start_monotonic,
                            )
                        )
                        if broker_ip_changed and writable_settings_root is not None:
                            broker_ip_persist_pending = True
                        if broker_ip_changed:
                            close_result = close_mqtt_client(mqtt_adapter, transport)
                            if close_result.errors:
                                _print_log(
                                    "recovery",
                                    "mqtt close errors={}".format(
                                        ",".join(close_result.errors)
                                    ),
                                    start_monotonic=start_monotonic,
                                )
                            mqtt_adapter = build_mqtt_client_adapter(
                                runtime_config,
                                socket_pool=network_stack.socket_pool,
                                ssl_context=network_stack.ssl_context,
                            )
                            if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                                _collect_garbage()
                else:
                    signature = network_error_signature(
                        network_stack,
                        had_ready_link=wifi_was_ready,
                    )
                    if signature != last_wifi_failure_signature:
                        last_wifi_failure_signature = signature
                        wifi_failure_signature_count = 1
                    else:
                        wifi_failure_signature_count += 1
                    if _is_after_ready_wifi_failure(signature):
                        wifi_after_ready_failure_count += 1
                    else:
                        wifi_after_ready_failure_count = 0
                    if _should_log_wifi_failure_signature(
                        signature,
                        wifi_failure_signature_count,
                        reset_station,
                    ):
                        _print_log(
                            "recovery",
                            "wifi signature={} count={} errors={}".format(
                                signature or "none",
                                wifi_failure_signature_count,
                                ",".join(network_stack.errors)
                                if network_stack.errors
                                else "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if _should_fast_reboot_wifi_after_ready(
                        signature,
                        wifi_after_ready_failure_count,
                        wifi_recovery_elapsed_s,
                    ):
                        reboot_reason = "wifi_after_ready_failure"
                        reboot_kind = _recovery_reboot_kind(
                            reboot_reason,
                            fs_writable=fs_writable,
                        )
                        _print_log(
                            "recovery",
                            (
                                "action={}_reboot reason={} signature={} "
                                "count={} elapsed_s={:.0f}"
                            ).format(
                                reboot_kind,
                                reboot_reason,
                                signature or "none",
                                wifi_after_ready_failure_count,
                                wifi_recovery_elapsed_s,
                            ),
                            start_monotonic=start_monotonic,
                        )
                        _perform_recovery_reboot(
                            reboot_reason,
                            reboot_kind,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            web_runtime=web_runtime,
                            sensor_service=sensor_service,
                            switch_service=switch_service,
                        )
            if recovery_decision.attempt_mqtt_rebuild and network_link_is_ready(
                network_stack
            ):
                if (
                    not mqtt_connack_timeout_recovery_pending
                    and _should_verify_mqtt_before_rebuild(
                        transport,
                        recovery_state,
                        now_monotonic,
                    )
                ):
                    _print_log(
                        "recovery",
                        (
                            "mqtt action=verify_before_rebuild last_success_s={:.1f}"
                        ).format(
                            max(
                                0.0,
                                float(now_monotonic)
                                - float(getattr(transport, "last_success_at", -1.0)),
                            )
                        ),
                        start_monotonic=start_monotonic,
                    )
                else:
                    mqtt_disconnect_reason = getattr(
                        transport,
                        "last_disconnect_reason",
                        "",
                    )
                    connack_timeout_station_reset = bool(
                        mqtt_connack_timeout_recovery_pending
                    )
                    if (
                        not connack_timeout_station_reset
                        and not _should_rebuild_mqtt_adapter_for_recovery(
                            mqtt_adapter,
                            mqtt_disconnect_reason,
                        )
                        and not _should_reset_mqtt_station_for_plain_connect_failure(
                            recovery_state,
                            now_monotonic,
                            mqtt_disconnect_reason,
                            last_mqtt_station_reset_at,
                        )
                    ):
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=hold_rebuild reason=broker_connect "
                                "elapsed_s={:.0f}"
                            ).format(
                                _mqtt_recovery_elapsed_s(
                                    recovery_state,
                                    now_monotonic,
                                )
                            ),
                            start_monotonic=start_monotonic,
                        )
                    else:
                        plain_station_reset = (
                            _should_reset_mqtt_station_for_plain_connect_failure(
                                recovery_state,
                                now_monotonic,
                                mqtt_disconnect_reason,
                                last_mqtt_station_reset_at,
                            )
                        )
                        mqtt_station_reset = bool(
                            connack_timeout_station_reset
                            or plain_station_reset
                            or _mqtt_disconnect_reason_requires_rebuild(
                                mqtt_disconnect_reason
                            )
                        )
                        mqtt_socket_refresh = False
                        if mqtt_station_reset:
                            station_reset_reason = (
                                _station_reset_reason_from_mqtt_flags(
                                    connack_timeout_station_reset,
                                    plain_station_reset,
                                    mqtt_disconnect_reason,
                                )
                            )
                            station_verify = _verify_station_reset_needed_for_mqtt(
                                runtime_config,
                                network_stack,
                                mqtt_adapter,
                                reason=station_reset_reason,
                                start_monotonic=start_monotonic,
                            )
                            if not _should_keep_mqtt_station_reset_after_verify(
                                mqtt_disconnect_reason,
                                station_verify,
                            ):
                                mqtt_station_reset = False
                                mqtt_socket_refresh = bool(station_verify.station_ready)
                                if connack_timeout_station_reset:
                                    mqtt_connack_timeout_recovery_pending = False
                                    last_mqtt_connect_attempt_at = -1.0
                                connack_timeout_station_reset = False
                                plain_station_reset = False
                                _print_log(
                                    "recovery",
                                    (
                                        "mqtt action=skip_station_reset "
                                        "reason={} status={}"
                                    ).format(
                                        station_reset_reason,
                                        station_verify.status or "unknown",
                                    ),
                                    start_monotonic=start_monotonic,
                                )
                            elif (
                                not station_verify.reset_needed
                                and _mqtt_disconnect_reason_indicates_socket_poison(
                                    mqtt_disconnect_reason
                                )
                            ):
                                _print_log(
                                    "recovery",
                                    (
                                        "mqtt action=force_station_reset "
                                        "reason={} status={} signal=socket_poison"
                                    ).format(
                                        station_reset_reason,
                                        station_verify.status or "unknown",
                                    ),
                                    start_monotonic=start_monotonic,
                                )
                        close_result = close_mqtt_client(mqtt_adapter, transport)
                        if mqtt_disconnect_reason:
                            transport.mark_disconnected(reason=mqtt_disconnect_reason)
                        if close_result.errors:
                            _print_log(
                                "recovery",
                                "mqtt close errors={}".format(
                                    ",".join(close_result.errors)
                                ),
                                start_monotonic=start_monotonic,
                            )
                        if mqtt_client_memory_failure_count > 0:
                            _collect_garbage()
                        if connack_timeout_station_reset:
                            last_mqtt_station_reset_at = float(now_monotonic)
                            _print_log(
                                "recovery",
                                (
                                    "mqtt action=station_reset "
                                    "reason=connack_timeout elapsed_s={:.0f}"
                                ).format(
                                    _mqtt_recovery_elapsed_s(
                                        recovery_state,
                                        now_monotonic,
                                    )
                                ),
                                start_monotonic=start_monotonic,
                            )
                        elif plain_station_reset:
                            last_mqtt_station_reset_at = float(now_monotonic)
                            _print_log(
                                "recovery",
                                (
                                    "mqtt action=station_reset "
                                    "reason=plain_connect_failure elapsed_s={:.0f}"
                                ).format(
                                    _mqtt_recovery_elapsed_s(
                                        recovery_state,
                                        now_monotonic,
                                    )
                                ),
                                start_monotonic=start_monotonic,
                            )
                        elif mqtt_station_reset:
                            last_mqtt_station_reset_at = float(now_monotonic)
                            _print_log(
                                "recovery",
                                (
                                    "mqtt action=station_reset "
                                    "reason={} elapsed_s={:.0f}"
                                ).format(
                                    station_reset_reason,
                                    _mqtt_recovery_elapsed_s(
                                        recovery_state,
                                        now_monotonic,
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                        if (
                            mqtt_station_reset
                            or getattr(network_stack, "socket_pool", None) is None
                        ):
                            network_stack = reconnect_network_stack(
                                runtime_config,
                                network_stack,
                                max_attempts=_recovery_reconnect_attempts(
                                    mqtt_station_reset
                                ),
                                retry_delay_s=_recovery_reconnect_delay_s(
                                    mqtt_station_reset
                                ),
                                rebuild_socket_artifacts=mqtt_station_reset,
                                reset_station=mqtt_station_reset,
                                cycle_radio=mqtt_station_reset,
                                log_start_monotonic=start_monotonic,
                            )
                            if plain_station_reset or connack_timeout_station_reset:
                                last_mqtt_connect_attempt_at = -1.0
                        elif mqtt_socket_refresh:
                            network_stack = refresh_network_socket_artifacts(
                                network_stack
                            )
                        runtime_config, broker_ip_changed = (
                            _refresh_broker_ip_for_mqtt(
                                runtime_config,
                                network_stack,
                                settings_root=writable_settings_root,
                                start_monotonic=start_monotonic,
                            )
                        )
                        if broker_ip_changed and writable_settings_root is not None:
                            broker_ip_persist_pending = True
                        mqtt_adapter = build_mqtt_client_adapter(
                            runtime_config,
                            socket_pool=network_stack.socket_pool,
                            ssl_context=network_stack.ssl_context,
                        )
                        if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                            _collect_garbage()
                        (
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                        ) = _update_mqtt_memory_failure_window(
                            mqtt_adapter.errors,
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        )
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=rebuild broker={} socket_pool={} "
                                "station_reset={}"
                            ).format(
                                mqtt_adapter.active_broker
                                or mqtt_adapter.broker
                                or "none",
                                "ready"
                                if network_stack.socket_pool is not None
                                else "none",
                                1 if mqtt_station_reset else 0,
                            ),
                            start_monotonic=start_monotonic,
                        )
                        if connack_timeout_station_reset:
                            mqtt_connack_timeout_recovery_pending = False
                            last_mqtt_connect_attempt_at = -1.0
                            _print_log(
                                "recovery",
                                (
                                    "mqtt warmup phase=skipped "
                                    "reason=connack_timeout_station_reset"
                                ),
                                start_monotonic=start_monotonic,
                            )
                            if _mqtt_repeated_failure_budget_exhausted(
                                repeated_mqtt_connect_failure_started_at,
                                time.monotonic(),
                            ):
                                reboot_reason = "mqtt_repeated_connect_failures"
                                if _recovery_hard_reset_marker_should_suppress(
                                    reboot_reason,
                                    repeated_mqtt_connect_failure_started_at,
                                    time.monotonic(),
                                ):
                                    _print_log(
                                        "recovery",
                                        (
                                            "action=hard_reboot_deferred "
                                            "reason={} marker=nvm count={} "
                                            "elapsed_s={}"
                                        ).format(
                                            reboot_reason,
                                            repeated_mqtt_connect_failure_count,
                                            _mqtt_repeated_failure_elapsed_s(
                                                repeated_mqtt_connect_failure_started_at,
                                                time.monotonic(),
                                            ),
                                        ),
                                        start_monotonic=start_monotonic,
                                    )
                                    repeated_mqtt_connect_failure_count = 0
                                    repeated_mqtt_connect_failure_started_at = -1.0
                                    await asyncio.sleep(0.05)
                                    continue
                                marker_status = (
                                    "set"
                                    if _mark_recovery_hard_reset_requested(
                                        reboot_reason
                                    )
                                    else "unavailable"
                                )
                                reboot_kind = _recovery_reboot_kind(
                                    reboot_reason,
                                    fs_writable=fs_writable,
                                )
                                _print_log(
                                    "recovery",
                                    (
                                        "action={}_reboot reason={} count={} "
                                        "elapsed_s={} marker={}"
                                    ).format(
                                        reboot_kind,
                                        reboot_reason,
                                        repeated_mqtt_connect_failure_count,
                                        _mqtt_repeated_failure_elapsed_s(
                                            repeated_mqtt_connect_failure_started_at,
                                            time.monotonic(),
                                        ),
                                        marker_status,
                                    ),
                                    start_monotonic=start_monotonic,
                                )
                                _perform_recovery_reboot(
                                    reboot_reason,
                                    reboot_kind,
                                    fs_writable=fs_writable,
                                    start_monotonic=start_monotonic,
                                    mqtt_adapter=mqtt_adapter,
                                    transport=transport,
                                    runtime_config=runtime_config,
                                    network_stack=network_stack,
                                    web_runtime=web_runtime,
                                    sensor_service=sensor_service,
                                    switch_service=switch_service,
                                )
                            mqtt_adapter = build_mqtt_client_adapter(
                                runtime_config,
                                socket_pool=network_stack.socket_pool,
                                ssl_context=network_stack.ssl_context,
                            )
                            if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                                _collect_garbage()
                        if _should_reboot_mqtt_memory_failures(
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        ):
                            reboot_reason = "mqtt_memory_allocation_failures"
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} elapsed_s={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    mqtt_client_memory_failure_count,
                                    int(
                                        max(
                                            0.0,
                                            float(now_monotonic)
                                            - float(
                                                mqtt_client_memory_failure_started_at
                                            ),
                                        )
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                runtime_config=runtime_config,
                                network_stack=network_stack,
                                web_runtime=web_runtime,
                                sensor_service=sensor_service,
                                switch_service=switch_service,
                            )
            ntp_allowed = _ntp_allowed_for_startup(
                mqtt_enabled=plan.mqtt_enabled,
                transport=transport,
                ntp_state=ntp_state,
            )
            if not ntp_allowed and plan.ntp_enabled and plan.mqtt_enabled:
                ntp_defer_detail = _transport_queue_summary(transport)
                if (
                    not ntp_startup_defer_logged
                    or ntp_defer_detail != last_ntp_defer_detail
                ):
                    ntp_startup_defer_logged = True
                    last_ntp_defer_detail = ntp_defer_detail
                    _print_log(
                        "ntp",
                        "phase=deferred reason=mqtt_startup_pending {}".format(
                            ntp_defer_detail
                        ),
                        start_monotonic=start_monotonic,
                    )
            if plan.ntp_enabled and ntp_allowed:
                last_ntp_defer_detail = ""
                ntp_result = maybe_sync_ntp(
                    runtime_config,
                    network_stack,
                    state=ntp_state,
                    now_monotonic=now_monotonic,
                )
                ntp_state = ntp_result.state
                if ntp_result.phase != "skipped":
                    _print_log(
                        "ntp",
                        "phase={} server={} rtc={} errors={}".format(
                            ntp_result.phase,
                            ntp_state.server or DEFAULT_NTP_SERVER,
                            ntp_state.datetime_text or "unknown",
                            ",".join(ntp_result.errors)
                            if ntp_result.errors
                            else "none",
                        ),
                        start_monotonic=start_monotonic,
                    )
                    if ntp_result.phase == "cooldown":
                        _print_log(
                            "ntp",
                            (
                                "marker=cooldown_entered server={} retry_after_s={}"
                            ).format(
                                ntp_state.server or DEFAULT_NTP_SERVER,
                                int(
                                    max(
                                        float(ntp_state.cooldown_until)
                                        - float(now_monotonic),
                                        0.0,
                                    )
                                ),
                            ),
                            start_monotonic=start_monotonic,
                        )
                    elif ntp_result.phase == "disabled":
                        _print_log(
                            "ntp",
                            (
                                "marker=disabled server={} "
                                "reason=attempt_limit_exhausted"
                            ).format(ntp_state.server or DEFAULT_NTP_SERVER),
                            start_monotonic=start_monotonic,
                        )
                    if ntp_result.phase == "synced":
                        _collect_garbage()
            if (
                plan.mqtt_enabled
                and not transport.connected
                and recovery_decision.allow_mqtt_connect
                and (float(now_monotonic) - float(last_mqtt_connect_attempt_at))
                >= _mqtt_connect_retry_interval_s(
                    recovery_state,
                    now_monotonic,
                    getattr(transport, "last_disconnect_reason", ""),
                )
            ):
                if mqtt_adapter.phase != "ready":
                    runtime_config, broker_ip_changed = (
                        _refresh_broker_ip_for_mqtt(
                            runtime_config,
                            network_stack,
                            settings_root=writable_settings_root,
                            start_monotonic=start_monotonic,
                        )
                    )
                    if broker_ip_changed and writable_settings_root is not None:
                        broker_ip_persist_pending = True
                    if mqtt_client_memory_failure_count > 0:
                        _collect_garbage()
                    mqtt_adapter = build_mqtt_client_adapter(
                        runtime_config,
                        socket_pool=network_stack.socket_pool,
                        ssl_context=network_stack.ssl_context,
                    )
                    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                        _collect_garbage()
                    (
                        mqtt_client_memory_failure_count,
                        mqtt_client_memory_failure_started_at,
                    ) = _update_mqtt_memory_failure_window(
                        mqtt_adapter.errors,
                        mqtt_client_memory_failure_count,
                        mqtt_client_memory_failure_started_at,
                        now_monotonic,
                    )
                    last_mqtt_connect_attempt_at = float(now_monotonic)
                    if mqtt_adapter.phase != "ready":
                        _print_log(
                            "mqtt",
                            "connect phase=deferred broker={} errors={}".format(
                                mqtt_adapter.broker or "none",
                                ",".join(mqtt_adapter.errors)
                                if mqtt_adapter.errors
                                else "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        if _should_reboot_mqtt_memory_failures(
                            mqtt_client_memory_failure_count,
                            mqtt_client_memory_failure_started_at,
                            now_monotonic,
                        ):
                            reboot_reason = "mqtt_memory_allocation_failures"
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} elapsed_s={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    mqtt_client_memory_failure_count,
                                    int(
                                        max(
                                            0.0,
                                            float(now_monotonic)
                                            - float(
                                                mqtt_client_memory_failure_started_at
                                            ),
                                        )
                                    ),
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                runtime_config=runtime_config,
                                network_stack=network_stack,
                                web_runtime=web_runtime,
                                sensor_service=sensor_service,
                                switch_service=switch_service,
                            )
                        await asyncio.sleep(0.05)
                        continue
                mqtt_connect_attempt_count += 1
                _print_log(
                    "mqtt",
                    (
                        "connect_context attempt={} recovery_phase={} "
                        "recovery_elapsed_s={} wifi_ready={} network_phase={} "
                        "ipv4={} socket_source={} {} {} last_reason={}"
                    ).format(
                        mqtt_connect_attempt_count,
                        recovery_state.phase or "none",
                        int(_mqtt_recovery_elapsed_s(recovery_state, now_monotonic)),
                        1 if network_link_is_ready(network_stack) else 0,
                        network_stack.phase or "none",
                        network_stack.ip_address or "none",
                        getattr(network_stack, "socket_artifact_source", "")
                        or "unknown",
                        _memory_summary(),
                        _transport_queue_summary(transport),
                        getattr(transport, "last_disconnect_reason", "") or "none",
                    ),
                    start_monotonic=start_monotonic,
                )
                budget_checked_at = time.monotonic()
                if repeated_mqtt_connect_failure_count > 0:
                    if _should_stage_mqtt_station_reset_before_reboot(
                        repeated_mqtt_connect_failure_count,
                        repeated_mqtt_connect_failure_started_at,
                        budget_checked_at,
                        last_mqtt_station_reset_at,
                    ):
                        network_stack, mqtt_adapter = (
                            _stage_mqtt_station_reset_before_reboot(
                                runtime_config,
                                network_stack,
                                mqtt_adapter,
                                transport,
                                failure_count=repeated_mqtt_connect_failure_count,
                                first_failure_at=(
                                    repeated_mqtt_connect_failure_started_at
                                ),
                                now_monotonic=budget_checked_at,
                                start_monotonic=start_monotonic,
                            )
                        )
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                        mqtt_connack_timeout_recovery_pending = False
                        last_mqtt_station_reset_at = float(budget_checked_at)
                        last_mqtt_connect_attempt_at = -1.0
                        await asyncio.sleep(0.05)
                        continue
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=budget_checked_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                        await asyncio.sleep(0.05)
                        continue
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                transport.mark_connect_requested()
                connack_timeout_failure_recorded = False
                (
                    tcp_preflight_error,
                    tcp_preflight_target,
                    tcp_preflight_finished_at,
                    connect_probe_error,
                    connect_probe_target,
                    connect_probe_client_id,
                    connect_probe_code,
                    connect_probe_finished_at,
                ) = _mqtt_preconnect_probe(
                    mqtt_adapter,
                    network_stack,
                    start_monotonic=start_monotonic,
                    connect_delay_s=mqtt_preflight_connect_delay_s,
                )
                connack_timeout_candidate = _mqtt_connect_probe_is_connack_timeout(
                    tcp_preflight_error=tcp_preflight_error,
                    connect_probe_error=connect_probe_error,
                    connect_probe_code=connect_probe_code,
                )
                socket_progress_candidate = _mqtt_preconnect_has_socket_progress(
                    tcp_preflight_error=tcp_preflight_error,
                    connect_probe_error=connect_probe_error,
                )
                post_reset_preconnect_failed = False
                if connack_timeout_candidate:
                    if repeated_mqtt_connect_failure_count <= 0:
                        repeated_mqtt_connect_failure_started_at = float(
                            connect_probe_finished_at
                        )
                        repeated_mqtt_connect_failure_count = 1
                        connack_timeout_failure_recorded = True
                if (
                    connack_timeout_candidate
                    and repeated_mqtt_connect_failure_count <= 1
                    and connack_timeout_failure_recorded
                ):
                    transport.mark_disconnected(
                        reason=tcp_preflight_error
                        or connect_probe_error
                        or "mqtt_preconnect_connack_timeout"
                    )
                    close_result = close_mqtt_client(mqtt_adapter, transport)
                    if close_result.errors:
                        _print_log(
                            "recovery",
                            "mqtt close errors={}".format(
                                ",".join(close_result.errors)
                            ),
                            start_monotonic=start_monotonic,
                        )
                    _collect_garbage()
                    station_verify = _verify_station_reset_needed_for_mqtt(
                        runtime_config,
                        network_stack,
                        mqtt_adapter,
                        reason="connack_timeout_preconnect",
                        start_monotonic=start_monotonic,
                    )
                    preconnect_station_reset = bool(station_verify.reset_needed)
                    if preconnect_station_reset:
                        last_mqtt_station_reset_at = float(time.monotonic())
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=station_reset "
                                "reason=connack_timeout_preconnect count={} "
                                "elapsed_s={} tcp_error={} probe_error={}"
                            ).format(
                                repeated_mqtt_connect_failure_count,
                                _elapsed_since_s(
                                    repeated_mqtt_connect_failure_started_at,
                                    connect_probe_finished_at,
                                ),
                                tcp_preflight_error or "none",
                                connect_probe_error or "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        network_stack = reconnect_network_stack(
                            runtime_config,
                            network_stack,
                            max_attempts=_recovery_reconnect_attempts(True),
                            retry_delay_s=_recovery_reconnect_delay_s(True),
                            rebuild_socket_artifacts=True,
                            reset_station=True,
                            cycle_radio=False,
                            log_start_monotonic=start_monotonic,
                        )
                        last_mqtt_connect_attempt_at = -1.0
                    else:
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=skip_station_reset "
                                "reason=connack_timeout_preconnect status={}"
                            ).format(station_verify.status or "unknown"),
                            start_monotonic=start_monotonic,
                        )
                        network_stack = refresh_network_socket_artifacts(network_stack)
                    runtime_config, broker_ip_changed = (
                        _refresh_broker_ip_for_mqtt(
                            runtime_config,
                            network_stack,
                            settings_root=writable_settings_root,
                            start_monotonic=start_monotonic,
                        )
                    )
                    if broker_ip_changed and writable_settings_root is not None:
                        broker_ip_persist_pending = True
                    mqtt_adapter = build_mqtt_client_adapter(
                        runtime_config,
                        socket_pool=network_stack.socket_pool,
                        ssl_context=network_stack.ssl_context,
                    )
                    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                        _collect_garbage()
                    _print_log(
                        "recovery",
                        (
                            "mqtt action=rebuild broker={} socket_pool={} "
                            "station_reset={} reason=connack_timeout_preconnect"
                        ).format(
                            mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
                            "ready"
                            if network_stack.socket_pool is not None
                            else "none",
                            1 if preconnect_station_reset else 0,
                        ),
                        start_monotonic=start_monotonic,
                    )
                    budget_checked_at = time.monotonic()
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=budget_checked_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                        await asyncio.sleep(0.05)
                        continue
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                    if not network_link_is_ready(network_stack):
                        _print_log(
                            "mqtt",
                            "post_reset_preconnect phase=skipped reason=wifi_not_ready",
                            start_monotonic=start_monotonic,
                        )
                        tcp_preflight_error = "network_not_ready"
                        connect_probe_error = "mqtt_connect_probe_skipped:network"
                        connect_probe_finished_at = time.monotonic()
                        connack_timeout_candidate = False
                        socket_progress_candidate = False
                        post_reset_preconnect_failed = True
                    else:
                        mqtt_adapter = build_mqtt_client_adapter(
                            runtime_config,
                            socket_pool=network_stack.socket_pool,
                            ssl_context=network_stack.ssl_context,
                        )
                        if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                            _collect_garbage()
                        (
                            tcp_preflight_error,
                            tcp_preflight_target,
                            tcp_preflight_finished_at,
                            connect_probe_error,
                            connect_probe_target,
                            connect_probe_client_id,
                            connect_probe_code,
                            connect_probe_finished_at,
                        ) = _mqtt_preconnect_probe(
                            mqtt_adapter,
                            network_stack,
                            start_monotonic=start_monotonic,
                            connect_delay_s=mqtt_preflight_connect_delay_s,
                        )
                        connack_timeout_candidate = (
                            _mqtt_connect_probe_is_connack_timeout(
                                tcp_preflight_error=tcp_preflight_error,
                                connect_probe_error=connect_probe_error,
                                connect_probe_code=connect_probe_code,
                            )
                        )
                        socket_progress_candidate = (
                            _mqtt_preconnect_has_socket_progress(
                                tcp_preflight_error=tcp_preflight_error,
                                connect_probe_error=connect_probe_error,
                            )
                        )
                        post_reset_preconnect_failed = bool(
                            tcp_preflight_error or connect_probe_error
                        )
                        _print_log(
                            "mqtt",
                            (
                                "post_reset_preconnect phase=probe "
                                "probe_phase={} broker={} target={} port={} "
                                "client_id={} connack={} "
                                "raw_pattern={} errors={}"
                            ).format(
                                "connack_error"
                                if connect_probe_error
                                else "connack_ok",
                                mqtt_adapter.active_broker
                                or mqtt_adapter.broker
                                or "none",
                                connect_probe_target or "none",
                                mqtt_adapter.port,
                                connect_probe_client_id or "none",
                                connect_probe_code,
                                _mqtt_preconnect_pattern_label(
                                    connack_timeout_candidate=(
                                        connack_timeout_candidate
                                    ),
                                    socket_progress_candidate=(
                                        socket_progress_candidate
                                    ),
                                ),
                                connect_probe_error or "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if post_reset_preconnect_failed:
                        repeated_mqtt_connect_failure_count += 1
                        mqtt_connack_timeout_recovery_pending = True
                if post_reset_preconnect_failed:
                    decision_finished_at = time.monotonic()
                    transport.mark_disconnected(
                        reason=tcp_preflight_error
                        or connect_probe_error
                        or "mqtt_post_reset_preconnect_failed"
                    )
                    recovery_state = RecoveryState(
                        phase="mqtt",
                        phase_started_at=repeated_mqtt_connect_failure_started_at,
                        last_mqtt_rebuild_at=float(time.monotonic()),
                    )
                    last_mqtt_connect_attempt_at = float(time.monotonic())
                    _print_log(
                        "mqtt",
                        (
                            "connect_decision attempt={} raw_pattern={} "
                            "will_minimqtt=0 repeated_count={} "
                            "hard_reset_elapsed_s={}"
                        ).format(
                            mqtt_connect_attempt_count,
                            "post_reset_preconnect",
                            repeated_mqtt_connect_failure_count,
                            _elapsed_since_s(
                                repeated_mqtt_connect_failure_started_at,
                                decision_finished_at,
                            ),
                        ),
                        start_monotonic=start_monotonic,
                    )
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=decision_finished_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                    await asyncio.sleep(0.05)
                    continue
                if socket_progress_candidate:
                    if repeated_mqtt_connect_failure_count <= 0:
                        repeated_mqtt_connect_failure_started_at = float(
                            connect_probe_finished_at
                        )
                        repeated_mqtt_connect_failure_count = 1
                    else:
                        repeated_mqtt_connect_failure_count += 1
                    budget_checked_at = time.monotonic()
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=budget_checked_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                        await asyncio.sleep(0.05)
                        continue
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                    transport.mark_disconnected(
                        reason=tcp_preflight_error
                        or connect_probe_error
                        or "mqtt_preconnect_socket_progress"
                    )
                    close_result = close_mqtt_client(mqtt_adapter, transport)
                    if close_result.errors:
                        _print_log(
                            "recovery",
                            "mqtt close errors={}".format(
                                ",".join(close_result.errors)
                            ),
                            start_monotonic=start_monotonic,
                        )
                    _collect_garbage()
                    station_verify = _verify_station_reset_needed_for_mqtt(
                        runtime_config,
                        network_stack,
                        mqtt_adapter,
                        reason="socket_progress",
                        start_monotonic=start_monotonic,
                    )
                    socket_progress_station_reset = bool(station_verify.reset_needed)
                    socket_progress_cycle_radio = (
                        _should_cycle_mqtt_radio_for_socket_progress(
                            station_verify,
                            last_mqtt_station_reset_at,
                        )
                    )
                    if socket_progress_cycle_radio:
                        socket_progress_station_reset = True
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=force_station_reset "
                                "reason=socket_progress status={} "
                                "signal=socket_progress"
                            ).format(station_verify.status or "unknown"),
                            start_monotonic=start_monotonic,
                        )
                    if socket_progress_station_reset:
                        last_mqtt_station_reset_at = float(time.monotonic())
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=station_reset reason=socket_progress "
                                "count={} elapsed_s={} cycle_radio={} "
                                "tcp_error={} probe_error={}"
                            ).format(
                                repeated_mqtt_connect_failure_count,
                                _elapsed_since_s(
                                    repeated_mqtt_connect_failure_started_at,
                                    connect_probe_finished_at,
                                ),
                                1 if socket_progress_cycle_radio else 0,
                                tcp_preflight_error or "none",
                                connect_probe_error or "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        network_stack = reconnect_network_stack(
                            runtime_config,
                            network_stack,
                            max_attempts=_recovery_reconnect_attempts(True),
                            retry_delay_s=_recovery_reconnect_delay_s(True),
                            rebuild_socket_artifacts=True,
                            reset_station=True,
                            cycle_radio=socket_progress_cycle_radio,
                            log_start_monotonic=start_monotonic,
                        )
                    else:
                        _print_log(
                            "recovery",
                            (
                                "mqtt action=skip_station_reset "
                                "reason=socket_progress status={}"
                            ).format(station_verify.status or "unknown"),
                            start_monotonic=start_monotonic,
                        )
                        network_stack = refresh_network_socket_artifacts(network_stack)
                    runtime_config, broker_ip_changed = _refresh_broker_ip_for_mqtt(
                        runtime_config,
                        network_stack,
                        settings_root=writable_settings_root,
                        start_monotonic=start_monotonic,
                    )
                    if broker_ip_changed and writable_settings_root is not None:
                        broker_ip_persist_pending = True
                    mqtt_adapter = build_mqtt_client_adapter(
                        runtime_config,
                        socket_pool=network_stack.socket_pool,
                        ssl_context=network_stack.ssl_context,
                    )
                    if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                        _collect_garbage()
                    _print_log(
                        "recovery",
                        (
                            "mqtt action=rebuild broker={} socket_pool={} "
                            "station_reset={} reason=socket_progress"
                        ).format(
                            mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
                            "ready"
                            if network_stack.socket_pool is not None
                            else "none",
                            1 if socket_progress_station_reset else 0,
                        ),
                        start_monotonic=start_monotonic,
                    )
                    budget_checked_at = time.monotonic()
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=budget_checked_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                        await asyncio.sleep(0.05)
                        continue
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                    if not network_link_is_ready(network_stack):
                        _print_log(
                            "mqtt",
                            "post_reset_preconnect phase=skipped reason=wifi_not_ready",
                            start_monotonic=start_monotonic,
                        )
                        tcp_preflight_error = "network_not_ready"
                        connect_probe_error = "mqtt_connect_probe_skipped:network"
                        connect_probe_finished_at = time.monotonic()
                        connack_timeout_candidate = False
                        socket_progress_candidate = False
                        post_reset_preconnect_failed = True
                    else:
                        mqtt_adapter = build_mqtt_client_adapter(
                            runtime_config,
                            socket_pool=network_stack.socket_pool,
                            ssl_context=network_stack.ssl_context,
                        )
                        if _mqtt_client_init_memory_failed(mqtt_adapter.errors):
                            _collect_garbage()
                        (
                            tcp_preflight_error,
                            tcp_preflight_target,
                            tcp_preflight_finished_at,
                            connect_probe_error,
                            connect_probe_target,
                            connect_probe_client_id,
                            connect_probe_code,
                            connect_probe_finished_at,
                        ) = _mqtt_preconnect_probe(
                            mqtt_adapter,
                            network_stack,
                            start_monotonic=start_monotonic,
                            connect_delay_s=mqtt_preflight_connect_delay_s,
                        )
                        connack_timeout_candidate = (
                            _mqtt_connect_probe_is_connack_timeout(
                                tcp_preflight_error=tcp_preflight_error,
                                connect_probe_error=connect_probe_error,
                                connect_probe_code=connect_probe_code,
                            )
                        )
                        socket_progress_candidate = (
                            _mqtt_preconnect_has_socket_progress(
                                tcp_preflight_error=tcp_preflight_error,
                                connect_probe_error=connect_probe_error,
                            )
                        )
                        post_reset_preconnect_failed = bool(
                            tcp_preflight_error or connect_probe_error
                        )
                        _print_log(
                            "mqtt",
                            (
                                "post_reset_preconnect phase=probe "
                                "probe_phase={} broker={} target={} port={} "
                                "client_id={} connack={} raw_pattern={} errors={}"
                            ).format(
                                "connack_error"
                                if connect_probe_error
                                else "connack_ok",
                                mqtt_adapter.active_broker
                                or mqtt_adapter.broker
                                or "none",
                                connect_probe_target or "none",
                                mqtt_adapter.port,
                                connect_probe_client_id or "none",
                                connect_probe_code,
                                _mqtt_preconnect_pattern_label(
                                    connack_timeout_candidate=(
                                        connack_timeout_candidate
                                    ),
                                    socket_progress_candidate=(
                                        socket_progress_candidate
                                    ),
                                ),
                                connect_probe_error or "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if post_reset_preconnect_failed:
                        repeated_mqtt_connect_failure_count += 1
                        mqtt_connack_timeout_recovery_pending = True
                        decision_finished_at = time.monotonic()
                        transport.mark_disconnected(
                            reason=tcp_preflight_error
                            or connect_probe_error
                            or "mqtt_post_reset_preconnect_failed"
                        )
                        recovery_state = RecoveryState(
                            phase="mqtt",
                            phase_started_at=(repeated_mqtt_connect_failure_started_at),
                            last_mqtt_rebuild_at=float(time.monotonic()),
                        )
                        last_mqtt_connect_attempt_at = float(time.monotonic())
                        _print_log(
                            "mqtt",
                            (
                                "connect_decision attempt={} raw_pattern={} "
                                "will_minimqtt=0 repeated_count={} "
                                "hard_reset_elapsed_s={}"
                            ).format(
                                mqtt_connect_attempt_count,
                                "post_reset_preconnect",
                                repeated_mqtt_connect_failure_count,
                                _elapsed_since_s(
                                    repeated_mqtt_connect_failure_started_at,
                                    decision_finished_at,
                                ),
                            ),
                            start_monotonic=start_monotonic,
                        )
                        reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                            failure_count=repeated_mqtt_connect_failure_count,
                            first_failure_at=(repeated_mqtt_connect_failure_started_at),
                            now_monotonic=decision_finished_at,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            web_runtime=web_runtime,
                            sensor_service=sensor_service,
                            switch_service=switch_service,
                        )
                        if reboot_action == "suppressed":
                            repeated_mqtt_connect_failure_count = 0
                            repeated_mqtt_connect_failure_started_at = -1.0
                        if reboot_action != "none":
                            await asyncio.sleep(0.05)
                            continue
                        await asyncio.sleep(0.05)
                        continue
                    if socket_progress_candidate:
                        decision_finished_at = time.monotonic()
                        transport.mark_disconnected(
                            reason=tcp_preflight_error
                            or connect_probe_error
                            or "mqtt_preconnect_socket_progress"
                        )
                        recovery_state = RecoveryState(
                            phase="mqtt",
                            phase_started_at=(repeated_mqtt_connect_failure_started_at),
                            last_mqtt_rebuild_at=float(time.monotonic()),
                        )
                        last_mqtt_connect_attempt_at = float(time.monotonic())
                        _print_log(
                            "mqtt",
                            (
                                "connect_decision attempt={} raw_pattern={} "
                                "will_minimqtt=0 repeated_count={} "
                                "hard_reset_elapsed_s={}"
                            ).format(
                                mqtt_connect_attempt_count,
                                "socket_progress",
                                repeated_mqtt_connect_failure_count,
                                _elapsed_since_s(
                                    repeated_mqtt_connect_failure_started_at,
                                    decision_finished_at,
                                ),
                            ),
                            start_monotonic=start_monotonic,
                        )
                        reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                            failure_count=repeated_mqtt_connect_failure_count,
                            first_failure_at=(repeated_mqtt_connect_failure_started_at),
                            now_monotonic=decision_finished_at,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            web_runtime=web_runtime,
                            sensor_service=sensor_service,
                            switch_service=switch_service,
                        )
                        if reboot_action == "suppressed":
                            repeated_mqtt_connect_failure_count = 0
                            repeated_mqtt_connect_failure_started_at = -1.0
                        if reboot_action != "none":
                            await asyncio.sleep(0.05)
                            continue
                        await asyncio.sleep(0.05)
                        continue
                if connack_timeout_candidate:
                    if not connack_timeout_failure_recorded:
                        repeated_mqtt_connect_failure_count += 1
                    decision_finished_at = time.monotonic()
                    transport.mark_disconnected(
                        reason=tcp_preflight_error
                        or connect_probe_error
                        or "mqtt_preconnect_connack_timeout"
                    )
                    recovery_state = RecoveryState(
                        phase="mqtt",
                        phase_started_at=repeated_mqtt_connect_failure_started_at,
                        last_mqtt_rebuild_at=float(time.monotonic()),
                    )
                    last_mqtt_connect_attempt_at = float(time.monotonic())
                    mqtt_connack_timeout_recovery_pending = True
                    _print_log(
                        "mqtt",
                        (
                            "connect_decision attempt={} raw_pattern={} "
                            "will_minimqtt=0 repeated_count={} "
                            "hard_reset_elapsed_s={}"
                        ).format(
                            mqtt_connect_attempt_count,
                            "connack_timeout",
                            repeated_mqtt_connect_failure_count,
                            _elapsed_since_s(
                                repeated_mqtt_connect_failure_started_at,
                                decision_finished_at,
                            ),
                        ),
                        start_monotonic=start_monotonic,
                    )
                    reboot_action = _maybe_reboot_for_mqtt_failure_budget(
                        failure_count=repeated_mqtt_connect_failure_count,
                        first_failure_at=repeated_mqtt_connect_failure_started_at,
                        now_monotonic=decision_finished_at,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        web_runtime=web_runtime,
                        sensor_service=sensor_service,
                        switch_service=switch_service,
                    )
                    if reboot_action == "suppressed":
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
                    if reboot_action != "none":
                        await asyncio.sleep(0.05)
                        continue
                    await asyncio.sleep(0.05)
                    continue
                mqtt_connect_mode = (
                    "raw" if raw_mqtt_connect_enabled(mqtt_adapter) else "minimqtt"
                )
                _print_log(
                    "mqtt",
                    (
                        "connect_decision attempt={} raw_pattern={} "
                        "will_minimqtt={} connect_mode={} repeated_count={} "
                        "hard_reset_elapsed_s={}"
                    ).format(
                        mqtt_connect_attempt_count,
                        _mqtt_preconnect_pattern_label(
                            connack_timeout_candidate=connack_timeout_candidate,
                            socket_progress_candidate=socket_progress_candidate,
                        ),
                        0 if mqtt_connect_mode == "raw" else 1,
                        mqtt_connect_mode,
                        repeated_mqtt_connect_failure_count,
                        _elapsed_since_s(
                            repeated_mqtt_connect_failure_started_at,
                            connect_probe_finished_at,
                        ),
                    ),
                    start_monotonic=start_monotonic,
                )
                connect_started_at = time.monotonic()
                connect_result = connect_mqtt_client(
                    mqtt_adapter,
                    transport,
                    preflight=_should_preflight_broker(mqtt_adapter),
                )
                connect_finished_at = time.monotonic()
                mqtt_adapter = connect_result.adapter
                connect_phase = connect_result.phase
                last_mqtt_connect_attempt_at = float(connect_finished_at)
                if connect_phase == "connected":
                    mqtt_preflight_connect_delay_s = (
                        _reset_mqtt_preflight_connect_delay(
                            mqtt_preflight_connect_delay_s,
                            start_monotonic=start_monotonic,
                        )
                    )
                    repeated_mqtt_connect_failure_count = 0
                    repeated_mqtt_connect_failure_started_at = -1.0
                    mqtt_connack_timeout_recovery_pending = False
                    mqtt_client_memory_failure_count = 0
                    mqtt_client_memory_failure_started_at = -1.0
                    last_mqtt_station_reset_at = -1.0
                    if _recovery_hard_reset_marker_is_set(
                        "mqtt_repeated_connect_failures"
                    ):
                        _clear_recovery_hard_reset_marker(
                            "mqtt_repeated_connect_failures"
                        )
                        _print_log(
                            "recovery",
                            "action=clear_hard_reset_marker reason=mqtt_connected",
                            start_monotonic=start_monotonic,
                        )
                    _print_log(
                        "mqtt",
                        "connect phase={} broker={} elapsed_s={:.1f}".format(
                            connect_phase,
                            mqtt_adapter.active_broker or "none",
                            connect_finished_at - connect_started_at,
                        ),
                        start_monotonic=start_monotonic,
                    )
                    sync_before = _transport_queue_summary(transport)
                    sync_result = sync_transport_to_client(
                        mqtt_adapter,
                        transport,
                        require_clean_poll_before_subscribe=True,
                    )
                    mqtt_adapter = sync_result.adapter
                    if sync_result.phase == "error":
                        _print_log(
                            "mqtt",
                            _mqtt_sync_summary(
                                sync_result,
                                transport,
                                source="post_connect",
                                before_queues=sync_before,
                            ),
                            start_monotonic=start_monotonic,
                        )
                    elif _mqtt_sync_should_log_success(sync_result):
                        _print_log(
                            "mqtt",
                            _mqtt_sync_summary(
                                sync_result,
                                transport,
                                source="post_connect",
                                before_queues=sync_before,
                            ),
                            start_monotonic=start_monotonic,
                        )
                    elif sync_result.phase == "deferred":
                        _print_log(
                            "mqtt",
                            _mqtt_sync_summary(
                                sync_result,
                                transport,
                                source="post_connect",
                                before_queues=sync_before,
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if (
                        mqtt_subscribe_recovery_pending
                        and _startup_subscription_recovery_drained(
                            sync_result,
                            transport,
                        )
                    ):
                        steady_state = replace(
                            steady_state,
                            connection_generation=transport.connection_generation,
                        )
                        mqtt_subscribe_recovery_pending = False
                        if runtime_config.switch.present:
                            deferred_switch_subscription_generation = (
                                transport.connection_generation
                            )
                        _print_log(
                            "mqtt",
                            (
                                "recovery phase=subscription_recovered "
                                "action=suppress_duplicate_startup_queue gen={} "
                                "{} retained_replay=disabled"
                            ).format(
                                transport.connection_generation,
                                _transport_queue_summary(transport),
                            ),
                            start_monotonic=start_monotonic,
                        )
                    if _is_mqtt_subscription_failure(sync_result):
                        mqtt_subscribe_recovery_pending = True
                        deferred_switch_subscription_generation = 0
                        mqtt_adapter = _recover_mqtt_subscription_failure(
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            sync_result=sync_result,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                        )
                        recovery_state = RecoveryState(
                            phase="mqtt",
                            phase_started_at=now_monotonic,
                            last_mqtt_rebuild_at=now_monotonic,
                        )
                        last_mqtt_connect_attempt_at = float(now_monotonic) - 5.0
                        _collect_garbage()
                        await asyncio.sleep(0.05)
                        continue
                    if broker_ip_persist_pending and sync_result.phase != "error":
                        _persist_broker_ip_after_mqtt_connect(
                            runtime_config,
                            settings_root=writable_settings_root,
                            start_monotonic=start_monotonic,
                        )
                        broker_ip_persist_pending = False
                    _collect_garbage()
                    _log_memory_checkpoint(start_monotonic, "post_mqtt_connect")
                elif connect_phase == "error":
                    if not tcp_preflight_error and not connect_probe_error:
                        mqtt_preflight_connect_delay_s = (
                            _increase_mqtt_preflight_connect_delay(
                                mqtt_preflight_connect_delay_s,
                                start_monotonic=start_monotonic,
                            )
                        )
                    _print_log(
                        "mqtt",
                        "connect phase=error broker={} errors={}".format(
                            mqtt_adapter.active_broker or mqtt_adapter.broker or "none",
                            ",".join(connect_result.errors)
                            if connect_result.errors
                            else "none",
                        ),
                        start_monotonic=start_monotonic,
                    )
                    connack_timeout_pattern = (
                        _mqtt_connect_attempt_is_connack_timeout_pattern(
                            tcp_preflight_error=tcp_preflight_error,
                            connect_probe_error=connect_probe_error,
                            connect_probe_code=connect_probe_code,
                            connect_errors=connect_result.errors,
                        )
                    )
                    _print_log(
                        "mqtt",
                        (
                            "connect_cleanup attempt={} phase=error "
                            "elapsed_s={:.1f} raw_pattern={} classified={} "
                            "client_sock={} wifi_ready={} ipv4={} {}"
                        ).format(
                            mqtt_connect_attempt_count,
                            connect_finished_at - connect_started_at,
                            "connack_timeout" if connack_timeout_candidate else "none",
                            1 if connack_timeout_pattern else 0,
                            1 if _mqtt_client_socket_present(mqtt_adapter) else 0,
                            1 if network_link_is_ready(network_stack) else 0,
                            network_stack.ip_address or "none",
                            _transport_queue_summary(transport),
                        ),
                        start_monotonic=start_monotonic,
                    )
                    if connack_timeout_pattern:
                        mqtt_connack_timeout_recovery_pending = True
                        if repeated_mqtt_connect_failure_count <= 0:
                            repeated_mqtt_connect_failure_started_at = float(
                                connect_finished_at
                            )
                            repeated_mqtt_connect_failure_count = 1
                        elif not connack_timeout_failure_recorded:
                            repeated_mqtt_connect_failure_count += 1
                        if _mqtt_repeated_failure_budget_exhausted(
                            repeated_mqtt_connect_failure_started_at,
                            connect_finished_at,
                        ) or _should_fast_reboot_mqtt_connect_failures(
                            repeated_mqtt_connect_failure_count,
                            repeated_mqtt_connect_failure_started_at,
                            connect_finished_at,
                        ):
                            if _should_stage_mqtt_station_reset_before_reboot(
                                repeated_mqtt_connect_failure_count,
                                repeated_mqtt_connect_failure_started_at,
                                connect_finished_at,
                                last_mqtt_station_reset_at,
                            ):
                                network_stack, mqtt_adapter = (
                                    _stage_mqtt_station_reset_before_reboot(
                                        runtime_config,
                                        network_stack,
                                        mqtt_adapter,
                                        transport,
                                        failure_count=(
                                            repeated_mqtt_connect_failure_count
                                        ),
                                        first_failure_at=(
                                            repeated_mqtt_connect_failure_started_at
                                        ),
                                        now_monotonic=connect_finished_at,
                                        start_monotonic=start_monotonic,
                                    )
                                )
                                repeated_mqtt_connect_failure_count = 0
                                repeated_mqtt_connect_failure_started_at = -1.0
                                mqtt_connack_timeout_recovery_pending = False
                                last_mqtt_station_reset_at = float(
                                    connect_finished_at
                                )
                                last_mqtt_connect_attempt_at = -1.0
                                await asyncio.sleep(0.05)
                                continue
                            reboot_reason = "mqtt_repeated_connect_failures"
                            if _recovery_hard_reset_marker_should_suppress(
                                reboot_reason,
                                repeated_mqtt_connect_failure_started_at,
                                connect_finished_at,
                            ):
                                _print_log(
                                    "recovery",
                                    (
                                        "action=hard_reboot_deferred reason={} "
                                        "marker=nvm count={} elapsed_s={}"
                                    ).format(
                                        reboot_reason,
                                        repeated_mqtt_connect_failure_count,
                                        _mqtt_repeated_failure_elapsed_s(
                                            repeated_mqtt_connect_failure_started_at,
                                            connect_finished_at,
                                        ),
                                    ),
                                    start_monotonic=start_monotonic,
                                )
                                repeated_mqtt_connect_failure_count = 0
                                repeated_mqtt_connect_failure_started_at = -1.0
                                await asyncio.sleep(0.05)
                                continue
                            marker_status = (
                                "set"
                                if _mark_recovery_hard_reset_requested(reboot_reason)
                                else "unavailable"
                            )
                            reboot_kind = _recovery_reboot_kind(
                                reboot_reason,
                                fs_writable=fs_writable,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "action={}_reboot reason={} count={} "
                                    "elapsed_s={} marker={}"
                                ).format(
                                    reboot_kind,
                                    reboot_reason,
                                    repeated_mqtt_connect_failure_count,
                                    _mqtt_repeated_failure_elapsed_s(
                                        repeated_mqtt_connect_failure_started_at,
                                        connect_finished_at,
                                    ),
                                    marker_status,
                                ),
                                start_monotonic=start_monotonic,
                            )
                            _perform_recovery_reboot(
                                reboot_reason,
                                reboot_kind,
                                fs_writable=fs_writable,
                                start_monotonic=start_monotonic,
                                mqtt_adapter=mqtt_adapter,
                                transport=transport,
                                runtime_config=runtime_config,
                                network_stack=network_stack,
                                web_runtime=web_runtime,
                                sensor_service=sensor_service,
                                switch_service=switch_service,
                            )
                    else:
                        repeated_mqtt_connect_failure_count = 0
                        repeated_mqtt_connect_failure_started_at = -1.0
            if plan.mqtt_enabled:
                defer_switch_subscriptions = (
                    _defer_switch_subscriptions_until_after_startup_publish(
                        runtime_config
                    )
                )
                iteration = run_steady_state_iteration(
                    transport,
                    runtime_config,
                    switch_service,
                    sensor_service,
                    state=steady_state,
                    version=__version__,
                    now_monotonic=now_monotonic,
                    active_broker=mqtt_adapter.active_broker,
                    ip_address=network_stack.ip_address or "",
                    settings_root=writable_settings_root,
                    subscribe_switch_topics=not defer_switch_subscriptions,
                    publish_switch_startup=False,
                    include_switch_meta_channels=False,
                )
            else:
                iteration = _inactive_steady_state_iteration(
                    steady_state,
                    runtime_config,
                )
            steady_state = iteration.state
            runtime_config = iteration.runtime_config
            sensor_issue = _sensor_issue_text(iteration.errors)
            sensor_poll_observed = (
                bool(sensor_issue) or iteration.sensor_publish_phase == "published"
            )
            if sensor_issue:
                _print_log(
                    "sensor",
                    "poll phase={} published={} errors={}".format(
                        iteration.sensor_publish_phase,
                        iteration.sensor_published_count,
                        sensor_issue,
                    ),
                    start_monotonic=start_monotonic,
                )
            if sensor_poll_observed:
                if _sensor_errors_indicate_not_found(iteration.errors):
                    (
                        sensor_not_found_failure_count,
                        sensor_not_found_failure_started_at,
                    ) = _update_sensor_not_found_window(
                        iteration.errors,
                        sensor_not_found_failure_count,
                        sensor_not_found_failure_started_at,
                        now_monotonic,
                    )
                    if _should_reboot_sensor_not_found(
                        sensor_not_found_failure_count,
                        sensor_not_found_failure_started_at,
                        now_monotonic,
                    ):
                        elapsed_s = int(
                            max(
                                0.0,
                                float(now_monotonic)
                                - float(sensor_not_found_failure_started_at),
                            )
                        )
                        reboot_reason = "sensor_not_found"
                        if (
                            sensor_reinit_attempt_count
                            < SENSOR_NOT_FOUND_REINIT_MAX_ATTEMPTS
                        ):
                            sensor_reinit_attempt_count += 1
                            (
                                sensor_adapter,
                                sensor_service,
                                sensor_snapshot,
                            ) = _restart_sensor_stack(
                                sensor_runtime,
                                sensor_service,
                                runtime_config,
                            )
                            steady_state = replace(
                                steady_state,
                                last_sensor_publish_at=-1.0,
                            )
                            reinit_issue = _sensor_error_text(
                                sensor_runtime,
                                sensor_adapter,
                                sensor_service,
                                sensor_snapshot,
                            )
                            _print_log(
                                "recovery",
                                (
                                    "sensor action=reinit reason={} attempt={} "
                                    "count={} elapsed_s={} adapter={} service={} "
                                    "metrics={} errors={}"
                                ).format(
                                    reboot_reason,
                                    sensor_reinit_attempt_count,
                                    sensor_not_found_failure_count,
                                    elapsed_s,
                                    sensor_adapter.phase,
                                    sensor_service.phase,
                                    len((sensor_snapshot.metrics or {})),
                                    reinit_issue or "none",
                                ),
                                start_monotonic=start_monotonic,
                            )
                            sensor_not_found_failure_count = 0
                            sensor_not_found_failure_started_at = -1.0
                            if sensor_snapshot.phase == "ready":
                                sensor_reinit_attempt_count = 0
                            await asyncio.sleep(0.05)
                            continue
                        reboot_kind = _recovery_reboot_kind(
                            reboot_reason,
                            fs_writable=fs_writable,
                        )
                        _print_log(
                            "recovery",
                            (
                                "sensor action={}_reboot reason={} "
                                "count={} elapsed_s={} errors={}"
                            ).format(
                                reboot_kind,
                                reboot_reason,
                                sensor_not_found_failure_count,
                                elapsed_s,
                                sensor_issue or "sensor_not_found",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        _perform_recovery_reboot(
                            reboot_reason,
                            reboot_kind,
                            fs_writable=fs_writable,
                            start_monotonic=start_monotonic,
                            mqtt_adapter=mqtt_adapter,
                            transport=transport,
                            runtime_config=runtime_config,
                            network_stack=network_stack,
                            web_runtime=web_runtime,
                            sensor_service=sensor_service,
                            switch_service=switch_service,
                        )
                else:
                    sensor_not_found_failure_count = 0
                    sensor_not_found_failure_started_at = -1.0
                    sensor_reinit_attempt_count = 0
            if iteration.subscribed_topics:
                if _defer_switch_subscriptions_until_after_startup_publish(
                    runtime_config
                ):
                    deferred_switch_subscription_generation = (
                        transport.connection_generation
                    )
                else:
                    deferred_switch_subscription_generation = 0
                _print_log(
                    "mqtt",
                    "subscriptions queued topics={} gen={} {}".format(
                        ",".join(iteration.subscribed_topics),
                        transport.connection_generation,
                        _transport_queue_summary(transport),
                    ),
                    start_monotonic=start_monotonic,
                )
            for result in iteration.command_results:
                if not _should_log_command_result(result):
                    continue
                _print_log(
                    "mqtt",
                    (
                        "command type={} phase={} topic={} requested={} "
                        "published={} persistence_mode={} errors={}"
                    ).format(
                        result.command_type,
                        result.phase,
                        result.topic,
                        result.requested_state or "none",
                        result.published_count,
                        result.persistence_mode or persistence_mode,
                        ",".join(result.errors) if result.errors else "none",
                    ),
                    start_monotonic=start_monotonic,
                )
            if plan.ntp_enabled and _command_results_request_ntp_resync(
                iteration.command_results
            ):
                ntp_state = _ntp_state_forced_resync(ntp_state)
                ntp_startup_defer_logged = False
                last_ntp_defer_detail = ""
                _print_log(
                    "ntp",
                    "phase=resync_requested reason=time_config_update",
                    start_monotonic=start_monotonic,
                )
            reboot_request = _command_result_reboot_request(iteration.command_results)
            if float(now_monotonic) >= float(next_health_at):
                _print_log(
                    _runtime_device_id(runtime_config),
                    (
                        "health network_phase={} recovery_phase={} ssid={} ipv4={} "
                        "dns_health={} ntp_health={} mqtt_connected={} "
                        "active_broker={} wifi_signature={}"
                    ).format(
                        network_stack.phase,
                        recovery_state.phase,
                        network_stack.ssid or "none",
                        network_stack.ip_address or "none",
                        _dns_health_text(network_stack, mqtt_adapter, ntp_state),
                        _ntp_health_text(ntp_state),
                        transport.connected,
                        mqtt_adapter.active_broker or "none",
                        _wifi_signature_text(
                            network_stack,
                            had_ready_link=wifi_was_ready,
                        ),
                    ),
                    start_monotonic=start_monotonic,
                )
                while float(next_health_at) <= float(now_monotonic):
                    next_health_at += 300.0
            if float(now_monotonic) >= float(next_periodic_gc_at):
                before = _memory_summary()
                _collect_garbage()
                after = _memory_summary()
                periodic_gc_count += 1
                if (periodic_gc_count % 5) == 0:
                    _print_log(
                        "memory",
                        "phase=periodic_gc before={} after={} {}".format(
                            before,
                            after,
                            _transport_queue_summary(transport),
                        ),
                        start_monotonic=start_monotonic,
                    )
                while float(next_periodic_gc_at) <= float(now_monotonic):
                    next_periodic_gc_at += 60.0
            if transport.connected:
                sync_before = _transport_queue_summary(transport)
                sync_result = sync_transport_to_client(
                    mqtt_adapter,
                    transport,
                    require_clean_poll_before_subscribe=True,
                )
                mqtt_adapter = sync_result.adapter
                if sync_result.phase == "error":
                    _print_log(
                        "mqtt",
                        _mqtt_sync_summary(
                            sync_result,
                            transport,
                            source="main_loop",
                            before_queues=sync_before,
                        ),
                        start_monotonic=start_monotonic,
                    )
                elif _mqtt_sync_should_log_success(sync_result):
                    _print_log(
                        "mqtt",
                        _mqtt_sync_summary(
                            sync_result,
                            transport,
                            source="main_loop",
                            before_queues=sync_before,
                        ),
                        start_monotonic=start_monotonic,
                    )
                elif sync_result.phase == "deferred":
                    _print_log(
                        "mqtt",
                        _mqtt_sync_summary(
                            sync_result,
                            transport,
                            source="main_loop",
                            before_queues=sync_before,
                        ),
                        start_monotonic=start_monotonic,
                    )
                if _is_mqtt_subscription_failure(sync_result):
                    mqtt_subscribe_recovery_pending = True
                    deferred_switch_subscription_generation = 0
                    mqtt_adapter = _recover_mqtt_subscription_failure(
                        runtime_config=runtime_config,
                        network_stack=network_stack,
                        mqtt_adapter=mqtt_adapter,
                        transport=transport,
                        sync_result=sync_result,
                        fs_writable=fs_writable,
                        start_monotonic=start_monotonic,
                    )
                    recovery_state = RecoveryState(
                        phase="mqtt",
                        phase_started_at=now_monotonic,
                        last_mqtt_rebuild_at=now_monotonic,
                    )
                    last_mqtt_connect_attempt_at = float(now_monotonic) - 5.0
                    _collect_garbage()
                    await asyncio.sleep(0.05)
                    continue
                if (
                    deferred_switch_subscription_generation
                    == transport.connection_generation
                    and not getattr(transport, "published_messages", ())
                    and not getattr(transport, "subscriptions", ())
                ):
                    switch_topics = _subscribe_switch_runtime_topics(
                        transport,
                        runtime_config,
                    )
                    deferred_switch_subscription_generation = 0
                    if switch_topics:
                        _print_log(
                            "mqtt",
                            (
                                "switch_subscriptions queued topics={} gen={} {}"
                            ).format(
                                ",".join(switch_topics),
                                transport.connection_generation,
                                _transport_queue_summary(transport),
                            ),
                            start_monotonic=start_monotonic,
                        )
                if reboot_request is not None:
                    reboot_mode = str(
                        getattr(reboot_request, "reboot_mode", "") or "soft"
                    )
                    if getattr(reboot_request, "command_type", "") == "fwupdate":
                        _print_log(
                            "ota",
                            "action={}_reboot reason=fwupdate_prepare".format(
                                _runtime_restart_kind(runtime_config, reboot_mode)
                            ),
                            start_monotonic=start_monotonic,
                        )
                        _request_runtime_soft_reboot(
                            "ota:fwupdate_prepare",
                            requested_kind=reboot_mode,
                        )
                    else:
                        _print_log(
                            "runtime",
                            (
                                "action={}_reboot reason=mqtt:restart "
                                "requested={} message_id={}"
                            ).format(
                                _runtime_restart_kind(runtime_config, reboot_mode),
                                reboot_mode,
                                getattr(reboot_request, "message_id", "") or "none",
                            ),
                            start_monotonic=start_monotonic,
                        )
                        _request_runtime_soft_reboot(
                            "mqtt:restart",
                            requested_kind=reboot_mode,
                        )
                poll_result = poll_mqtt_client(mqtt_adapter, transport)
                mqtt_adapter = poll_result.adapter
                if poll_result.phase == "error" and not _is_recoverable_mqtt_poll_error(
                    poll_result.errors
                ):
                    _print_log(
                        "mqtt",
                        "poll phase={} received={} errors={}".format(
                            poll_result.phase,
                            poll_result.received_count,
                            ",".join(poll_result.errors)
                            if poll_result.errors
                            else "none",
                        ),
                        start_monotonic=start_monotonic,
                    )
            await asyncio.sleep(0.05)
    except Exception as exc:
        loop_error = exc
        _print_log(
            "runtime",
            "fatal error={} type={}".format(str(exc), type(exc).__name__),
            start_monotonic=start_monotonic,
        )
        raise
    finally:
        shutdown_prepared_for_reload = _consume_soft_reload_prepared()
        if shutdown_prepared_for_reload:
            _print_log(
                "runtime",
                "shutdown cleanup=skipped reason=prepared_reload",
                start_monotonic=start_monotonic,
            )
        else:
            _mark_unprepared_shutdown_cleanup(
                start_monotonic,
                reason="finally",
            )
            _stop_web_runtime_for_shutdown(
                web_runtime,
                start_monotonic=start_monotonic,
            )
            if plan.mqtt_enabled and transport.connected:
                try:
                    disconnect_result = disconnect_mqtt_client(
                        mqtt_adapter,
                        transport,
                        runtime_config,
                    )
                    mqtt_adapter = disconnect_result.adapter
                    if disconnect_result.errors:
                        _print_log(
                            "mqtt",
                            (
                                "disconnect phase={} published={} subscribed={} "
                                "errors={}"
                            ).format(
                                disconnect_result.phase,
                                disconnect_result.published_count,
                                disconnect_result.subscribed_count,
                                ",".join(disconnect_result.errors),
                            ),
                            start_monotonic=start_monotonic,
                        )
                except Exception as exc:
                    _print_log(
                        "mqtt",
                        "disconnect phase=error errors={}".format(str(exc)),
                        start_monotonic=start_monotonic,
                    )
            elif plan.mqtt_enabled:
                try:
                    close_result = close_mqtt_client(mqtt_adapter, transport)
                    if close_result.errors:
                        _print_log(
                            "mqtt",
                            "close phase={} errors={}".format(
                                close_result.phase,
                                ",".join(close_result.errors),
                            ),
                            start_monotonic=start_monotonic,
                        )
                except Exception as exc:
                    _print_log(
                        "mqtt",
                        "close phase=error errors={}".format(str(exc)),
                        start_monotonic=start_monotonic,
                    )
            _stop_feature_services_for_shutdown(
                sensor_service=sensor_service,
                switch_service=switch_service,
                start_monotonic=start_monotonic,
            )
            _teardown_network_for_shutdown(
                network_stack,
                start_monotonic=start_monotonic,
                cycle_radio=False,
            )
        if loop_error is not None:
            _print_log(
                "runtime",
                "shutdown after fatal type={}".format(type(loop_error).__name__),
                start_monotonic=start_monotonic,
            )
