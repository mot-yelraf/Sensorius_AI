"""Read Ecowitt data from a fresh process after repeated connection failures.

The worker accepts only known read endpoints and never loads runtime settings
or sends smart-plug commands. Its parent enforces a hard execution deadline.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 512 * 1024
RECOVERY_TIMEOUT_SEC = 8
READ_ENDPOINTS = frozenset({
    "get_version", "get_network_info", "get_sensors_info",
    "get_livedata_info", "get_rain_totals",
})


def request_in_fresh_process(base_url: str, endpoint: str, params: dict) -> Any:
    """Return one bounded read-only response using the installed interpreter."""
    if endpoint not in READ_ENDPOINTS:
        raise ValueError("Unsupported Ecowitt recovery endpoint.")
    if getattr(sys, "frozen", False):
        raise RuntimeError("Ecowitt recovery requires a Python runtime.")
    result = subprocess.run(
        [sys.executable, "-m", "sensorius.saiEcowittRecovery"],
        input=json.dumps({"base_url": base_url, "endpoint": endpoint, "params": params}),
        capture_output=True, text=True, timeout=RECOVERY_TIMEOUT_SEC,
        cwd=Path(__file__).resolve().parents[1], check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Ecowitt recovery worker exited with code {result.returncode}.")
    if len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("Ecowitt recovery response was unexpectedly large.")
    reply = json.loads(result.stdout)
    if not isinstance(reply, dict) or "payload" not in reply:
        error = reply.get("error") if isinstance(reply, dict) else None
        raise RuntimeError(error or "Invalid Ecowitt recovery response.")
    return reply["payload"]


def main() -> None:
    """Serve one validated request on stdin without reading or writing settings."""
    from .saiEcowitt import normalize_gateway_url

    try:
        request = json.load(sys.stdin)
        base_url = normalize_gateway_url(request["base_url"])
        endpoint, params = request["endpoint"], request["params"]
        if endpoint not in READ_ENDPOINTS or not isinstance(params, dict):
            raise ValueError("Unsupported Ecowitt recovery request.")
        with httpx.stream(
            "GET", f"{base_url}/{endpoint}", params=params or None,
            timeout=5, follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("Gateway response was unexpectedly large.")
            payload = json.loads(content)
        reply = json.dumps({"payload": payload})
        if len(reply.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("Ecowitt recovery response was unexpectedly large.")
    except Exception as exc:
        reply = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    print(reply)


if __name__ == "__main__":
    main()
