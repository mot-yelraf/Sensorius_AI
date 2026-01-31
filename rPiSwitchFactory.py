"""Switch factory for creating concrete relay/MQTT backends from settings.

Flow:
1) rPiSwitchSettingsManager loads per-switch TOML into a SettingsWrapper.
2) rPiSwitchFactory reads the settings and instantiates the correct backend.
3) rPiSwitch wraps the created backend in a controller that tracks state,
   publishes MQTT events, and enforces safety rules.
"""

import board
import digitalio
from rPiUtils import printDM, debug_enabled
from rPiSwitchSettingsManager import SwitchSettingsManager

MODULE = "rPiSwitchFactory"
DEBUG = debug_enabled(MODULE)

SINGLE_DETECT_PIN = getattr(board, "D23", None)     # SINGLE IOT (active-high)
DUAL_DETECT_PIN = getattr(board, "D27", None)     # DUAL Switch (active-high)
ES_DETECT_PIN = getattr(board, "D5", None)     # Electronics-Salon (active-high)
WS_DETECT_PIN = getattr(board, "D12", None)  # Waveshare (active-low)

def _probe_grounded(pin_obj) -> bool:
    """Return True if the pin reads as grounded when pulled-up; safe no-op if N/A."""
    try:
        if pin_obj is None:
            return False
        dio = digitalio.DigitalInOut(pin_obj)
        dio.direction = digitalio.Direction.INPUT
        dio.pull = digitalio.Pull.UP
        grounded = (not dio.value)
        dio.deinit()
        return grounded
    except Exception:
        return False

def detect_switch_variant() -> dict:
    """
    Hardware probe → return a dict describing the detected board:
      {
        "template": "switch_1_relay" | "switch_2_relay" | "switch_3_relay",
        "en_bcm": 23|27|5|12,       # the enable BCM we will use/store
        "active": "high"|"low",     # relay polarity
        "channels": 1|2|3
      }
    Priority order:
      - Waveshare (WS_DETECT_PIN=D12) → 3-relay, active-low
      - Electronics-Salon (ES_DETECT_PIN=D5) → 3-relay, active-high
      - 2-relay board via D27 grounded → 2-relay, active-high
      - 1-relay board via D23 grounded → 1-relay, active-high
      - Fallback → 3-relay, active-high (safe default)
    """
    # 3-relay “families” with explicit detect pads:
    if _probe_grounded(WS_DETECT_PIN):
        return {"template": "switch_3_relay", "en_bcm": 12, "active": "low",  "channels": 3}
    if _probe_grounded(ES_DETECT_PIN):
        return {"template": "switch_3_relay", "en_bcm": 5,  "active": "high", "channels": 3}

    # Heuristics for 2- and 1-relay shields: treat their EN pin as a detect pad
    if _probe_grounded(DUAL_DETECT_PIN):
        return {"template": "switch_2_relay", "en_bcm": 27, "active": "high", "channels": 2}
    if _probe_grounded(SINGLE_DETECT_PIN):
        return {"template": "switch_1_relay", "en_bcm": 23, "active": "high", "channels": 1}

    # Fallback → 3-relay, active-high
    return {"template": "switch_3_relay", "en_bcm": 5, "active": "high", "channels": 3}

def _template_name_for_en_bcm(en_bcm: int) -> str:
    """Map known EN pins to your template names (default to 3-relay)."""
    mapping = {23: "switch_1_relay", 27: "switch_2_relay", 5: "switch_3_relay", 12: "switch_3_relay"}
    return mapping.get(int(en_bcm), "switch_3_relay")


