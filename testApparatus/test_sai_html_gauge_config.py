"""Focused tests for dashboard gauge metadata."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sensorius.saiHtml import APP_VERSION, get_gauge_config, render_dashboard, render_graph_modal


def test_soil_fertility_index_gauge_config_uses_contract_scale_and_zones():
    cfg = get_gauge_config()["Soil Fertility Index"]

    assert cfg["unit"] == "%"
    assert cfg["min"] == 0
    assert cfg["max"] == 100
    assert cfg["ticks"] == [0, 25, 50, 75, 100]
    assert cfg["zones"] == [
        {"strokeStyle": "#f00", "min": 0, "max": 50},
        {"strokeStyle": "#ffcc00", "min": 50, "max": 75},
        {"strokeStyle": "#66cc66", "min": 75, "max": 100},
    ]


def test_fullscreen_graph_uses_soil_fertility_gauge_zone_backgrounds():
    html = "".join(render_graph_modal(switch_installed=False))

    assert "const GRAPH_GAUGE_CONFIG = " in html
    assert '"Soil Fertility Index"' in html
    assert "const gaugeZonesBackgroundGraph" in html
    assert "soilFertilityGaugeZones(graphMetricNameFromKey(k))" in html
    assert "gaugeZonesBackgroundGraph: { zonesByAxis: gaugeZonesByAxis }" in html
    assert "y1Opts.min = gaugeAxisBounds.y1.min" in html


def test_fullscreen_multiday_graph_marks_every_midnight_on_x_axis():
    html = "".join(render_graph_modal(switch_installed=False))

    assert "const useDailyXAxis = xSpanMs > (24 * 3600 * 1000);" in html
    assert "unit: useSixHourXAxis ? 'hour' : (useDailyXAxis ? 'day' : 'hour')" in html
    assert "autoSkip: !useDailyXAxis" in html
    assert "if (isMidnight(tval)) return fmtDate(tval);" in html
    assert "return isMidnight(v) ? 1.5 : 1;" in html


def test_fullscreen_graph_under_ten_days_adds_six_hour_markers():
    html = "".join(render_graph_modal(switch_installed=False))

    assert "const useSixHourXAxis = useDailyXAxis && xSpanMs < (10 * 24 * 3600 * 1000);" in html
    assert "afterBuildTicks: function(axis){" in html
    assert "const remainder = cursor.getHours() % 6;" in html
    assert "sixHourTicks.push({ value: cursor.getTime() });" in html
    assert "cursor.setHours(cursor.getHours() + 6);" in html
    assert "stepSize: useSixHourXAxis ? 6 : 1" in html
    assert "return useSixHourXAxis ? fmtDayMarkerTime(tval) : fmtTime(tval);" in html
    assert ": (useSixHourXAxis ? 'rgba(0,0,0,0.16)' : 'rgba(0,0,0,0.1)');" in html


def test_fullscreen_graph_defines_pressure_helper_in_modal_script_scope():
    html = "".join(render_graph_modal(switch_installed=False))

    helper = "function graphPressureMetric(metric){"
    call = "pressureMetric: graphPressureMetric(graphMetricNameFromKey(k))"
    assert helper in html
    assert call in html
    assert html.index(helper) < html.index(call)
    assert "pressureMetric: pressureTrendMetric(" not in html


def test_fullscreen_graph_modal_has_astral_selector_and_sky_panel():
    html = "".join(render_graph_modal(switch_installed=False))

    assert 'id="fullscreen_graph_dashboard" class="button black"' in html
    assert 'title="Return to dashboard"' in html
    assert "top:1rem;" in html
    assert "left:1rem;" in html
    assert "bottom:1rem;left:50%" not in html
    assert "Close full screen graph" not in html
    assert "<select id='astral_select' title='Astral graph selection'>" in html
    assert "<option value='sun_moon'>Sun &amp; Moon</option>" in html
    assert "value='14d'" in html
    assert "value='30d'" in html
    assert "value='60d'" in html
    assert "value='90d'" in html
    assert "Max range:" not in html
    assert 'id="fullscreen_astral_graph"' in html
    assert "function drawFullscreenAstralGraph(payload, xMin, xMax)" in html
    assert "flex:0 0 clamp(120px, 18vh, 190px);" in html
    assert "#fullscreen_graph_container.has-astral{ padding-bottom:3.5rem; }" in html
    assert "const padB = 34;" in html
    assert "const buildSmoothAstralKeys = (points) => {" in html
    assert "key.s = sign * Math.min(leftMag, rightMag);" in html
    assert "curveHermite((x - a.x) / span, a.y, a.s, b.y, b.s, span)" in html
    assert "const yForElev = graphSkyYMapper(yBase, padT + 2, padT + graphH - 2, maxElev, minElev);" in html
    assert "ctx.fillStyle = '#dff1ff';" in html
    assert "ctx.fillStyle = '#000000';" in html
    assert "astral: astralSel ? normalizeAstralMode(astralSel.value || 'none') : 'none'" in html


def test_fullscreen_graph_it_requires_sensor_metric_selection():
    html = "".join(render_graph_modal(switch_installed=False))

    assert (
        "id='graphButton' class='button blue' title='Select at least one sensor and metric' "
        "onclick='loadGraph(event)' disabled"
    ) in html
    assert "const GRAPH_SENSOR_METRIC_PAIRS = [" in html
    assert "function hasGraphSensorMetricSelection(){" in html
    assert "function updateGraphButtonState(){" in html
    assert "btn.disabled = isLoading || !hasSelection;" in html
    assert "if(m1) m1.onchange = updateGraphButtonState;" in html
    assert "if(!hasGraphSensorMetricSelection()){" in html


def test_dashboard_micrograph_uses_soil_fertility_gauge_scale():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["soil-123"],
            {"soil-123": {"Soil Fertility Index": 82.0}},
            {"soil-123": {"Soil Fertility Index": {"min": 70.0, "avg": 82.0, "max": 92.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"soil-123": ["Soil Fertility Index"]},
            expected_display_style_map={"soil-123": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "isSoilFertilityIndex ? gaugeConfig?.['Soil Fertility Index']" in html
    assert "yScaleOptions.min = cfgMin" in html
    assert "yScaleOptions.max = cfgMax" in html


def test_dashboard_micrograph_moves_duration_to_card_title_and_shows_time_ticks():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-test123"],
            {"avpd-test123": {"Ambient VPD": 1.2}},
            {"avpd-test123": {"Ambient VPD": {"min": 1.0, "avg": 1.1, "max": 1.3}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"avpd-test123": ["Ambient VPD"]},
            expected_display_style_map={"avpd-test123": {"METRIC_1": "Graph24hr"}},
            display_style="Gauge",
        )
    )

    assert "<div class='metric-title'>24hr Ambient VPD (kPa)</div>" in html
    assert "const durationPrefix = normalized === 'Graph6hr' ? '6hr '" in html
    assert "title.textContent = durationPrefix + baseTitle;" in html
    assert "title: { display: false }" in html
    assert "display: true," in html
    assert "unit: 'hour'," in html
    assert "major: { enabled: true }," in html
    assert "callback: formatXAxisTick" in html
    assert "return month + pad2(date.getDate());" in html
    assert "return pad2(date.getHours()) + ':' + pad2(date.getMinutes());" in html


def test_dashboard_gauge_title_has_no_graph_duration():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-test123"],
            {"avpd-test123": {"Ambient VPD": 1.2}},
            {"avpd-test123": {"Ambient VPD": {"min": 1.0, "avg": 1.1, "max": 1.3}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"avpd-test123": ["Ambient VPD"]},
            expected_display_style_map={"avpd-test123": {"METRIC_1": "Gauge"}},
            display_style="Graph24hr",
        )
    )

    assert "<div class='metric-title'>Ambient VPD (kPa)</div>" in html
    assert "<div class='metric-title'>24hr Ambient VPD (kPa)</div>" not in html


def test_weewx_wind_direction_micrograph_renders_speed_banded_wind_rose():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["weewx-station"],
            {"weewx-station": {"Wind Direction": 270.0, "Wind Speed": 12.5}},
            {"weewx-station": {"Wind Speed": {"min": 2.0, "avg": 8.0, "max": 18.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"weewx-station": ["Wind Direction"]},
            expected_display_style_map={"weewx-station": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "function renderWindRoseMicrograph(canvas, directionSeries, speedSeries, rangeLabel)" in html
    assert "metricNormForRequest === 'wind direction'" in html
    assert "'&sensor_id2=' + encodeURIComponent(sensor)" in html
    assert "encodeURIComponent('Wind Speed')" in html
    assert "const directionNames = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];" in html
    assert "{ min: 0, max: 5, label: '0-5', color: '#bde8ff' }" in html
    assert "{ min: 30, max: Infinity, label: '30+', color: '#0b376d' }" in html
    assert "renderWindRoseMicrograph(canvas, seriesObj, speedSeries, xTitleText)" in html
    assert "canvas.setAttribute('aria-label', `${rangeLabel} wind rose." in html
    assert "baseTitle = 'Wind-Rose';" in html
    assert "baseTitle = `Wind Direction (${unit})`;" in html
    assert "title.textContent = durationPrefix + baseTitle;" in html
    assert "window.updateMetricCardTitle(container, norm)" in html


def test_weewx_direction_only_compass_matches_wind_rose_canvas_height():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["weewx-station"],
            {"weewx-station": {"Wind Direction": 270.0, "Wind Speed": 3.1}},
            {"weewx-station": {"Wind Speed": {"min": 0.0, "avg": 1.7, "max": 7.4}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"weewx-station": ["Wind Direction"]},
            expected_display_style_map={"weewx-station": {"METRIC_1": "Gauge"}},
            display_style="Gauge",
        )
    )

    compass = html[html.index("function drawCompassGauge"):html.index("function getMetricCanvasSize")]
    assert "const cssSize = 205;" in compass
    assert "class='micrograph-canvas' width='260' height='205'" in html


def test_dashboard_micrograph_no_data_does_not_show_toast():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["offline-sensor"],
            {"offline-sensor": {}},
            {"offline-sensor": {}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"offline-sensor": ["Temperature"]},
            expected_display_style_map={"offline-sensor": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "window.showToast('No data in selected graph window', 'warn');" not in html


def test_dashboard_live_refresh_has_recovery_hooks():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-2k7r1y"],
            {"avpd-2k7r1y": {"Ambient VPD": 2.8}},
            {"avpd-2k7r1y": {"Ambient VPD": {"min": 2.6, "avg": 3.1, "max": 4.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"avpd-2k7r1y": ["Ambient VPD"]},
            expected_display_style_map={"avpd-2k7r1y": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "function fetchWithTimeout" in html
    assert "window.__updateGaugesInFlight" in html
    assert "micrographInflightStaleMs" in html
    assert "canvas.dataset.micrographInflightAt" in html
    assert "document.addEventListener('visibilitychange'" in html


def test_dashboard_renders_centered_overview_graphic_at_bottom():
    html = "".join(
        render_dashboard(
            "All",
            None,
            [],
            {},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={},
        )
    )
    css = (Path(__file__).resolve().parents[1] / "ui_static" / "css" / "app.css").read_text(encoding="utf-8")

    graphic = "<div class='dashboard-overview-graphic'>"
    assert graphic in html
    assert f"src='/ui_static/01-sensorius-overview-v5.png?v={APP_VERSION}'" in html
    assert html.index(graphic) < html.index("<div id='modal-host'></div>")
    assert ".dashboard-overview-graphic{" in css
    assert "order:9999;" in css
    assert "justify-content:center;" in css
    assert "width:min(100%, 522px);" in css
    assert "border-radius:12px;" in css


def test_dashboard_metric_cards_render_and_refresh_trend_arrows():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-2k7r1y"],
            {"avpd-2k7r1y": {"Ambient VPD": 2.8}},
            {
                "avpd-2k7r1y": {
                    "Ambient VPD": {
                        "min": 2.6,
                        "avg": 3.1,
                        "max": 4.0,
                        "trend": {
                            "rate_per_hour": -0.25,
                            "samples": 20,
                            "window_s": 1140,
                            "provisional": False,
                        },
                    }
                }
            },
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"avpd-2k7r1y": ["Ambient VPD"]},
        )
    )

    assert "class='trend-arrow' data-metric='Ambient VPD' data-rate='-0.25'" in html
    assert "function trendThresholds(metric)" in html
    assert "return [span * .0025, span * .025]" in html
    assert "if (pressureTrendMetric(metric)) return [.1, 1]" in html
    assert "function trendAngle(score)" in html
    assert "<path d='M2 16H29M22 10L29 16L22 22'/>" in html
    assert "applyTrendArrow(trendArrowEl, statsMetric, stat.trend)" in html
    assert "initializeTrendArrows();" in html


def test_barometric_pressure_uses_one_decimal_in_gauges_and_graph_axes():
    gauge_config = get_gauge_config()

    assert gauge_config["Baro-Pressure"]["display_precision"] == 1
    assert gauge_config["Plant Baro-Pressure"]["display_precision"] == 1

    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-2k7r1y"],
            {"avpd-2k7r1y": {"Baro-Pressure": 1008.9}},
            {"avpd-2k7r1y": {}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"avpd-2k7r1y": ["Baro-Pressure"]},
        )
    )

    assert "1008.9 hPa" in html
    assert "if (isBarometricPressure) return num.toFixed(1);" in html
    assert "if (pressureAxis) return num.toFixed(1);" in html
    assert "context.dataset.pressureMetric" in html


def test_dashboard_trend_arrow_uses_thin_extended_svg_geometry():
    css = (
        Path(__file__).resolve().parents[1] / "ui_static" / "css" / "app.css"
    ).read_text(encoding="utf-8")

    assert ".trend-arrow{" in css
    assert "width:1.025rem; height:1.85rem" in css
    assert "stroke-width:2" in css
    assert "stroke-linecap:round; stroke-linejoin:round" in css


def test_dashboard_switch_layout_drift_schedules_layout_refresh():
    gauge_config = get_gauge_config()
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["avpd-2k7r1y"],
            {"avpd-2k7r1y": {"Ambient VPD": 2.8}},
            {"avpd-2k7r1y": {"Ambient VPD": {"min": 2.6, "avg": 3.1, "max": 4.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=gauge_config,
            expected_gauge_map={"avpd-2k7r1y": ["Ambient VPD"]},
            expected_display_style_map={"avpd-2k7r1y": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    start = html.index("async function updateGauges()")
    end = html.index("function refreshOnceAfterSensorAdded")
    block = html[start:end]

    assert "if (reason.startsWith('switch:')) {" in block
    assert "console.info('[layout-refresh-switch]', reason);" in block
    assert "if (scheduleLayoutRefresh(reason, sig)) { finishUpdateGaugesRun(finishOptions, { ok: true }); return; }" in block
    assert (
        "} else {      if (scheduleLayoutRefresh(layoutDrift.reason, sig)) "
        "{ finishUpdateGaugesRun(finishOptions, { ok: true }); return; }    }"
    ) in block
    assert block.count("for (const sid of available)") == 1
    assert block.count("const values     = d.values") == 1


def test_dashboard_gauge_refresh_applies_sensor_statuses():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["aht-rvwi73"],
            {"aht-rvwi73": {"Temperature": 25.2}},
            {"aht-rvwi73": {"Temperature": {"min": 24.0, "avg": 25.0, "max": 26.0}}},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={"aht-rvwi73": ["Temperature"]},
            expected_display_style_map={"aht-rvwi73": {"METRIC_1": "Graph24hr"}},
            display_style="Graph24hr",
        )
    )

    assert "function applySensorStatuses(data){" in html
    start = html.index("async function updateGauges()")
    end = html.index("function refreshOnceAfterSensorAdded")
    block = html[start:end]
    assert "if (typeof applySensorStatuses === 'function') applySensorStatuses(d);" in block


def test_dashboard_metric_refresh_isolates_bad_cards():
    html = "".join(
        render_dashboard(
            "All",
            None,
            ["aqi-a", "aqi-b", "aqi-c"],
            {
                "aqi-a": {"Temperature": 72.0},
                "aqi-b": {"Temperature": None},
                "aqi-c": {"Temperature": 74.0},
            },
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={
                "aqi-a": ["Temperature"],
                "aqi-b": ["Temperature"],
                "aqi-c": ["Temperature"],
            },
        )
    )

    assert "initGauge: metric card init failed" in html
    start = html.index("async function updateGauges()")
    end = html.index("function refreshOnceAfterSensorAdded")
    block = html[start:end]
    assert "updateGauges: status update failed" in block
    assert "updateGauges: layout drift check failed" in block
    assert "updateGauges: sensor UI sync failed" in block
    assert "updateGauges: metric update failed" in block
    assert "updateGauges: sensor update failed" in block


def test_moon_position_footer_falls_back_to_nearest_moon_event():
    astro_payload = {
        "ok": True,
        "lat": 40.0,
        "lon": -105.0,
        "tz": "America/Denver",
        "sunrise": "05:59",
        "sunset": "20:23",
        "sun_noon": "13:11",
        "sun_points": [],
        "moon_points": [],
        "moon_phase_value": 18.5,
        "moon_phase_label": "Waning Gibbous",
        "moon_lit_pct": 78,
        "moon_rise": "00:30",
        "moon_set": "10:32",
        "moon_rise_today": "",
        "moon_set_today": "10:32",
        "moon_declination": None,
        "moon_position_source": "",
        "moon_next_phase_label": "3rd Quarter",
        "moon_next_phase_date": "2026-06-08",
        "moon_visible_angle": None,
        "moon_reference_angle": None,
    }
    html = "".join(
        render_dashboard(
            "All",
            None,
            [],
            {},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={},
            astro_payload=astro_payload,
        )
    )

    assert "function pickMoonEventRaw(sameDay, nearest){" in html
    assert "const mrRaw = pickMoonEventRaw(data && data.moon_rise_today, data && data.moon_rise);" in html
    assert "moonRiseEl.textContent = fmtSun(mrRaw);" in html
    assert "const moonPhaseRiseRaw = pickMoonEventRaw(data && data.moon_rise_today, data && data.moon_rise);" in html
    assert "riseEl.textContent = fmtMoonTime(moonPhaseRiseRaw);" in html
    assert "const setMoonNextPhaseLabel = (rawLabel) => {" in html
    assert "const namedFullMoon = label !== 'New Moon' && /\\sMoon$/.test(label);" in html
    assert "nextPhaseLabelEl.append(document.createElement('br'));" in html
    assert '"moon_rise": "00:30"' in html


def test_sun_position_card_renders_29_day_overlay():
    astro_payload = {
        "ok": True,
        "lat": 40.0,
        "lon": -105.0,
        "tz": "America/Denver",
        "sunrise": "05:59",
        "sunset": "20:23",
        "sun_noon": "13:11",
        "sun_points": [],
        "moon_points": [],
        "moon_phase_value": 18.5,
        "moon_phase_label": "Waning Gibbous",
        "moon_lit_pct": 78,
        "moon_rise": "",
        "moon_set": "",
        "moon_rise_today": "",
        "moon_set_today": "",
        "moon_declination": None,
        "moon_position_source": "",
        "moon_next_phase_label": "3rd Quarter",
        "moon_next_phase_date": "2026-06-08",
        "moon_visible_angle": None,
        "moon_reference_angle": None,
        "position_29d": [
            {
                "date": "2026-06-10",
                "label": "Jun10",
                "sun": [[0, -20.1], [720, 68.2]],
                "moon": [[0, 12.4], [720, -6.5]],
                "moon_phase_value": 24.1,
                "moon_lit_pct": 39,
                "moon_visible_angle": 128.5,
            },
            {
                "date": "2026-06-11",
                "label": "Jun11",
                "sun": [[0, -20.2], [720, 68.3]],
                "moon": [[0, 2.4], [720, 8.5]],
                "moon_phase_value": 25.1,
                "moon_lit_pct": 27,
                "moon_visible_angle": 131.5,
            },
        ],
    }
    html = "".join(
        render_dashboard(
            "All",
            None,
            [],
            {},
            {},
            SimpleNamespace(expected_gauge_map={}),
            gauge_config=get_gauge_config(),
            expected_gauge_map={},
            astro_payload=astro_payload,
        )
    )

    assert "id='sunBox' aria-live='polite' role='button'" in html
    assert "id='moonBox' aria-live='polite' role='button'" in html
    assert "#sunBox,#moonBox{cursor:pointer;}" in html
    assert ".astro-box.card-loading .dashboard-card-spinner{display:inline-block;}" in html
    assert "function setAstroCardsLoading(isLoading){" in html
    assert "['moonBox','sunBox','sunMoon29Card'].forEach((id) => setDashboardCardLoading(id, isLoading));" in html
    assert "const keepExistingAstro = warming && astroData && astroData.ok;" in html
    assert "setAstroCardsLoading(warming && !keepExistingAstro);" in html
    assert "if (keepExistingAstro) return;" in html
    assert "if (!warming) { delete astroData.reason; delete astroData.cache_status; }" in html
    assert "if (typeof setAstroCardsLoading === 'function') setAstroCardsLoading(isDashboardWarmingPayload(data));" in html
    assert "target.closest('#sunBox,#moonBox')" in html
    assert "id='sunMoon29Canvas'" in html
    assert "29 Day Sun/Moon Position" in html
    assert "function drawSunMoon29Day(data)" in html
    assert "function makeSmoothSkyYMapper(yBase, topY, bottomY, maxElev, minElev)" in html
    assert "const buildSunCurveKeys = () => {" in html
    assert "const buildMoonRiseSetDisplayPoints = () => {" in html
    assert "const buildMoonDisplayPoints = () => {" in html
    assert "const softenMoonLowerExtrema = (pts) => {" in html
    assert "const mrToday = toMin(data && data.moon_rise_today);" in html
    assert "const msToday = toMin(data && data.moon_set_today);" in html
    assert "const riseMin = mrToday;" in html
    assert "const setMin = msToday;" in html
    assert "const riseSet = buildMoonRiseSetDisplayPoints();" in html
    assert "if (riseSet.length >= 2) return riseSet;" in html
    assert "const sampled = softenMoonLowerExtrema(smoothSampledElevationPath(moonPoints, 2));" in html
    assert "built.push({m, y: sunYForMin(m)});" in html
    assert "built.push({m, y: yAt(m)});" in html
    assert "const moonDisplayPoints = buildMoonDisplayPoints();" in html
    assert "const yVals = sorted.map((p) => yForElev(p.e));" in html
    assert "yForMoonElev" not in html
    assert "const yForElev = makeSmoothSkyYMapper(yBase, padY + 2, c.height - padY - 2, maxElev, minElev);" in html
    assert "const yForElev = makeSmoothSkyYMapper(yBase, padT + 2, padT + graphH - 2, maxElev, minElev);" in html
    assert "const drawTinyMoonPhase = (day, cx, cy) => {" in html
    assert "const r = (moonPhaseCardSize * 0.15) / 2;" in html
    assert "ctx.rotate((rotationDeg * Math.PI) / 180);" in html
    assert "const days = Array.isArray(data && data.position_29d)" in html
    assert "window.openSunMoon29Day = openSunMoon29Day;" in html
    assert "window.closeSunMoon29Day = closeSunMoon29Day;" in html
    assert '"label": "Jun10"' in html
    assert '"moon_visible_angle": 128.5' in html
