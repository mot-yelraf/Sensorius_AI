"""Wrap the configured MiniMQTT client in a firmware-friendly adapter.

This module centralizes connect, poll, publish, subscribe, and disconnect
behavior so the main application can reason about MQTT state through a small
and testable interface.
"""

import gc
import time
from dataclasses import dataclass

MQTT_CONNECT_SOCKET_TIMEOUT_S = 3
MQTT_POLL_SOCKET_TIMEOUT_S = 1
MQTT_SUBACK_SOCKET_TIMEOUT_S = MQTT_POLL_SOCKET_TIMEOUT_S
MQTT_PUBACK_SOCKET_TIMEOUT_S = MQTT_POLL_SOCKET_TIMEOUT_S
MQTT_CONNECT_RETRIES = 1
MQTT_SLOW_OPERATION_MS = 10000
MQTT_RAW_SEND_CHUNK_BYTES = 256
# Nonzero only for temporary diagnostics; normal runtime stays QoS0.
MQTT_RAW_VERIFY_PUBLISH_BYTES = 0
MQTT_OPTIONAL_CLIENT_KWARGS = (
    "socket_timeout",
    "connect_retries",
    "client_id",
)


@dataclass(frozen=True)
class MQTTClientAdapter:
    """Describe the current MQTT client binding state."""

    phase: str
    driver_kind: str
    broker: str
    port: int
    broker_targets: tuple = ()
    active_broker: str = ""
    resolved_broker_ip: str = ""
    client: object | None = None
    client_class: object | None = None
    client_kwargs: dict | None = None
    socket_compat_enabled: bool = False
    flexible_callback_enabled: bool = False
    published_index: int = 0
    subscription_index: int = 0
    errors: tuple = ()


@dataclass(frozen=True)
class MQTTClientSyncResult:
    """Describe one adapter operation."""

    phase: str
    adapter: MQTTClientAdapter
    published_count: int = 0
    subscribed_count: int = 0
    received_count: int = 0
    errors: tuple = ()
    operation: str = ""
    topic: str = ""
    retain: bool = False
    payload_bytes: int = -1
    pending_count: int = 0
    elapsed_ms: int = -1
    diagnostic: str = ""


def build_mqtt_client_adapter(
    runtime_config,
    *,
    socket_pool=None,
    ssl_context=None,
    modules=None,
):
    """Build a clean-room MQTT client adapter when runtime MQTT is enabled."""
    if not runtime_config.mqtt_enabled:
        return MQTTClientAdapter(
            phase="inactive",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=(),
        )

    if socket_pool is None:
        return MQTTClientAdapter(
            phase="unavailable",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=("socket_pool_unavailable",),
        )

    client_class = _resolve_mqtt_class(modules)
    if client_class is None:
        return MQTTClientAdapter(
            phase="unavailable",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=runtime_config.mqtt.connection_targets,
            errors=("mqtt_client_module_unavailable",),
        )

    broker_targets = runtime_config.mqtt.connection_targets
    if not broker_targets:
        return MQTTClientAdapter(
            phase="unavailable",
            driver_kind="none",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=(),
            errors=("mqtt_broker_ip_unavailable",),
        )
    socket_compat_enabled = _should_enable_socket_compat(modules)
    flexible_callback_enabled = _should_enable_flexible_callback(modules)
    if _should_wrap_socket_pool_before_connect(modules):
        mqtt_socket_pool = _wrap_minimqtt_socket_pool(socket_pool)
    else:
        mqtt_socket_pool = socket_pool
    kwargs = {
        "socket_pool": mqtt_socket_pool,
        "port": runtime_config.mqtt.port,
        "keep_alive": 60,
        "socket_timeout": MQTT_CONNECT_SOCKET_TIMEOUT_S,
        "connect_retries": MQTT_CONNECT_RETRIES,
    }
    client_id = _runtime_mqtt_client_id(runtime_config)
    if client_id:
        kwargs["client_id"] = client_id
    if runtime_config.mqtt.use_tls or runtime_config.mqtt.port == 8883:
        kwargs["ssl_context"] = ssl_context
    if runtime_config.mqtt.username:
        kwargs["username"] = runtime_config.mqtt.username
    if runtime_config.mqtt.password:
        kwargs["password"] = runtime_config.mqtt.password

    try:
        client_kwargs = dict(kwargs)
        client = _instantiate_client(client_class, client_kwargs, broker_targets[0])
    except Exception as exc:
        return MQTTClientAdapter(
            phase="error",
            driver_kind="adafruit_minimqtt",
            broker=runtime_config.mqtt.preferred_host,
            port=runtime_config.mqtt.port,
            broker_targets=broker_targets,
            errors=("mqtt_client_init_failed", str(exc)),
        )

    return MQTTClientAdapter(
        phase="ready",
        driver_kind="adafruit_minimqtt",
        broker=runtime_config.mqtt.preferred_host,
        port=runtime_config.mqtt.port,
        broker_targets=broker_targets,
        active_broker=broker_targets[0],
        client=client,
        client_class=client_class,
        client_kwargs=dict(client_kwargs),
        socket_compat_enabled=socket_compat_enabled,
        flexible_callback_enabled=flexible_callback_enabled,
        errors=(),
    )


def connect_mqtt_client(adapter, transport, *, preflight=True):
    """Connect the bound client and wire inbound message delivery."""
    if adapter.phase != "ready" or adapter.client is None:
        return MQTTClientSyncResult(
            phase=adapter.phase,
            adapter=adapter,
            errors=adapter.errors,
        )

    targets = adapter.broker_targets or ()
    if not targets:
        error = "mqtt_connect_failed:broker_ip_unavailable"
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=(error,),
        )

    broker = targets[0]
    error, resolved_ip = _resolve_broker_target(adapter, broker)
    if preflight and error:
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=(error,),
        )

    raw_result = _connect_mqtt_client_raw(adapter, transport, broker, resolved_ip)
    if raw_result is not None:
        return raw_result

    _bind_on_message(
        adapter.client,
        transport,
        flexible=adapter.flexible_callback_enabled,
    )
    try:
        adapter.client.connect()
        _set_minimqtt_runtime_socket_timeout(
            adapter.client,
            MQTT_POLL_SOCKET_TIMEOUT_S,
        )
        if adapter.socket_compat_enabled:
            _ensure_minimqtt_socket_compat(adapter.client)
        transport.mark_connected()
        return _mqtt_connect_success_result(adapter, resolved_ip)
    except Exception as exc:
        error = "mqtt_connect_failed:{}:{}".format(broker, exc)
        close_errors = _force_close_mqtt_client_socket(adapter.client)[1]
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=(error,) + close_errors,
        )


def _mqtt_connect_success_result(adapter, resolved_ip):
    return MQTTClientSyncResult(
        phase="connected",
        adapter=_mqtt_connected_adapter(adapter, resolved_ip),
        errors=(),
    )


def _connect_mqtt_client_raw(adapter, transport, broker, resolved_ip):
    """Connect with a raw MQTT CONNECT packet when MiniMQTT handoff is unsafe."""
    if not raw_mqtt_connect_enabled(adapter):
        return None
    socket_pool = _mqtt_adapter_socket_pool(adapter)
    socket_factory = getattr(socket_pool, "socket", None)
    if not callable(socket_factory):
        return None
    connect_target = str(resolved_ip or broker or "").strip()
    if not connect_target:
        return None

    sock = None
    client_id = _mqtt_probe_client_id(adapter)
    try:
        sock = socket_factory()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(MQTT_CONNECT_SOCKET_TIMEOUT_S)
        sock.connect((connect_target, int(getattr(adapter, "port", 1883) or 1883)))
        _send_mqtt_packet(sock, _mqtt_connect_packet(adapter, client_id))
        _read_mqtt_connack(sock, connect_target)
        _bind_on_message(
            adapter.client,
            transport,
            flexible=adapter.flexible_callback_enabled,
        )
        _adopt_raw_mqtt_socket(adapter.client, sock)
        sock = None
        _set_minimqtt_runtime_socket_timeout(
            adapter.client,
            MQTT_POLL_SOCKET_TIMEOUT_S,
        )
        transport.mark_connected()
        return _mqtt_connect_success_result(adapter, resolved_ip or connect_target)
    except Exception as exc:
        close_errors = ()
        if sock is not None:
            close_errors = _close_raw_mqtt_socket(adapter.client, sock)
        error = "mqtt_connect_failed:{}:raw:{}:{}".format(
            broker,
            type(exc).__name__,
            exc,
        )
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=(error,) + close_errors,
        )


def _read_mqtt_connack(sock, target):
    header = _recv_mqtt_byte(sock)
    if header != 0x20:
        raise OSError("mqtt_connack_unexpected:{}:{:02x}".format(target, header))
    remaining = _recv_mqtt_remaining_length(sock)
    payload = _recv_socket_exact(sock, remaining)
    if len(payload) < 2:
        raise OSError("mqtt_connack_short:{}:{}".format(target, len(payload)))
    return_code = int(payload[1])
    if return_code:
        raise OSError("mqtt_connack_code:{}:{}".format(target, return_code))
    return return_code


def _adopt_raw_mqtt_socket(client, sock):
    try:
        setattr(client, "_sock", sock)
    except Exception:
        pass
    try:
        setattr(client, "_is_connected", True)
    except Exception:
        pass
    try:
        setattr(client, "connected", True)
    except Exception:
        pass
    try:
        setattr(client, "_backwards_compatible_sock", not _socket_can_recv_into(sock))
    except Exception:
        pass
    try:
        setattr(client, "_last_msg_sent_timestamp", int(time.monotonic() * 1000))
    except Exception:
        pass


def _socket_can_recv_into(sock):
    return callable(getattr(sock, "recv_into", None))


def raw_mqtt_connect_enabled(adapter):
    """Return True when startup should use the raw MQTT connect path."""
    if bool(getattr(adapter, "socket_compat_enabled", False)) or _mqtt_adapter_uses_tls(
        adapter
    ):
        return False
    socket_pool = _mqtt_adapter_socket_pool(adapter)
    return callable(getattr(socket_pool, "socket", None))