class OneRelaySwitch:   

    def __init__(self, settings=None, controlPin=None, detectPin=None, reverse_logic=False, name=None, index=1):
        self.switch_index = index
        self.is_present = False
        self.reverse_logic = False
        self.controlPin = controlPin
        self.settings = settings
        self.device = settings.get("Switch", {}).get("DEVICE", "switch")
        self.serial_num = settings.get("Switch", {}).get("SERIAL_NUM", "unknown")
        self.switch_id = settings.get("Switch", {}).get("SWITCH_ID", "unknown")        
        self.location = settings.get("Switch", {}).get("SWITCH_LOCATION", "unknown")
        
        self.switches = []
        self.states = {}
    
        detect_pin = self.detect_relay_board()
        if detect_pin is None:
            if DEBUG:
                printDM("No relay board detected on known detect pins.", location=MODULE)
            return  # Exit early — switch is not present
        
        if detect_pin:
            self.reverse_logic = (detect_pin == WS_DETECT_PIN)
        self.is_present = True
        if DEBUG:
            printDM(f"Relay #{self.switch_index} detected, reverse logic = {self.reverse_logic}", location=MODULE)

        dio = digitalio.DigitalInOut(self.controlPin)
        dio.direction = digitalio.Direction.OUTPUT
        dio.value = True if self.reverse_logic else False  # ensure OFF at startup
        self.switches.append(dio)

        nameIndex = settings.get("Switch", {}).get(f"SWITCH_{self.switch_index}", f"{name}{self.switch_index}")
        self.states[nameIndex] = False

    def get_switch_names(self):
        return list(self.states.keys())

    def set_state(self, name: str, on: bool):
        if name in self.states:
            index = list(self.states.keys()).index(name)
            self.states[name] = on
            gpio_val = not on if self.reverse_logic else on
            self.switches[index].value = gpio_val
            printDM(f"[{name}] Relay #{self.switch_index} set to {'ON' if on else 'OFF'} → GPIO = {gpio_val}", location=MODULE)
            return True
        else:
            printDM(f"[{name}] Relay not found in state map!", location=MODULE)
        return False

    def get_state(self, name: str) -> bool:
        return self.states.get(name, False)

# --- helpers -------------------------------------------------------------

def _bool_active(level: str) -> bool:
    # "high" -> True; "low" -> False; default to True
    return str(level or "high").strip().lower() == "high"

def _board_pin_from_bcm(bcm: int):
    # Map BCM integer → board.D<BCM>, raise if unavailable
    name = f"D{int(bcm)}"
    if not hasattr(board, name):
        raise ValueError(f"No board pin for BCM {bcm}")
    return getattr(board, name)

def _get_sw(settings: dict, key: str, default=None):
    return (settings.get("Switch", {}) or {}).get(key, default)

def _iter_channel_defs(sw: dict, max_n: int = 8):
    """Yield normalized (index, label, bcm_pin) for channels found."""
    # normalize keys like SWITCH_2_Pin -> SWITCH_2_PIN
    def norm(k): return k.strip().upper().replace(" ", "_").replace("-","_")
    up = {norm(k): v for k, v in (sw or {}).items()}
    for n in range(1, max_n + 1):
        label = up.get(f"SWITCH_{n}", f"Relay {n}")
        pin   = up.get(f"SWITCH_{n}_PIN", None)
        if pin in ("", None):
            continue
        try:
            bcm = int(pin)
        except Exception:
            printDM(f"Ignoring non-integer SWITCH_{n}_PIN={pin}", location=MODULE)
            continue
        yield n, str(label), bcm

def ensure_switch_settings_for_host(host_switch_id: str, switch_loc: str | None = None) -> dict:
    """
    Ensure switch_settings/<host_switch_id>/switch.toml matches the detected hardware.
    - If missing: materialize from the detected factory template.
    - If present but EN/polarity disagree with detection: retarget using the template while preserving ID/location.
    Returns the loaded settings dict.
    """
    mgr = SwitchSettingsManager("switch_settings")
    path = mgr.get_path(host_switch_id)
    detected = detect_switch_variant()
    want_template = detected["template"]
    want_en = detected["en_bcm"]
    want_active = detected["active"]

    if not path.exists():
        # First time: create from the detected template
        mgr.materialize_from_template(host_switch_id, want_template, switch_loc=switch_loc)
        # also make sure ACTIVE matches detection
        mgr.update_setting(host_switch_id, "SWITCH_ACTIVE", want_active)
        mgr.update_setting(host_switch_id, "SWITCH_EN_PIN", want_en)
        return mgr.load(host_switch_id) or {}

    # Exists → check whether settings line up with detection
    doc = mgr.load(host_switch_id) or {}
    sw = (doc.get("Switch", {}) or {})
    cur_en = sw.get("SWITCH_EN_PIN")
    cur_active = (sw.get("SWITCH_ACTIVE", "") or "").strip().lower()
    has_any_channels = any(k.startswith("SWITCH_") and k.endswith("_PIN") for k in sw.keys())

    # If channels layout is missing/empty OR EN/polarity disagree → retarget
    need_retarget = (not has_any_channels) or (cur_en != want_en) or (cur_active != want_active)
    if need_retarget:
        mgr.retarget_to_template(host_switch_id, want_template)
        mgr.update_setting(host_switch_id, "SWITCH_ACTIVE", want_active)
        mgr.update_setting(host_switch_id, "SWITCH_EN_PIN", want_en)
        if DEBUG:
            printDM(f"[{host_switch_id}] Retargeted to {want_template} (EN={want_en}, ACTIVE={want_active})",
                    location=MODULE)
        return mgr.load(host_switch_id) or {}

    return doc

