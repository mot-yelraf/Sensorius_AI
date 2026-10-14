"""Discover halt-capable Nodus devices and confirm their MQTT halt results.

Halt leaves relay levels held and preserves hub settings and historical data.
Only the final correlated result confirms that application filesystem sync finished.
"""
from __future__ import annotations


def halt_device_ids(ingest) -> list[str]:
    """Return physical MQTT targets explicitly advertising halt support."""
    if ingest is None:
        return []
    removed = getattr(ingest, "is_nodus_device_removed", lambda _device: False)
    devices = []
    for device, meta in list((getattr(ingest, "discovery_cache", {}) or {}).items()):
        if not isinstance(meta, dict):
            continue
        capabilities = meta.get("capabilities")
        if isinstance(capabilities, dict) and capabilities.get("halt") is True and not removed(device):
            devices.append(device)
    return sorted(set(devices))


async def halt_device(ingest, device_id: str) -> dict:
    """Send one halt request and require a successful final halted result."""
    outcome = {"device_id": device_id, "halted": False}
    published = ingest.publish_nodus_halt(device_id)
    if not published.get("ok"):
        return {**outcome, "error": "Failed to publish halt request"}
    message_id = published["message_id"]
    # The result is authoritative even when the acceptance ACK was lost.
    result = await ingest.wait_for_config_result(message_id, timeout=12.0)
    if not isinstance(result, dict):
        return {**outcome, "error": "Halt result not received; device state is unconfirmed"}
    if result.get("device_id") != device_id or result.get("message_id") != message_id:
        return {**outcome, "error": "Halt result identity mismatch"}
    if result.get("applied") is not True or result.get("halt") is not True or result.get("phase") != "halted" or result.get("error"):
        return {**outcome, "error": result.get("error") or "Device did not confirm halt"}
    return {**outcome, "halted": True, "relays": result.get("relays")}