def _mqtt_connected_adapter(adapter, resolved_ip):
    return MQTTClientAdapter(
        phase=adapter.phase,
        driver_kind=adapter.driver_kind,
        broker=adapter.broker,
        port=adapter.port,
        broker_targets=adapter.broker_targets,
        active_broker=adapter.active_broker,
        resolved_broker_ip=resolved_ip,
        client=adapter.client,
        client_class=adapter.client_class,
        client_kwargs=adapter.client_kwargs,
        socket_compat_enabled=adapter.socket_compat_enabled,
        flexible_callback_enabled=adapter.flexible_callback_enabled,
        published_index=adapter.published_index,
        subscription_index=adapter.subscription_index,
        errors=adapter.errors,
    )


def sync_transport_to_client(
    adapter,
    transport,
    *,
    slow_operation_ms=None,
    require_clean_poll_before_subscribe=False,
):
    """Flush newly queued subscriptions and publishes to the bound client."""
    if adapter.phase != "ready" or adapter.client is None or not transport.connected:
        return MQTTClientSyncResult(
            phase="skipped",
            adapter=adapter,
            errors=adapter.errors
            if adapter.phase != "ready"
            else ("transport_not_connected",),
        )

    slow_threshold = _slow_operation_threshold_ms(slow_operation_ms)
    pending_publish_count = max(
        0,
        len(transport.published_messages) - int(adapter.published_index or 0),
    )
    pending_subscription_count = len(
        transport.subscriptions[adapter.subscription_index :]
    )
    publish_index = _next_publish_index(adapter, transport)
    if pending_subscription_count:
        publish_index = _next_startup_priority_publish_index(adapter, transport)

    if publish_index is None:
        if (
            require_clean_poll_before_subscribe
            and pending_subscription_count
            and not _subscription_clean_poll_ready(transport)
        ):
            return MQTTClientSyncResult(
                phase="deferred",
                adapter=adapter,
                published_count=0,
                subscribed_count=0,
                errors=(),
                operation="subscribe",
                pending_count=pending_subscription_count,
            )
        return _sync_subscriptions_to_client(
            adapter,
            transport,
            slow_threshold=slow_threshold,
            pending_subscription_count=pending_subscription_count,
        )

    message = transport.published_messages[publish_index]
    message_topic = message.topic
    message_retain = bool(message.retain)
    startup_meta_publish = _is_retained_startup_meta_publish(message)
    collect_after_publish = _should_collect_after_startup_meta_publish(message)
    payload = _serialize_payload(message.payload)
    payload_bytes = _payload_size(payload)
    operation_started = time.monotonic()
    try:
        publish_result = _publish_mqtt(
            adapter.client,
            message.topic,
            payload,
            message.retain,
            startup_meta_publish=startup_meta_publish,
        )
    except Exception as exc:
        elapsed_ms = _elapsed_ms(operation_started)
        transport.mark_disconnected(
            reason="mqtt_publish_failed:{}:{}".format(message_topic, exc)
        )
        if not message_retain:
            _drop_published_message(transport, publish_index)
        return MQTTClientSyncResult(
            phase="error",
            adapter=_mqtt_adapter_with_queue_indexes(adapter, 0, 0),
            published_count=0,
            subscribed_count=0,
            errors=(
                "mqtt_publish_failed:{}:bytes={}:{}".format(
                    message_topic,
                    payload_bytes,
                    exc,
                ),
            ),
            operation="publish",
            topic=message_topic,
            retain=message_retain,
            payload_bytes=payload_bytes,
            pending_count=pending_publish_count,
            elapsed_ms=elapsed_ms,
        )
    elapsed_ms = _elapsed_ms(operation_started)
    _record_raw_publish_ack_diagnostic(
        transport,
        adapter.client,
        message_topic,
        publish_result,
    )
    transport.record_publish_success(
        message_topic,
        payload_bytes=payload_bytes,
        retain=message_retain,
        socket_state=_socket_state(getattr(adapter.client, "_sock", None)),
        client_connected=_client_connected_state(adapter.client),
        backcompat=1 if adapter.socket_compat_enabled else 0,
    )
    transport.mark_success()
    _drop_published_message(transport, publish_index)
    message = None
    payload = None
    if collect_after_publish:
        _collect_garbage()
    if _operation_is_slow(elapsed_ms, slow_threshold):
        transport.mark_disconnected(
            reason="mqtt_publish_slow:{}:elapsed_ms={}".format(
                message_topic,
                elapsed_ms,
            )
        )
        return MQTTClientSyncResult(
            phase="error",
            adapter=_mqtt_adapter_with_queue_indexes(adapter, 0, 0),
            published_count=1,
            subscribed_count=0,
            errors=(
                "mqtt_publish_slow:{}:bytes={}:elapsed_ms={}".format(
                    message_topic,
                    payload_bytes,
                    elapsed_ms,
                ),
            ),
            operation="publish",
            topic=message_topic,
            retain=message_retain,
            payload_bytes=payload_bytes,
            pending_count=pending_publish_count,
            elapsed_ms=elapsed_ms,
        )
    return MQTTClientSyncResult(
        phase="synced",
        adapter=_mqtt_adapter_with_queue_indexes(adapter, 0, 0),
        published_count=1,
        subscribed_count=0,
        errors=(),
        operation="publish",
        topic=message_topic,
        retain=message_retain,
        payload_bytes=payload_bytes,
        pending_count=pending_publish_count,
        elapsed_ms=elapsed_ms,
    )


def _publish_mqtt(client, topic, payload, retain, *, startup_meta_publish=False):
    sock = getattr(client, "_sock", None)
    if sock is None or not callable(getattr(sock, "send", None)):
        client.publish(topic, payload, retain=retain)
        return _mqtt_publish_result(0, 0, -1, -1, 0)
    packet_size = _mqtt_publish_packet_size(topic, payload, qos=0)
    if (
        startup_meta_publish
        and MQTT_RAW_VERIFY_PUBLISH_BYTES > 0
        and packet_size > MQTT_RAW_VERIFY_PUBLISH_BYTES
        and _socket_can_recv(sock)
    ):
        return _publish_mqtt_qos1_verified(client, sock, topic, payload, retain)
    packet = _mqtt_qos0_publish_packet(topic, payload, retain)
    _send_mqtt_packet(sock, packet, chunked=startup_meta_publish)
    return _mqtt_publish_result(0, 0, len(packet), -1, 0)


def _mqtt_qos0_publish_packet(topic, payload, retain):
    return _mqtt_publish_packet(topic, payload, retain, qos=0, packet_id=0)


def _mqtt_qos1_publish_packet(topic, payload, retain, packet_id):
    return _mqtt_publish_packet(topic, payload, retain, qos=1, packet_id=packet_id)


def _mqtt_publish_packet(topic, payload, retain, *, qos=0, packet_id=0):
    topic_bytes = _mqtt_bytes(topic)
    payload_bytes = _mqtt_bytes(payload)
    qos = int(qos or 0)
    packet_id_bytes = 2 if qos else 0
    remaining = 2 + len(topic_bytes) + packet_id_bytes + len(payload_bytes)
    remaining_bytes = _mqtt_remaining_length(remaining)
    header = 0x30 | ((qos & 0x03) << 1) | (0x01 if retain else 0)
    packet = bytearray(1 + len(remaining_bytes) + remaining)
    packet[0] = header
    offset = 1
    for byte in remaining_bytes:
        packet[offset] = byte
        offset += 1
    packet[offset] = (len(topic_bytes) >> 8) & 0xFF
    packet[offset + 1] = len(topic_bytes) & 0xFF
    offset += 2
    packet[offset : offset + len(topic_bytes)] = topic_bytes
    offset += len(topic_bytes)
    if qos:
        packet[offset] = (packet_id >> 8) & 0xFF
        packet[offset + 1] = packet_id & 0xFF
        offset += 2
    packet[offset : offset + len(payload_bytes)] = payload_bytes
    return packet


def _mqtt_publish_packet_size(topic, payload, *, qos=0):
    topic_bytes = _mqtt_bytes(topic)
    payload_bytes = _mqtt_bytes(payload)
    packet_id_bytes = 2 if int(qos or 0) else 0
    remaining = 2 + len(topic_bytes) + packet_id_bytes + len(payload_bytes)
    return 1 + len(_mqtt_remaining_length(remaining)) + remaining


def _publish_mqtt_qos1_verified(client, sock, topic, payload, retain):
    packet_id = _next_mqtt_packet_id(client)
    packet = _mqtt_qos1_publish_packet(topic, payload, retain, packet_id)
    runtime_timeout = _poll_timeout_for_client(client)
    puback_timeout_set = False
    puback_elapsed_ms = -1
    operation_started = time.monotonic()
    puback_started = operation_started
    try:
        _send_mqtt_packet(sock, packet, chunked=True)
        if float(runtime_timeout) != float(MQTT_PUBACK_SOCKET_TIMEOUT_S):
            puback_timeout_set = _set_mqtt_socket_timeout(
                sock,
                MQTT_PUBACK_SOCKET_TIMEOUT_S,
            )
        puback_started = time.monotonic()
        _read_mqtt_puback(sock, packet_id)
        puback_elapsed_ms = _elapsed_ms(puback_started)
    except Exception as exc:
        puback_elapsed_ms = _elapsed_ms(puback_started)
        raise OSError(
            _mqtt_raw_publish_failure_detail(
                topic,
                packet_id,
                packet,
                sock,
                _elapsed_ms(operation_started),
                puback_elapsed_ms,
                runtime_timeout,
                MQTT_PUBACK_SOCKET_TIMEOUT_S,
                puback_timeout_set,
                exc,
            )
        )
    finally:
        if puback_timeout_set:
            _set_mqtt_socket_timeout(sock, runtime_timeout)
    return _mqtt_publish_result(
        1,
        packet_id,
        len(packet),
        puback_elapsed_ms,
        puback_timeout_set,
    )


