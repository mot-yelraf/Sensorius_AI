"""Track official NWS weather events and once-per-event automation receipts.

The hub polls independently of browser sessions, only for the US forecast
provider and an Astral point validated by NWS. Event identities and actor
receipts survive restarts; CAP updates refresh details without retriggering.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import __version__
from .saiUtils import printDM
from .saiWeatherForecast import normalize_weather_forecast_provider

POLL_SECONDS = 60
US_STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI".split())
HEADERS = {"User-Agent": f"Sensorius/{__version__} https://github.com/mot-yelraf/Sensorius_AI",
           "Accept": "application/geo+json"}
VTEC = re.compile(r"/[OTEX]\.\w+\.([A-Z0-9]{4})\.([A-Z]{2})\.([WAY])\.(\d{4})\.(\d{6}T\d{4}Z)-\d{6}T\d{4}Z/")


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def event_message(event: dict, tz_name: str = "UTC") -> str:
    """Describe event type, validity, expected duration, area and instructions."""
    try:
        tz = ZoneInfo(tz_name)
    except (ValueError, KeyError):
        tz = timezone.utc

    def fmt(value):
        stamp = _epoch(value)
        return datetime.fromtimestamp(stamp, tz).strftime("%b %d, %Y %I:%M %p %Z") if stamp is not None else "Not specified"

    start = event.get("onset") or event.get("effective")
    end = event.get("ends") or event.get("expires")
    start_epoch, end_epoch = _epoch(start), _epoch(end)
    duration = ""
    if start_epoch is not None and end_epoch is not None and end_epoch >= start_epoch:
        minutes = round((end_epoch - start_epoch) / 60)
        duration = f" (about {minutes // 60}h {minutes % 60}m)"
    lines = [f"Severe Weather — {event.get('event', 'NWS alert')}",
             str(event.get("headline") or ""),
             f"Starts: {fmt(start)}\nEnds: {fmt(end)}{duration}",
             f"Area: {event.get('areaDesc') or 'Configured Astral location'}",
             f"Severity: {event.get('severity') or 'Unknown'}; certainty: {event.get('certainty') or 'Unknown'}",
             str(event.get("description") or ""), str(event.get("instruction") or ""),
             f"Source: {event.get('senderName') or 'US National Weather Service'}"]
    return "\n".join(line for line in lines if line)


class WeatherAlertService:
    """Poll NWS and expose cached active events to automation and web views."""

    def __init__(self, *, settings, data_logger, supervisor=None):
        self.settings = settings
        self.data_logger = data_logger
        self.supervisor = supervisor
        self.records = data_logger.load_weather_alert_state()
        self._active_keys = set()
        self._location = None
        self._timezone = "UTC"
        self._verified_until = 0.0
        self._checked = 0.0
        self.reason = "waiting"
        self.stale = False

    def enabled(self):
        """Return whether the selected forecast provider is US NWS."""
        return normalize_weather_forecast_provider(self.settings.get_setting("WeatherForecast", "PROVIDER", "met_no")) == "us"

    def snapshot(self):
        """Return unexpired warnings without performing network or disk I/O."""
        now = time.time()
        enabled = self.enabled()
        events = []
        if enabled and self._location is not None:
            for key in sorted(self._active_keys):
                record = self.records.get(key, {})
                event = record.get("event", {})
                effective = _epoch(event.get("effective")) or 0
                expires = _epoch(event.get("expires")) or 0
                end = _epoch(event.get("ends")) or expires
                if effective <= now < min(expires, end):
                    events.append(dict(event, event_key=key, message=event_message(event, self._timezone)))
        return {"enabled": enabled, "active": bool(events), "events": events,
                "message": "\n\n".join(event["message"] for event in events),
                "stale": self.stale, "checked_at": self._checked,
                "reason": self.reason if enabled else "provider_disabled"}

    def pending(self, events, actor_key):
        """Return events which have not triggered this automation actor."""
        return [event for event in events if actor_key not in self.records.get(event["event_key"], {}).get("triggered", [])]

    def mark_triggered(self, events, actor_key):
        """Persist an actor receipt so CAP updates and restarts do not repeat it."""
        updated = copy.deepcopy(self.records)
        for event in events:
            record = updated.get(event["event_key"])
            if record is not None and actor_key not in record.setdefault("triggered", []):
                record["triggered"].append(actor_key)
        self.data_logger.save_weather_alert_state(updated)
        self.records = updated

    def queue_alert(self, payload, actor_key):
        """Atomically retain a dashboard message and its once-only actor receipt."""
        updated = copy.deepcopy(self.records)
        for key in payload["weather_event_ids"]:
            record = updated[key]
            alert_id = hashlib.sha256(f"{key}:{actor_key}".encode()).hexdigest()
            payload["weather_alert_id"] = alert_id
            record.setdefault("alerts", {}).setdefault(alert_id, dict(payload))
            if actor_key not in record.setdefault("triggered", []):
                record["triggered"].append(actor_key)
        self.data_logger.save_weather_alert_state(updated)
        self.records = updated

    def pending_alerts(self):
        """Return undismissed messages for currently validated active events."""
        return [payload for event in self.snapshot()["events"]
                for payload in self.records[event["event_key"]].get("alerts", {}).values()
                if not payload.get("dismissed")]

    def dismiss_alert(self, alert_id):
        """Persist a dismissal independently of the automation trigger receipt."""
        updated = copy.deepcopy(self.records)
        for record in updated.values():
            payload = record.get("alerts", {}).get(alert_id)
            if payload is not None:
                payload["dismissed"] = True
                self.data_logger.save_weather_alert_state(updated)
                self.records = updated
                return True
        return False

    @staticmethod
    def actor_key(rule_id, controller_id, action, rearm_revision=None):
        """Identify an actor independently of rule descriptions and condition edits."""
        actor = {key: action.get(key) for key in ("type", "switch_key", "to", "set")}
        identity = [rule_id, controller_id, actor]
        if rearm_revision:
            identity.append(rearm_revision)
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def ingest(self, payload):
        """Apply one complete active-alert snapshot, preserving event identities."""
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise ValueError("NWS alerts response has no feature collection")
        now = time.time()
        records = copy.deepcopy(self.records)
        aliases = {alias: key for key, record in records.items() for alias in record.get("aliases", [])}
        active = set()
        for feature in sorted(features, key=lambda row: str((row.get("properties") or {}).get("sent") or "")):
            event = feature.get("properties") or {}
            identifier = str(event.get("id") or feature.get("id") or "")
            if not identifier or event.get("status") != "Actual":
                continue
            references = [str(ref.get("identifier") or ref.get("@id") or "") for ref in event.get("references", []) if isinstance(ref, dict)]
            event_aliases = [identifier] + references
            vtec_keys = []
            for raw in (event.get("parameters") or {}).get("VTEC", []):
                for office, phenomenon, significance, number, start in VTEC.findall(str(raw)):
                    year = start[:2] if not start.startswith("000000") else str(event.get("sent") or "")[2:4]
                    vtec_keys.append(f"vtec:{year}:{office}:{phenomenon}:{significance}:{number}")
            event_aliases += vtec_keys
            key = next((aliases[alias] for alias in event_aliases if alias in aliases), None)
            key = key or (sorted(vtec_keys)[0] if vtec_keys else identifier)
            old = records.get(key, {})
            record = {"event": event, "aliases": sorted(set(old.get("aliases", []) + event_aliases)),
                      "triggered": old.get("triggered", []), "alerts": old.get("alerts", {}), "seen": now}
            records[key] = record
            aliases.update({alias: key for alias in record["aliases"]})
            name = str(event.get("event") or "").lower()
            categories = event.get("category") or []
            if event.get("messageType") == "Cancel":
                active.discard(key)
            elif (name.endswith(" warning") or name.endswith(" watch")) and (not categories or "Met" in categories or "Geo" in categories):
                active.add(key)
        # Keep receipts well beyond alert expiry, without unbounded growth.
        records = {key: value for key, value in records.items()
                   if key in active or now - float(value.get("seen", 0)) < 90 * 86400}
        self.data_logger.save_weather_alert_state(records)
        self.records, self._active_keys = records, active
        self._checked, self.stale, self.reason = now, False, "ok"

    async def refresh(self):
        """Validate the Astral US location and refresh the location-filtered feed."""
        await asyncio.to_thread(self.settings.get_section, "WeatherForecast", reload_if_changed=True)
        if not self.enabled():
            self._active_keys.clear()
            self._location = None
            self.reason = "provider_disabled"
            return
        try:
            resolved = await asyncio.to_thread(self.settings.resolve_astral_location, persist_if_auto=False, timeout_sec=2.5)
            lat, lon = float(resolved["lat"]), float(resolved["lon"])
            if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("Invalid Astral coordinates")
        except (KeyError, TypeError, ValueError):
            self._active_keys.clear()
            self._location = None
            self.reason = "location_unavailable"
            return
        location = (round(lat, 4), round(lon, 4))
        if self._location != location:
            self._active_keys.clear()
            self._location = None
            self._verified_until = 0
        self._timezone = str(resolved.get("tz") or "UTC")
        async with httpx.AsyncClient(headers=HEADERS, timeout=8.0) as client:
            if time.time() >= self._verified_until:
                response = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
                if response.status_code in (400, 404):
                    self._active_keys.clear()
                    self._location = None
                    self.reason = "outside_us_coverage"
                    return
                response.raise_for_status()
                props = response.json().get("properties") or {}
                state = ((props.get("relativeLocation") or {}).get("properties") or {}).get("state")
                if state not in US_STATES or not props.get("forecastZone"):
                    self._active_keys.clear()
                    self._location = None
                    self.reason = "outside_us_coverage"
                    return
                self._location = location
                self._verified_until = time.time() + 86400
            response = await client.get("https://api.weather.gov/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
            response.raise_for_status()
            self.ingest(response.json())

    async def run(self):
        """Poll once per minute and keep the task supervisor heartbeat current."""
        while True:
            if self.supervisor:
                self.supervisor.feedthedogs("NWS Weather Alerts")
            try:
                await self.refresh()
            except Exception as exc:
                self.stale, self.reason = True, "provider_unavailable"
                printDM(f"NWS alert refresh failed: {exc}", location="saiWeatherAlerts", level="warning")
            for _ in range(POLL_SECONDS // 5):
                if self.supervisor:
                    self.supervisor.feedthedogs("NWS Weather Alerts")
                await asyncio.sleep(5)
