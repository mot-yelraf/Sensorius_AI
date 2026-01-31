-- inspect_readings_timestamps.sql
-- Run with:  sqlite3 sensorius_data.db < inspect_readings_timestamps.sql

.headers on
.mode column

-- 1. Total row count
SELECT COUNT(*) AS total_rows FROM readings;

-- 2. Sample of newest rows (by timestamp string)
SELECT timestamp, sensor_id, metric, value
FROM readings
ORDER BY timestamp DESC
LIMIT 20;

-- 3. Count of "ISO-like" timestamps (YYYY-MM-DDTHH:MM:SS... pattern)
--    This is rough but good enough to see if things look ISO-ish.
SELECT COUNT(*) AS iso_like
FROM readings
WHERE timestamp LIKE '____-__-__T__:%';

-- 4. Count of "epoch-like" timestamps (numeric, no '-')
SELECT COUNT(*) AS epoch_like
FROM readings
WHERE timestamp GLOB '[0-9]*'
  AND instr(timestamp, '-') = 0;

-- 5. Sample of epoch-like rows
SELECT timestamp, sensor_id, metric, value
FROM readings
WHERE timestamp GLOB '[0-9]*'
  AND instr(timestamp, '-') = 0
ORDER BY timestamp
LIMIT 20;

-- 6. Sample of ISO-like rows (if present)
SELECT timestamp, sensor_id, metric, value
FROM readings
WHERE timestamp LIKE '____-__-__T__:%'
ORDER BY timestamp
LIMIT 20;