def _mqtt_publish_result(qos, packet_id, packet_bytes, puback_ms, timeout_set):
    return (
        int(qos or 0),
        int(packet_id or 0),
        int(packet_bytes or 0),
        int(puback_ms or 0),
        1 if timeout_set else 0,
    )


def _mqtt_ack_result(raw_ack, packet_id, packet_bytes, ack_ms, timeout_set):
    return (
        1 if raw_ack else 0,
        int(packet_id or 0),
        int(packet_bytes or 0),
        int(ack_ms or 0),
        1 if timeout_set else 0,
    )


def _record_raw_publish_ack_diagnostic(transport, client, topic, publish_result):
    try:
        qos, packet_id, _packet_bytes, puback_ms, timeout_set = publish_result
    except Exception:
        return
    if int(qos or 0) != 1:
        return
    _record_raw_ack_diagnostic(
        transport,
        client,
        "puback",
        topic,
        packet_id,
        puback_ms,
        MQTT_PUBACK_SOCKET_TIMEOUT_S,
        timeout_set,
    )


def _record_raw_subscribe_ack_diagnostic(transport, client, topic, subscribe_result):
    try:
        raw_ack, packet_id, _packet_bytes, suback_ms, timeout_set = subscribe_result
    except Exception:
        return
    if not int(raw_ack or 0):
        return
    _record_raw_ack_diagnostic(
        transport,
        client,
        "suback",
        topic,
        packet_id,
        suback_ms,
        MQTT_SUBACK_SOCKET_TIMEOUT_S,
        timeout_set,
    )


def _record_raw_ack_diagnostic(
    transport,
    client,
    kind,
    topic,
    packet_id,
    ack_ms,
    timeout_s,
    timeout_set,
):
    record_ack = getattr(transport, "record_ack_read", None)
    if not callable(record_ack):
        return
    sock = getattr(client, "_sock", None)
    record_ack(
        kind,
        topic,
        stage="complete",
        packet_id=packet_id,
        elapsed_ms=ack_ms,
        timeout_s=timeout_s,
        timeout_set=bool(timeout_set),
        socket_state=_socket_state(sock),
        socket_caps=_socket_capability_summary(sock),
    )


def _mqtt_bytes(value):
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8")


def _runtime_mqtt_client_id(runtime_config):
    for section_name, attr_name in (
        ("sensor", "sensor_id"),
        ("switch", "device_id"),
        ("network", "hostname"),
    ):
        section = getattr(runtime_config, section_name, None)
        value = str(getattr(section, attr_name, "") or "").strip()
        if value:
            return value
    return ""


def _mqtt_remaining_length(value):
    encoded = bytearray()
    remaining = int(value or 0)
    while True:
        digit = remaining % 128
        remaining = remaining // 128
        if remaining > 0:
            digit = digit | 0x80
        encoded.append(digit)
        if remaining <= 0:
            break
    return encoded


def _send_mqtt_packet(sock, packet, *, chunked=False):
    total = len(packet)
    sent_total = 0
    while sent_total < total:
        if chunked:
            chunk_end = min(sent_total + MQTT_RAW_SEND_CHUNK_BYTES, total)
        else:
            chunk_end = total
        chunk = packet[sent_total:chunk_end]
        sent = _send_socket_bytes(sock, chunk)
        if sent is None:
            if chunked:
                sent_total = chunk_end
                continue
            return
        sent_total += int(sent or 0)
        if sent <= 0:
            raise OSError("mqtt_socket_send_zero")


def _send_socket_bytes(sock, data):
    send = getattr(sock, "send")
    try:
        return send(data)
    except TypeError as exc:
        if not _is_socket_nbytes_required_error(exc):
            raise
        return send(data, len(data))


def _sync_subscriptions_to_client(
    adapter,
    transport,
    *,
    slow_threshold=MQTT_SLOW_OPERATION_MS,
    pending_subscription_count=None,
):
    subscribed_count = 0
    pending_subscriptions = transport.subscriptions[adapter.subscription_index :]
    if pending_subscription_count is None:
        pending_subscription_count = len(pending_subscriptions)
    last_subscribed_topic = ""
    elapsed_ms = -1
    for topic in pending_subscriptions:
        operation_started = time.monotonic()
        try:
            subscribe_result = _subscribe_mqtt_qos0(adapter.client, topic)
        except Exception as exc:
            elapsed_ms = _elapsed_ms(operation_started)
            if subscribed_count:
                transport.compact(
                    published_keep_from=0,
                    subscriptions_keep_from=(
                        adapter.subscription_index + subscribed_count
                    ),
                )
            transport.mark_disconnected(
                reason="mqtt_subscribe_failed:{}:{}".format(topic, exc)
            )
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                published_count=0,
                subscribed_count=subscribed_count,
                errors=(
                    "mqtt_subscribe_failed:topic={}:index={}/{}:{}".format(
                        topic,
                        subscribed_count,
                        pending_subscription_count,
                        exc,
                    ),
                ),
                operation="subscribe",
                topic=topic,
                pending_count=pending_subscription_count,
                elapsed_ms=elapsed_ms,
            )
        elapsed_ms = _elapsed_ms(operation_started)
        _record_raw_subscribe_ack_diagnostic(
            transport,
            adapter.client,
            topic,
            subscribe_result,
        )
        subscribed_count += 1
        last_subscribed_topic = topic
        if _operation_is_slow(elapsed_ms, slow_threshold):
            transport.compact(
                published_keep_from=0,
                subscriptions_keep_from=adapter.subscription_index
                + subscribed_count,
            )
            transport.mark_disconnected(
                reason="mqtt_subscribe_slow:{}:elapsed_ms={}".format(
                    topic,
                    elapsed_ms,
                )
            )
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                published_count=0,
                subscribed_count=subscribed_count,
                errors=(
                    "mqtt_subscribe_slow:topic={}:index={}/{}:elapsed_ms={}".format(
                        topic,
                        subscribed_count - 1,
                        pending_subscription_count,
                        elapsed_ms,
                    ),
                ),
                operation="subscribe",
                topic=topic,
                pending_count=pending_subscription_count,
                elapsed_ms=elapsed_ms,
            )

    if subscribed_count:
        transport.compact(
            published_keep_from=0,
            subscriptions_keep_from=adapter.subscription_index + subscribed_count,
        )

    updated_adapter = _mqtt_adapter_with_queue_indexes(adapter, 0, 0)
    return MQTTClientSyncResult(
        phase="synced",
        adapter=updated_adapter,
        published_count=0,
        subscribed_count=subscribed_count,
        errors=(),
        operation="subscribe" if subscribed_count else "",
        topic=last_subscribed_topic,
        pending_count=pending_subscription_count,
        elapsed_ms=elapsed_ms,
    )


def _subscription_clean_poll_ready(transport):
    last_loop_at = _transport_float_attr(transport, "last_loop_at", -1.0)
    if last_loop_at < 0.0:
        return False
    required_at = max(
        _transport_float_attr(transport, "last_connected_at", -1.0),
        _transport_float_attr(transport, "last_publish_at", -1.0),
    )
    return last_loop_at >= required_at


def _transport_float_attr(transport, attr_name, default):
    try:
        return float(getattr(transport, attr_name, default))
    except Exception:
        return float(default)


def _subscribe_mqtt_qos0(client, topic):
    sock = getattr(client, "_sock", None)
    if (
        sock is None
        or not callable(getattr(sock, "send", None))
        or not _socket_can_recv(sock)
    ):
        client.subscribe(topic)
        return _mqtt_ack_result(0, 0, -1, -1, 0)
    packet_id = _next_mqtt_packet_id(client)
    packet = _mqtt_qos0_subscribe_packet(topic, packet_id)
    memory_before = _memory_snapshot()
    runtime_timeout = _poll_timeout_for_client(client)
    suback_timeout_set = False
    send_elapsed_ms = -1
    suback_elapsed_ms = -1
    operation_started = time.monotonic()
    suback_started = operation_started
    try:
        send_started = time.monotonic()
        _send_mqtt_packet(sock, packet)
        send_elapsed_ms = _elapsed_ms(send_started)
        if float(runtime_timeout) != float(MQTT_SUBACK_SOCKET_TIMEOUT_S):
            suback_timeout_set = _set_mqtt_socket_timeout(
                sock,
                MQTT_SUBACK_SOCKET_TIMEOUT_S,
            )
        suback_started = time.monotonic()
        _read_mqtt_suback(sock, packet_id)
        suback_elapsed_ms = _elapsed_ms(suback_started)
    except Exception as exc:
        suback_elapsed_ms = _elapsed_ms(suback_started)
        raise OSError(
            _mqtt_raw_subscribe_failure_detail(
                topic,
                packet_id,
                packet,
                sock,
                memory_before,
                _memory_snapshot(),
                _elapsed_ms(operation_started),
                send_elapsed_ms,
                suback_elapsed_ms,
                runtime_timeout,
                MQTT_SUBACK_SOCKET_TIMEOUT_S,
                suback_timeout_set,
                exc,
            )
        )
    finally:
        if suback_timeout_set:
            _set_mqtt_socket_timeout(sock, runtime_timeout)
    return _mqtt_ack_result(
        1,
        packet_id,
        len(packet),
        suback_elapsed_ms,
        suback_timeout_set,
    )


def _mqtt_qos0_subscribe_packet(topic, packet_id):
    topic_bytes = _mqtt_bytes(topic)
    remaining = 2 + 2 + len(topic_bytes) + 1
    remaining_bytes = _mqtt_remaining_length(remaining)
    packet = bytearray(1 + len(remaining_bytes) + remaining)
    packet[0] = 0x82
    offset = 1
    for byte in remaining_bytes:
        packet[offset] = byte
        offset += 1
    packet[offset] = (packet_id >> 8) & 0xFF
    packet[offset + 1] = packet_id & 0xFF
    offset += 2
    packet[offset] = (len(topic_bytes) >> 8) & 0xFF
    packet[offset + 1] = len(topic_bytes) & 0xFF
    offset += 2
    packet[offset : offset + len(topic_bytes)] = topic_bytes
    offset += len(topic_bytes)
    packet[offset] = 0
    return packet


