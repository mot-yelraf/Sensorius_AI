import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Add this line
import argparse
import os
import logging

# Configuration
DB_PATH = os.getenv("DB_PATH", "sensor_data.db")
DAYS_TO_KEEP = int(os.getenv("DAYS_TO_KEEP", 30))
LOCAL_TIMEZONE = ZoneInfo("America/Chicago")  # <-- Set your local timezone here

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

def prune_old_records(db_path, days_to_keep, dry_run=False):
    # Use local timezone to match logged timestamps
    cutoff_dt = datetime.now(LOCAL_TIMEZONE) - timedelta(days=days_to_keep)
    cutoff = cutoff_dt.isoformat()

    logging.info(f"Pruning records older than {cutoff}")

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM readings WHERE timestamp < ?", (cutoff,))
            count = cursor.fetchone()[0]
            logging.info(f"Records to prune: {count}")

            if not dry_run and count > 0:
                cursor.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff,))
                conn.commit()
                logging.info("Pruning completed.")
            elif dry_run:
                logging.info("Dry run mode. No records deleted.")

    except Exception as e:
        logging.error(f"Error pruning data: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prune old sensor data from SQLite database.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate pruning without deleting records")
    parser.add_argument("--days", type=int, default=DAYS_TO_KEEP, help="Days of data to retain")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to SQLite database file")
    args = parser.parse_args()

    prune_old_records(args.db, args.days, args.dry_run)