# --- GPIO (Pi) implementation -------------------------------------------

class LocalGPIOSwitch:
    def __init__(self, settings: dict, _persist_detection: bool = True):
        self.settings = settings or {}
        sw = self.settings.get("Switch", {}) or {}
        self.device     = sw.get("DEVICE", "switch")
        self.serial_num = sw.get("SERIAL_NUM", "unknown")
        self.switch_id  = sw.get("SWITCH_ID", "unknown")
        self.location   = sw.get("SWITCH_LOCATION", "unknown")
        self.active_state = _bool_active(sw.get("SWITCH_ACTIVE", "high"))
        self._persist_detection = bool(_persist_detection)

        # Always probe hardware; only persist if detected differs from file
        # decide polarity/EN *before* touching channel pins
        inferred_pin = self._detect_relay_board()
        if inferred_pin is not None:
            inferred_level = "low" if inferred_pin == WS_DETECT_PIN else "high"
            # If SWITCH_ACTIVE not set, seed it; either way, set runtime polarity
            if not str(sw.get("SWITCH_ACTIVE", "")).strip():
                sw["SWITCH_ACTIVE"] = inferred_level
            self.active_state = _bool_active(sw.get("SWITCH_ACTIVE", inferred_level))


            # Persist and then re-bind EN pin and re-apply OFF states
            if self._persist_detection:
                try:
                    self._persist_detected_enpin_and_polarity(inferred_pin, inferred_level)
                except Exception as e:
                    printDM(f"Persist detect failed: {e}", location=MODULE)


        # Optional bank enable pin
        self.en_pin = None
        # (re)create EN pin from latest settings and ensure it’s in the
        # "board enabled but channels OFF" (by default init) level for the current polarity
        # bring EN pin up now, using the final self.active_state
        self._ensure_en_pin_from_settings(sw)   
        # Create channels from config
        self.channels = []   # list of dicts: {"n", "name", "dio"}
        
        for n, label, bcm in _iter_channel_defs(sw):
            try:
                dio = digitalio.DigitalInOut(_board_pin_from_bcm(bcm))
                dio.direction = digitalio.Direction.OUTPUT
                # honor last state with polarity
                last = bool(sw.get(f"SWITCH_{n}_LAST_STATE", False))
                dio.value = (last if self.active_state else (not last))
                self.channels.append({"n": n, "name": label, "dio": dio})
            except Exception as e:
                printDM(f"Channel {n} init failed (BCM {bcm}): {e}", location=MODULE)
        
        # re-apply OFF/last_state to all channels using final polarity
        self._apply_initial_channel_states(sw)  
        

        # If no channels, try detect pins just to hint presence & polarity
        self.is_present = len(self.channels) > 0 or (inferred_pin is not None)
        
        if DEBUG:
            printDM(f"[{self.switch_id}] present={self.is_present} active_state={self.active_state} "
                    f"channels={len(self.channels)}", location=MODULE)

    # ----- helpers -----
    def _ensure_en_pin_from_settings(self, sw_block: dict) -> None:
        """(Re)create the EN pin from current settings and drive it to the 'enabled' level
        that keeps all relays OFF given the current active_state."""
        try:
            en = sw_block.get("SWITCH_EN_PIN", None)
            if en in (None, ""):
                # If previously created, drop it cleanly
                try:
                    if self.en_pin:
                        self.en_pin.deinit()
                except Exception:
                    pass
                self.en_pin = None
                return
            en_bcm = int(en)
            # tear down any previous binding before re-creating
            try:
                if self.en_pin:
                    self.en_pin.deinit()
            except Exception:
                pass
            dio = digitalio.DigitalInOut(_board_pin_from_bcm(en_bcm))
            dio.direction = digitalio.Direction.OUTPUT
            # EN logic: for active-high boards, True enables with relays off; for active-low, False
            dio.value = True if self.active_state else False
            self.en_pin = dio
        except Exception as e:
            printDM(f"SWITCH_EN_PIN init failed ({sw_block.get('SWITCH_EN_PIN')}): {e}", location=MODULE)
            self.en_pin = None

    def _apply_initial_channel_states(self, sw_block: dict) -> None:
        """Apply OFF/default states to all channels using the *final* active_state.
        We use SWITCH_n_LAST_STATE if present; default False (OFF)."""
        for ch in self.channels:
            n     = ch["n"]
            label = ch["name"]
            dio   = ch["dio"]
            last  = bool(sw_block.get(f"SWITCH_{n}_LAST_STATE", False))  # default OFF
            # Logical OFF == False; map to GPIO considering polarity:
            # active-high: OFF -> False ; active-low: OFF -> True
            desired_gpio = (last if self.active_state else (not last))
            try:
                dio.value = desired_gpio
                if DEBUG:
                    off_on = "ON" if last else "OFF"
                    printDM(f"[{self.switch_id}] {label} -> {off_on} (GPIO={dio.value})", location=MODULE)
            except Exception as e:
                printDM(f"[{self.switch_id}] init state apply failed for {label}: {e}", location=MODULE)

    def _persist_detected_enpin_and_polarity(self, inferred_pin, inferred_level: str) -> None:
        """
        Write-through the detected enable pin and polarity to switch.toml:
          - 1-relay  : EN=23, active=high
          - 2-relay  : EN=27, active=high
          - ES 3-rel : EN=5 , active=high
          - WS 3-rel : EN=12, active=low
        """
        # Map board pin → BCM number we store
        if inferred_pin == ES_DETECT_PIN:
            en_bcm = 5;  level = "high"
        elif inferred_pin == WS_DETECT_PIN:
            en_bcm = 12; level = "low"
        elif inferred_pin == DUAL_DETECT_PIN:
            en_bcm = 27; level = "high"
        elif inferred_pin == SINGLE_DETECT_PIN:
            en_bcm = 23; level = "high"
        else:
            return  # unknown detect pin; do nothing

        mgr = SwitchSettingsManager("switch_settings")
        sid = (self.switch_id or "").strip()
        if not sid or sid.lower() in {"unknown", "__probe__"}:
            return

        # Only write if changed to avoid extra IO
        doc = mgr.load(sid) or {}
        swb = doc.setdefault("Switch", {})
        cur_en  = swb.get("SWITCH_EN_PIN", None)
        cur_lvl = (swb.get("SWITCH_ACTIVE", "") or "").strip().lower()

        if cur_en != en_bcm:
            mgr.update_setting(sid, "SWITCH_EN_PIN", en_bcm)
            if DEBUG:
                printDM(f"[{sid}] SWITCH_EN_PIN -> {en_bcm}", location=MODULE)

        if cur_lvl != level:
            mgr.update_setting(sid, "SWITCH_ACTIVE", level)
            if DEBUG:
                printDM(f"[{sid}] SWITCH_ACTIVE -> {level}", location=MODULE)

        # Reflect in-memory immediately so runtime matches disk
        swb["SWITCH_EN_PIN"] = en_bcm
        swb["SWITCH_ACTIVE"] = level

        if DEBUG:
            printDM(f"[{self.switch_id}] present={self.is_present} active_state={self.active_state} "
                    f"channels={len(self.channels)}", location=MODULE)


    def _detect_relay_board(self):
        # Safe fallback presence probe; won’t throw if pins absent
        # Add DUAL_DETECT_PIN and SINGLE_DETECT_PIN here
        for dp in (ES_DETECT_PIN, WS_DETECT_PIN, DUAL_DETECT_PIN, SINGLE_DETECT_PIN):
            try:
                if dp is None:
                    continue
                det = digitalio.DigitalInOut(dp)
                det.direction = digitalio.Direction.INPUT
                det.pull = digitalio.Pull.UP
                grounded = not det.value
                det.deinit()
                if grounded:
                    return dp  # presence found
            except Exception:
                pass
        return None

    # API expected by SwitchController (or callers)
    def get_switch_names(self):
        return [c["name"] for c in self.channels]

    def get_state(self, name: str) -> bool:
        ch = next((c for c in self.channels if c["name"] == name), None)
        if not ch: return False
        # read back as logical on/off
        val = ch["dio"].value
        return bool(val) if self.active_state else (not bool(val))

    def set_state(self, name: str, on: bool) -> bool:
        ch = next((c for c in self.channels if c["name"] == name), None)
        if not ch:
            printDM(f"[{self.switch_id}] channel '{name}' not found", location=MODULE)
            return False
        ch["dio"].value = (on if self.active_state else (not on))
        if DEBUG:
            printDM(f"[{self.switch_id}] {name} -> {'ON' if on else 'OFF'} "
                    f"(GPIO={ch['dio'].value})", location=MODULE)
        return True