def _next_mqtt_packet_id(client):
    try:
        packet_id = int(getattr(client, "_cpynodus_packet_id", 0)) + 1
    except Exception:
        packet_id = 1
    if packet_id > 0xFFFF:
        packet_id = 1
    try:
        setattr(client, "_cpynodus_packet_id", packet_id)
    except Exception:
        pass
    return packet_id


def _mqtt_raw_subscribe_failure_detail(
    topic,
    packet_id,
    packet,
    sock,
    memory_before,
    memory_after,
    elapsed_ms,
    send_elapsed_ms,
    suback_elapsed_ms,
    runtime_timeout,
    suback_timeout,
    suback_timeout_set,
    exc,
):
    topic_bytes = _mqtt_bytes(topic)
    return (
        "raw_subscribe_diag pkt_id={} pkt_bytes={} topic_len={} "
        "elapsed_ms={} send_ms={} suback_ms={} runtime_timeout_s={} "
        "suback_timeout_s={} suback_timeout_set={} sock={} caps={} "
        "before={} after={} error={}"
    ).format(
        int(packet_id or 0),
        _payload_size(packet),
        _payload_size(topic_bytes),
        int(elapsed_ms or 0),
        int(send_elapsed_ms or 0),
        int(suback_elapsed_ms or 0),
        _timeout_summary(runtime_timeout),
        _timeout_summary(suback_timeout),
        1 if suback_timeout_set else 0,
        _socket_state(sock),
        _socket_capability_summary(sock),
        _memory_summary(memory_before),
        _memory_summary(memory_after),
        exc,
    )


def _mqtt_raw_publish_failure_detail(
    topic,
    packet_id,
    packet,
    sock,
    elapsed_ms,
    puback_elapsed_ms,
    runtime_timeout,
    puback_timeout,
    puback_timeout_set,
    exc,
):
    topic_bytes = _mqtt_bytes(topic)
    return (
        "raw_publish_diag qos=1 pkt_id={} pkt_bytes={} topic_len={} "
        "elapsed_ms={} puback_ms={} runtime_timeout_s={} "
        "puback_timeout_s={} puback_timeout_set={} sock={} caps={} error={}"
    ).format(
        int(packet_id or 0),
        _payload_size(packet),
        _payload_size(topic_bytes),
        int(elapsed_ms or 0),
        int(puback_elapsed_ms or 0),
        _timeout_summary(runtime_timeout),
        _timeout_summary(puback_timeout),
        1 if puback_timeout_set else 0,
        _socket_state(sock),
        _socket_capability_summary(sock),
        exc,
    )


def _read_mqtt_puback(sock, packet_id):
    stage = "header"
    try:
        header = _recv_mqtt_byte(sock)
        if header != 0x40:
            raise OSError("mqtt_puback_unexpected:{:02x}".format(header))
        stage = "remaining"
        remaining = _recv_mqtt_remaining_length(sock)
        stage = "payload"
        payload = _recv_socket_exact(sock, remaining)
        if len(payload) < 2:
            raise OSError("mqtt_puback_short:{}".format(len(payload)))
        stage = "packet_id"
        received_id = (payload[0] << 8) | payload[1]
        if received_id != packet_id:
            raise OSError("mqtt_puback_packet_id:{}:{}".format(received_id, packet_id))
    except Exception as exc:
        raise OSError("mqtt_puback_stage={}:{}".format(stage, exc))


def _read_mqtt_suback(sock, packet_id):
    stage = "header"
    try:
        header = _recv_mqtt_byte(sock)
        if header != 0x90:
            raise OSError("mqtt_suback_unexpected:{:02x}".format(header))
        stage = "remaining"
        remaining = _recv_mqtt_remaining_length(sock)
        stage = "payload"
        payload = _recv_socket_exact(sock, remaining)
        if len(payload) < 3:
            raise OSError("mqtt_suback_short:{}".format(len(payload)))
        stage = "packet_id"
        received_id = (payload[0] << 8) | payload[1]
        if received_id != packet_id:
            raise OSError(
                "mqtt_suback_packet_id:{}:{}".format(received_id, packet_id)
            )
        stage = "return_code"
        return_code = payload[2]
        if return_code > 2:
            raise OSError("mqtt_suback_failed:{}".format(return_code))
    except Exception as exc:
        raise OSError("mqtt_suback_stage={}:{}".format(stage, exc))


def _recv_mqtt_remaining_length(sock):
    multiplier = 1
    value = 0
    while True:
        encoded = _recv_mqtt_byte(sock)
        value += (encoded & 0x7F) * multiplier
        if (encoded & 0x80) == 0:
            return value
        multiplier *= 128
        if multiplier > 128 * 128 * 128:
            raise OSError("mqtt_remaining_length_invalid")


def _recv_mqtt_byte(sock):
    data = _recv_socket_exact(sock, 1)
    if not data:
        raise OSError("mqtt_socket_recv_empty")
    return data[0]


def _recv_socket_exact(sock, nbytes):
    data = bytearray()
    remaining = int(nbytes or 0)
    while remaining > 0:
        chunk = _recv_socket_bytes(sock, remaining)
        if not chunk:
            raise OSError("mqtt_socket_recv_empty")
        data.extend(chunk)
        remaining -= len(chunk)
    return data


def _recv_socket_bytes(sock, nbytes):
    recv = getattr(sock, "recv", None)
    if callable(recv):
        return recv(nbytes)
    recv_into = getattr(sock, "recv_into", None)
    if callable(recv_into):
        buffer = bytearray(nbytes)
        try:
            count = recv_into(buffer, nbytes)
        except TypeError as exc:
            if not _is_socket_nbytes_required_error(exc):
                raise
            count = recv_into(buffer)
        return bytes(buffer[: int(count or 0)])
    raise OSError("mqtt_socket_recv_unavailable")


def _socket_can_recv(sock):
    return callable(getattr(sock, "recv", None)) or callable(
        getattr(sock, "recv_into", None)
    )


def _mqtt_adapter_with_queue_indexes(adapter, published_index, subscription_index):
    return MQTTClientAdapter(
        phase=adapter.phase,
        driver_kind=adapter.driver_kind,
        broker=adapter.broker,
        port=adapter.port,
        broker_targets=adapter.broker_targets,
        active_broker=adapter.active_broker,
        resolved_broker_ip=adapter.resolved_broker_ip,
        client=adapter.client,
        client_class=adapter.client_class,
        client_kwargs=adapter.client_kwargs,
        socket_compat_enabled=adapter.socket_compat_enabled,
        flexible_callback_enabled=adapter.flexible_callback_enabled,
        published_index=published_index,
        subscription_index=subscription_index,
        errors=adapter.errors,
    )


def poll_mqtt_client(adapter, transport):
    """Poll the client loop so inbound MQTT messages can reach the transport."""
    if adapter.phase != "ready" or adapter.client is None or not transport.connected:
        return MQTTClientSyncResult(
            phase="skipped",
            adapter=adapter,
            errors=adapter.errors
            if adapter.phase != "ready"
            else ("transport_not_connected",),
        )

    if adapter.socket_compat_enabled:
        return _poll_mqtt_client_compat(adapter, transport)
    return _poll_mqtt_client_raw(adapter, transport)


def _poll_mqtt_client_raw(adapter, transport):
    before = len(transport.received_messages)
    if _client_connected_state(adapter.client) == "0":
        transport.mark_disconnected(
            reason=_mqtt_poll_client_disconnected_error(adapter.client)
        )
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            received_count=0,
            errors=(_mqtt_poll_client_disconnected_error(adapter.client),),
        )
    socket_result = _poll_mqtt_client_socket(adapter, transport, before)
    if socket_result is not None:
        return socket_result
    try:
        adapter.client.loop(timeout=_poll_timeout_for_client(adapter.client))
    except TypeError as exc:
        if _is_loop_timeout_signature_error(exc):
            adapter.client.loop()
        elif _is_minimqtt_wrapped_socket_error(exc):
            error = _minimqtt_socket_error(
                adapter.client,
                exc,
                client_connected=_client_connected_state(adapter.client),
                publish_diagnostic=transport.publish_diagnostic(),
                loop_diagnostic=transport.loop_diagnostic(),
            )
            transport.mark_disconnected(reason=error)
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=(error,),
            )
        elif _is_callback_arity_error(exc):
            transport.mark_disconnected(
                reason="mqtt_poll_callback_failed:{}".format(exc)
            )
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=("mqtt_poll_callback_failed:{}".format(exc),),
            )
        else:
            raise
    except ValueError:
        try:
            adapter.client.loop(
                timeout=max(1.0, _poll_timeout_for_client(adapter.client))
            )
        except TypeError as exc:
            if _is_loop_timeout_signature_error(exc):
                adapter.client.loop()
            elif _is_minimqtt_wrapped_socket_error(exc):
                error = _minimqtt_socket_error(
                    adapter.client,
                    exc,
                    client_connected=_client_connected_state(adapter.client),
                    publish_diagnostic=transport.publish_diagnostic(),
                    loop_diagnostic=transport.loop_diagnostic(),
                )
                transport.mark_disconnected(reason=error)
                return MQTTClientSyncResult(
                    phase="error",
                    adapter=adapter,
                    received_count=0,
                    errors=(error,),
                )
            elif _is_callback_arity_error(exc):
                transport.mark_disconnected(
                    reason="mqtt_poll_callback_failed:{}".format(exc)
                )
                return MQTTClientSyncResult(
                    phase="error",
                    adapter=adapter,
                    received_count=0,
                    errors=("mqtt_poll_callback_failed:{}".format(exc),),
                )
            else:
                raise
        except RuntimeError as exc:
            if not _is_pystack_exhausted(exc):
                raise
            error = _mqtt_poll_pystack_error(
                adapter.client,
                "minimqtt_loop_retry",
                exc,
            )
            transport.mark_disconnected(reason=error)
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=(error,),
            )
    except OSError as exc:
        error = _mqtt_poll_oserror_detail(adapter.client, transport, exc)
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            received_count=0,
            errors=(error,),
        )
    except RuntimeError as exc:
        if not _is_pystack_exhausted(exc):
            raise
        error = _mqtt_poll_pystack_error(adapter.client, "minimqtt_loop", exc)
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            received_count=0,
            errors=(error,),
        )
    transport.record_loop_success(
        received_count=len(transport.received_messages) - before,
        timeout=_poll_timeout_for_client(adapter.client),
        socket_state=_socket_state(getattr(adapter.client, "_sock", None)),
        client_connected=_client_connected_state(adapter.client),
        backcompat=0,
    )
    transport.mark_success()
    return MQTTClientSyncResult(
        phase="polled",
        adapter=adapter,
        received_count=max(0, len(transport.received_messages) - before),
        errors=(),
    )


