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

## 🤖 4. Module Descriptions

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

## 5. GPIO Pin Assignments

| Purpose             | GPIO Pin | Physical Pin | Notes                                |
| ------------------- | -------- | ------------ | ------------------------------------ |
| Relay 1             | GPIO20   | Pin 38       | Configurable via TOML                |
| Detect Pin          | GPIO5    | Pin 29       | GND relay board is connected |
| I2C\_1 SDA (Sensor) | GPIO2    | Pin 3        | Default I2C bus                      |
| I2C\_1 SCL (Sensor) | GPIO3    | Pin 5        |                                      |
| I2C\_0 SDA1 (Plant) | GPIO0    | Pin 27       | Dedicated for VPDPlant sensor        |
| I2C\_0 SCL1 (Plant) | GPIO1    | Pin 28       |                                      |

---

## 6. setup.sh Instructions (Raspberry Pi)

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

## 7. macOS Setup (Hub + MQTT Only)

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

## 8. Windows 11 Setup (Hub + MQTT Only)

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

---

## 8. Supported Sensors & Metrics

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

---

### VPDSensor (based on **BME280**)

* **I2C Bus**: I2C\_1 (GPIO2/SDA, GPIO3/SCL)
* **Metrics Stored**:

  * `Temperature` — °C
  * `Temperature_F` — °F
  * `Rel-Humidity` — % (relative)
  * `Humidity` — g/m³ (absolute)
  * `Ambient VPD` — kPa
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
  * `Baro-Pressure` — hPa

  **Plant probe additions (I2C\_0):**

  * `Temperature Plant` — °C
  * `Temperature_F Plant` — °F
  * `Rel-Humidity Plant` — %
  * `Humidity Plant` — g/m³
  * `Plant VPD` — kPa
  * `Baro-Pressure Plant` — hPa

---

> All timestamps are in UTC. `tz`, `tzOffset`, and `tzName` are pushed to the device and used in the UI to localize time.

---

## 9. Attribution

* **System Architecture**: TW Farley
* **Implementation and Coding**: TW Farley and ChatGPT/Codex
