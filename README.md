# Sensorius

**Environmental Sensing + Automation Hub**

Sensorius Automatio Instrumentorum (Sensorius AI or Sensorius) is a modular, Python-based system for managing environmental sensors and controlling relays via MQTT. It features a real-time web dashboard, automated onboarding for new devices, and robust data logging and visualization. Sensorius can be set up on Raspberry Pi (with directly connected sensors & Nodus devices) or setup on macOS, Windows 10/11, and Linux using Nodus sensors and switches. 

Sensorius & Nodus (see my cPyNodus project) were developed to automate greenhouse operations, but there are other applications requiring straight forward sense and control features, using Sensorius' switch automations; and Sensorius' unique system-wide sensor calibration, e.g. 'System Calibration' can assist in the task of calibrating the systems temperature and humidity sensors.

---

## 1. Program Description

Sensorius supports:

* Onboarding of Wi-Fi connected sensors and switches using the Pico 2 W (required for Nodus)
* MQTT-based communication between sensors/switches and the Pi hub
* Data logging into a local SQLite database
* A FastAPI web UI for dashboards, graphs, and device configuration
* Relay control with scriptable triggers and overrides

---

## 2. System Architecture

```
                     +------------------------+        +------------------+
                     |      Sensorius Hub     |<------>| Home Assistant   |
                     |  (FastAPI + MQTT + DB) |        |     (HA)         |
                     +------------------------+        +------------------+
                         ^            ^          
                         |            |
                 +-------+            +-------+
                 |                            |
                 v                            v
         +---------------+            +----------------+
         | Nodus Sensor  |            | Nodus Switch   |
         |  (e.g. CO2)   |            |  IoT Relay     |
         +---------------+            +----------------+
                |                              |
        MQTT pub/sub                      MQTT pub/sub 
```

---

## 3. Program Flow

### Sensor Onboarding (Pico 2 W)

1. Pico 2 W boots into AP mode: `Sensor_Setup`
2. Sensorius connects via `connect_and_configure_sensor.py`
3. Fetches `/itaot` to get hostname and topic
4. Pushes Wi-Fi + sensor config as JSON
5. Pico 2 W reboots and publishes data to Pi MQTT broker

### Switch Onboarding

1. Identical AP onboarding via `connect_and_configure_switch.py`
2. Config file includes GPIO pin, location, and label info
3. Switch logic and relays initialized on the Pi

---

## 4. Module Descriptions

| Module                            | Purpose                                       |
| --------------------------------- |--------------------------------------------- |
| `saiMQTTClient.py`                | Publishes onboard sensor data to MQTT         |
| `saiMQTTIngest.py`                | Subscribes to sensor topics, stores in DB     |
| `saiWebRoutes.py`                 | FastAPI routes for dashboard, graph, setupUI |
| `saiSwitchFactory.py`             | Detects relays, wraps GPIO output control     |
| `saiTaskSupervisor.py`            | Supervises and restarts async tasks           |
| `saiWatchdog.py`                  | Monitors heartbeats and exits on timeout      |
| `connect_and_configure_sensor.py` | Automates onboarding for Pico 2 W sensors     |
| `connect_and_configure_switch.py` | Onboards switch-only Pico 2 W devices         |
| `settings_switch.toml`            | Sample configuration for onboarded switch     |

---

## 5. setup.sh Instructions (Raspberry Pi)

Run `setup.sh` to prepare the Pi environment:

```bash
chmod +x setup.sh
sudo ./setup.sh
```

This script:

* Installs system and Python dependencies
* Enables I2C and sets regional Wi-Fi settings
* Installs and enables a systemd service (`sensorius.service`)
* Configures the hostname and timezone

---

## 6. macOS Setup (Hub + MQTT Only)

macOS runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on macOS.

Use one of the macOS setup scripts:

```bash
chmod +x setup_mac.sh
./setup_mac.sh
```

Or with `uv`:

```bash
chmod +x setup_mac_uv.sh
./setup_mac_uv.sh
```

Notes:

* These scripts install Python 3.13.5 and create a local `.venv`.
* Mosquitto is installed and configured with anonymous access on port 1883.
* GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
* If `pywebview` is not installed, Sensorius will continue headless.
* Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

---

## 7. Windows 11 Setup (Hub + MQTT Only)

Windows runs Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported on Windows.