def _poll_mqtt_client_socket(adapter, transport, before_count):
    sock = getattr(adapter.client, "_sock", None)
    if sock is None or not _socket_can_recv(sock):
        return None
    try:
        received_count = _poll_mqtt_socket_once(sock, transport)
    except OSError as exc:
        error = _mqtt_poll_oserror_detail(adapter.client, transport, exc)
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            received_count=0,
            errors=(error,),
        )
    except RuntimeError as exc:
        if not _is_pystack_exhausted(exc):
            raise
        error = _mqtt_poll_pystack_error(adapter.client, "raw_socket", exc)
        transport.mark_disconnected(reason=error)
        return MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            received_count=0,
            errors=(error,),
        )
    received_total = max(
        int(received_count or 0),
        len(transport.received_messages) - before_count,
    )
    transport.record_loop_success(
        received_count=received_total,
        timeout=_poll_timeout_for_client(adapter.client),
        socket_state=_socket_state(sock),
        client_connected=_client_connected_state(adapter.client),
        backcompat=0,
    )
    transport.mark_success()
    return MQTTClientSyncResult(
        phase="polled",
        adapter=adapter,
        received_count=max(0, received_total),
        errors=(),
    )


def _poll_mqtt_socket_once(sock, transport):
    stage = "header"
    recv = getattr(sock, "recv", None)
    recv_into = getattr(sock, "recv_into", None)
    can_recv = callable(recv)
    can_recv_into = callable(recv_into)
    scratch = bytearray(1)
    try:
        if can_recv:
            data = recv(1)
            if not data:
                raise OSError("mqtt_socket_recv_empty")
            header = data[0]
        elif can_recv_into:
            try:
                count = recv_into(scratch, 1)
            except TypeError as exc:
                if not _is_socket_nbytes_required_error(exc):
                    raise
                count = recv_into(scratch)
            if not count:
                raise OSError("mqtt_socket_recv_empty")
            header = scratch[0]
        else:
            raise OSError("mqtt_socket_recv_unavailable")
    except OSError as exc:
        if _is_mqtt_poll_no_data_error(exc):
            return 0
        raise
    except RuntimeError as exc:
        if _is_pystack_exhausted(exc):
            raise RuntimeError("raw_stage={}:{}".format(stage, exc))
        raise
    stage = "remaining_length"
    try:
        packet_type = (header >> 4) & 0x0F
        multiplier = 1
        remaining = 0
        while True:
            if can_recv:
                data = recv(1)
                if not data:
                    raise OSError("mqtt_socket_recv_empty")
                encoded = data[0]
            else:
                try:
                    count = recv_into(scratch, 1)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
                    count = recv_into(scratch)
                if not count:
                    raise OSError("mqtt_socket_recv_empty")
                encoded = scratch[0]
            remaining += (encoded & 0x7F) * multiplier
            if (encoded & 0x80) == 0:
                break
            multiplier *= 128
            if multiplier > 128 * 128 * 128:
                raise OSError("mqtt_remaining_length_invalid")
        packet = b""
        if remaining:
            stage = "payload"
            packet = bytearray(remaining)
            offset = 0
            if can_recv:
                while offset < remaining:
                    data = recv(remaining - offset)
                    if not data:
                        raise OSError("mqtt_socket_recv_empty")
                    count = len(data)
                    packet[offset : offset + count] = data
                    offset += count
            else:
                packet_view = memoryview(packet)
                while offset < remaining:
                    try:
                        count = recv_into(packet_view[offset:], remaining - offset)
                    except TypeError as exc:
                        if not _is_socket_nbytes_required_error(exc):
                            raise
                        count = recv_into(packet_view[offset:])
                    if not count:
                        raise OSError("mqtt_socket_recv_empty")
                    offset += int(count)
        if packet_type == 3:
            if len(packet) < 2:
                raise OSError("mqtt_publish_short:{}".format(len(packet)))
            topic_len = (packet[0] << 8) | packet[1]
            topic_start = 2
            topic_end = topic_start + topic_len
            if len(packet) < topic_end:
                raise OSError(
                    "mqtt_publish_topic_short:{}:{}".format(
                        len(packet),
                        topic_len,
                    )
                )
            stage = "topic_decode"
            topic_data = packet[topic_start:topic_end]
            try:
                topic = topic_data.decode("utf-8")
            except Exception:
                topic = bytes(topic_data).decode("utf-8", errors="ignore")
            offset = topic_end
            qos = (header >> 1) & 0x03
            if qos:
                if len(packet) < offset + 2:
                    raise OSError("mqtt_publish_packet_id_short")
                packet_id = (packet[offset] << 8) | packet[offset + 1]
                offset += 2
                if qos == 1:
                    stage = "puback"
                    _send_mqtt_packet(sock, _mqtt_puback_packet(packet_id))
            stage = "payload_decode"
            payload_data = packet[offset:]
            try:
                payload_text = payload_data.decode("utf-8")
            except Exception:
                payload_text = bytes(payload_data).decode("utf-8", errors="ignore")
            stage = "transport_receive"
            transport.receive(topic, payload_text)
            return 1
        if packet_type == 14:
            raise OSError("mqtt_disconnect_packet")
        return 0
    except RuntimeError as exc:
        if _is_pystack_exhausted(exc):
            raise RuntimeError("raw_stage={}:{}".format(stage, exc))
        raise


def _mqtt_puback_packet(packet_id):
    return bytes((0x40, 0x02, (packet_id >> 8) & 0xFF, packet_id & 0xFF))


def _is_mqtt_poll_no_data_error(exc):
    errno = getattr(exc, "errno", None)
    if errno in (11, 116):
        return True
    args = getattr(exc, "args", ()) or ()
    if args and args[0] in (11, 116):
        return True
    text = str(exc or "").lower()
    if "timeout" in text or "timed out" in text:
        return True
    if "etimedout" in text or "eagain" in text:
        return True
    if "errno 116" in text or "errno 11" in text:
        return True
    if "would block" in text or "again" in text:
        return True
    return "mqtt_socket_recv_empty" in text


def _is_pystack_exhausted(exc):
    text = str(exc or "").lower()
    return "pystack exhausted" in text


def _mqtt_poll_pystack_error(client, source, exc):
    sock = getattr(client, "_sock", None)
    return (
        "mqtt_poll_failed:pystack source={} sock={} caps={} "
        "client_connected={} error={}"
    ).format(
        str(source or "unknown"),
        _socket_state(sock),
        _socket_capability_summary(sock),
        _client_connected_state(client),
        exc,
    )


def _mqtt_poll_oserror_detail(client, transport, exc):
    base = "mqtt_poll_failed:{}".format(exc)
    if not _is_bad_file_descriptor_error(exc):
        return base
    sock = getattr(client, "_sock", None)
    details = []
    if sock is not None:
        details.append("poll_sock={}".format(_socket_state(sock)))
        details.append("poll_caps={}".format(_socket_capability_summary(sock)))
        details.append("poll_timeout_s={}".format(_socket_timeout_summary(sock)))
    for method_name in ("publish_diagnostic", "loop_diagnostic", "ack_diagnostic"):
        diagnostic = ""
        method = getattr(transport, method_name, None)
        if callable(method):
            try:
                diagnostic = str(method() or "").strip()
            except Exception:
                diagnostic = ""
        if diagnostic:
            details.append(diagnostic)
    if not details:
        return base
    return "{} {}".format(base, " ".join(details))


def _is_bad_file_descriptor_error(exc):
    errno = getattr(exc, "errno", None)
    if errno == 9:
        return True
    args = getattr(exc, "args", ()) or ()
    if args and args[0] == 9:
        return True
    text = str(exc or "").lower()
    return (
        "ebadf" in text
        or "[errno 9]" in text
        or "errno 9" in text
        or "bad file descriptor" in text
    )


def _socket_timeout_summary(sock):
    gettimeout = getattr(sock, "gettimeout", None)
    if callable(gettimeout):
        try:
            return _timeout_summary(gettimeout())
        except Exception:
            pass
    for attr_name in ("timeout", "_timeout", "socket_timeout"):
        try:
            value = getattr(sock, attr_name)
        except Exception:
            continue
        if value is not None:
            return _timeout_summary(value)
    try:
        history = getattr(sock, "timeouts", None)
    except Exception:
        history = None
    if history:
        try:
            return _timeout_summary(history[-1])
        except Exception:
            pass
    return "unknown"


def _socket_capability_summary(sock):
    if sock is None:
        return "none"
    return "{}:send{} recv{} recv_into{}".format(
        type(sock).__name__,
        1 if callable(getattr(sock, "send", None)) else 0,
        1 if callable(getattr(sock, "recv", None)) else 0,
        1 if callable(getattr(sock, "recv_into", None)) else 0,
    )


