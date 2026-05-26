#!/usr/bin/env python3
"""Live Astral geolocation diagnostic for Sensorius.

This is intentionally separate from the pytest regression tests. It contacts
the configured IP geolocation providers and prints the coordinates that the
running host can resolve.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiSettings import saiSettings


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "--"


def _provider_details(timeout: float) -> list[dict[str, object]]:
    import httpx

    rows: list[dict[str, object]] = []
    with httpx.Client(timeout=timeout) as client:
        for provider, url in saiSettings.IP_GEOLOCATION_PROVIDERS:
            row: dict[str, object] = {
                "provider": provider,
                "url": url,
                "status": "",
                "lat": None,
                "lon": None,
                "tz": "",
                "city": "",
                "region": "",
                "ip": "",
                "error": "",
            }
            try:
                resp = client.get(url)
                row["status"] = resp.status_code
                payload = resp.json() or {}
                if payload.get("success") is False:
                    row["error"] = payload.get("message") or "unsuccessful response"
                elif str(payload.get("status") or "").strip().lower() == "fail":
                    row["error"] = payload.get("message") or "failed response"
                else:
                    lat, lon, tz_name = saiSettings._extract_ip_geolocation(payload)
                    row["lat"] = lat
                    row["lon"] = lon
                    row["tz"] = tz_name
                    row["city"] = str(payload.get("city") or "").strip()
                    row["region"] = str(payload.get("region") or payload.get("regionName") or "").strip()
                    row["ip"] = str(payload.get("ip") or payload.get("query") or "").strip()
            except Exception as exc:
                row["error"] = f"{exc.__class__.__name__}: {exc}"
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report live Astral location resolution for this Sensorius host."
    )
    parser.add_argument(
        "--force-auto",
        action="store_true",
        help="Temporarily ignore saved manual lat/lon and test IP geolocation.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist an IP-geolocation result to the active Sensorius settings.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request provider timeout in seconds.",
    )
    parser.add_argument(
        "--all-providers",
        action="store_true",
        help="Print each configured provider response for comparison.",
    )
    args = parser.parse_args()

    settings = saiSettings(apply_live=False)
    active_path = Path(settings.get_active_settings_path())
    saved_lat = str(settings.get_setting("Astral", "LATITUDE", "") or "").strip()
    saved_lon = str(settings.get_setting("Astral", "LONGITUDE", "") or "").strip()
    saved_tz = str(settings.get_setting("Astral", "TIMEZONE", "") or "").strip()
    saved_source = str(settings.get_setting("Astral", "SOURCE", "") or "").strip()
    saved_provider = str(settings.get_setting("Astral", "PROVIDER", "") or "").strip()

    if args.force_auto:
        settings.set_many_in_memory(
            [
                ("Astral", "LATITUDE", ""),
                ("Astral", "LONGITUDE", ""),
                ("Astral", "TIMEZONE", ""),
                ("Astral", "SOURCE", ""),
                ("Astral", "PROVIDER", ""),
            ]
        )

    resolved = settings.resolve_astral_location(
        persist_if_auto=bool(args.persist),
        timeout_sec=float(args.timeout),
    )

    print(f"Active settings: {active_path}")
    print("Saved Astral:")
    print(f"  latitude:  {_text(saved_lat)}")
    print(f"  longitude: {_text(saved_lon)}")
    print(f"  timezone:  {_text(saved_tz)}")
    print(f"  source:    {_text(saved_source)}")
    print(f"  provider:  {_text(saved_provider)}")
    if saved_lat and saved_lon and not args.force_auto:
        print("  note: saved coordinates are present; use --force-auto to test IP geolocation.")

    print("Resolved Astral:")
    print(f"  source:    {_text(resolved.get('source'))}")
    print(f"  provider:  {_text(resolved.get('provider'))}")
    print(f"  latitude:  {_text(resolved.get('lat'))}")
    print(f"  longitude: {_text(resolved.get('lon'))}")
    print(f"  timezone:  {_text(resolved.get('tz'))}")
    print(f"  altitude:  {_text(resolved.get('altitude'))}")
    if resolved.get("error"):
        print(f"  error:     {resolved.get('error')}")
    if args.persist:
        persisted = resolved.get("source") == "ip" and resolved.get("lat") is not None and resolved.get("lon") is not None
        print(f"Persisted: {'yes' if persisted else 'no'}")
    if args.all_providers:
        print("Provider Details:")
        for row in _provider_details(float(args.timeout)):
            print(f"  {row['provider']}:")
            print(f"    status:    {_text(row.get('status'))}")
            print(f"    latitude:  {_text(row.get('lat'))}")
            print(f"    longitude: {_text(row.get('lon'))}")
            print(f"    timezone:  {_text(row.get('tz'))}")
            print(f"    city:      {_text(row.get('city'))}")
            print(f"    region:    {_text(row.get('region'))}")
            print(f"    ip:        {_text(row.get('ip'))}")
            if row.get("error"):
                print(f"    error:     {row.get('error')}")

    ok = resolved.get("lat") is not None and resolved.get("lon") is not None and bool(str(resolved.get("tz") or "").strip())
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
