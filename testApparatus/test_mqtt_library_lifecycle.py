"""Check the installed Paho API and retry thread without a live broker.

A separate interpreter prevents other tests' module-level MQTT stubs from
masking mismatches with the pinned production dependency.
"""

import subprocess
import sys
from pathlib import Path


def test_real_paho_retries_first_connection_and_stops(tmp_path):
    code = r'''
import asyncio
import threading
import time
from unittest.mock import patch
import paho.mqtt.client as mqtt
from sensorius.saiMQTTIngest import saiMQTTIngest
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="sensorius-test")
started = threading.Event()
attempts = []
def reconnect():
    attempts.append(1)
    if len(attempts) == 1:
        raise OSError("simulated unavailable broker")
    client._state = mqtt._ConnectionState.MQTT_CS_CONNECTED
    started.set()
    return mqtt.MQTT_ERR_SUCCESS
obj = object.__new__(saiMQTTIngest)
obj.client = obj.ha_client = client
obj._connected_evt = obj._ha_connected_evt = asyncio.Event()
obj._started = False
obj.broker = "unused.invalid"
obj.port = 1883
with patch.object(client, "reconnect", reconnect), patch.object(client, "_reconnect_wait", lambda: time.sleep(0.01)), patch.object(client, "_loop", lambda timeout: time.sleep(0.01) or mqtt.MQTT_ERR_SUCCESS):
    asyncio.run(obj.start())
    try:
        assert started.wait(3), attempts
        assert client._thread.is_alive()
        assert client.is_connected()
    finally:
        obj.stop()
    assert client._thread is None
    assert not obj._started
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
