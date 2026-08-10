#!/usr/bin/env python3
"""Profile Sensorius Web UI flows in a real Chromium session.

Profiles:
- dashboard initial load
- system settings modal
- sensor settings modal
- switch settings modal
- 6 day weather forecast modal
- full-screen graph modal
- integrated biodynamic calendar navigation and initial render
- integrated biodynamic calendar month selectors
- optional direct Ecowitt get_livedata_info latency, size, and sections

The script launches headless Chromium with the DevTools protocol enabled,
drives the dashboard UI, and prints summary timing stats plus raw samples.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import websockets


DEFAULT_BASE_URL = "http://127.0.0.1:8000/"
MAX_ECOWITT_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_CHROME_PATHS = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


@dataclass
class Scenario:
    name: str
    label: str
    js_factory: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Profile Sensorius Web UI flows.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Dashboard base URL.")
    parser.add_argument("--samples", type=int, default=3, help="Samples per scenario.")
    parser.add_argument("--timeout-sec", type=float, default=20.0, help="Per-step timeout.")
    parser.add_argument("--cooldown-ms", type=int, default=1200, help="Pause between scenarios within a sample.")
    parser.add_argument("--chrome-path", default="", help="Explicit Chrome/Chromium binary path.")
    parser.add_argument("--debug-port", type=int, default=9222, help="Chrome DevTools port.")
    parser.add_argument("--output-json", default="", help="Optional path for raw JSON output.")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the dashboard HTTP reachability check before launching Chrome.",
    )
    parser.add_argument(
        "--scenarios",
        default="all",
        help="Comma-separated scenarios to run. Use all, or any of: system_settings,sensor_settings,switch_settings,weather_forecast,fullscreen_graph,calendar,calendar_month_selectors.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first scenario error instead of recording it and continuing.",
    )
    parser.add_argument("--sensor-id", default="", help="Sensor id to use for the sensor_settings scenario.")
    parser.add_argument("--switch-id", default="", help="Switch id to use for the switch_settings scenario.")
    parser.add_argument(
        "--ecowitt-url",
        default="",
        help="Optional Ecowitt gateway base URL to profile through get_livedata_info.",
    )
    parser.add_argument(
        "--ecowitt-sections",
        default="all",
        help="Comma-separated response sections to retain in profiler output; filtering is client-side.",
    )
    parser.add_argument(
        "--ecowitt-only",
        action="store_true",
        help="Profile only the Ecowitt gateway without launching Chromium or requiring the dashboard.",
    )
    return parser.parse_args()


def normalize_ecowitt_profile_url(value: str) -> str:
    """Return a safe plain-HTTP get_livedata_info URL for profiler use."""
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Ecowitt URL must be a plain HTTP base URL without credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Ecowitt URL must not include a path, query, or fragment.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Ecowitt URL contains an invalid port.") from exc
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port else host
    base_url = urllib.parse.urlunsplit(("http", netloc, "", "", ""))
    return f"{base_url}/get_livedata_info"


def parse_ecowitt_sections(value: str) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(
        item.strip() for item in str(value or "all").split(",") if item.strip()
    ))
    if not requested or any(item.lower() == "all" for item in requested):
        return ()
    return requested


def collect_ecowitt_livedata_sample(
    gateway_url: str,
    timeout_sec: float,
    sections: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Time one complete Ecowitt live-data response and summarize its sections."""
    endpoint = normalize_ecowitt_profile_url(gateway_url)
    started = time.perf_counter()
    try:
        response = requests.get(
            endpoint,
            timeout=max(float(timeout_sec), 1.0),
            allow_redirects=False,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        response.raise_for_status()
        if len(response.content) > MAX_ECOWITT_RESPONSE_BYTES:
            raise RuntimeError("Ecowitt live-data response exceeded 2 MiB.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Ecowitt live-data response was not a JSON object.")
        missing = [name for name in sections if name not in payload]
        selected = {
            name: payload[name]
            for name in (sections or tuple(payload.keys()))
            if name in payload
        }
        section_counts = {
            name: len(value) if isinstance(value, (list, dict)) else 1
            for name, value in selected.items()
        }
        return {
            "ok": True,
            "total_ms": elapsed_ms,
            "status": response.status_code,
            "response_bytes": len(response.content),
            "response_section_count": len(payload),
            "selected_sections": list(selected),
            "missing_sections": missing,
            "section_item_counts": section_counts,
            "data": selected,
            "endpoint": endpoint,
        }
    except Exception as exc:
        return {
            "ok": False,
            "total_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "error": str(exc),
            "endpoint": endpoint,
        }


def resolve_chrome_path(explicit: str) -> str:
    candidates = [explicit] if explicit else []
    if not explicit:
        candidates.extend(
            str(path)
            for path in sorted(
                (Path.home() / "Library" / "Caches" / "ms-playwright").glob(
                    "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"
                ),
                reverse=True,
            )
        )
    candidates.extend(DEFAULT_CHROME_PATHS)
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if resolved and Path(resolved).exists():
            return resolved
    raise SystemExit("Chrome/Chromium binary not found. Pass --chrome-path.")


class ChromeSession:
    def __init__(self, chrome_path: str, debug_port: int) -> None:
        self.chrome_path = chrome_path
        self.debug_port = debug_port
        self.proc: subprocess.Popen[str] | None = None
        self.user_data_dir: str | None = None
        self.ws_url: str | None = None

    def start(self) -> None:
        self.user_data_dir = tempfile.mkdtemp(prefix="sensorius-profiler-chrome-")
        cmd = [
            self.chrome_path,
            f"--remote-debugging-port={self.debug_port}",
            f"--user-data-dir={self.user_data_dir}",
            "--headless=new",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,1400",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.ws_url = self._wait_for_devtools()

    def _wait_for_devtools(self) -> str:
        deadline = time.time() + 10.0
        last_error = ""
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{self.debug_port}/json/version",
                    timeout=0.5,
                )
                resp.raise_for_status()
                payload = resp.json()
                ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
                if ws_url:
                    return ws_url
            except Exception as exc:  # pragma: no cover - startup race
                last_error = str(exc)
            time.sleep(0.1)
        raise RuntimeError(f"Chrome DevTools did not start: {last_error}")

    def create_target(self, url: str) -> str:
        encoded = urllib.parse.quote(url, safe=":/?&=%")
        endpoints = (
            ("put", f"http://127.0.0.1:{self.debug_port}/json/new?{encoded}"),
            ("get", f"http://127.0.0.1:{self.debug_port}/json/new?{encoded}"),
        )
        for method, endpoint in endpoints:
            try:
                resp = getattr(requests, method)(endpoint, timeout=2.0)
                if resp.ok:
                    payload = resp.json()
                    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
                    if ws_url:
                        return ws_url
            except Exception:
                continue
        raise RuntimeError("Unable to create Chrome DevTools target.")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        if self.user_data_dir:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)


