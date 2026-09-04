"""Cover ordering for integrated biodynamic cache warming.

Persistent cache behavior is exercised through the integrated SQLite calendar
service; this module retains the shared prewarm-order contract.
"""

from __future__ import annotations

from datetime import date

import sensorius.saiBiodynamics as saiBiodynamics

def test_biodynamic_prewarm_month_anchors_are_ordered_by_ui_value():
    anchors = saiBiodynamics.biodynamic_prewarm_month_anchors(
        date(2026, 7, 15),
        past_months=3,
        future_months=4,
    )

    assert [item.isoformat() for item in anchors] == [
        "2026-07-01",
        "2026-06-01",
        "2026-08-01",
        "2026-09-01",
        "2026-10-01",
        "2026-11-01",
        "2026-05-01",
        "2026-04-01",
    ]
