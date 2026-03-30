"""Manual relay board cycle test for locally attached switch hardware.

Use this script for bench testing relay channels outside the main application
when validating board wiring and polarity behavior.
"""

import asyncio
from saiSwitchFactory import ThreeRelaySwitch
from saiUtils import printDM

# --- Configuration ---
REPEAT_DELAY_SEC = 1.0  # time each relay stays on before switching
CYCLE_DELAY_SEC = 1.0   # delay between cycles

# --- Dummy minimal settings for test (no triggers needed) ---
mock_settings = {
    "Switch": {
        "DEVICE": "SwitchTest",
        "SWITCH_ID": "test01",
        "LOCATION": "TestBench",
        "SWITCH_1": "Fan",
        "SWITCH_2": "Light",
        "SWITCH_3": "Pump"
    }
}

async def cycle_relays_forever():
    switch = ThreeRelaySwitch(mock_settings)
    relay_names = switch.get_switch_names()

    printDM(f"Starting relay cycle test: {relay_names}", location="RelayTest")

    while True:
        for name in relay_names:
            printDM(f"Turning ON: {name}", location="RelayTest")
            switch.set_state(name, True)
            await asyncio.sleep(REPEAT_DELAY_SEC)
            printDM(f"Turning OFF: {name}", location="RelayTest")
            switch.set_state(name, False)
        await asyncio.sleep(CYCLE_DELAY_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(cycle_relays_forever())
    except KeyboardInterrupt:
        printDM("Relay test interrupted by user.", location="RelayTest")