class CDPClient:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.conn: websockets.WebSocketClientProtocol | None = None
        self._next_id = 0

    async def __aenter__(self) -> "CDPClient":
        self.conn = await websockets.connect(self.ws_url, max_size=20_000_000)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.conn:
            await self.conn.close()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.conn:
            raise RuntimeError("CDP connection is not open.")
        self._next_id += 1
        msg_id = self._next_id
        await self.conn.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await self.conn.recv()
            payload = json.loads(raw)
            if payload.get("id") != msg_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"CDP {method} failed: {payload['error']}")
            return payload.get("result", {})

    async def setup(self) -> None:
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Network.enable")
        await self.send("Performance.enable")

    async def navigate(self, url: str, timeout_sec: float) -> None:
        await self.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            state = await self.evaluate("document.readyState")
            if state == "complete":
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Timed out loading {url}")

    async def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = str(details.get("text") or "JavaScript evaluation failed")
            exception = details.get("exception") or {}
            description = exception.get("description") or exception.get("value")
            if description:
                text = f"{text}: {description}"
            raise RuntimeError(text)
        value = result.get("result", {}).get("value")
        return value

    async def performance_metrics(self) -> dict[str, float]:
        result = await self.send("Performance.getMetrics")
        return {
            str(item.get("name") or ""): float(item.get("value") or 0.0)
            for item in result.get("metrics", [])
            if item.get("name")
        }


def _js_string(value: str) -> str:
    return json.dumps(value)


