#!/usr/bin/env python3
"""
Test Nodus endpoint responsiveness for /hayd and /itaot.

Behavior:
- 11 iterations
- GET /hayd, then wait 1s
- GET /itaot, then wait 13s
- Print request durations (and failures) with timestamps
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx


HOST = "apvpd-luvk44.local"
PORT = 8000
ITERATIONS = 11

BETWEEN_ENDPOINTS_S = 1.0   # delay between /hayd and /itaot
LOOP_DELAY_S = 13.0         # delay after /itaot before next loop

# Tight-ish but realistic. Tune if you need.
TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)

HEADERS = {
    "Accept": "application/json",
    "Connection": "close",
    "User-Agent": "SensoriusNodusProbe/1.0",
}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(sec: float | None) -> str:
    return "FAIL" if sec is None else f"{sec:.3f}s"


async def _get_with_timing(client: httpx.AsyncClient, url: str) -> tuple[float | None, int | None, Any]:
    t0 = time.perf_counter()
    try:
        r = await client.get(url, headers=HEADERS)
        dt = time.perf_counter() - t0
        # Try JSON, but don't fail the timing if JSON parse fails
        body: Any
        try:
            body = r.json()
        except Exception:
            body = r.text
        return dt, r.status_code, body
    except Exception as e:
        dt = time.perf_counter() - t0
        return None, None, f"{type(e).__name__}: {e} (after {dt:.3f}s)"


async def main() -> None:
    base = f"http://{HOST}:{PORT}"
    hayd_url = f"{base}/hayd"
    itaot_url = f"{base}/itaot"

    print(f"[{_ts()}] Target: {base}")
    print(f"[{_ts()}] Iterations={ITERATIONS}, between_endpoints={BETWEEN_ENDPOINTS_S}s, loop_delay={LOOP_DELAY_S}s")
    print("")

    hayd_times: list[float] = []
    itaot_times: list[float] = []
    hayd_fail = 0
    itaot_fail = 0

    # Disable keepalive to better reflect "fresh" connections each time.
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=5)

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits, http2=False) as client:
        for i in range(1, ITERATIONS + 1):
            print(f"[{_ts()}] --- Iteration {i}/{ITERATIONS} ---")

            # /hayd
            dt, code, body = await _get_with_timing(client, hayd_url)
            if dt is None:
                hayd_fail += 1
                print(f"[{_ts()}] /hayd  -> {_fmt(dt)}  code={code}  err={body}")
            else:
                hayd_times.append(dt)
                status = None
                if isinstance(body, dict):
                    status = body.get("STATUS")
                print(f"[{_ts()}] /hayd  -> {_fmt(dt)}  code={code}  STATUS={status}")

            time.sleep(BETWEEN_ENDPOINTS_S)

            # /itaot
            dt, code, body = await _get_with_timing(client, itaot_url)
            if dt is None:
                itaot_fail += 1
                print(f"[{_ts()}] /itaot -> {_fmt(dt)}  code={code}  err={body}")
            else:
                itaot_times.append(dt)
                keys = None
                if isinstance(body, dict):
                    keys = list(body.keys())
                print(f"[{_ts()}] /itaot -> {_fmt(dt)}  code={code}  keys={keys}")

            print("")
            time.sleep(LOOP_DELAY_S)

    def _summary(name: str, times: list[float], fails: int) -> None:
        if not times:
            print(f"{name}: no successes (fails={fails})")
            return
        times_sorted = sorted(times)
        n = len(times_sorted)
        p50 = times_sorted[int(0.50 * (n - 1))]
        p90 = times_sorted[int(0.90 * (n - 1))]
        print(
            f"{name}: n_ok={n} n_fail={fails} "
            f"min={times_sorted[0]:.3f}s p50={p50:.3f}s p90={p90:.3f}s max={times_sorted[-1]:.3f}s"
        )

    print("========== Summary ==========")
    _summary("/hayd ", hayd_times, hayd_fail)
    _summary("/itaot", itaot_times, itaot_fail)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
