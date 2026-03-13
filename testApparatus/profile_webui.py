#!/usr/bin/env python3
"""Profile Sensorius Web UI flows in a real Chrome session.

Profiles:
- dashboard initial load
- system settings modal
- sensor settings modal
- switch settings modal
- biodynamic calendar modal

The script launches headless Chrome with the DevTools protocol enabled,
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
DEFAULT_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
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
    return parser.parse_args()


def resolve_chrome_path(explicit: str) -> str:
    candidates = [explicit] if explicit else []
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
      try {{ window.BackdropModal && window.BackdropModal.close('biodynamicCalendarModal'); }} catch (_err) {{}}
      const extra = [
        'system-settings-root',
        'setupPiModal',
        'sensorSettingsModal',
        'switchSettingsModal',
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
    }},
    findSystemSettingsTrigger() {{
      return document.querySelector("a[title='Open System Settings']");
    }},
    findSensorSettingsTrigger(sensorId) {{
      const wanted = String(sensorId || '').trim().toLowerCase();
      const links = Array.from(document.querySelectorAll("a[onclick*='editSensorSettings']"));
      return links.find((el) => {{
        const onclick = String(el.getAttribute('onclick') || '').toLowerCase();
        return wanted && onclick.includes(wanted);
      }}) || links[0] || null;
    }},
    findSwitchSettingsTrigger(switchId) {{
      const wanted = String(switchId || '').trim().toLowerCase();
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
    discoverTargets() {{
      const sensorId = document.querySelector('.sensor-group[data-sensor-id]')?.getAttribute('data-sensor-id') || '';
      let switchId = '';
      const swLink = Array.from(document.querySelectorAll("a[onclick*='editSwitchSettings']"))[0];
      if (swLink) {{
        const onclick = String(swLink.getAttribute('onclick') || '');
        const match = onclick.match(/editSwitchSettings\\(\\"([^\\"]+)\\"\\)/) || onclick.match(/editSwitchSettings\\('([^']+)'\\)/);
        if (match) switchId = match[1];
      }}
      return {{
        sensor_id: sensorId,
        switch_id: switchId,
        sensor_count: document.querySelectorAll('.sensor-group[data-sensor-id]').length,
        switch_settings_links: document.querySelectorAll("a[onclick*='editSwitchSettings']").length,
      }};
    }},
    async profileAction(config) {{
      this.closeKnownModals();
      await this.nextPaint();
      const fetches = [];
      const longTasks = [];
      const originalFetch = window.fetch.bind(window);
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
        }};
      }} finally {{
        if (observer) observer.disconnect();
        window.fetch = originalFetch;
      }}
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
    const sensorId = window.__sensProfiler.discoverTargets().sensor_id;
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
    const switchId = window.__sensProfiler.discoverTargets().switch_id;
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
        name="calendar",
        label="Calendar",
        js_factory="""
