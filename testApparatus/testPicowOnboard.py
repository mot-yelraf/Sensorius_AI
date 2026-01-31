# test_picow_onboarding.py

import asyncio
import json
import requests
from rPiUtils import get_time_settings
from connect_and_configure_sensor import (
    original_pi_info,
    PICOW_AP_SSID,
    PICOW_AP_PASSWORD,
    ITAOT_URL,
    UPDATE_URL,
    connect_to_ap,
    reconnect_to_pi,
    run_nmcli,
)

# ---- Injected Test Identity ----
HOSTNAME = "co2-test1"
LOCATION = "Lab"
DEVICE = "CO2"

# ---- Format Settings Payload ----
def generate_settings_payload():
    sensor_id = HOSTNAME.split("-")[1]
    time_cfg = get_time_settings()

    return [
        {"section": "Network", "key": "SSID",      "value": original_pi_info.get("ssid", "")},
        {"section": "Network", "key": "PASSWORD",  "value": original_pi_info.get("password", "")},
        {"section": "Network", "key": "HOSTNAME",  "value": HOSTNAME},
        {"section": "Sensor",  "key": "DEVICE",    "value": DEVICE},
        {"section": "Sensor",  "key": "SENSOR_ID", "value": sensor_id},
        {"section": "Sensor",  "key": "LOCATION",  "value": LOCATION},
        {"section": "Sensor",  "key": "BROKER",    "value": original_pi_info.get("broker", "")},
        {"section": "Time",    "key": "tz",        "value": time_cfg["tz"]},
        {"section": "Time",    "key": "tzOffset",  "value": time_cfg["tzOffset"]},
        {"section": "Time",    "key": "tzName",    "value": time_cfg["tzName"]},
    ]

# ---- Robust Fetch /itaot With Retry ----
async def fetch_itaot_with_retry(max_attempts=3, delay_sec=2):
    for attempt in range(1, max_attempts + 1):
        print(f"[INFO] Attempting to fetch /itaot (Attempt {attempt}/{max_attempts})...")
        try:
            response = requests.get(ITAOT_URL, timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[WARN] Attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(delay_sec)
    print("[ERROR] Failed to get /itaot after multiple attempts.")
    return None

# ---- Main Routine ----
async def run_onboarding_test():
    print("[TEST] Connecting to PicoW AP...")
    if not connect_to_ap(PICOW_AP_SSID, PICOW_AP_PASSWORD):
        print("[ERROR] Could not connect to Sensor_Setup")
        return

    await asyncio.sleep(1)

    print("[INFO] Attempting to fetch /itaot data from PicoW...")
    info = await fetch_itaot_with_retry()
    if not info:
        print("[ERROR] Aborting test due to missing /itaot response.")
        return

    hostname = info.get("hostname")
    mqtt_topic = info.get("mqtt_topic")
    print(f"[INFO] PicoW identity: hostname={hostname}, topic={mqtt_topic}")

    print("[INFO] Preparing settings payload...")
    new_settings = generate_settings_payload()

    try:
        print("[TEST] Posting test settings to PicoW...")
        headers = {"Content-Type": "application/json"}
        response = requests.post(UPDATE_URL, headers=headers, data=json.dumps(new_settings), timeout=5)

        if response.status_code == 200:
            print("[TEST] ✅ PicoW settings update succeeded.")
        else:
            print(f"[WARN] ❌ PicoW rejected settings: {response.status_code} — {response.text}")
            return
    except Exception as e:
        print(f"[ERROR] Exception during settings POST: {e}")
        return

    await asyncio.sleep(2)

    print("[TEST] Reconnecting to Pi network...")
    if reconnect_to_pi():
        print("[TEST] ✅ Successfully reconnected to Pi network.")
    else:
        print("[ERROR] ❌ Failed to reconnect to Pi Wi-Fi.")

if __name__ == "__main__":
    asyncio.run(run_onboarding_test())