def build_js_helper(timeout_ms: int) -> str:
    return f"""
(() => {{
  window.__sensProfiler = window.__sensProfiler || {{
    timeoutMs: {timeout_ms},
    sleep(ms) {{
      return new Promise(resolve => setTimeout(resolve, ms));
    }},
    click(el) {{
      if (!el) throw new Error('click target missing');
      el.dispatchEvent(new MouseEvent('click', {{ bubbles: true, cancelable: true, view: window }}));
    }},
    async nextPaint() {{
      await new Promise(resolve => requestAnimationFrame(() => resolve()));
      await new Promise(resolve => requestAnimationFrame(() => resolve()));
    }},
    async waitFor(predicate, timeoutMs, label) {{
      const started = performance.now();
      while ((performance.now() - started) < timeoutMs) {{
        try {{
          const value = predicate();
          if (value) return value;
        }} catch (_err) {{}}
        await this.sleep(25);
      }}
      throw new Error(`timeout waiting for ${{label}}`);
    }},
    closeKnownModals() {{
      try {{ window.closeSystemSettingsModal && window.closeSystemSettingsModal(); }} catch (_err) {{}}
      try {{ window.closeSensorSettingsModal && window.closeSensorSettingsModal(); }} catch (_err) {{}}
      try {{ window.closeSwitchSettingsModal && window.closeSwitchSettingsModal(); }} catch (_err) {{}}
      try {{ window.BackdropModal && window.BackdropModal.close('weatherForecastModal'); }} catch (_err) {{}}
      try {{ window.BackdropModal && window.BackdropModal.close('biodynamicCalendarModal'); }} catch (_err) {{}}
      const extra = [
        'system-settings-root',
        'setupPiModal',
        'sensorSettingsModal',
        'switchSettingsModal',
        'weatherForecastModal',
        'biodynamicCalendarModal',
      ];
      for (const id of extra) {{
        const el = document.getElementById(id);
        if (!el) continue;
        const root = el.closest('.modal-backdrop') || el;
        if (root && root.parentNode && id !== 'setupPiModal') root.parentNode.removeChild(root);
      }}
      const sysRoot = document.getElementById('system-settings-root');
      if (sysRoot && sysRoot.parentNode) sysRoot.parentNode.removeChild(sysRoot);
      const fullscreenGraph = document.getElementById('fullscreen_graph_container');
      if (fullscreenGraph) fullscreenGraph.style.display = 'none';
      const graphModal = document.getElementById('graphModal');
      if (graphModal) graphModal.style.display = 'none';
      if (window.graphChart && typeof window.graphChart.destroy === 'function') {{
        try {{ window.graphChart.destroy(); }} catch (_err) {{}}
        window.graphChart = null;
      }}
    }},
    findSystemSettingsTrigger() {{
      return document.querySelector("a[title='Open System Settings']");
    }},
    sensorIds() {{
      const ids = [];
      const seen = new Set();
      const add = (raw) => {{
        const sid = String(raw || '').trim();
        if (!sid || seen.has(sid)) return;
        seen.add(sid);
        ids.push(sid);
      }};
      document.querySelectorAll('.sensor-group[data-sensor-id]').forEach((el) => add(el.getAttribute('data-sensor-id')));
      document.querySelectorAll('.sensor-order-btn[data-sensor-id]').forEach((el) => add(el.getAttribute('data-sensor-id')));
      document.querySelectorAll('.metric-container[data-sensor]').forEach((el) => add(el.getAttribute('data-sensor')));
      document.querySelectorAll('.sensor-group[id^="group_"]').forEach((el) => add(String(el.id || '').replace(/^group_/, '')));
      return ids;
    }},
    switchIds() {{
      const ids = [];
      const seen = new Set();
      const add = (raw) => {{
        const sid = String(raw || '').trim();
        if (!sid || seen.has(sid)) return;
        seen.add(sid);
        ids.push(sid);
      }};
      document.querySelectorAll('.switch-metric-container[data-switch-ids]').forEach((card) => {{
        String(card.getAttribute('data-switch-ids') || '').split(',').forEach(add);
      }});
      document.querySelectorAll('[data-switch-id]').forEach((el) => add(el.getAttribute('data-switch-id')));
      Array.from(document.querySelectorAll("a[onclick*='editSwitchSettings']")).forEach((link) => {{
        const onclick = String(link.getAttribute('onclick') || '');
        const match = onclick.match(/editSwitchSettings\\(\\"([^\\"]+)\\"\\)/) || onclick.match(/editSwitchSettings\\('([^']+)'\\)/);
        if (match) add(match[1]);
      }});
      return ids;
    }},
    visibleMetricTargets() {{
      const targets = [];
      const seen = new Set();
      const add = (sensor, metric) => {{
        const sid = String(sensor || '').trim();
        const name = String(metric || '').trim();
        const key = sid + '::' + name;
        if (!sid || !name || seen.has(key)) return;
        seen.add(key);
        targets.push({{ sensor_id: sid, metric: name }});
      }};
      document.querySelectorAll('.metric-container[data-sensor][data-metric]').forEach((el) => {{
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) add(el.getAttribute('data-sensor'), el.getAttribute('data-metric'));
      }});
      return targets;
    }},
    findSensorSettingsTrigger(sensorId) {{
      const wanted = String(sensorId || '').trim().toLowerCase();
      const groups = Array.from(document.querySelectorAll('.sensor-group'));
      const group = groups.find((el) => {{
        const attr = String(el.getAttribute('data-sensor-id') || '').trim().toLowerCase();
        const byId = String(el.id || '').replace(/^group_/, '').trim().toLowerCase();
        return wanted && (attr === wanted || byId === wanted);
      }});
      if (group) {{
        const scoped = group.querySelector("a[title*='Settings'], a[aria-label*='Settings']");
        if (scoped) return scoped;
      }}
      const links = Array.from(document.querySelectorAll("a[onclick*='editSensorSettings']"));
      return links.find((el) => {{
        const onclick = String(el.getAttribute('onclick') || '').toLowerCase();
        return wanted && onclick.includes(wanted);
      }}) || links[0] || null;
    }},
    findSwitchSettingsTrigger(switchId) {{
      const wanted = String(switchId || '').trim().toLowerCase();
      const cards = Array.from(document.querySelectorAll('.switch-metric-container[data-switch-ids]'));
      for (const card of cards) {{
        const ids = String(card.getAttribute('data-switch-ids') || '').toLowerCase().split(',').map((item) => item.trim());
        if (wanted && ids.includes(wanted)) {{
          const scoped = card.querySelector("a[title*='Settings'], a[aria-label*='Settings'], a[onclick*='editSwitchSettings']");
          if (scoped) return scoped;
        }}
      }}
      const links = Array.from(document.querySelectorAll("a[onclick*='editSwitchSettings']"));
      return links.find((el) => {{
        const onclick = String(el.getAttribute('onclick') || '').toLowerCase();
        return wanted && onclick.includes(wanted);
      }}) || links[0] || null;
    }},
    navMetrics() {{
      const nav = performance.getEntriesByType('navigation')[0];
      if (!nav) return null;
      return {{
        dom_content_loaded_ms: Number(nav.domContentLoadedEventEnd || 0),
        load_event_ms: Number(nav.loadEventEnd || 0),
        response_end_ms: Number(nav.responseEnd || 0),
        transfer_size: Number(nav.transferSize || 0),
        encoded_body_size: Number(nav.encodedBodySize || 0),
        decoded_body_size: Number(nav.decodedBodySize || 0),
      }};
    }},
    pageMetrics() {{
      const resources = performance.getEntriesByType('resource').map((entry) => ({{
        name: String(entry.name || ''),
        initiator_type: String(entry.initiatorType || ''),
        duration_ms: Number((entry.duration || 0).toFixed(2)),
        transfer_size: Number(entry.transferSize || 0),
        encoded_body_size: Number(entry.encodedBodySize || 0),
        decoded_body_size: Number(entry.decodedBodySize || 0),
      }}));
      const slowest = resources.slice().sort((a, b) => b.duration_ms - a.duration_ms).slice(0, 8);
      return {{
        resource_count: resources.length,
        transfer_size: resources.reduce((sum, item) => sum + item.transfer_size, 0),
        encoded_body_size: resources.reduce((sum, item) => sum + item.encoded_body_size, 0),
        decoded_body_size: resources.reduce((sum, item) => sum + item.decoded_body_size, 0),
        slowest,
      }};
    }},
    discoverTargets() {{
      const sensorIds = this.sensorIds();
      const switchIds = this.switchIds();
      return {{
        sensor_id: sensorIds[0] || '',
        switch_id: switchIds[0] || '',
        sensor_count: sensorIds.length,
        switch_count: switchIds.length,
        metric_count: this.visibleMetricTargets().length,
        sensor_settings_links: document.querySelectorAll("a[onclick*='editSensorSettings'], .sensor-group a[title*='Settings']").length,
        switch_settings_links: document.querySelectorAll("a[onclick*='editSwitchSettings']").length,
      }};
    }},
    async profileAction(config) {{
      this.closeKnownModals();
      if (config.setup) {{
        await config.setup();
      }}
      await this.nextPaint();
      const fetches = [];
      const alerts = [];
      const longTasks = [];
      const originalFetch = window.fetch.bind(window);
      const originalAlert = window.alert.bind(window);
      window.alert = (message) => {{
        alerts.push(String(message || ''));
      }};
      window.fetch = async (...args) => {{
        const started = performance.now();
        const input = args[0];
        const url = typeof input === 'string' ? input : String(input && input.url || '');
        try {{
          const response = await originalFetch(...args);
          fetches.push({{
            url,
            ok: !!response.ok,
            status: Number(response.status || 0),
            duration_ms: Number((performance.now() - started).toFixed(2)),
          }});
          return response;
        }} catch (err) {{
          fetches.push({{
            url,
            ok: false,
            status: 0,
            duration_ms: Number((performance.now() - started).toFixed(2)),
            error: String(err),
          }});
          throw err;
        }}
      }};
      let observer = null;
      if ('PerformanceObserver' in window) {{
        observer = new PerformanceObserver((list) => {{
          for (const entry of list.getEntries()) {{
            longTasks.push(Number((entry.duration || 0).toFixed(2)));
          }}
        }});
        try {{ observer.observe({{ type: 'longtask', buffered: true }}); }} catch (_err) {{}}
      }}
      const started = performance.now();
      try {{
        const actionValue = await config.open();
        const modal = await this.waitFor(config.waitFor, config.timeoutMs || this.timeoutMs, config.label);
        if (config.ready) {{
          await this.waitFor(() => config.ready(modal), config.timeoutMs || this.timeoutMs, `${{config.label}} ready`);
        }}
        await this.nextPaint();
        const total = performance.now() - started;
        return {{
          ok: true,
          selected_value: actionValue || '',
          total_ms: Number(total.toFixed(2)),
          fetch_count: fetches.length,
          fetch_total_ms: Number(fetches.reduce((sum, item) => sum + (item.duration_ms || 0), 0).toFixed(2)),
          max_fetch_ms: Number(fetches.reduce((max, item) => Math.max(max, item.duration_ms || 0), 0).toFixed(2)),
          long_task_count: longTasks.length,
          long_task_total_ms: Number(longTasks.reduce((sum, value) => sum + value, 0).toFixed(2)),
          long_task_max_ms: Number(longTasks.reduce((max, value) => Math.max(max, value), 0).toFixed(2)),
          fetches,
          alerts,
        }};
      }} finally {{
        if (observer) observer.disconnect();
        try {{ this.closeKnownModals(); }} catch (_err) {{}}
        window.fetch = originalFetch;
        window.alert = originalAlert;
      }}
    }},
    async profileDashboardRefresh() {{
      if (typeof window.updateGauges !== 'function') throw new Error('updateGauges is unavailable');
      await window.updateGauges({{ ignoreVisibility: true, ignoreModal: true }});
      await this.nextPaint();
      const overview = document.querySelector("img[src*='01-sensorius-overview-v5.png']");
      if (overview && !overview.complete) {{
        await Promise.race([
          new Promise(resolve => overview.addEventListener('load', resolve, {{ once: true }})),
          new Promise(resolve => overview.addEventListener('error', resolve, {{ once: true }})),
          this.sleep(5000),
        ]);
      }}
      performance.clearResourceTimings();
      const started = performance.now();
      await window.updateGauges({{ ignoreVisibility: true, ignoreModal: true }});
      await this.nextPaint();
      const resources = performance.getEntriesByType('resource').map((entry) => String(entry.name || ''));
      return {{
        ok: true,
        total_ms: Number((performance.now() - started).toFixed(2)),
        resource_count: resources.length,
        resources,
        overview_image_requested: resources.some((url) => url.includes('01-sensorius-overview-v5.png')),
      }};
    }},
  }};
  return true;
}})()
"""