Use one of the Windows setup scripts (run in an elevated PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_win11.ps1
```

Or with `uv`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup_win11_uv.ps1
```

Notes:

* These scripts use `winget` and require running PowerShell as Administrator.
* Python 3.13.5 is installed via `pyenv-win` (pip script) or `uv` (uv script).
* Mosquitto is installed and configured with anonymous access on port 1883.
* GUI is optional. Set `SENSORIUS_GUI=0` to force headless mode.
* If `pywebview` is not installed, Sensorius will continue headless.
* Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

---

## 8. Linux Setup (Debian/Ubuntu, Hub + MQTT Only)

Linux non-Pi hosts run Sensorius as an MQTT hub and web UI only. Directly connected sensors and GPIO are not supported in this setup path.

Use the Linux setup script:

```bash
chmod +x setup_linux.sh
./setup_linux.sh
```

Notes:

* Uses `apt` to install precompiled system packages (`python3`, `mosquitto`, etc.).
* Installs Python dependencies from `setup_reqs_linux.txt`.
* Defaults to wheel-only Python installs (`PIP_ONLY_BINARY=1`) to avoid source builds.
* Set `INSTALL_PYWEBVIEW=0` to skip pywebview and force headless mode.
* Access the UI in a browser at `http://127.0.0.1:8000` (or `http://<host-ip>:8000` from another device).

---

## 9. Application Startup

### Manual Start

```bash
python3 Sensorius.py
```

### Enable and Start as Service

```bash
sudo systemctl enable sensorius.service
sudo systemctl start sensorius.service
```

### Logging and Debug Environment Variables

Use a project `.env` file as the primary configuration method.
This is the recommended approach for both manual runs and service deployments.

Create or edit `.env` in the project root:

```env
# -----------------------------
# Sensorius runtime .env file
# -----------------------------

# Log verbosity: DEBUG, INFO, WARNING, ERROR
SENSORIUS_LOG_LEVEL=INFO

# Enable rotating file logging (true/false)
SENSORIUS_FILE_LOG=false

# Log file path/name used when file logging is enabled
SENSORIUS_LOG_FILE=sensorius.log

# Show low-level HTTP client/server debug logs (true/false)
SENSORIUS_HTTP_DEBUG=false

# Debug module filter:
# - comma-separated module names, or
# - ALL
SENSORIUS_DEBUG_MODULES=Sensorius,saiSensor,saiMQTTIngest,saiHtml,saiSwitch,saiWebRoutes

# HTTP bind host/port for the web app
SENSORIUS_HTTP_HOST=0.0.0.0
SENSORIUS_HTTP_PORT=8000

# GUI behavior:
# - empty => auto detect
# - 1/true/yes/on => force GUI
# - 0/false/no/off => force headless
SENSORIUS_GUI=

# Watchdog timing controls
SENSORIUS_WATCHDOG_TIMEOUT_SEC=71
SENSORIUS_WATCHDOG_LOOP_INTERVAL_SEC=10.0
SENSORIUS_WATCHDOG_JITTER_SEC=0.8

# Garbage collector scheduling controls
SENSORIUS_GC_ENABLED=true
SENSORIUS_GC_INTERVAL_SEC=29
SENSORIUS_GC_JITTER_SEC=0.7
SENSORIUS_GC_MIN_SLEEP_SEC=1.0
SENSORIUS_GC_FULL_EVERY_N=10

# Optional API key for protected web endpoints
SAI_WEB_API_KEY=

# Linux display/backend hints used by GUI launch/service setup
DISPLAY=
WAYLAND_DISPLAY=
GDK_BACKEND=x11
WEBKIT_DISABLE_COMPOSITING_MODE=1
```

Temporary shell overrides (session-only) are still supported, for example:

```bash
export SENSORIUS_LOG_LEVEL=DEBUG
export SENSORIUS_GUI=0
```

---

## 10. GPIO Pin Assignments

### Supported Relay Configurations (from `switch_settings/factory/`)

| Configuration         | Enable GPIO (Physical) | Switch   | GPIO (Physical) |
| --------------------- | ---------------------- | -------- | --------------- |
| `switch_1_relay.toml` | GPIO23 (Pin 16)        | Switch 1 | GPIO26 (Pin 37) |
|                       |                        |          |                 |
| `switch_2_relay.toml` | GPIO27 (Pin 13)        | Switch 1 | GPIO26 (Pin 37) |
| `switch_2_relay.toml` |                        | Switch 2 | GPIO20 (Pin 38) |
|                       |                        |          |                 |
| `switch_3_relay.toml` | GPIO5 (Pin 29)         | Switch 1 | GPIO26 (Pin 37) |
| `switch_3_relay.toml` |                        | Switch 2 | GPIO20 (Pin 38) |
| `switch_3_relay.toml` |                        | Switch 3 | GPIO21 (Pin 40) |

### Sensor I2C Pins

| Purpose             | GPIO Pin | Physical Pin | Notes                                |
| ------------------- | -------- | ------------ | ------------------------------------ |
| I2C\_1 SDA (Sensor) | GPIO2    | Pin 3        | Default I2C bus                      |
| I2C\_1 SCL (Sensor) | GPIO3    | Pin 5        |                                      |
| I2C\_0 SDA1 (Plant) | GPIO0    | Pin 27       | Dedicated for VPDPlant sensor        |
| I2C\_0 SCL1 (Plant) | GPIO1    | Pin 28       |                                      | 

---

## 11. Supported Sensors & Metrics

Each sensor defines its own `self.measurements` list, which determines the exact metrics written to the database. Each metric is timestamped and stored in `sensor_data.db`.

---

### AQISensor (based on **BME680**)

* **I2C Bus**: I2C\_1 (GPIO2/SDA, GPIO3/SCL)
* **Metrics Stored**:

  * `Temperature` — °C
  * `Temperature_F` — °F
  * `Rel-Humidity` — % (relative)
  * `Humidity` — g/m³ (absolute)
  * `Air Quality` — AQI (derived from gas resistance)
  * `Ambient VPD` — kPa
  * `Dew-Point` — °C
  * `Dew-Point_F` — °F
  * `Dewpoint Depression` — °C
  * `DewVPD Risk` — %
  * `Baro-Pressure` — hPa

---

### CO2Sensor (based on **SCD30** or **SCD4x**)

* **I2C Bus**: I2C\_1 (GPIO2/SDA, GPIO3/SCL)
* **Metrics Stored**:

  * `CO2` — ppm
  * `Temperature` — °C
  * `Temperature_F` — °F
  * `Rel-Humidity` — % (relative)
  * `Humidity` — g/m³ (absolute)
  * `Ambient VPD` — kPa
  * `Dew-Point` — °C
  * `Dew-Point_F` — °F
  * `Dewpoint Depression` — °C
  * `DewVPD Risk` — %

---

### VPDSensor (based on **BME280**)

* **I2C Bus**: I2C\_1 (GPIO2/SDA, GPIO3/SCL)
* **Metrics Stored**:

  * `Temperature` — °C
  * `Temperature_F` — °F
  * `Rel-Humidity` — % (relative)
  * `Humidity` — g/m³ (absolute)
  * `Ambient VPD` — kPa
  * `Dew-Point` — °C
  * `Dew-Point_F` — °F
  * `Dewpoint Depression` — °C
  * `DewVPD Risk` — %
  * `Bar-Pressure` — hPa

---

### VPDPlantSensor (dual **BME280** on I2C\_1 and I2C\_0)

* **I2C Buses**:

  * **Ambient**: I2C\_1 (GPIO2/SDA, GPIO3/SCL)
  * **Plant Probe**: I2C\_0 (GPIO0/SDA1, GPIO1/SCL1)

* **Metrics Stored**:

  * `Temperature` — °C (ambient)
  * `Temperature_F` — °F
  * `Rel-Humidity` — %
  * `Humidity` — g/m³
  * `Ambient VPD` — kPa
  * `Dew-Point` — °C
  * `Dew-Point_F` — °F
  * `Dewpoint Depression` — °C
  * `DewVPD Risk` — %
  * `Baro-Pressure` — hPa

  **Plant probe additions (I2C\_0):**

  * `Temperature Plant` — °C
  * `Temperature_F Plant` — °F
  * `Rel-Humidity Plant` — %
  * `Humidity Plant` — g/m³
  * `Plant VPD` — kPa
  * `Plant Dew-Point` — °C
  * `Plant Dew-Point_F` — °F
  * `Plant Dewpoint Depression` — °C
  * `Plant DewVPD Risk` — %
  * `Baro-Pressure Plant` — hPa

---

> All timestamps are in UTC. `tz`, `tzOffset`, and `tzName` are pushed to the device and used in the UI to localize time.

---

## 12. Supported Switches

Sensorius supports:

* Directly connected switches (up to 3 relays)
* Nodus devices with switches enabled
Relay-capable configurations include:

* Single relay (individual relay control)
* 1-relay hat configuration
* 2-relay hat configuration
* 3-relay hat configuration

Switch channels are exposed in the UI and can be controlled manually, by automation rules, or by MQTT-connected workflows.

---

## 13. Switch Automations

Switch automations support:

* Rule-level enable/disable (Basic and Advanced rules)
* Sensor + metric threshold conditions (for example: `Temperature_F > 82`)
* Threshold hysteresis and minimum interval timing to reduce relay chatter
* Time-of-day windows (`start` / `end`) and day-based scheduling (`days` in Advanced rules)
* Timer-based schedules (`duration_min`, `freq_hours`) for periodic ON windows

---

## 14. Attribution

* **System Architecture**: TW Farley
* **Implementation and Coding**: TW Farley and ChatGPT/Codex
