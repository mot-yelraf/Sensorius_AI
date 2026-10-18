"""Pytest coverage for metric-name alias canonicalization.

These tests keep metric naming stable across UI, ingest, and storage paths by
verifying common aliases normalize to canonical names.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensorius.saiHtml import canonicalize_metric_name, get_gauge_config


def test_canonicalize_veml_ppfd_alias():
    cfg = get_gauge_config()
    assert canonicalize_metric_name("PPFD", cfg) == "Estimated PPFD"
    assert "Estimated PPFD" in cfg
    assert "Visible Light Intensity" in cfg


def test_canonicalize_soil_moisture_alias():
    cfg = get_gauge_config()
    assert canonicalize_metric_name("Soil Moisture", cfg) == "Soil Moisture"
    assert canonicalize_metric_name("Soil-Moisture", cfg) == "Soil Moisture"


def test_canonicalize_dewpoint_aliases():
    cfg = get_gauge_config()
    assert canonicalize_metric_name("Dewpoint Deficit", cfg) == "Dew Point Deficit"
    assert canonicalize_metric_name("dewVPD Risk", cfg) == "DewVPD Risk"


def test_soil_temperature_aliases_keep_fahrenheit_distinct():
    cfg = get_gauge_config()
    assert canonicalize_metric_name("Soil Temp_C", cfg) == "Soil Temperature"
    assert canonicalize_metric_name("soil temperature", cfg) == "Soil Temperature"
    assert canonicalize_metric_name("soil-temp_c", cfg) == "Soil Temperature"
    assert canonicalize_metric_name("Soil Temp_F", cfg) == "Soil Temp_F"