SCENARIOS = (
    Scenario(
        name="system_settings",
        label="System Settings",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'system settings modal',
  open: async () => {
    if (typeof window.editSystemSettings === 'function') {
      await window.editSystemSettings();
      return '';
    }
    const trigger = await window.__sensProfiler.waitFor(
      () => window.__sensProfiler.findSystemSettingsTrigger(),
      window.__sensProfiler.timeoutMs,
      'system settings trigger'
    );
    window.__sensProfiler.click(trigger);
    return '';
  },
  waitFor: () => {
    const el = document.getElementById('setupPiModal');
    if (!el) return null;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' ? el : null;
  },
  ready: (modal) => modal.querySelector('.system-settings-menu .system-menu-btn, .system-menu-btn'),
}))()
""",
    ),
    Scenario(
        name="sensor_settings",
        label="Sensor Settings",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'sensor settings modal',
  open: async () => {
    const targetSensorId = String(window.__sensProfilerTargetSensorId || '').trim();
    const sensorId = targetSensorId || window.__sensProfiler.discoverTargets().sensor_id;
    if (!sensorId) throw new Error('No sensor found on dashboard');
    if (typeof window.editSensorSettings === 'function') {
      await window.editSensorSettings(sensorId);
      return sensorId;
    }
    const trigger = await window.__sensProfiler.waitFor(
      () => window.__sensProfiler.findSensorSettingsTrigger(sensorId),
      window.__sensProfiler.timeoutMs,
      'sensor settings trigger'
    );
    window.__sensProfiler.click(trigger);
    return sensorId;
  },
  waitFor: () => document.getElementById('sensorSettingsModal'),
  ready: (modal) => modal.querySelector('#sensorSettingsForm'),
}))()
""",
    ),
    Scenario(
        name="switch_settings",
        label="Switch Settings",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'switch settings modal',
  open: async () => {
    const targetSwitchId = String(window.__sensProfilerTargetSwitchId || '').trim();
    const switchId = targetSwitchId || window.__sensProfiler.discoverTargets().switch_id;
    if (!switchId) throw new Error('No switch found on dashboard');
    if (typeof window.editSwitchSettings === 'function') {
      await window.editSwitchSettings(switchId);
      return switchId;
    }
    const trigger = await window.__sensProfiler.waitFor(
      () => window.__sensProfiler.findSwitchSettingsTrigger(switchId),
      window.__sensProfiler.timeoutMs,
      'switch settings trigger'
    );
    window.__sensProfiler.click(trigger);
    return switchId;
  },
  waitFor: () => document.getElementById('switchSettingsModal'),
  ready: (modal) => modal.querySelector('#switchSettingsPane form'),
}))()
""",
    ),
    Scenario(
        name="weather_forecast",
        label="Full-Screen Weather Forecast",
        js_factory="""