(() => window.__sensProfiler.profileAction({
  label: 'biodynamic calendar modal',
  open: async () => {
    if (typeof window.openBiodynamicCalendarModal === 'function') {
      await window.openBiodynamicCalendarModal();
      return '';
    }
    const trigger = await window.__sensProfiler.waitFor(
      () => document.getElementById('bioOpenBtn'),
      window.__sensProfiler.timeoutMs,
      'calendar trigger'
    );
    window.__sensProfiler.click(trigger);
    return '';
  },
  waitFor: () => document.getElementById('biodynamicCalendarModal'),
  ready: (modal) => modal.querySelector('#bioModalCalendar .bio-day'),
}))()
""",
    ),
)


async def collect_dashboard_sample(client: CDPClient) -> dict[str, Any]:
    nav = await client.evaluate("window.__sensProfiler.navMetrics()")
    targets = await client.evaluate("window.__sensProfiler.discoverTargets()")
    return {
        "ok": True,
        "navigation": nav,
        "targets": targets,
    }


async def collect_scenario_sample(client: CDPClient, scenario: Scenario) -> dict[str, Any]:
    sample = await client.evaluate(scenario.js_factory)
    if not isinstance(sample, dict):
        raise RuntimeError(f"{scenario.name} returned no structured result")
    return sample


async def pause_between_scenarios(client: CDPClient, cooldown_ms: int) -> None:
    if cooldown_ms <= 0:
        return
    await client.evaluate(f"window.__sensProfiler.sleep({int(cooldown_ms)})")


def summarize_metric(values: list[float]) -> dict[str, float] | None:
    vals = [float(v) for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    ordered = sorted(vals)
    return {
        "min_ms": round(ordered[0], 2),
        "median_ms": round(statistics.median(ordered), 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
        "max_ms": round(ordered[-1], 2),
    }


def build_summary(samples: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    dashboard_nav = [item.get("navigation") or {} for item in samples.get("dashboard", [])]
    out["dashboard"] = {
        "load_event_ms": summarize_metric([item.get("load_event_ms") for item in dashboard_nav]),
        "dom_content_loaded_ms": summarize_metric([item.get("dom_content_loaded_ms") for item in dashboard_nav]),
    }
    for scenario in SCENARIOS:
        rows = samples.get(scenario.name, [])
        out[scenario.name] = {
            "total_ms": summarize_metric([row.get("total_ms") for row in rows]),
            "fetch_total_ms": summarize_metric([row.get("fetch_total_ms") for row in rows]),
            "max_fetch_ms": summarize_metric([row.get("max_fetch_ms") for row in rows]),
            "long_task_total_ms": summarize_metric([row.get("long_task_total_ms") for row in rows]),
        }
    return out


def print_summary(base_url: str, samples: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> None:
    print(f"Base URL: {base_url}")
    print()
    dash_targets = [row.get("targets") or {} for row in samples.get("dashboard", [])]
    first_targets = dash_targets[0] if dash_targets else {}
    print(
        "Targets: "
        f"sensors={first_targets.get('sensor_count', 0)}, "
        f"switch_links={first_targets.get('switch_settings_links', 0)}, "
        f"first_sensor={first_targets.get('sensor_id', '') or '-'}, "
        f"first_switch={first_targets.get('switch_id', '') or '-'}"
    )
    print()
    dash = summary.get("dashboard", {})
    print("Dashboard")
    print(f"  load_event_ms: {json.dumps(dash.get('load_event_ms'))}")
    print(f"  dom_content_loaded_ms: {json.dumps(dash.get('dom_content_loaded_ms'))}")
    print()
    for scenario in SCENARIOS:
        block = summary.get(scenario.name, {})
        print(scenario.label)
        print(f"  total_ms: {json.dumps(block.get('total_ms'))}")
        print(f"  fetch_total_ms: {json.dumps(block.get('fetch_total_ms'))}")
        print(f"  max_fetch_ms: {json.dumps(block.get('max_fetch_ms'))}")
        print(f"  long_task_total_ms: {json.dumps(block.get('long_task_total_ms'))}")
        print()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    chrome = ChromeSession(resolve_chrome_path(args.chrome_path), args.debug_port)
    chrome.start()
    target_ws = chrome.create_target("about:blank")
    timeout_ms = int(args.timeout_sec * 1000)
    try:
        async with CDPClient(target_ws) as client:
            await client.setup()
            await client.evaluate(build_js_helper(timeout_ms))
            results: dict[str, list[dict[str, Any]]] = {
                "dashboard": [],
                **{scenario.name: [] for scenario in SCENARIOS},
            }

            for index in range(args.samples):
                await client.navigate(args.base_url, args.timeout_sec)
                await client.evaluate(build_js_helper(timeout_ms))
                results["dashboard"].append(await collect_dashboard_sample(client))
                await pause_between_scenarios(client, args.cooldown_ms)

                for scenario in SCENARIOS:
                    sample = await collect_scenario_sample(client, scenario)
                    sample["sample_index"] = index + 1
                    results[scenario.name].append(sample)
                    await pause_between_scenarios(client, args.cooldown_ms)

            summary = build_summary(results)
            payload = {
                "base_url": args.base_url,
                "sample_count": args.samples,
                "summary": summary,
                "samples": results,
            }
            return payload
    finally:
        chrome.stop()


def main() -> int:
    args = parse_args()
    try:
        payload = asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Profiler failed: {exc}", file=sys.stderr)
        return 1

    print_summary(args.base_url, payload["samples"], payload["summary"])
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Raw JSON written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