def _poll_mqtt_client_compat(adapter, transport):
    before = len(transport.received_messages)
    loop = getattr(adapter.client, "loop", None)
    if callable(loop):
        _ensure_minimqtt_socket_compat(adapter.client)
        client_connected = _client_connected_state(adapter.client)
        if client_connected == "0":
            error = _mqtt_poll_client_disconnected_error(adapter.client)
            transport.mark_disconnected(reason=error)
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=(error,),
            )
        timeout = _poll_timeout_for_client(adapter.client)
        used_timeout = timeout
        try:
            loop(timeout=timeout)
        except TypeError as exc:
            if not _is_loop_timeout_signature_error(exc):
                if _is_minimqtt_wrapped_socket_error(exc):
                    error = _minimqtt_socket_error(
                        adapter.client,
                        exc,
                        client_connected=client_connected,
                        publish_diagnostic=transport.publish_diagnostic(),
                        loop_diagnostic=transport.loop_diagnostic(),
                    )
                    transport.mark_disconnected(reason=error)
                    return MQTTClientSyncResult(
                        phase="error",
                        adapter=adapter,
                        received_count=0,
                        errors=(error,),
                    )
                if _is_callback_arity_error(exc):
                    transport.mark_disconnected(
                        reason="mqtt_poll_callback_failed:{}".format(exc)
                    )
                    return MQTTClientSyncResult(
                        phase="error",
                        adapter=adapter,
                        received_count=0,
                        errors=("mqtt_poll_callback_failed:{}".format(exc),),
                    )
                raise
            used_timeout = -1.0
            loop()
        except ValueError:
            try:
                used_timeout = max(1.0, timeout)
                loop(timeout=used_timeout)
            except TypeError as exc:
                if not _is_loop_timeout_signature_error(exc):
                    if _is_minimqtt_wrapped_socket_error(exc):
                        error = _minimqtt_socket_error(
                            adapter.client,
                            exc,
                            client_connected=client_connected,
                            publish_diagnostic=transport.publish_diagnostic(),
                            loop_diagnostic=transport.loop_diagnostic(),
                        )
                        transport.mark_disconnected(reason=error)
                        return MQTTClientSyncResult(
                            phase="error",
                            adapter=adapter,
                            received_count=0,
                            errors=(error,),
                        )
                    if _is_callback_arity_error(exc):
                        transport.mark_disconnected(
                            reason="mqtt_poll_callback_failed:{}".format(exc)
                        )
                        return MQTTClientSyncResult(
                            phase="error",
                            adapter=adapter,
                            received_count=0,
                            errors=("mqtt_poll_callback_failed:{}".format(exc),),
                        )
                    raise
                used_timeout = -1.0
                loop()
            except RuntimeError as exc:
                if not _is_pystack_exhausted(exc):
                    raise
                error = _mqtt_poll_pystack_error(
                    adapter.client,
                    "compat_loop_retry",
                    exc,
                )
                transport.mark_disconnected(reason=error)
                return MQTTClientSyncResult(
                    phase="error",
                    adapter=adapter,
                    received_count=0,
                    errors=(error,),
                )
        except OSError as exc:
            error = _mqtt_poll_oserror_detail(adapter.client, transport, exc)
            transport.mark_disconnected(reason=error)
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=(error,),
            )
        except RuntimeError as exc:
            if not _is_pystack_exhausted(exc):
                raise
            error = _mqtt_poll_pystack_error(adapter.client, "compat_loop", exc)
            transport.mark_disconnected(reason=error)
            return MQTTClientSyncResult(
                phase="error",
                adapter=adapter,
                received_count=0,
                errors=(error,),
            )
    received_count = len(transport.received_messages) - before
    if callable(loop):
        backcompat = 0
        if getattr(adapter.client, "_backwards_compatible_sock", False):
            backcompat = 1
        transport.record_loop_success(
            received_count=received_count,
            timeout=used_timeout,
            socket_state=_socket_state(getattr(adapter.client, "_sock", None)),
            client_connected=_client_connected_state(adapter.client),
            backcompat=backcompat,
        )
        transport.mark_success()
    return MQTTClientSyncResult(
        phase="polled",
        adapter=adapter,
        received_count=max(0, received_count),
        errors=(),
    )