(async () => {
  if (window.__weatherForecastEnabled === false) throw new Error('Weather forecast disabled on dashboard');
  const started = performance.now();
  const response = await fetch('/weather-forecast', {cache: 'no-store'});
  const html = await response.text();
  const ready = response.ok
    && html.includes('id="dashboardReturn"')
    && html.includes('id="forecastDialog"');
  return {
    ok: ready,
    total_ms: Number((performance.now() - started).toFixed(2)),
    status: response.status,
    decoded_body_size: html.length,
    target: '/weather-forecast',
  };
})()
""",
    ),
    Scenario(
        name="fullscreen_graph",
        label="Full-Screen Graph",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'full-screen graph modal',
  open: async () => {
    const targets = window.__sensProfiler.visibleMetricTargets();
    if (!targets.length) throw new Error('No graphable metric found on dashboard');
    const target = targets[0];

    if (typeof window.openGraphModal !== 'function') {
      const trigger = await window.__sensProfiler.waitFor(
        () => document.querySelector("a[title='View Graph'], a[onclick*='openGraphModal']"),
        window.__sensProfiler.timeoutMs,
        'graph modal trigger'
      );
      window.__sensProfiler.click(trigger);
    } else {
      await window.openGraphModal();
    }

    await window.__sensProfiler.waitFor(
      () => document.getElementById('graphModal') && window.getComputedStyle(document.getElementById('graphModal')).display !== 'none',
      window.__sensProfiler.timeoutMs,
      'graph builder modal'
    );

    const sensorSel = document.getElementById('sensor1_select');
    const metricSel = document.getElementById('metric1_select');
    if (!sensorSel || !metricSel) throw new Error('Graph controls not found');

    sensorSel.value = target.sensor_id;
    if (typeof window.populateMetricsFor === 'function') {
      await window.populateMetricsFor('sensor1_select', 'metric1_select');
    }

    const metricValues = Array.from(metricSel.options || []).map((option) => option.value).filter(Boolean);
    metricSel.value = metricValues.includes(target.metric) ? target.metric : (metricValues[0] || '');
    if (!metricSel.value) throw new Error('No metric options found for graph target');

    const range = document.querySelector("input[name='range'][value='24h']")
      || document.querySelector("input[name='range'][value='6h']")
      || document.querySelector("input[name='range']");
    if (range) {
      range.checked = true;
      if (typeof window.toggleCustomTime === 'function') window.toggleCustomTime(false);
    }

    const button = document.getElementById('graphButton');
    if (!button) throw new Error('Graph button not found');
    window.__sensProfiler.click(button);
    return target.sensor_id + '::' + metricSel.value;
  },
  waitFor: () => {
    const el = document.getElementById('fullscreen_graph_container');
    if (!el) return null;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' ? el : null;
  },
  ready: (modal) => window.graphChart && document.getElementById('fullscreen_graph'),
}))()
""",
    ),
    Scenario(
        name="calendar",
        label="Integrated Calendar",
        js_factory="",
    ),
    Scenario(
        name="calendar_month_selectors",
        label="Calendar Month Selectors",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'biodynamic calendar month selectors',
  open: async () => {
    const calendar = document.getElementById('calendar');
    const label = document.getElementById('monthLabel');
    const prev = document.getElementById('prevBtn');
    const next = document.getElementById('nextBtn');
    if (!calendar || !label || !prev || !next) throw new Error('Integrated calendar month controls not found');

    const initialLabel = String(label.textContent || '').trim();
    const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    const shiftLabel = (baseLabel, delta) => {
      const parts = String(baseLabel || '').trim().split(/\\s+/);
      const monthIndex = monthNames.indexOf(parts[0] || '');
      const year = parseInt(parts[1] || '', 10);
      if (monthIndex < 0 || !Number.isFinite(year)) throw new Error(`Unable to parse month label: ${baseLabel}`);
      const d = new Date(year, monthIndex + delta, 1);
      return `${monthNames[d.getMonth()]} ${d.getFullYear()}`;
    };
    const shiftMonth = async (button, beforeLabel, labelText) => {
      window.__sensProfiler.click(button);
      await window.__sensProfiler.waitFor(
        () => String(label.textContent || '').trim() && String(label.textContent || '').trim() !== beforeLabel && calendar.querySelector('.bio-day') && calendar.getAttribute('aria-busy') !== 'true',
        window.__sensProfiler.timeoutMs,
        labelText
      );
      return String(label.textContent || '').trim();
    };
    const nextLabel = await shiftMonth(next, initialLabel, 'next month render');
    const secondNextLabel = await shiftMonth(next, nextLabel, 'second next month render');
    const prevLabel = await shiftMonth(prev, secondNextLabel, 'previous month render');
    const finalLabel = await shiftMonth(prev, prevLabel, 'second previous month render');
    const expected = [
      initialLabel,
      shiftLabel(initialLabel, 1),
      shiftLabel(initialLabel, 2),
      shiftLabel(initialLabel, 1),
      initialLabel,
    ];
    const actual = [initialLabel, nextLabel, secondNextLabel, prevLabel, finalLabel];
    if (actual.join(' -> ') !== expected.join(' -> ')) {
      throw new Error(`Calendar month selector sequence mismatch: expected ${expected.join(' -> ')}, got ${actual.join(' -> ')}`);
    }
    return `${initialLabel} -> ${nextLabel} -> ${secondNextLabel} -> ${prevLabel} -> ${finalLabel}`;
  },
  waitFor: () => document.getElementById('calendar'),
  ready: (calendar) => calendar.querySelector('.bio-day') && calendar.getAttribute('aria-busy') !== 'true',
}))()
""",
    ),
)


def select_scenarios(raw: str) -> tuple[Scenario, ...]:
    requested = [item.strip() for item in str(raw or "all").split(",") if item.strip()]
    if not requested or any(item.lower() == "all" for item in requested):
        return SCENARIOS

    by_name = {scenario.name: scenario for scenario in SCENARIOS}
    selected: list[Scenario] = []
    invalid: list[str] = []
    for item in requested:
        key = item.lower()
        scenario = by_name.get(key)
        if not scenario:
            invalid.append(item)
            continue
        if scenario not in selected:
            selected.append(scenario)

    if invalid:
        valid = ",".join(by_name)
        raise SystemExit(f"Unknown scenario(s): {', '.join(invalid)}. Valid: all,{valid}")
    return tuple(selected)


def scenario_error_is_skip(scenario: Scenario, error_text: str) -> bool:
    text = str(error_text or "").lower()
    if scenario.name == "switch_settings" and "no switch found" in text:
        return True
    if scenario.name == "sensor_settings" and "no sensor found" in text:
        return True
    if scenario.name == "weather_forecast" and "weather forecast disabled" in text:
        return True
    if scenario.name == "fullscreen_graph" and "no graphable metric found" in text:
        return True
    return False


def preflight_dashboard(base_url: str, timeout_sec: float) -> None:
    try:
        response = requests.get(base_url, timeout=max(float(timeout_sec), 1.0), stream=True)
        response.raise_for_status()
        response.close()
    except Exception as exc:
        raise RuntimeError(f"Dashboard preflight failed for {base_url}: {exc}") from exc


def performance_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    seconds_metrics = ("TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration")
    result = {
        f"{name.replace('Duration', '').lower()}_ms": round(
            max(0.0, after.get(name, 0.0) - before.get(name, 0.0)) * 1000.0,
            2,
        )
        for name in seconds_metrics
    }
    result["js_heap_used_mb"] = round(after.get("JSHeapUsedSize", 0.0) / (1024.0 * 1024.0), 2)
    result["nodes"] = round(after.get("Nodes", 0.0), 0)
    result["documents"] = round(after.get("Documents", 0.0), 0)
    return result


async def collect_dashboard_sample(client: CDPClient) -> dict[str, Any]:
    nav = await client.evaluate("window.__sensProfiler.navMetrics()")
    page = await client.evaluate("window.__sensProfiler.pageMetrics()")
    targets = await client.evaluate("window.__sensProfiler.discoverTargets()")
    performance_after = await client.performance_metrics()
    try:
        refresh = await client.evaluate("window.__sensProfiler.profileDashboardRefresh()")
    except Exception as exc:
        refresh = {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "navigation": nav,
        "page": page,
        "renderer": performance_delta({}, performance_after),
        "refresh": refresh,
        "targets": targets,
    }


async def navigate_to_integrated_calendar(
    client: CDPClient,
    base_url: str,
    timeout_sec: float,
    timeout_ms: int,
) -> dict[str, Any]:
    started = time.monotonic()
    await client.evaluate(
        """
