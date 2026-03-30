"""Small query helper for gas and humidity slices in the runtime database.

This script is intended for quick local analysis of stored sensor readings when
checking calibration or trend behavior outside the web UI.
"""

import sqlite3
import csv

# --- Configurable Variables ---
db_path = r"/home/twfarley/saiSensorius/sensor_data.db"
output_csv_path = r"/home/twfarley/saiSensorius/gas_rh_dump.csv"
sensor_id = "AQI_airco"  # Change to match your target sensor ID

# --- Connect and Query ---
query = """
SELECT r1.timestamp, r1.value AS rel_humidity, r2.value AS gas_resistance
FROM readings r1
JOIN readings r2
  ON r1.timestamp = r2.timestamp AND r1.sensor_id = r2.sensor_id
WHERE r1.metric = 'Rel-Humidity' AND r2.metric = 'Gas'
  AND r1.sensor_id = ?
ORDER BY r1.timestamp;
"""

with sqlite3.connect(db_path) as conn, open(output_csv_path, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["timestamp", "rel_humidity", "gas_resistance"])
    for row in conn.execute(query, (sensor_id,)):
        writer.writerow(row)

print(f"[INFO] Exported gas/RH data to {output_csv_path}")
