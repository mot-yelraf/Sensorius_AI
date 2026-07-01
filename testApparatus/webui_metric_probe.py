#!/usr/bin/env python3
"""Long-running Sensorius Web UI metric update probe.

This probe keeps a real dashboard page open in headless Chrome while also
polling the dashboard JSON endpoint directly. The combined view helps separate
frontend refresh stalls from backend route stalls.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import urllib.parse
from typing import Any

import requests

try:
    from profile_webui import CDPClient, ChromeSession, resolve_chrome_path
except ModuleNotFoundError:  # pragma: no cover - useful when run as a module
    from testApparatus.profile_webui import CDPClient, ChromeSession, resolve_chrome_path


DEFAULT_BASE_URL = "http://127.0.0.1:8000/"


def parse_args() -> argparse.Namespace:
    default_output = Path(tempfile.gettempdir()) / (
        "sensorius_webui_metric_probe_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".jsonl"
    )
    parser = argparse.ArgumentParser(description="Monitor Sensorius Web UI metric updates over time.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Dashboard base URL.")
    parser.add_argument("--sensor-id", default="All", help="Dashboard sensor_id value to monitor.")
    parser.add_argument("--duration-sec", type=float, default=3600.0, help="Run duration. Use 0 for unlimited.")
    parser.add_argument("--sample-sec", type=float, default=15.0, help="Seconds between browser samples.")
    parser.add_argument("--backend-timeout-sec", type=float, default=12.0, help="Backend JSON request timeout.")
    parser.add_argument("--stall-sec", type=float, default=75.0, help="Age threshold before a refresh is stale.")
    parser.add_argument("--reload-on-stall", action="store_true", help="Reload the dashboard after a stale frontend alert.")
    parser.add_argument("--chrome-path", default="", help="Explicit Chrome/Chromium binary path.")
    parser.add_argument("--debug-port", type=int, default=9242, help="Chrome DevTools port.")
    parser.add_argument("--output-jsonl", default=str(default_output), help="Path for JSONL probe output.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip initial backend reachability check.")
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_base_url(base_url: str) -> str:
    text = str(base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    if not text.endswith("/"):
        text += "/"
    return text


def dashboard_json_url(base_url: str, sensor_id: str) -> str:
    parsed = urllib.parse.urlsplit(normalize_base_url(base_url))
    query = urllib.parse.urlencode({"json_only": "true", "sensor_id": sensor_id or "All"})
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def stable_hash(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except Exception:
        raw = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


def latest_timestamp(timestamps: dict[str, Any]) -> str:
    values = [str(item or "").strip() for item in (timestamps or {}).values() if str(item or "").strip()]
    return max(values) if values else ""


def summarize_backend_payload(payload: dict[str, Any]) -> dict[str, Any]:
    values = payload.get("values") if isinstance(payload, dict) else {}
    timestamps = payload.get("timestamps") if isinstance(payload, dict) else {}
    values = values if isinstance(values, dict) else {}
    timestamps = timestamps if isinstance(timestamps, dict) else {}
    metric_count = 0
    for metric_map in values.values():
        if isinstance(metric_map, dict):
            metric_count += len(metric_map)
    return {
        "sensor_count": len(values),
        "metric_count": metric_count,
        "latest_timestamp": latest_timestamp(timestamps),
        "timestamps": dict(timestamps),
        "values_hash": stable_hash(values),
    }


def poll_backend(base_url: str, sensor_id: str, timeout_sec: float) -> dict[str, Any]:
    url = dashboard_json_url(base_url, sensor_id)
    started = time.monotonic()
    try:
        response = requests.get(url, timeout=max(float(timeout_sec), 1.0))
        duration_ms = (time.monotonic() - started) * 1000.0
        body: dict[str, Any] = {}
        error = ""
        if response.ok:
            try:
                body = response.json()
            except Exception as exc:
                error = f"json_error:{exc}"
        else:
            error = f"http_{response.status_code}"
        summary = summarize_backend_payload(body) if response.ok and not error else {}
        return {
            "ok": bool(response.ok and not error),
            "url": url,
            "status": int(response.status_code),
            "duration_ms": round(duration_ms, 1),
            "error": error,
            **summary,
        }
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        return {
            "ok": False,
            "url": url,
            "status": 0,
            "duration_ms": round(duration_ms, 1),
            "error": str(exc),
        }


PROBE_INSTALL_JS = r"""
(() => {
  const state = window.__sensoriusMetricProbe = window.__sensoriusMetricProbe || {
    installedAtMs: Date.now(),
    console: [],
    errors: [],
    fetches: [],
    updateRuns: [],
    metricMutations: [],
    lastMetricMutationAtMs: 0,
    lastJsonFetchAtMs: 0,
    lastJsonFetchOk: null,
    lastJsonFetchStatus: 0,
    lastJsonFetchError: '',
    lastJsonPayloadAtMs: 0,
    lastJsonPayloadLatestTimestamp: '',
    lastJsonPayloadTimestamps: {},
    lastJsonPayloadValuesHash: '',
    updateWrapInstalled: false,
    observerInstalled: false,
    consoleInstalled: false,
    fetchInstalled: false,
    errorInstalled: false
  };
  const pushBounded = (list, item, limit) => {
    list.push(item);
    while (list.length > limit) list.shift();
  };
  if (!state.consoleInstalled) {
    state.consoleInstalled = true;
    ['debug', 'info', 'warn', 'error'].forEach((level) => {
      const original = console[level] ? console[level].bind(console) : console.log.bind(console);
      console[level] = (...args) => {
        pushBounded(state.console, {
          atMs: Date.now(),
          level,
          message: args.map((item) => {
            try { return typeof item === 'string' ? item : JSON.stringify(item); }
            catch (_err) { return String(item); }
          }).join(' ')
        }, 200);
        return original(...args);
      };
    });
  }
  if (!state.errorInstalled) {
    state.errorInstalled = true;
    window.addEventListener('error', (event) => {
      pushBounded(state.errors, {
        atMs: Date.now(),
        type: 'error',
        message: String(event.message || ''),
        source: String(event.filename || ''),
        line: Number(event.lineno || 0)
      }, 200);
    });
    window.addEventListener('unhandledrejection', (event) => {
      pushBounded(state.errors, {
        atMs: Date.now(),
        type: 'unhandledrejection',
        message: String(event.reason || '')
      }, 200);
    });
  }
  if (!state.fetchInstalled && window.fetch) {
    state.fetchInstalled = true;
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const started = performance.now();
      const input = args[0];
      const url = typeof input === 'string' ? input : String((input && input.url) || '');
      try {
        const response = await originalFetch(...args);
        const durationMs = Number((performance.now() - started).toFixed(1));
        const item = { atMs: Date.now(), url, ok: !!response.ok, status: Number(response.status || 0), durationMs };
        pushBounded(state.fetches, item, 300);
        if (url.includes('json_only=true')) {
          state.lastJsonFetchAtMs = item.atMs;
          state.lastJsonFetchOk = item.ok;
          state.lastJsonFetchStatus = item.status;
          state.lastJsonFetchError = '';
          try {
            response.clone().json().then((payload) => {
              const timestamps = (payload && payload.timestamps && typeof payload.timestamps === 'object') ? payload.timestamps : {};
              const values = (payload && payload.values && typeof payload.values === 'object') ? payload.values : {};
              const timestampValues = Object.keys(timestamps).map((key) => String(timestamps[key] || '')).filter(Boolean).sort();
              state.lastJsonPayloadAtMs = Date.now();
              state.lastJsonPayloadLatestTimestamp = timestampValues.length ? timestampValues[timestampValues.length - 1] : '';
              state.lastJsonPayloadTimestamps = timestamps;
              try { state.lastJsonPayloadValuesHash = String(JSON.stringify(values).length) + ':' + String(Object.keys(values).length); }
              catch (_err) { state.lastJsonPayloadValuesHash = ''; }
            }).catch((err) => {
              state.lastJsonFetchError = String(err);
            });
          } catch (err) {
            state.lastJsonFetchError = String(err);
          }
        }
        return response;
      } catch (err) {
        const durationMs = Number((performance.now() - started).toFixed(1));
        const item = { atMs: Date.now(), url, ok: false, status: 0, durationMs, error: String(err) };
        pushBounded(state.fetches, item, 300);
        if (url.includes('json_only=true')) {
          state.lastJsonFetchAtMs = item.atMs;
          state.lastJsonFetchOk = false;
          state.lastJsonFetchStatus = 0;
          state.lastJsonFetchError = String(err);
        }
        throw err;
      }
    };
  }
  const installObserver = () => {
    if (state.observerInstalled) return;
    const targets = Array.from(document.querySelectorAll('.metric-current-value'));
    if (!targets.length) return;
    state.observerInstalled = true;
    const observer = new MutationObserver((records) => {
      state.lastMetricMutationAtMs = Date.now();
      for (const record of records) {
        const el = record.target && record.target.nodeType === 3 ? record.target.parentElement : record.target;
        pushBounded(state.metricMutations, {
          atMs: Date.now(),
          id: String((el && el.id) || ''),
          text: String((el && el.textContent) || '').trim()
        }, 200);
      }
    });
    targets.forEach((el) => observer.observe(el, { childList: true, characterData: true, subtree: true }));
  };
  const wrapUpdateGauges = () => {
    if (state.updateWrapInstalled || typeof window.updateGauges !== 'function') return;
    const originalUpdateGauges = window.updateGauges;
    if (originalUpdateGauges.__metricProbeWrapped) {
      state.updateWrapInstalled = true;
      return;
    }
    const wrapped = async function(...args) {
      const run = { atMs: Date.now(), ok: null, durationMs: 0, error: '' };
      pushBounded(state.updateRuns, run, 200);
      const started = performance.now();
      try {
        const result = await originalUpdateGauges.apply(this, args);
        run.ok = true;
        return result;
      } catch (err) {
        run.ok = false;
        run.error = String(err);
        throw err;
      } finally {
        run.durationMs = Number((performance.now() - started).toFixed(1));
      }
    };
    wrapped.__metricProbeWrapped = true;
    window.updateGauges = wrapped;
    state.updateWrapInstalled = true;
  };
  installObserver();
  wrapUpdateGauges();
  if (!state.installTimer) {
    state.installTimer = setInterval(() => {
      try { installObserver(); } catch (_err) {}
      try { wrapUpdateGauges(); } catch (_err) {}
    }, 1000);
  }
  return true;
})()
"""


BROWSER_SNAPSHOT_JS = r"""
(() => {
  const state = window.__sensoriusMetricProbe || {};
  const nowMs = Date.now();
  const metricCards = Array.from(document.querySelectorAll('.metric-container[data-sensor][data-metric]'));
  const metrics = metricCards.map((card) => {
    const valueEl = card.querySelector('.metric-current-value');
    return {
      sensor_id: String(card.getAttribute('data-sensor') || ''),
      metric: String(card.getAttribute('data-metric') || ''),
      value_text: String((valueEl && valueEl.textContent) || '').trim()
    };
  });
  let lastJson = null;
  let lastJsonAtMs = 0;
  try { if (typeof __lastJsonOnly !== 'undefined') lastJson = __lastJsonOnly; } catch (_err) {}
  try { if (typeof __lastJsonOnlyAtMs !== 'undefined') lastJsonAtMs = Number(__lastJsonOnlyAtMs || 0); } catch (_err) {}
  const timestamps = (lastJson && lastJson.timestamps && typeof lastJson.timestamps === 'object') ? lastJson.timestamps : {};
  const timestampValues = Object.keys(timestamps).map((key) => String(timestamps[key] || '')).filter(Boolean).sort();
  return {
    url: String(window.location.href || ''),
    ready_state: String(document.readyState || ''),
    hidden: !!document.hidden,
    update_time_text: String((document.getElementById('update_time') || {}).textContent || '').trim(),
    metric_count: metrics.length,
    metrics,
    metrics_hash: JSON.stringify(metrics),
    last_json_age_sec: lastJsonAtMs ? Number(((nowMs - lastJsonAtMs) / 1000).toFixed(1)) : null,
    last_json_latest_timestamp: timestampValues.length ? timestampValues[timestampValues.length - 1] : '',
    update_in_flight: !!window.__updateGaugesInFlight,
    update_in_flight_age_sec: window.__updateGaugesStartedAt ? Number(((nowMs - window.__updateGaugesStartedAt) / 1000).toFixed(1)) : 0,
    update_queued: !!window.__updateGaugesQueued,
    update_run_seq: Number(window.__updateGaugesRunSeq || 0),
    update_finished_seq: Number(window.__updateGaugesFinishedSeq || 0),
    update_finished_age_sec: window.__updateGaugesFinishedAt ? Number(((nowMs - window.__updateGaugesFinishedAt) / 1000).toFixed(1)) : null,
    update_last_duration_ms: Number(window.__updateGaugesLastDurationMs || 0),
    update_last_ok: window.__updateGaugesLastOk,
    update_last_error: String(window.__updateGaugesLastError || ''),
    probe: {
      installed: !!window.__sensoriusMetricProbe,
      last_metric_mutation_age_sec: state.lastMetricMutationAtMs ? Number(((nowMs - state.lastMetricMutationAtMs) / 1000).toFixed(1)) : null,
      last_json_fetch_age_sec: state.lastJsonFetchAtMs ? Number(((nowMs - state.lastJsonFetchAtMs) / 1000).toFixed(1)) : null,
      last_json_payload_age_sec: state.lastJsonPayloadAtMs ? Number(((nowMs - state.lastJsonPayloadAtMs) / 1000).toFixed(1)) : null,
      last_json_payload_latest_timestamp: String(state.lastJsonPayloadLatestTimestamp || ''),
      last_json_payload_values_hash: String(state.lastJsonPayloadValuesHash || ''),
      last_json_fetch_ok: state.lastJsonFetchOk,
      last_json_fetch_status: Number(state.lastJsonFetchStatus || 0),
      last_json_fetch_error: String(state.lastJsonFetchError || ''),
      update_runs: (state.updateRuns || []).slice(-20),
      fetches: (state.fetches || []).slice(-40),
      console: (state.console || []).slice(-40),
      errors: (state.errors || []).slice(-40)
    }
  };
})()
"""


def compact_browser_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot.get("metrics") if isinstance(snapshot, dict) else []
    if not isinstance(metrics, list):
        metrics = []
    compact = dict(snapshot)
    compact["metrics_hash"] = stable_hash(metrics)
    compact["metrics"] = metrics[:20]
    return compact


def classify_sample(browser: dict[str, Any], backend: dict[str, Any], stall_sec: float) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if not backend.get("ok"):
        alerts.append({"kind": "backend_unreachable", "detail": str(backend.get("error") or backend.get("status") or "")})
        return alerts

    probe = browser.get("probe") if isinstance(browser.get("probe"), dict) else {}
    last_json_age = browser.get("last_json_age_sec")
    payload_age = probe.get("last_json_payload_age_sec")
    fetch_age = probe.get("last_json_fetch_age_sec")
    effective_json_age = payload_age if payload_age is not None else last_json_age
    if effective_json_age is None and fetch_age is None:
        alerts.append({"kind": "frontend_no_json_seen", "detail": "browser has not observed a json_only refresh"})
    elif effective_json_age is not None and float(effective_json_age) > stall_sec:
        alerts.append({"kind": "frontend_json_stale", "age_sec": float(effective_json_age)})
    elif fetch_age is not None and float(fetch_age) > stall_sec:
        alerts.append({"kind": "frontend_fetch_stale", "age_sec": float(fetch_age)})

    backend_ts = str(backend.get("latest_timestamp") or "")
    frontend_ts = str(probe.get("last_json_payload_latest_timestamp") or browser.get("last_json_latest_timestamp") or "")
    if backend_ts and frontend_ts and backend_ts != frontend_ts:
        age = effective_json_age if effective_json_age is not None else fetch_age
        if age is None or float(age) > stall_sec:
            alerts.append({"kind": "frontend_timestamp_lag", "backend": backend_ts, "frontend": frontend_ts})

    if bool(browser.get("update_in_flight")) and float(browser.get("update_in_flight_age_sec") or 0.0) > stall_sec:
        alerts.append({"kind": "update_gauges_stuck", "age_sec": float(browser.get("update_in_flight_age_sec") or 0.0)})

    if probe.get("last_json_fetch_ok") is False:
        alerts.append(
            {
                "kind": "frontend_fetch_error",
                "status": int(probe.get("last_json_fetch_status") or 0),
                "detail": str(probe.get("last_json_fetch_error") or ""),
            }
        )
    if browser.get("update_last_ok") is False and str(browser.get("update_last_error") or ""):
        alerts.append({"kind": "update_gauges_error", "detail": str(browser.get("update_last_error") or "")})
    return alerts


def write_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def print_sample_line(index: int, browser: dict[str, Any], backend: dict[str, Any], alerts: list[dict[str, Any]]) -> None:
    alert_text = ",".join(str(item.get("kind") or "") for item in alerts) or "-"
    probe = browser.get("probe") if isinstance(browser.get("probe"), dict) else {}
    frontend_ts = str(probe.get("last_json_payload_latest_timestamp") or browser.get("last_json_latest_timestamp") or "-")
    frontend_age = probe.get("last_json_payload_age_sec")
    if frontend_age is None:
        frontend_age = browser.get("last_json_age_sec")
    print(
        f"[{index:05d}] backend_ok={int(bool(backend.get('ok')))} "
        f"backend_ms={backend.get('duration_ms', 0)} "
        f"backend_ts={backend.get('latest_timestamp', '-') or '-'} "
        f"browser_json_age={frontend_age} "
        f"browser_ts={frontend_ts or '-'} "
        f"update_seq={browser.get('update_run_seq', 0)}/{browser.get('update_finished_seq', 0)} "
        f"metrics={browser.get('metric_count', 0)} alerts={alert_text}",
        flush=True,
    )


async def run(args: argparse.Namespace) -> int:
    base_url = normalize_base_url(args.base_url)
    output_path = Path(args.output_jsonl).expanduser()
    if not args.skip_preflight:
        preflight = poll_backend(base_url, args.sensor_id, args.backend_timeout_sec)
        if not preflight.get("ok"):
            raise RuntimeError(f"Backend preflight failed: {preflight.get('error') or preflight.get('status')}")

    chrome = ChromeSession(resolve_chrome_path(args.chrome_path), args.debug_port)
    stop_requested = False

    def _stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    previous_handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    chrome.start()
    target_ws = chrome.create_target("about:blank")
    started = time.monotonic()
    sample_index = 0
    try:
        async with CDPClient(target_ws) as client:
            await client.setup()
            await client.navigate(base_url, max(args.backend_timeout_sec, 20.0))
            await client.evaluate(PROBE_INSTALL_JS)
            print(f"Probe output: {output_path}", flush=True)
            write_jsonl(
                output_path,
                {
                    "type": "start",
                    "at": utc_now_iso(),
                    "base_url": base_url,
                    "sensor_id": args.sensor_id,
                    "stall_sec": args.stall_sec,
                },
            )

            while not stop_requested:
                if args.duration_sec and (time.monotonic() - started) >= args.duration_sec:
                    break
                sample_index += 1
                await client.evaluate(PROBE_INSTALL_JS)
                browser_raw = await client.evaluate(BROWSER_SNAPSHOT_JS)
                browser = compact_browser_snapshot(browser_raw if isinstance(browser_raw, dict) else {})
                backend = poll_backend(base_url, args.sensor_id, args.backend_timeout_sec)
                alerts = classify_sample(browser, backend, args.stall_sec)
                record = {
                    "type": "sample",
                    "at": utc_now_iso(),
                    "index": sample_index,
                    "browser": browser,
                    "backend": backend,
                    "alerts": alerts,
                }
                write_jsonl(output_path, record)
                print_sample_line(sample_index, browser, backend, alerts)
                if alerts and args.reload_on_stall and any(str(item.get("kind", "")).startswith("frontend_") for item in alerts):
                    write_jsonl(output_path, {"type": "action", "at": utc_now_iso(), "action": "reload", "alerts": alerts})
                    await client.navigate(base_url, max(args.backend_timeout_sec, 20.0))
                    await client.evaluate(PROBE_INSTALL_JS)
                await asyncio.sleep(max(float(args.sample_sec), 1.0))

            write_jsonl(output_path, {"type": "stop", "at": utc_now_iso(), "samples": sample_index})
            return 0
    finally:
        chrome.stop()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