(() => {
  const trigger = document.getElementById('bioOpenBtn');
  if (!trigger) throw new Error('Calendar dashboard trigger not found');
  trigger.click();
  return true;
})()
"""
    )
    deadline = time.monotonic() + max(float(timeout_sec), 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            ready = await client.evaluate(
                "window.location.pathname === '/calendar' && "
                "document.readyState === 'complete' && "
                "!!document.querySelector('#calendar .bio-day') && "
                "document.getElementById('calendar').getAttribute('aria-busy') !== 'true'"
            )
            if ready:
                break
        except Exception as exc:  # execution context changes during navigation
            last_error = str(exc)
        await asyncio.sleep(0.05)
    else:
        raise TimeoutError(f"Timed out loading integrated calendar from {base_url}: {last_error}")

    await client.evaluate(build_js_helper(timeout_ms))
    nav = await client.evaluate("window.__sensProfiler.navMetrics()")
    page = await client.evaluate("window.__sensProfiler.pageMetrics()")
    after = await client.performance_metrics()
    return {
        "ok": True,
        "total_ms": round((time.monotonic() - started) * 1000.0, 2),
        "navigation": nav,
        "page": page,
        "renderer": performance_delta({}, after),
        "selected_value": str(await client.evaluate("document.getElementById('monthLabel').textContent || ''")).strip(),
        "fetch_count": sum(1 for item in (page or {}).get("slowest", []) if item.get("initiator_type") == "fetch"),
        "fetch_total_ms": 0.0,
        "max_fetch_ms": max(
            [float(item.get("duration_ms") or 0.0) for item in (page or {}).get("slowest", []) if item.get("initiator_type") == "fetch"],
            default=0.0,
        ),
        "long_task_total_ms": 0.0,
        "alerts": [],
    }


async def collect_scenario_sample(client: CDPClient, scenario: Scenario, timeout_sec: float) -> dict[str, Any]:
    try:
        sample = await asyncio.wait_for(
            client.evaluate(scenario.js_factory),
            timeout=max(float(timeout_sec) + 5.0, 5.0),
        )
        if not isinstance(sample, dict):
            raise RuntimeError(f"{scenario.name} returned no structured result")
        return sample
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "skipped": False,
            "error": f"{scenario.name} exceeded hard timeout",
        }
    except Exception as exc:
        error_text = str(exc)
        return {
            "ok": False,
            "skipped": scenario_error_is_skip(scenario, error_text),
            "error": error_text,
        }


async def pause_between_scenarios(client: CDPClient, cooldown_ms: int) -> None:
    if cooldown_ms <= 0:
        return
    await client.evaluate(
        """