# --- MQTT (Pico2 W) proxy implementation ----------------------------------

class MQTTSwitch:
    """
    Proxy on the Pi for Pico2 W-based switches.
    No GPIO; publishes commands and (optionally) updates local state from MQTT ingest.
    """
    def __init__(self, settings: dict, mqtt_client=None):
        self.settings  = settings or {}
        sw = self.settings.get("Switch", {}) or {}
        self.device     = sw.get("DEVICE", "switch")
        self.serial_num = sw.get("SERIAL_NUM", " ")
        self.switch_id  = sw.get("SWITCH_ID", " ")
        self.location   = sw.get("SWITCH_LOCATION", "Unknown")
        self.is_present = True  # logical presence

        # channel labels (0–2 supported by your spec)
        self.channels = []
        for n in (1, 2):
            label = sw.get(f"SWITCH_{n}", f"Relay {n}")
            # If PIN omitted, the Pico2 W may still use its own defaults (22/5, 17/10).
            # Presence of a label is enough to expose the channel in UI.
            self.channels.append({"n": n, "name": str(label)})

        self.switch_topics = {}
        try:
            for ch in self.channels:
                label = (ch["name"] or "").strip()
                slug  = label.lower().replace(" ", "_")
                if self.switch_id and slug:
                    self.switch_topics[label] = f"switch/{self.switch_id}/{slug}"
        except Exception:
            pass

        # mqtt client (optional if you only render labels)
        self.mqtt = mqtt_client

    def get_switch_names(self):
        return [c["name"] for c in self.channels]

    def get_state(self, name: str) -> bool:
        # If you wire state sync via rPiMQTTIngest, return the latest cached value here.
        return False

    def set_state(self, name: str, on: bool) -> bool:
        ch = next((c for c in self.channels if c["name"] == name), None)
        if not ch:
            printDM(f"[{self.switch_id}] channel '{name}' not found", location=MODULE)
            return False
        if not self.mqtt:
            printDM(f"[{self.switch_id}] no MQTT client bound; skipping publish", location=MODULE)
            return False
        # Example topic; adapt to your convention
        topic = f"switch/{self.switch_id}/set/{ch['n']}"
        payload = "ON" if on else "OFF"
        try:
            self.mqtt.publish(topic, payload)
            if DEBUG:
                printDM(f"[{self.switch_id}] MQTT publish {topic}={payload}", location=MODULE)
            return True
        except Exception as e:
            printDM(f"[{self.switch_id}] MQTT publish failed: {e}", location=MODULE)
            return False

# --- factory -------------------------------------------------------------

def create_switch(settings=None, mqtt_client=None):
    sw = (settings or {}).get("Switch", {}) or {}
    typ = str(sw.get("TYPE", "pi")).strip().lower()
    if typ in ("picow", "pico2w"):
        return MQTTSwitch(settings=settings, mqtt_client=mqtt_client)
    # default: Pi GPIO
    return LocalGPIOSwitch(settings=settings)

# Back-compat helper (pure probe; no persistence)
def detect_relay_board():
    try:
        # Build a probe-only instance that won't write to disk
        inst = LocalGPIOSwitch({"Switch": {"SWITCH_ID": "__probe__"}}, _persist_detection=False)
        return inst._detect_relay_board()
    except Exception:
        return None
