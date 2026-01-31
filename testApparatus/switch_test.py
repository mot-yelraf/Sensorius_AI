# switch_test.py

import time
import sys
import board
import digitalio

# ----- Configuration -----
DEFAULT_DETECT_PIN = board.D5      # for active-high, Electronics-Salon RPi Relay Board
ALTERNATE_DETECT_PIN = board.D12   # for active-low, Waveshare RPi Relay Board
TOGGLE_PIN = board.D26             # GPIO21 (Board pin 40)
TOGGLE_INTERVAL = 5.0              # seconds
DEBUG = True

# ----- Helper Functions -----
def detect_relay_board():
    for dp in [DEFAULT_DETECT_PIN, ALTERNATE_DETECT_PIN]:
        detect = digitalio.DigitalInOut(dp)
        detect.direction = digitalio.Direction.INPUT
        detect.pull = digitalio.Pull.UP
        grounded = not detect.value
        detect.deinit()
        if grounded:
            return dp
    return None

def cleanup_and_exit(dio, reverse_logic=False):
    print("\n[INFO] Caught interrupt, cleaning up GPIO...")
    if dio:
        dio.value = True if reverse_logic else False  # ensure OFF at shutdown
        dio.deinit()
    sys.exit(0)

# ----- Main Loop -----
print(f"[INFO] Toggling GPIO {TOGGLE_PIN} every {TOGGLE_INTERVAL} seconds (CTRL+C to stop)")
dio = None
reverse_logic = False

try:
    detect_pin = detect_relay_board()
    if detect_pin is None:
        print("[ERROR] No relay board detected on known detect pins.")
        cleanup_and_exit(dio)

    reverse_logic = (detect_pin == ALTERNATE_DETECT_PIN)
    if DEBUG:
        print(f"[INFO] Relay board detected on {detect_pin}, reverse logic = {reverse_logic}")    

    dio = digitalio.DigitalInOut(TOGGLE_PIN)
    dio.direction = digitalio.Direction.OUTPUT
    dio.value = True if reverse_logic else False  # ensure OFF at startup
    state = False

    while True:
        state = not state
        gpio_val = not state if reverse_logic else state
        dio.value = gpio_val
        print(f"[INFO] GPIO {TOGGLE_PIN} set to {'HIGH' if gpio_val else 'LOW'}")
        time.sleep(TOGGLE_INTERVAL)

except KeyboardInterrupt:
    cleanup_and_exit(dio, reverse_logic)