(() => new Promise(resolve => setTimeout(resolve, %d)))()
"""
        % int(cooldown_ms)
    )


async def configure_profiler_targets(client: CDPClient, args: argparse.Namespace) -> None:
    await client.evaluate(
        "window.__sensProfilerTargetSensorId = "
        f"{_js_string(str(getattr(args, 'sensor_id', '') or ''))};"
        "window.__sensProfilerTargetSwitchId = "
        f"{_js_string(str(getattr(args, 'switch_id', '') or ''))};"
        "true"
    )


async def ensure_profiler_helper(
    client: CDPClient,
    args: argparse.Namespace,
    timeout_ms: int,
) -> None:
    deadline = time.monotonic() + max(float(args.timeout_sec), 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if await client.evaluate("document.readyState === 'complete'"):
                await client.evaluate(build_js_helper(timeout_ms))
                await configure_profiler_targets(client, args)
                return
        except Exception as exc:  # execution context can change during a recovery reload
            last_error = str(exc)
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Timed out restoring profiler helper after dashboard refresh: {last_error}")


def summarize_metric(values: list[float], suffix: str = "ms") -> dict[str, float] | None:
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    ordered = sorted(vals)
    return {
        f"min_{suffix}": round(ordered[0], 2),
        f"median_{suffix}": round(statistics.median(ordered), 2),
        f"mean_{suffix}": round(statistics.fmean(ordered), 2),
        f"max_{suffix}": round(ordered[-1], 2),
    }


def build_summary(samples: dict[str, list[dict[str, Any]]], scenarios: tuple[Scenario, ...] = SCENARIOS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    dashboard_nav = [item.get("navigation") or {} for item in samples.get("dashboard", [])]
    dashboard_pages = [item.get("page") or {} for item in samples.get("dashboard", [])]
    dashboard_renderers = [item.get("renderer") or {} for item in samples.get("dashboard", [])]
    dashboard_refreshes = [item.get("refresh") or {} for item in samples.get("dashboard", [])]
    out["dashboard"] = {
        "load_event_ms": summarize_metric([item.get("load_event_ms") for item in dashboard_nav]),
        "dom_content_loaded_ms": summarize_metric([item.get("dom_content_loaded_ms") for item in dashboard_nav]),
        "response_end_ms": summarize_metric([item.get("response_end_ms") for item in dashboard_nav]),
        "resource_transfer_bytes": summarize_metric([item.get("transfer_size") for item in dashboard_pages]),
        "resource_decoded_bytes": summarize_metric([item.get("decoded_body_size") for item in dashboard_pages]),
        "renderer_task_ms": summarize_metric([item.get("task_ms") for item in dashboard_renderers]),
        "renderer_script_ms": summarize_metric([item.get("script_ms") for item in dashboard_renderers]),
        "js_heap_used_mb": summarize_metric([item.get("js_heap_used_mb") for item in dashboard_renderers]),
        "refresh_total_ms": summarize_metric([item.get("total_ms") for item in dashboard_refreshes]),
        "refresh_error_count": sum(1 for item in dashboard_refreshes if item.get("ok") is False),
        "overview_image_refresh_requests": sum(1 for item in dashboard_refreshes if item.get("overview_image_requested")),
    }
    ecowitt_rows = samples.get("ecowitt_livedata", [])
    if ecowitt_rows:
        out["ecowitt_livedata"] = {
            "ok_count": sum(1 for row in ecowitt_rows if row.get("ok") is True),
            "error_count": sum(1 for row in ecowitt_rows if row.get("ok") is False),
            "errors": [str(row.get("error") or "") for row in ecowitt_rows if row.get("error")][:3],
            "total_ms": summarize_metric([row.get("total_ms") for row in ecowitt_rows]),
            "response_bytes": summarize_metric([row.get("response_bytes") for row in ecowitt_rows], "bytes"),
            "response_section_count": summarize_metric([
                row.get("response_section_count") for row in ecowitt_rows
            ], "count"),
            "selected_sections": next(
                (row.get("selected_sections") for row in ecowitt_rows if row.get("selected_sections") is not None),
                [],
            ),
        }
    for scenario in scenarios:
        rows = samples.get(scenario.name, [])
        errors = [str(row.get("error") or "") for row in rows if row.get("error")]
        alerts = [
            str(alert)
            for row in rows
            for alert in (row.get("alerts") or [])
            if str(alert or "").strip()
        ]
        out[scenario.name] = {
            "ok_count": sum(1 for row in rows if row.get("ok") is True),
            "error_count": sum(1 for row in rows if row.get("ok") is False and not row.get("skipped")),
            "skipped_count": sum(1 for row in rows if row.get("skipped")),
            "errors": errors[:3],
            "alerts": alerts[:3],
            "total_ms": summarize_metric([row.get("total_ms") for row in rows]),
            "fetch_total_ms": summarize_metric([row.get("fetch_total_ms") for row in rows]),
            "max_fetch_ms": summarize_metric([row.get("max_fetch_ms") for row in rows]),
            "long_task_total_ms": summarize_metric([row.get("long_task_total_ms") for row in rows]),
        }
    return out


def print_summary(
    base_url: str,
    samples: dict[str, list[dict[str, Any]]],
    summary: dict[str, Any],
    scenarios: tuple[Scenario, ...] = SCENARIOS,
) -> None:
    print(f"Base URL: {base_url}")
    print()
    dash_targets = [row.get("targets") or {} for row in samples.get("dashboard", [])]
    first_targets = dash_targets[0] if dash_targets else {}
    print(
        "Targets: "
        f"sensors={first_targets.get('sensor_count', 0)}, "
        f"switches={first_targets.get('switch_count', 0)}, "
        f"switch_links={first_targets.get('switch_settings_links', 0)}, "
        f"first_sensor={first_targets.get('sensor_id', '') or '-'}, "
        f"first_switch={first_targets.get('switch_id', '') or '-'}"
    )
    print()
    dash = summary.get("dashboard", {})
    print("Dashboard")
    print(f"  load_event_ms: {json.dumps(dash.get('load_event_ms'))}")
    print(f"  dom_content_loaded_ms: {json.dumps(dash.get('dom_content_loaded_ms'))}")
    print(f"  response_end_ms: {json.dumps(dash.get('response_end_ms'))}")
    print(f"  resource_transfer_bytes: {json.dumps(dash.get('resource_transfer_bytes'))}")
    print(f"  resource_decoded_bytes: {json.dumps(dash.get('resource_decoded_bytes'))}")
    print(f"  renderer_task_ms: {json.dumps(dash.get('renderer_task_ms'))}")
    print(f"  renderer_script_ms: {json.dumps(dash.get('renderer_script_ms'))}")
    print(f"  js_heap_used_mb: {json.dumps(dash.get('js_heap_used_mb'))}")
    print(f"  refresh_total_ms: {json.dumps(dash.get('refresh_total_ms'))}")
    print(f"  refresh_errors: {dash.get('refresh_error_count', 0)}")
    print(f"  overview_image_refresh_requests: {dash.get('overview_image_refresh_requests', 0)}")
    print()
    ecowitt = summary.get("ecowitt_livedata")
    if ecowitt:
        print("Ecowitt get_livedata_info")
        print(f"  samples: ok={ecowitt.get('ok_count', 0)}, errors={ecowitt.get('error_count', 0)}")
        print(f"  total_ms: {json.dumps(ecowitt.get('total_ms'))}")
        print(f"  response_bytes: {json.dumps(ecowitt.get('response_bytes'))}")
        print(f"  response_section_count: {json.dumps(ecowitt.get('response_section_count'))}")
        print(f"  selected_sections: {json.dumps(ecowitt.get('selected_sections') or [])}")
        for error in ecowitt.get("errors") or []:
            print(f"  error: {error}")
        print()
    for scenario in scenarios:
        block = summary.get(scenario.name, {})
        print(scenario.label)
        print(
            "  samples: "
            f"ok={block.get('ok_count', 0)}, "
            f"skipped={block.get('skipped_count', 0)}, "
            f"errors={block.get('error_count', 0)}"
        )
        print(f"  total_ms: {json.dumps(block.get('total_ms'))}")
        print(f"  fetch_total_ms: {json.dumps(block.get('fetch_total_ms'))}")
        print(f"  max_fetch_ms: {json.dumps(block.get('max_fetch_ms'))}")
        print(f"  long_task_total_ms: {json.dumps(block.get('long_task_total_ms'))}")
        for alert in block.get("alerts") or []:
            print(f"  alert: {alert}")
        for error in block.get("errors") or []:
            print(f"  error: {error}")
        print()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = select_scenarios(args.scenarios)
    dashboard_scenarios = tuple(
        scenario for scenario in scenarios if scenario.name not in {"calendar", "calendar_month_selectors"}
    )
    calendar_scenario = next((scenario for scenario in scenarios if scenario.name == "calendar"), None)
    calendar_month_scenario = next(
        (scenario for scenario in scenarios if scenario.name == "calendar_month_selectors"),
        None,
    )
    results: dict[str, list[dict[str, Any]]] = {
        "dashboard": [],
        **{scenario.name: [] for scenario in scenarios},
    }
    ecowitt_url = str(getattr(args, "ecowitt_url", "") or "").strip()
    ecowitt_sections = parse_ecowitt_sections(getattr(args, "ecowitt_sections", "all"))
    if getattr(args, "ecowitt_only", False) and not ecowitt_url:
        raise ValueError("--ecowitt-only requires --ecowitt-url.")
    if ecowitt_url:
        results["ecowitt_livedata"] = []
        for index in range(args.samples):
            sample = await asyncio.to_thread(
                collect_ecowitt_livedata_sample,
                ecowitt_url,
                args.timeout_sec,
                ecowitt_sections,
            )
            sample["sample_index"] = index + 1
            results["ecowitt_livedata"].append(sample)
            if args.fail_fast and sample.get("ok") is False:
                raise RuntimeError(str(sample.get("error") or "Ecowitt live-data profiling failed"))
    if getattr(args, "ecowitt_only", False):
        summary = build_summary(results, ())
        return {
            "base_url": args.base_url,
            "ecowitt_url": ecowitt_url,
            "ecowitt_sections": list(ecowitt_sections) if ecowitt_sections else ["all"],
            "sample_count": args.samples,
            "scenarios": [],
            "summary": summary,
            "samples": results,
        }

    if not args.skip_preflight:
        preflight_dashboard(args.base_url, args.timeout_sec)
    chrome = ChromeSession(resolve_chrome_path(args.chrome_path), args.debug_port)
    chrome.start()
    target_ws = chrome.create_target("about:blank")
    timeout_ms = int(args.timeout_sec * 1000)
    try:
        async with CDPClient(target_ws) as client:
            await client.setup()
            await client.evaluate(build_js_helper(timeout_ms))
            await configure_profiler_targets(client, args)
            for index in range(args.samples):
                await client.navigate(args.base_url, args.timeout_sec)
                await client.evaluate(build_js_helper(timeout_ms))
                await configure_profiler_targets(client, args)
                results["dashboard"].append(await collect_dashboard_sample(client))
                await ensure_profiler_helper(client, args, timeout_ms)
                await pause_between_scenarios(client, args.cooldown_ms)

                for scenario in dashboard_scenarios:
                    sample = await collect_scenario_sample(client, scenario, args.timeout_sec)
                    sample["sample_index"] = index + 1
                    results[scenario.name].append(sample)
                    if args.fail_fast and sample.get("ok") is False and not sample.get("skipped"):
                        raise RuntimeError(str(sample.get("error") or f"{scenario.name} failed"))
                    await pause_between_scenarios(client, args.cooldown_ms)

                if calendar_scenario or calendar_month_scenario:
                    try:
                        calendar_sample = await navigate_to_integrated_calendar(
                            client,
                            args.base_url,
                            args.timeout_sec,
                            timeout_ms,
                        )
                    except Exception as exc:
                        calendar_sample = {"ok": False, "skipped": False, "error": str(exc)}
                    calendar_sample["sample_index"] = index + 1
                    if calendar_scenario:
                        results[calendar_scenario.name].append(calendar_sample)
                    if args.fail_fast and calendar_sample.get("ok") is False:
                        raise RuntimeError(str(calendar_sample.get("error") or "integrated calendar failed"))
                    await pause_between_scenarios(client, args.cooldown_ms)

                    if calendar_month_scenario:
                        if calendar_sample.get("ok") is True:
                            month_sample = await collect_scenario_sample(
                                client,
                                calendar_month_scenario,
                                args.timeout_sec,
                            )
                        else:
                            month_sample = {
                                "ok": False,
                                "skipped": False,
                                "error": "Integrated calendar did not load",
                            }
                        month_sample["sample_index"] = index + 1
                        results[calendar_month_scenario.name].append(month_sample)
                        if args.fail_fast and month_sample.get("ok") is False:
                            raise RuntimeError(str(month_sample.get("error") or "calendar month selectors failed"))
                        await pause_between_scenarios(client, args.cooldown_ms)

            summary = build_summary(results, scenarios)
            payload = {
                "base_url": args.base_url,
                "ecowitt_url": ecowitt_url,
                "ecowitt_sections": list(ecowitt_sections) if ecowitt_sections else ["all"],
                "sample_count": args.samples,
                "scenarios": [scenario.name for scenario in scenarios],
                "summary": summary,
                "samples": results,
            }
            return payload
    finally:
        chrome.stop()


def main() -> int:
    args = parse_args()
    try:
        scenarios = select_scenarios(args.scenarios)
        payload = asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Profiler failed: {exc}", file=sys.stderr)
        return 1

    scenario_names = set(payload.get("scenarios") or [])
    scenarios = tuple(scenario for scenario in SCENARIOS if scenario.name in scenario_names)
    print_summary(args.base_url, payload["samples"], payload["summary"], scenarios)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Raw JSON written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