def disconnect_mqtt_client(adapter, transport, runtime_config):
    """Publish retained offline status, flush it, then disconnect the client."""
    from cpynodus_ii.features.publish_cycle import publish_shutdown_cycle

    if adapter.phase != "ready" or adapter.client is None:
        transport.mark_disconnected(
            reason="mqtt_adapter_not_ready:{}".format(adapter.phase)
        )
        return MQTTClientSyncResult(
            phase=adapter.phase,
            adapter=adapter,
            errors=adapter.errors,
        )

    sync_result = MQTTClientSyncResult(
        phase="skipped",
        adapter=adapter,
        errors=(),
    )
    try:
        publish_shutdown_cycle(transport, runtime_config)
        sync_result = sync_transport_to_client(adapter, transport)
    except OSError as exc:
        transport.mark_disconnected(
            reason="mqtt_disconnect_flush_failed:{}".format(exc)
        )
        sync_result = MQTTClientSyncResult(
            phase="error",
            adapter=adapter,
            errors=("mqtt_disconnect_flush_failed:{}".format(exc),),
        )
    try:
        disconnect = getattr(adapter.client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except OSError as exc:
                sync_result = MQTTClientSyncResult(
                    phase="error",
                    adapter=sync_result.adapter,
                    published_count=sync_result.published_count,
                    subscribed_count=sync_result.subscribed_count,
                    errors=sync_result.errors
                    + ("mqtt_disconnect_failed:{}".format(exc),),
                )
    finally:
        _closed_socket, close_errors = _force_close_mqtt_client_socket(
            adapter.client
        )
        if close_errors:
            sync_result = MQTTClientSyncResult(
                phase="error",
                adapter=sync_result.adapter,
                published_count=sync_result.published_count,
                subscribed_count=sync_result.subscribed_count,
                errors=sync_result.errors + close_errors,
            )
        transport.mark_disconnected(reason="mqtt_disconnect_requested")
    return MQTTClientSyncResult(
        phase="disconnected",
        adapter=sync_result.adapter,
        published_count=sync_result.published_count,
        subscribed_count=sync_result.subscribed_count,
        errors=sync_result.errors,
    )


def close_mqtt_client(adapter, transport):
    """Close MQTT without queuing shutdown or offline status publishes."""
    errors = ()
    if adapter.phase == "ready" and adapter.client is not None:
        disconnect = getattr(adapter.client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception as exc:
                if not _is_not_connected_close_error(exc):
                    errors = ("mqtt_close_failed:{}".format(exc),)
        _closed_socket, close_errors = _force_close_mqtt_client_socket(adapter.client)
        errors = errors + close_errors
    transport.mark_disconnected(reason="mqtt_close_requested")
    return MQTTClientSyncResult(
        phase="disconnected",
        adapter=adapter,
        errors=errors,
    )


def _force_close_mqtt_client_socket(client):
    """Close a MiniMQTT socket even when disconnect refuses the client state."""
    if client is None:
        return False, ()
    sock = getattr(client, "_sock", None)
    had_socket = sock is not None
    errors = ()
    close_socket = getattr(client, "_close_socket", None)
    if callable(close_socket):
        try:
            close_socket()
            sock = None
        except Exception as exc:
            errors = ("mqtt_socket_close_failed:{}".format(exc),)
            sock = getattr(client, "_sock", sock)
    if sock is not None:
        extra_errors = _close_raw_mqtt_socket(client, sock)
        errors = errors + extra_errors
    try:
        setattr(client, "_sock", None)
    except Exception:
        pass
    try:
        setattr(client, "_is_connected", False)
    except Exception:
        pass
    return had_socket, errors


def _close_raw_mqtt_socket(client, sock):
    errors = ()
    connection_manager = getattr(client, "_connection_manager", None)
    close_socket = getattr(connection_manager, "close_socket", None)
    if callable(close_socket):
        try:
            close_socket(sock)
            return ()
        except Exception as exc:
            errors = ("mqtt_socket_close_failed:{}".format(exc),)
    close = getattr(sock, "close", None)
    if callable(close):
        try:
            close()
            return ()
        except Exception as exc:
            errors = errors + ("mqtt_socket_direct_close_failed:{}".format(exc),)
    return errors


def _is_not_connected_close_error(exc):
    text = str(exc or "").strip().lower()
    return "not connected" in text


def _resolve_mqtt_class(modules):
    if isinstance(modules, dict) and modules.get("mqtt_cls") is not None:
        return modules["mqtt_cls"]
    try:
        from adafruit_minimqtt.adafruit_minimqtt import MQTT  # type: ignore
    except ImportError:
        return None
    return MQTT


def _instantiate_client(client_class, kwargs, broker):
    kwargs["broker"] = broker
    try:
        return client_class(**kwargs)
    except TypeError as exc:
        if not (
            _looks_unexpected_mqtt_kwarg_error(exc)
            and _drop_optional_mqtt_client_kwargs(kwargs)
        ):
            raise
    return client_class(**kwargs)


def _drop_optional_mqtt_client_kwargs(kwargs):
    removed = False
    for key in MQTT_OPTIONAL_CLIENT_KWARGS:
        if key in kwargs:
            removed = True
            try:
                del kwargs[key]
            except Exception:
                pass
    return removed


def _looks_unexpected_mqtt_kwarg_error(exc):
    text = str(exc or "").strip().lower()
    return "keyword" in text and ("unexpected" in text or "invalid" in text)


class _MiniMQTTSocketPoolCompat:
    def __init__(self, socket_pool):
        self._socket_pool = socket_pool

    def socket(self, *args, **kwargs):
        socket_obj = self._socket_pool.socket(*args, **kwargs)
        return _MiniMQTTSocketCompat(socket_obj)

    def __getattr__(self, name):
        return getattr(self._socket_pool, name)


class _MiniMQTTSocketCompat:
    def __init__(self, socket_obj):
        self._socket_obj = socket_obj

    def send(self, buffer):
        send = getattr(self._socket_obj, "send")
        try:
            return send(buffer)
        except TypeError as exc:
            if not _is_socket_nbytes_required_error(exc):
                raise
            return send(buffer, len(buffer))

    def recv_into(self, buffer, nbytes=None):
        if nbytes is None:
            return self._recv_into_compatible(
                self._socket_obj,
                buffer,
                len(buffer),
                True,
            )
        return self._recv_into_compatible(self._socket_obj, buffer, nbytes, False)

    def recv(self, nbytes):
        recv = getattr(self._socket_obj, "recv")
        return recv(nbytes)

    def _recv_into_from_recv(self, socket_obj, buffer, nbytes):
        recv = getattr(socket_obj, "recv")
        data = recv(nbytes)
        count = len(data or b"")
        if count:
            buffer[:count] = data
        return count

    def _recv_into_compatible(self, socket_obj, buffer, nbytes, prefer_single_arg):
        recv_into = getattr(socket_obj, "recv_into", None)
        if callable(recv_into):
            if prefer_single_arg:
                try:
                    return recv_into(buffer)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
                try:
                    return recv_into(buffer, nbytes)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
            else:
                try:
                    return recv_into(buffer, nbytes)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise
                try:
                    return recv_into(buffer)
                except TypeError as exc:
                    if not _is_socket_nbytes_required_error(exc):
                        raise

        raw_socket = _inner_socket_obj(socket_obj)
        if raw_socket is not None and raw_socket is not socket_obj:
            return self._recv_into_compatible(raw_socket, buffer, nbytes, False)

        return self._recv_into_from_recv(socket_obj, buffer, nbytes)

    def __getattr__(self, name):
        return getattr(self._socket_obj, name)


def _wrap_minimqtt_socket_pool(socket_pool):
    if socket_pool is None or isinstance(socket_pool, _MiniMQTTSocketPoolCompat):
        return socket_pool
    if not callable(getattr(socket_pool, "socket", None)):
        return socket_pool
    return _MiniMQTTSocketPoolCompat(socket_pool)


def _should_wrap_socket_pool_before_connect(modules):
    if not isinstance(modules, dict):
        return False
    return bool(modules.get("wrap_socket_pool_before_connect", False))


def _should_enable_socket_compat(modules):
    if not isinstance(modules, dict):
        return False
    return bool(
        modules.get("wrap_socket_pool_before_connect", False)
        or modules.get("wrap_socket_after_connect", False)
    )


def _should_enable_flexible_callback(modules):
    if not isinstance(modules, dict):
        return False
    return bool(modules.get("flexible_callback", False))


def _ensure_minimqtt_socket_compat(client):
    socket_obj = getattr(client, "_sock", None)
    if socket_obj is None or isinstance(socket_obj, _MiniMQTTSocketCompat):
        return
    try:
        client._sock = _MiniMQTTSocketCompat(socket_obj)
        client._backwards_compatible_sock = False
    except Exception:
        pass


def _set_minimqtt_runtime_socket_timeout(client, timeout):
    for attr_name in (
        "socket_timeout",
        "_socket_timeout",
        "recv_timeout",
        "_recv_timeout",
    ):
        value = getattr(client, attr_name, None)
        if _is_positive_number(value):
            try:
                setattr(client, attr_name, timeout)
            except Exception:
                pass
    socket_obj = getattr(client, "_sock", None)
    _set_mqtt_socket_timeout(socket_obj, timeout)


def _set_mqtt_socket_timeout(socket_obj, timeout):
    settimeout = getattr(socket_obj, "settimeout", None)
    if callable(settimeout):
        try:
            settimeout(timeout)
            return True
        except Exception:
            return False
    return False


def _timeout_summary(timeout):
    try:
        value = float(timeout)
    except Exception:
        return "unknown"
    int_value = int(value)
    if value == int_value:
        return str(int_value)
    return str(value)


def _inner_socket_obj(socket_obj):
    for attr_name in ("_socket", "_sock", "_socket_obj"):
        try:
            inner = getattr(socket_obj, attr_name, None)
        except Exception:
            inner = None
        if inner is not None and inner is not socket_obj:
            return inner
    return None


def _poll_timeout_for_client(client):
    for attr_name in (
        "socket_timeout",
        "_socket_timeout",
        "recv_timeout",
        "_recv_timeout",
    ):
        value = getattr(client, attr_name, None)
        if _is_positive_number(value):
            return max(0.1, float(value))
    socket_obj = getattr(client, "_socket", None)
    for attr_name in ("timeout", "_timeout"):
        value = getattr(socket_obj, attr_name, None)
        if _is_positive_number(value):
            return max(0.1, float(value))
    return float(MQTT_POLL_SOCKET_TIMEOUT_S)


def _preflight_broker_target(adapter, broker):
    error, _ip_address = _resolve_broker_target(adapter, broker)
    return error


def preflight_mqtt_broker(adapter, broker=None):
    """Resolve an MQTT broker target without opening a client socket."""
    target = (
        broker
        or getattr(adapter, "active_broker", "")
        or getattr(adapter, "broker", "")
    )
    return _resolve_broker_target(adapter, target)


def preflight_mqtt_broker_tcp(adapter, broker=None, *, timeout_s=None):
    """Open and close a TCP socket to the MQTT broker using adapter sockets."""
    target = (
        broker
        or getattr(adapter, "active_broker", "")
        or getattr(adapter, "broker", "")
    )
    target = str(target or "").strip()
    if not target:
        return "mqtt_tcp_preflight_failed:empty_broker", ""

    resolve_error, resolved_ip = _resolve_broker_target(adapter, target)
    if resolve_error:
        return resolve_error, ""
    connect_target = resolved_ip or target

    socket_pool = None
    if isinstance(adapter.client_kwargs, dict):
        socket_pool = adapter.client_kwargs.get("socket_pool")
    if socket_pool is None:
        return "mqtt_tcp_preflight_failed:{}:socket_pool_unavailable".format(
            connect_target
        ), connect_target

    socket_factory = getattr(socket_pool, "socket", None)
    if not callable(socket_factory):
        return "mqtt_tcp_preflight_failed:{}:socket_unavailable".format(
            connect_target
        ), connect_target

    sock = None
    try:
        sock = socket_factory()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(
                MQTT_CONNECT_SOCKET_TIMEOUT_S if timeout_s is None else timeout_s
            )
        sock.connect((connect_target, int(getattr(adapter, "port", 1883) or 1883)))
        return "", connect_target
    except Exception as exc:
        return "mqtt_tcp_preflight_failed:{}:{}:{}".format(
            connect_target,
            type(exc).__name__,
            exc,
        ), connect_target
    finally:
        if sock is not None:
            close = getattr(sock, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def preflight_mqtt_broker_connect(
    adapter,
    broker=None,
    *,
    timeout_s=None,
    client_id=None,
):
    """Perform a raw MQTT CONNECT/CONNACK probe using adapter sockets."""
    target = (
        broker
        or getattr(adapter, "active_broker", "")
        or getattr(adapter, "broker", "")
    )
    target = str(target or "").strip()
    if not target:
        return "mqtt_connect_probe_failed:empty_broker", "", "", -1

    resolve_error, resolved_ip = _resolve_broker_target(adapter, target)
    if resolve_error:
        return resolve_error, "", "", -1
    connect_target = resolved_ip or target

    if _mqtt_adapter_uses_tls(adapter):
        return "mqtt_connect_probe_skipped:{}:tls_enabled".format(
            connect_target
        ), connect_target, "", -1

    socket_pool = _mqtt_adapter_socket_pool(adapter)
    if socket_pool is None:
        return "mqtt_connect_probe_failed:{}:socket_pool_unavailable".format(
            connect_target
        ), connect_target, "", -1

    socket_factory = getattr(socket_pool, "socket", None)
    if not callable(socket_factory):
        return "mqtt_connect_probe_failed:{}:socket_unavailable".format(
            connect_target
        ), connect_target, "", -1

    sock = None
    client_id = _mqtt_probe_client_id(adapter, client_id)
    return_code = -1
    try:
        sock = socket_factory()
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(
                MQTT_CONNECT_SOCKET_TIMEOUT_S if timeout_s is None else timeout_s
            )
        sock.connect((connect_target, int(getattr(adapter, "port", 1883) or 1883)))
        _send_mqtt_packet(sock, _mqtt_connect_packet(adapter, client_id))
        header = _recv_mqtt_byte(sock)
        if header != 0x20:
            return (
                "mqtt_connect_probe_failed:{}:unexpected_header:{:02x}".format(
                    connect_target,
                    header,
                ),
                connect_target,
                client_id,
                return_code,
            )
        remaining = _recv_mqtt_remaining_length(sock)
        payload = _recv_socket_exact(sock, remaining)
        if len(payload) < 2:
            return (
                "mqtt_connect_probe_failed:{}:short_connack:{}".format(
                    connect_target,
                    len(payload),
                ),
                connect_target,
                client_id,
                return_code,
            )
        return_code = int(payload[1])
        if return_code:
            return (
                "mqtt_connect_probe_failed:{}:connack_code={}".format(
                    connect_target,
                    return_code,
                ),
                connect_target,
                client_id,
                return_code,
            )
        _send_mqtt_packet(sock, bytes((0xE0, 0x00)))
        return "", connect_target, client_id, return_code
    except Exception as exc:
        return (
            "mqtt_connect_probe_failed:{}:{}:{}".format(
                connect_target,
                type(exc).__name__,
                exc,
            ),
            connect_target,
            client_id,
            return_code,
        )
    finally:
        if sock is not None:
            close = getattr(sock, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _mqtt_adapter_socket_pool(adapter):
    client_kwargs = getattr(adapter, "client_kwargs", None)
    if isinstance(client_kwargs, dict):
        return client_kwargs.get("socket_pool")
    return None


def _mqtt_adapter_uses_tls(adapter):
    if int(getattr(adapter, "port", 0) or 0) == 8883:
        return True
    client_kwargs = getattr(adapter, "client_kwargs", None)
    if isinstance(client_kwargs, dict):
        return client_kwargs.get("ssl_context") is not None
    return False


def _mqtt_probe_client_id(adapter, client_id=None):
    if client_id is not None:
        return _mqtt_text(client_id)
    if isinstance(adapter.client_kwargs, dict):
        value = adapter.client_kwargs.get("client_id")
        if value:
            return _mqtt_text(value)
    client = getattr(adapter, "client", None)
    for attr_name in ("client_id", "_client_id", "_client_identifier"):
        value = getattr(client, attr_name, None)
        if value:
            return _mqtt_text(value)
    return "cpynodus-probe"


def _mqtt_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return "cpynodus-probe"
    return str(value or "").strip() or "cpynodus-probe"


def _mqtt_connect_packet(adapter, client_id):
    client_id_bytes = _mqtt_bytes(client_id)
    username = ""
    password = ""
    if isinstance(adapter.client_kwargs, dict):
        username = adapter.client_kwargs.get("username") or ""
        password = adapter.client_kwargs.get("password") or ""
    username_bytes = _mqtt_bytes(username) if username else b""
    password_bytes = _mqtt_bytes(password) if password else b""
    connect_flags = 0x02
    if username_bytes:
        connect_flags |= 0x80
    if password_bytes:
        connect_flags |= 0x40
    keep_alive = 60
    remaining = 10 + 2 + len(client_id_bytes)
    if username_bytes:
        remaining += 2 + len(username_bytes)
    if password_bytes:
        remaining += 2 + len(password_bytes)
    remaining_bytes = _mqtt_remaining_length(remaining)
    packet = bytearray(1 + len(remaining_bytes) + remaining)
    packet[0] = 0x10
    offset = 1
    for byte in remaining_bytes:
        packet[offset] = byte
        offset += 1
    packet[offset : offset + 6] = b"\x00\x04MQTT"
    offset += 6
    packet[offset] = 0x04
    packet[offset + 1] = connect_flags
    packet[offset + 2] = (keep_alive >> 8) & 0xFF
    packet[offset + 3] = keep_alive & 0xFF
    offset += 4
    offset = _write_mqtt_utf8(packet, offset, client_id_bytes)
    if username_bytes:
        offset = _write_mqtt_utf8(packet, offset, username_bytes)
    if password_bytes:
        offset = _write_mqtt_utf8(packet, offset, password_bytes)
    return packet


def _write_mqtt_utf8(packet, offset, value_bytes):
    packet[offset] = (len(value_bytes) >> 8) & 0xFF
    packet[offset + 1] = len(value_bytes) & 0xFF
    offset += 2
    packet[offset : offset + len(value_bytes)] = value_bytes
    return offset + len(value_bytes)


def _resolve_broker_target(adapter, broker):
    broker_text = str(broker or "").strip()
    if not broker_text:
        return "mqtt_connect_failed:empty_broker", ""
    if _looks_like_ip_literal(broker_text):
        return "", broker_text

    socket_pool = None
    if isinstance(adapter.client_kwargs, dict):
        socket_pool = adapter.client_kwargs.get("socket_pool")
    if socket_pool is None:
        return "", ""

    getaddrinfo = getattr(socket_pool, "getaddrinfo", None)
    if not callable(getaddrinfo):
        return "", ""

    try:
        resolved = getaddrinfo(broker_text, adapter.port)
    except Exception as exc:
        return "mqtt_resolve_failed:{}:{}".format(broker_text, exc), ""
    return "", _ip_from_getaddrinfo_result(resolved)


def _ip_from_getaddrinfo_result(resolved):
    try:
        first = resolved[0]
        sockaddr = first[-1]
        return str(sockaddr[0] or "").strip()
    except Exception:
        return ""


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
    if ":" in text:
        return True
    return False


def _is_positive_number(value):
    try:
        return float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _is_loop_timeout_signature_error(exc):
    text = str(exc or "").lower()
    if "keyword" in text:
        return True
    if "unexpected" in text and "timeout" in text:
        return True
    if "positional argument" in text and "timeout" in text:
        return True
    return False


def _is_callback_arity_error(exc):
    text = str(exc or "").lower()
    if "positional argument" in text and "given" in text:
        return True
    if "required positional argument" in text:
        return True
    return False


def _is_minimqtt_wrapped_socket_error(exc):
    text = str(exc or "").lower()
    return text == "function takes 3 positional arguments but 2 were given"


def _minimqtt_socket_error(
    client,
    exc,
    *,
    client_connected=None,
    publish_diagnostic="",
    loop_diagnostic="",
):
    socket_obj = getattr(client, "_sock", None)
    socket_state = _socket_state(socket_obj)
    backwards = 1 if getattr(client, "_backwards_compatible_sock", False) else 0
    connected_state = client_connected
    if connected_state is None:
        connected_state = _client_connected_state(client)
    error = (
        "mqtt_poll_failed:minimqtt_socket:sock={} client_connected={} "
        "backcompat={} error={}"
    ).format(
        socket_state,
        connected_state,
        backwards,
        exc,
    )
    diagnostic = str(publish_diagnostic or "").strip()
    if diagnostic:
        error = "{} {}".format(error, diagnostic)
    diagnostic = str(loop_diagnostic or "").strip()
    if diagnostic:
        error = "{} {}".format(error, diagnostic)
    return error


def _mqtt_poll_client_disconnected_error(client):
    socket_state = _socket_state(getattr(client, "_sock", None))
    return "mqtt_poll_failed:client_disconnected:sock={}".format(socket_state)


def _socket_state(socket_obj):
    if socket_obj is None:
        return "none"
    if isinstance(socket_obj, _MiniMQTTSocketCompat):
        return "wrapped"
    return "raw"


def _client_connected_state(client):
    for name in ("is_connected", "connected", "_connected"):
        try:
            value = getattr(client, name)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                return "error"
        if value is True:
            return "1"
        if value is False:
            return "0"
    return "unknown"


def _is_socket_nbytes_required_error(exc):
    text = str(exc or "").lower()
    if ("recv_into" in text or "send" in text) and "argument" in text:
        return True
    if "positional argument" in text and "given" in text and "takes" in text:
        return True
    return False


def _bind_on_message(client, transport, *, flexible=False):
    if flexible:
        def _on_message(*args):
            if len(args) >= 3:
                _, topic, message = args[-3:]
            elif len(args) == 2:
                topic, message = args
            else:
                raise TypeError("mqtt_on_message_callback_args_invalid")
            transport.receive(topic, _coerce_payload_text(message))
    else:
        def _on_message(_client, topic, message):
            transport.receive(topic, _coerce_payload_text(message))

    try:
        client.on_message = _on_message
    except Exception:
        pass

    def _on_disconnect(*_args):
        transport.mark_disconnected(reason="mqtt_client_on_disconnect")

    try:
        client.on_disconnect = _on_disconnect
    except Exception:
        pass


def _serialize_payload(payload):
    import json

    if isinstance(payload, str):
        return payload
    return json.dumps(dict(payload or {}), separators=(",", ":"))


def _payload_size(payload):
    try:
        return len(payload)
    except Exception:
        return 0


def _memory_snapshot():
    free_mem = -1
    mem_alloc = -1
    try:
        free_mem = int(gc.mem_free())
    except Exception:
        pass
    try:
        mem_alloc = int(gc.mem_alloc())
    except Exception:
        pass
    return free_mem, mem_alloc


def _memory_summary(snapshot):
    try:
        free_mem, mem_alloc = snapshot
    except Exception:
        free_mem = -1
        mem_alloc = -1
    return "free_mem={} mem_alloc={}".format(
        free_mem if free_mem >= 0 else "unknown",
        mem_alloc if mem_alloc >= 0 else "unknown",
    )


def _next_publish_index(adapter, transport):
    try:
        index = int(getattr(adapter, "published_index", 0) or 0)
    except Exception:
        index = 0
    try:
        if index < len(getattr(transport, "published_messages", ()) or ()):
            return max(0, index)
    except Exception:
        pass
    return None


def _next_startup_priority_publish_index(adapter, transport):
    """Return the next retained status publish before startup subscriptions."""
    start_index = _next_publish_index(adapter, transport)
    if start_index is None:
        return None
    messages = getattr(transport, "published_messages", ()) or ()
    for index in range(start_index, len(messages)):
        message = messages[index]
        if _is_startup_priority_publish(message):
            return index
    return None


def _is_startup_priority_publish(message):
    """Return True for retained identity/status topics that unblock startup."""
    if not bool(getattr(message, "retain", False)):
        return False
    topic = str(getattr(message, "topic", "") or "").strip().lower()
    if topic.startswith("homeassistant/"):
        return False
    return (
        topic.endswith("/meta")
        or "/meta/" in topic
        or topic.endswith("/status/heartbeat")
        or topic.endswith("/availability")
    )


def _is_retained_startup_meta_publish(message):
    """Return True for retained startup meta publishes needing chunked send."""
    if not bool(getattr(message, "retain", False)):
        return False
    topic = str(getattr(message, "topic", "") or "").strip().lower()
    if topic.startswith("homeassistant/"):
        return False
    return topic.endswith("/meta") or topic.endswith("/meta/switch")


def _should_collect_after_startup_meta_publish(message):
    """Return True when a successful retained device meta publish should GC."""
    if not bool(getattr(message, "retain", False)):
        return False
    topic = str(getattr(message, "topic", "") or "").strip().lower()
    if topic.startswith("homeassistant/"):
        return False
    return topic.endswith("/meta")


def _collect_garbage():
    try:
        gc.collect()
        return True
    except Exception:
        return False


def _drop_published_message(transport, index):
    try:
        del transport.published_messages[int(index)]
    except Exception:
        transport.compact(published_keep_from=int(index or 0) + 1)


def _slow_operation_threshold_ms(value):
    if value is None:
        return MQTT_SLOW_OPERATION_MS
    try:
        return int(value)
    except Exception:
        return MQTT_SLOW_OPERATION_MS


def _operation_is_slow(elapsed_ms, threshold_ms):
    try:
        return int(threshold_ms) > 0 and int(elapsed_ms) > int(threshold_ms)
    except Exception:
        return False


def _elapsed_ms(start_monotonic):
    """Return elapsed milliseconds since a monotonic start value."""
    try:
        return int((time.monotonic() - float(start_monotonic)) * 1000)
    except Exception:
        return -1


def _coerce_payload_text(message):
    if isinstance(message, (bytes, bytearray)):
        try:
            return bytes(message).decode("utf-8")
        except Exception:
            return bytes(message).decode("utf-8", errors="ignore")
    return str(message)
