"""Register switch settings, controls, history, and automation editing routes.

All actions use the owning runtime's shared switch controllers, logger, and MQTT
ingest. Explicit callbacks connect existing device settings, access checks, and
dashboard notifications without importing the main routes module.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Set
import copy
import json
import os
import re
import time

from fastapi import APIRouter, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from .saiRuntimeServices import RuntimeServices
from .saiWebRouteState import RouteState
from .saiWebContracts import (
    BoardType, DashboardBroadcast, DeviceNetworkInfo, HostnameResolver, ModalError,
    ProfileLog, ProtectedAccess, PushSettingsBatch, RestartDevice, SettingsFactory,
    SwitchSettingsFactory, SystemRootResolver, SystemSettingsFactory,
)
from .saiUtils import printDM

MODULE = "saiWebRoutes"

from dataclasses import dataclass
from html import escape
from . import __version__ as SAI_APP_VERSION
from .saiAutomationActivation import AutomationActivationError, activate_with_baselines
from .saiInputUnits import normalize_condition_inputs
from .saiWebDiagnostics import _is_true_text, _switch_channels_from_settings
try:
    from .saiDataLogger import build_switch_key as _build_switch_key
except ImportError:
    _build_switch_key = None


@dataclass(frozen=True)
class SwitchRouteDependencies:
    """Supply device, presentation, and access helpers to switch routes."""

    build_device_network_info: DeviceNetworkInfo
    build_switch_statistics_payload: Callable[[str, list[dict]], Awaitable[dict[str, Any]]]
    build_system_hostname_index: Callable[[str], dict[str, str]]
    display_nodus_board_type: BoardType
    env_map_with_defaults: Callable[[], dict[str, str]]
    invalidate_dashboard_caches: Callable[[], None]
    modal_error_response: ModalError
    nodus_values_match: Callable[[object, object], bool]
    read_hostname_from_system_settings: HostnameResolver
    request_nodus_device_restart: RestartDevice
    require_protected_access: ProtectedAccess
    resolve_channel_id_from_label: Callable[[str, str], str | None]
    resolve_system_settings_root: SystemRootResolver
    switch_broadcast: DashboardBroadcast
    ui_profile_log: ProfileLog
    wants_modal_json: Callable[[Request], bool]
    push_nodus_settings_batch: PushSettingsBatch
    switch_channel_id_from_identity_row: Callable[[dict], str]


def register_switch_routes(
    router: APIRouter, *, services: RuntimeServices, route_state: RouteState,
    settings, dependencies: SwitchRouteDependencies, settings_factory: SettingsFactory,
    switch_settings_factory: SwitchSettingsFactory,
    get_system_settings_manager: Callable[[], SystemSettingsFactory | None],
    status_cache_ttl: float, debug: bool = False,
) -> None:
    """Register switch and automation endpoints using explicit runtime ownership."""
    saiSettings = settings_factory
    SwitchSettingsManager = switch_settings_factory
    _SWITCH_STATUS_CACHE_TTL_SEC = status_cache_ttl
    DEBUG = debug
    _build_device_network_info = dependencies.build_device_network_info
    _build_switch_statistics_payload = dependencies.build_switch_statistics_payload
    _build_system_hostname_index = dependencies.build_system_hostname_index
    _display_nodus_board_type = dependencies.display_nodus_board_type
    _env_map_with_defaults = dependencies.env_map_with_defaults
    _invalidate_dashboard_caches = dependencies.invalidate_dashboard_caches
    _modal_error_response = dependencies.modal_error_response
    _nodus_values_match = dependencies.nodus_values_match
    _read_hostname_from_system_settings = dependencies.read_hostname_from_system_settings
    _request_nodus_device_restart = dependencies.request_nodus_device_restart
    _require_protected_access = dependencies.require_protected_access
    _resolve_channel_id_from_label = dependencies.resolve_channel_id_from_label
    _resolve_system_settings_root = dependencies.resolve_system_settings_root
    _switch_broadcast = dependencies.switch_broadcast
    _ui_profile_log = dependencies.ui_profile_log
    _wants_modal_json = dependencies.wants_modal_json
    push_nodus_settings_batch = dependencies.push_nodus_settings_batch
    _switch_channel_id_from_identity_row = dependencies.switch_channel_id_from_identity_row

    @router.post("/advanced/automations/test-bd-transition", response_class=JSONResponse)
    async def test_biodynamic_transition_alert(request: Request):
        """Broadcast a simulated BD transition without changing calendar or rule state."""
        service = getattr(request.app.state, "biodynamic_calendar_service", None)
        broadcaster = getattr(request.app.state, "switch_broadcast", None)
        if service is None or not callable(getattr(service, "current_transition_sync", None)):
            return JSONResponse(
                {"ok": False, "error": "Biodynamic calendar service is unavailable."},
                status_code=503,
            )
        if not callable(broadcaster):
            return JSONResponse(
                {"ok": False, "error": "Dashboard broadcaster is unavailable."},
                status_code=503,
            )

        current = await asyncio.to_thread(service.current_transition_sync)
        if not isinstance(current, dict) or not current.get("transition_at"):
            return JSONResponse(
                {"ok": False, "error": "Current biodynamic segment is unavailable."},
                status_code=503,
            )

        segment = {
            "sign": str(current.get("sign") or ""),
            "element": str(current.get("element") or ""),
            "plant_part": str(current.get("plant_part") or ""),
            "color": str(current.get("color") or ""),
            "accent": str(current.get("accent") or ""),
        }
        payload = {
            "type": "bd_transition",
            "test": True,
            "transition_at": datetime.now().astimezone().isoformat(),
            "from": dict(segment),
            "to": dict(segment),
        }
        await broadcaster(payload)
        return JSONResponse({"ok": True, "message": "Test BD transition broadcast.", "event": payload})


    async def _publish_remote_switch_last_state(
        switch_id: str,
        *,
        channel_index: int,
        channel_id: str,
        channel_label: str,
        new_state: bool,
        system_mgr=None,
        system_root: str | None = None,
        sys_host_index: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        if int(channel_index or 0) <= 0:
            return False, "Missing switch channel index"
        ok = await push_nodus_settings_batch(
            device_id=str(switch_id or "").strip(),
            device_type="switch",
            setting_file_key="switch",
            updates=[("Switch", f"SWITCH_{int(channel_index)}_LAST_STATE", bool(new_state))],
            sensor_file_name=None,
            system_mgr=system_mgr,
            system_root=system_root,
            sys_host_index=sys_host_index,
        )
        if not ok:
            return False, "Failed to publish remote switch config update"
        return True, ""


    def _iter_switch_controllers(web_server) -> Iterable[object]:
        """Yield only controllers that implement switch behavior."""
        controllers = getattr(web_server, "controllers", [])
        for candidate in controllers:
            # Accept anything that looks like a switch controller
            if hasattr(candidate, "get_switch_names") and callable(candidate.get_switch_names):
                yield candidate


    def _normalize_label(label: Optional[str]) -> Optional[str]:
        if not label:
            return None
        return label.strip()


    def _normalize_switch_id(switch_id: Optional[str]) -> Optional[str]:
        if not switch_id:
            return None
        return switch_id.strip().lower()


    def _controller_matches(
        ctrl: object,
        target_label: str,
        target_switch_id: Optional[str],
        target_location: Optional[str] = None,
    ) -> bool:
        try:
            names = set(n.strip() for n in ctrl.get_switch_names() or [])
        except Exception:
            names = set()
        if target_label not in names:
            return False

        if target_switch_id:
            ctrl_switch_id = getattr(ctrl, "switch_id", None)

            if ctrl_switch_id and _normalize_switch_id(ctrl_switch_id) != target_switch_id:
                return False

        if target_location:
            # Optional: allow narrowing by location if provided
            ctrl_loc = getattr(ctrl, "location", None)
            if (ctrl_loc or "").strip().lower() != (target_location or "").strip().lower():
                return False

        return True


    def _resolve_target_from_payload(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Accepts any of:
          - {"key": "switch_id::channel_id"}
          - {"key": "switch_id::label"}     # legacy/tolerated
          - {"switch_id": "...", "label": "..."}
          - {"switch_id": "...", "name": "..."}  # tolerant of older field names
          - {"label": "..."}                 # legacy (ambiguous if duplicates exist)
        Returns (switch_id, label, location) normalized.
        """
        raw_key = payload.get("key") or payload.get("switch_key")
        switch_id = payload.get("switch_id")
        label = payload.get("label") or payload.get("name")
        location = payload.get("location")

        if raw_key and "::" in raw_key:
            switch_id_part, label_part = raw_key.split("::", 1)
            switch_id = switch_id or switch_id_part
            label = label or label_part

        return (
            _normalize_switch_id(switch_id),
            _normalize_label(label),
            (location or None),
        )


    def _switch_key(switch_id: str, label: str) -> str:
        """
        Canonical switch key builder: use saiDataLogger.build_switch_key if present,
        otherwise fall back to '<switch_id>::<channel_id-or-label>'.
        """
        sid = (switch_id or "").strip()
        lab = (label or "").strip()
        ch_id = _resolve_channel_id_from_label(sid, lab)
        if _build_switch_key is not None:
            try:
                return _build_switch_key(sid, ch_id or lab)
            except Exception:
                pass
        return f"{sid}::{ch_id or lab}"


    @router.get("/edit-switch", response_class=HTMLResponse)
    async def edit_switch_page(
        request: Request,
        switch_id: str = Query(...),
        embed: int = Query(0),
    ):
        _route_started = time.monotonic()
        from .saiSwitchSettingsManager import SwitchSettingsManager

        manager = SwitchSettingsManager("switch_settings")
        settings_dict = manager.load(switch_id) or {}
        if not settings_dict:
            # Still return a minimal modal for 404
            html = (
                "<div class='modal-backdrop' style='display:flex'>"
                "<div class='modal'>"
                "<div class='modal-body'>"
                f"<h3>No settings for '{switch_id}'</h3>"
                "</div></div></div>"
            )
            return HTMLResponse(html, status_code=404)

        sw = (settings_dict or {}).get("Switch", {}) or {}
        channels = _switch_channels_from_settings(settings_dict)
        channel_indices = [int(ch.get("index") or 1) for ch in channels]

        ingest = services.mqtt_ingest
        nodus_firmware_version = ""
        switch_type = str(sw.get("TYPE", "") or "").strip().lower()
        if (
            ingest
            and switch_type in ("picow", "pico2w", "nodus", "remote", "mqtt")
            and hasattr(ingest, "get_nodus_firmware_version")
        ):
            try:
                nodus_firmware_version = str(
                    ingest.get_nodus_firmware_version(switch_id, device_type="switch")
                ).strip()
            except Exception:
                nodus_firmware_version = ""
        nodus_board_type = _display_nodus_board_type(
            switch_id,
            device_type="switch",
            settings_block=sw,
        )

        network_info = await _build_device_network_info(switch_id, device_type="switch")
        switch_statistics = await _build_switch_statistics_payload(switch_id, channels)

        # ---- render Jinja template to an HTML snippet ----
        templates = request.app.state.templates
        template = templates.get_template("modals/switch_settings.html")
        modal_html = template.render(
            switch_id=switch_id,
            settings=settings_dict,
            channel_indices=channel_indices,
            channels=channels,
            nodus_firmware_version=nodus_firmware_version,
            nodus_board_type=nodus_board_type,
            can_restart_device=(switch_type in ("picow", "pico2w", "nodus", "remote", "mqtt")),
            network_info=network_info,
            **switch_statistics,
        )

        # ---- embed=1 → just the modal markup (used by dashboard JS) ----
        if embed:
            _ui_profile_log("edit-switch", _route_started, embed=1, switch_id=switch_id, channels=len(channel_indices))
            return HTMLResponse(modal_html)

        # ---- full-page fallback (keeps existing behavior & JS wiring) ----
        page: list[str] = []
        page.append("<html><head><title>Edit Switch</title>")
        page.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
        # ensure app.css is loaded so modal styles look correct
        page.append(f"<link rel='stylesheet' href='/ui_static/css/app.css?v={SAI_APP_VERSION}'>")
        page.append(f"<link rel='stylesheet' href='/ui_static/css/mobile_settings.css?v={SAI_APP_VERSION}'>")
        page.append(f"<script src='/ui_static/js/mobile_settings.js?v={SAI_APP_VERSION}'></script>")
        page.append("</head><body>")
        page.append("<div id='modalHost'></div>")
        modal_json = json.dumps(modal_html).replace("</", "<\\/")
        page.append(f"<script>var __MODAL_HTML__ = {modal_json};</script>")
        page.append("<script>")
        page.append("  (function(){")
        page.append("    if (window.showSwitchSettingsModal) {")
        page.append("      window.showSwitchSettingsModal(__MODAL_HTML__);")
        page.append("    } else {")
        page.append("      var host = document.getElementById('modalHost');")
        page.append("      if (host) {")
        page.append("        host.innerHTML = __MODAL_HTML__;")
        page.append("        var modal = document.getElementById('switchSettingsModal');")
        page.append("        var backdrop = modal && modal.closest('.modal-backdrop');")
        page.append("        if (backdrop) backdrop.style.display = 'flex';")
        page.append("        if (window.activateInlineScripts) {")
        page.append("          window.activateInlineScripts(host);")
        page.append("        } else {")
        page.append("          host.querySelectorAll('script').forEach(function(oldScript) {")
        page.append("            var script = document.createElement('script');")
        page.append("            script.textContent = oldScript.textContent || '';")
        page.append("            oldScript.replaceWith(script);")
        page.append("          });")
        page.append("        }")
        page.append("        var settingsButton = host.querySelector('#switchMenuSettings');")
        page.append("        var infoButton = host.querySelector('#switchMenuStatistics');")
        page.append("        var settingsPane = host.querySelector('#switchSettingsPane');")
        page.append("        var infoPane = host.querySelector('#switchStatisticsPane');")
        page.append("        function selectSwitchPane(showInfo) {")
        page.append("          if (settingsPane) settingsPane.hidden = showInfo;")
        page.append("          if (infoPane) infoPane.hidden = !showInfo;")
        page.append("          if (settingsPane) settingsPane.style.display = showInfo ? 'none' : '';")
        page.append("          if (infoPane) infoPane.style.display = showInfo ? '' : 'none';")
        page.append("          if (settingsButton) settingsButton.classList.toggle('active', !showInfo);")
        page.append("          if (infoButton) infoButton.classList.toggle('active', showInfo);")
        page.append("        }")
        page.append("        if (settingsButton) settingsButton.addEventListener('click', function(){ selectSwitchPane(false); });")
        page.append("        if (infoButton) infoButton.addEventListener('click', function(){ selectSwitchPane(true); });")
        page.append("        selectSwitchPane(false);")
        page.append("        if (window.openSwitchSettingsModal) window.openSwitchSettingsModal();")
        page.append("        if (window.switchModalWire) window.switchModalWire();")
        page.append("      }")
        page.append("    }")
        page.append("  })();")
        page.append("</script>")
        page.append("</body></html>")

        _ui_profile_log("edit-switch", _route_started, embed=0, switch_id=switch_id, channels=len(channel_indices))
        return HTMLResponse(content="\n".join(page))


    @router.post("/submit-switch-settings")
    async def submit_switch_settings(request: Request):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        form = await request.form()

        def normalize_switch_id(raw: str) -> str:
            s = (raw or "").strip().replace(" ", "-")
            return "".join(ch for ch in s if ch.isalnum() or ch in "-_").lower() or "unknown-switch"

        def deep_merge_ordered(base: OrderedDict, update: dict | OrderedDict) -> OrderedDict:
            for k, v in (update or {}).items():
                if isinstance(v, dict):
                    if k not in base or not isinstance(base.get(k), dict):
                        base[k] = OrderedDict()
                    if not isinstance(base[k], OrderedDict):
                        base[k] = OrderedDict(base[k])
                    deep_merge_ordered(base[k], v)
                else:
                    base[k] = v
            return base

        # ---- validate ----
        switch_id_in_form = form.get("switch_id")
        if not switch_id_in_form:
            return _modal_error_response(request, "Missing switch_id", status_code=400)

        old_id = normalize_switch_id(switch_id_in_form)
        manager = SwitchSettingsManager("switch_settings")
        SystemSettingsMgr = get_system_settings_manager()
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)

        location_value = (form.get("location", "") or "").strip()
        new_id = old_id

        existing_doc = manager.load(old_id) or OrderedDict()
        if not isinstance(existing_doc, OrderedDict):
            existing_doc = OrderedDict(existing_doc)
        previous_doc = copy.deepcopy(existing_doc)
        existing_doc.setdefault("Switch", OrderedDict())

        # --- dynamically collect editable channel indices from the form
        idxs = set()
        pat = re.compile(r"^SWITCH_(\d+)_(?:LABEL|LAST_STATE|OVERRIDE_SCRIPT)$")
        for key in form.keys():
            m = pat.match(key)
            if m:
                idxs.add(int(m.group(1)))
        channel_indices = sorted(idxs)

        sw_block = OrderedDict({
            "SWITCH_LOCATION": location_value or existing_doc["Switch"].get("SWITCH_LOCATION", "Unknown"),
        })

        # Merge per-channel updates
        for i in channel_indices:
            label_key   = f"SWITCH_{i}_LABEL"
            last_state_key = f"SWITCH_{i}_LAST_STATE"
            override_key = f"SWITCH_{i}_OVERRIDE_SCRIPT"
            if label_key in form:
                sw_block[label_key] = (form.get(label_key, "") or "").strip()
            if last_state_key in form:
                sw_block[last_state_key] = str(form.get(last_state_key, "") or "").strip().lower() in {"1", "true", "on", "yes"}
            if override_key in form:
                sw_block[override_key] = str(form.get(override_key, "") or "").strip().lower() in {"1", "true", "on", "yes"}

        if existing_doc["Switch"].get("TYPE") == "ecowitt" and "SWITCH_1_LABEL" in sw_block:
            sw_block["SWITCH_1_LABEL"] = sw_block["SWITCH_1_LABEL"] or "Plug"

        merged_doc = deep_merge_ordered(OrderedDict(existing_doc), OrderedDict({"Switch": sw_block}))

        base_dir = Path(getattr(manager, "base_dir", "switch_settings"))
        old_dir = base_dir / old_id
        new_dir = base_dir / new_id

        switch_type = str((merged_doc.get("Switch", {}) or {}).get("TYPE", "") or "").strip().lower()
        if switch_type in ("picow", "pico2w", "nodus"):
            existing_switch_block = previous_doc.get("Switch", {}) if isinstance(previous_doc, dict) else {}
            remote_updates = [
                ("Switch", key, value)
                for key, value in sw_block.items()
                if not key.endswith("_LAST_STATE")
                if not _nodus_values_match(
                    existing_switch_block.get(key) if isinstance(existing_switch_block, dict) else None,
                    value,
                )
            ]
            remote_last_state_updates: list[dict[str, Any]] = []
            for key, value in sw_block.items():
                if not key.endswith("_LAST_STATE"):
                    continue
                if _nodus_values_match(
                    existing_switch_block.get(key) if isinstance(existing_switch_block, dict) else None,
                    value,
                ):
                    continue
                match = re.fullmatch(r"SWITCH_(\d+)_LAST_STATE", str(key or ""))
                if not match:
                    continue
                idx = int(match.group(1))
                current_switch_block = merged_doc.get("Switch", {}) if isinstance(merged_doc, dict) else {}
                previous_label = str(existing_switch_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_label = previous_label or str(current_switch_block.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_id = str(current_switch_block.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                remote_last_state_updates.append(
                    {
                        "index": idx,
                        "channel_id": channel_id,
                        "channel_label": channel_label,
                        "new_state": bool(value),
                    }
                )
            if remote_updates:
                ok = await push_nodus_settings_batch(
                    device_id=old_id,
                    device_type="switch",
                    setting_file_key="switch",
                    updates=remote_updates,
                    sensor_file_name=None,
                    system_mgr=system_mgr,
                    system_root=system_root,
                    sys_host_index=sys_host_index,
                )
                if not ok:
                    if _wants_modal_json(request):
                        return JSONResponse(
                            {"ok": False, "error": "Failed to apply remote switch settings."},
                            status_code=502,
                        )
                    return PlainTextResponse("Failed to apply remote switch settings.", status_code=502)
            for update in remote_last_state_updates:
                ok, err = await _publish_remote_switch_last_state(
                    old_id,
                    channel_index=int(update.get("index") or 0),
                    channel_id=str(update.get("channel_id") or "").strip(),
                    channel_label=str(update.get("channel_label") or "").strip(),
                    new_state=bool(update.get("new_state")),
                    system_mgr=system_mgr,
                    system_root=system_root,
                    sys_host_index=sys_host_index,
                )
                if not ok:
                    err_msg = err or "Failed to apply remote switch state."
                    if _wants_modal_json(request):
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": err_msg,
                                "channel_id": str(update.get("channel_id") or "").strip(),
                                "channel_label": str(update.get("channel_label") or "").strip(),
                            },
                            status_code=502,
                        )
                    return PlainTextResponse(err_msg, status_code=502)

        # Persist the local shadow only after remote applies/commands succeed.
        try:
            manager.save(old_id, merged_doc)   # assumes save can accept a full doc
        except TypeError:
            # Fallback if manager.save expects only partials: try 'save_full' if provided.
            if hasattr(manager, "save_full"):
                manager.save_full(old_id, merged_doc)
            else:
                # Last resort: re-save section-by-section in a deterministic order
                for section_name, section_map in merged_doc.items():
                    manager.save(old_id, OrderedDict([(section_name, section_map)]))

        _invalidate_dashboard_caches()
        try:
            active_controllers = services.switch_controllers
            controller_values = active_controllers.values() if isinstance(active_controllers, dict) else [active_controllers]
            for controller in controller_values:
                if str(getattr(controller, "switch_id", "") or "").strip().lower() == old_id:
                    controller.location = str((merged_doc.get("Switch", {}) or {}).get("SWITCH_LOCATION", "") or "")
        except Exception:
            pass

        if _wants_modal_json(request):
            return JSONResponse({"ok": True, "message": "Switch settings saved.", "switch_id": new_id})
        return RedirectResponse(url="/", status_code=303)


    @router.post("/switch-settings/restart-device")
    async def restart_switch_device(request: Request):
        form = await request.form()
        switch_id = str(form.get("switch_id") or "").strip().replace(" ", "-")
        switch_id = "".join(ch for ch in switch_id if ch.isalnum() or ch in "-_").lower()
        if not switch_id:
            return JSONResponse({"ok": False, "error": "Missing switch_id"}, status_code=400)

        manager = SwitchSettingsManager("switch_settings")
        doc = manager.load(switch_id) or {}
        switch_type = str(((doc.get("Switch", {}) or {}).get("TYPE", "") or "")).strip().lower()
        if switch_type not in {"picow", "pico2w", "nodus", "remote", "mqtt"}:
            return JSONResponse({"ok": False, "error": "Restart is only available for Nodus switches."}, status_code=400)

        SystemSettingsMgr = get_system_settings_manager()
        system_mgr = SystemSettingsMgr("system_settings") if SystemSettingsMgr else None
        system_root = _resolve_system_settings_root(system_mgr)
        sys_host_index = _build_system_hostname_index(system_root)
        target_device = str(
            _read_hostname_from_system_settings(
                switch_id,
                system_mgr,
                system_root,
                device_type="switch",
                sys_host_index=sys_host_index,
            )
            or switch_id
        ).strip()
        printDM(
            f"[restart-request] device_type=switch device_id={switch_id} target={target_device} via=webui",
            location=MODULE,
            level="info",
        )
        ok, message = await _request_nodus_device_restart(
            target_device=target_device,
            device_id=switch_id,
            device_type="switch",
            restart_mode="soft",
        )
        printDM(
            f"[restart-result] device_type=switch device_id={switch_id} target={target_device} ok={ok} message={message}",
            location=MODULE,
            level="info" if ok else "warning",
        )
        status_code = 200 if ok else 502
        payload_key = "message" if ok else "error"
        return JSONResponse(
            {"ok": ok, payload_key: message, "switch_id": switch_id, "target_device": target_device},
            status_code=status_code,
        )


    @router.get("/switch-chooser", response_class=HTMLResponse)
    async def switch_chooser(request: Request):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        import html
        from urllib.parse import quote

        mgr = SwitchSettingsManager("switch_settings")
        ids = mgr.list_switches() or []
        items = "\n".join(
            f'<li><a href="/edit-switch?switch_id={quote(sid)}">{html.escape(sid)}</a></li>'
            for sid in ids
        ) or "<li>No switches found. Add a device first.</li>"
        html_doc = f"""
        <!doctype html><html><head>
          <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
          <title>Select a switch — Sensorius</title>
          <style>body{{font-family:system-ui;margin:2rem}} a{{text-decoration:none}} li{{margin:.4rem 0}}</style>
        </head><body>
          <h2>Select a switch</h2>
          <ul>{items}</ul>
        </body></html>
        """
        return HTMLResponse(html_doc)


    @router.get("/switch-info", response_class=JSONResponse)
    async def api_switch_info(switch_id: str = Query(...)):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        try:
            mgr = SwitchSettingsManager("switch_settings")
            dat = mgr.load(switch_id)
            if not dat:
                return JSONResponse({"error": f"switch_id '{switch_id}' not found"}, status_code=404)

            sw = (dat.get("Switch") or {})

            # 1) Collect labels from SWITCH_<n>_LABEL in the new schema.
            labels: dict[int, str] = {}
            channel_ids: dict[int, str] = {}
            import re
            for k, v in sw.items():
                ks = str(k).strip()
                m = re.fullmatch(r"SWITCH_(\d+)_LABEL", ks, flags=re.IGNORECASE)
                if m:
                    idx = int(m.group(1))
                    label_text = ("" if v is None else str(v)).strip()
                    if not label_text:
                        continue
                    labels[idx] = label_text
                    channel_id_text = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                    if channel_id_text:
                        channel_ids[idx] = channel_id_text

            # 2) Determine channel count from CHANNELS (if present) or from the max SWITCH_<n> index
            try:
                raw_channels = next((sw.get(k) for k in ("CHANNELS", "channels", "Channels") if k in sw), 0)
                channels = int(raw_channels) if str(raw_channels).strip() else 0
            except Exception:
                channels = 0

            if not channels:
                channels = max(labels.keys(), default=1)

            astral_status = {
                "ok": False,
                "source": "",
                "lat": None,
                "lon": None,
                "tz": "",
                "message": "",
            }
            try:
                settings_local = saiSettings(apply_live=False)
                resolved = settings_local.resolve_astral_location(persist_if_auto=False, timeout_sec=2.5) or {}
                lat = resolved.get("lat")
                lon = resolved.get("lon")
                tz_name = str(resolved.get("tz") or "").strip()
                source = str(resolved.get("source") or "").strip()
                ok = lat is not None and lon is not None and bool(tz_name)
                astral_status = {
                    "ok": bool(ok),
                    "source": source,
                    "lat": float(lat) if lat is not None else None,
                    "lon": float(lon) if lon is not None else None,
                    "tz": tz_name,
                    "message": (
                        f"Astral location ready ({source or 'resolved'})."
                        if ok else
                        "Astral location is not currently resolved. Astral automations will evaluate false until location/timezone is available."
                    ),
                }
            except Exception as exc:
                printDM(f"Astral location check failed: {exc}", location=MODULE)
                astral_status = {
                    "ok": False,
                    "source": "",
                    "lat": None,
                    "lon": None,
                    "tz": "",
                    "message": "Astral location check failed. Check the Sensorius log.",
                }

            return {
                "switch_id": switch_id,
                "channels": channels,
                "labels": labels,
                "channel_ids": channel_ids,
                "astral_status": astral_status,
            }
        except Exception as exc:
            printDM(f"/switch-info error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": "Unable to read switch information."}, status_code=500)


    @router.get("/advanced/automations", response_class=JSONResponse)
    async def api_list_advanced_automations(switch_id: str = Query(...)):
        from .saiAutomationManager import AutomationManager
        def _parse_enabled(value) -> bool:
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "off", "no", ""}
            return bool(value)
        def _rule_targets_switch(payload: object, requested_switch_id: str) -> bool:
            wanted_sid = str(requested_switch_id or "").strip().lower()
            if wanted_sid == "__all__":
                return True
            if not wanted_sid:
                return False
            if not isinstance(payload, dict):
                return False
            script_json = (
                str(payload.get("script_json", ""))
                or str(payload.get("script", ""))
                or str(payload.get("json", ""))
                or ""
            )
            if not script_json:
                return False
            try:
                parsed = json.loads(script_json)
            except Exception:
                return False
            if not isinstance(parsed, dict):
                return False
            actions = parsed.get("actions") or []
            if isinstance(actions, dict):
                actions = [actions]
            for action in actions:
                if not isinstance(action, dict):
                    continue
                switch_key = str(action.get("switch_key", "") or "").strip()
                if not switch_key:
                    continue
                if "::" in switch_key:
                    sid_part, _suffix = switch_key.split("::", 1)
                    if str(sid_part or "").strip().lower() == wanted_sid:
                        return True
                    continue
                # Backward compatibility for older rules that stored only a label/channel suffix.
                return True
            return False
        try:
            mgr = AutomationManager("automation_settings")
            data = mgr.load(switch_id) or {}
            legacy_getter = getattr(mgr, "get_legacy_rule_ids", None)
            legacy_rule_ids = (
                set(legacy_getter(switch_id) or set())
                if callable(legacy_getter)
                else set()
            )

            adv = (
                data.get("Advanced")
                or data.get("advanced")
                or data.get("Triggers")
                or data.get("triggers")
                or {}
            )
            items = []
            # Sort by key for stable display
            for rule_id in sorted(adv.keys()):
                payload = adv[rule_id]
                if isinstance(payload, dict):
                    if not _rule_targets_switch(payload, switch_id):
                        continue
                    enabled = _parse_enabled(payload.get("enabled", True))
                    script_json = (
                        str(payload.get("script_json", ""))
                        or str(payload.get("script", ""))
                        or str(payload.get("json", ""))
                        or ""
                    )
                else:
                    continue
                items.append({
                    "rule_id": rule_id,
                    "enabled": enabled,
                    "script_json": script_json,
                    "legacy": str(rule_id) in legacy_rule_ids,
                })

            return {"switch_id": switch_id, "items": items}
        except Exception as exc:
            printDM(f"/advanced/automations error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": "Unable to read Advanced automations."}, status_code=500)


    @router.get("/automation-context", response_class=JSONResponse)
    async def automation_context():
        """Return the system-wide actor directory used by the automation editor."""
        mgr = SwitchSettingsManager("switch_settings")
        actors: list[dict[str, str]] = []
        switch_ids = mgr.list_switches() or []
        for sid in switch_ids:
            doc = mgr.load(sid) or {}
            sw = doc.get("Switch") or {}
            for idx in range(1, 33):
                label = str(sw.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                channel_id = str(sw.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                if not label:
                    continue
                actors.append({
                    "type": "switch",
                    "switch_id": sid,
                    "label": label,
                    "channel_id": channel_id,
                    "value": f"{sid}::{channel_id or label}",
                    "display": f"{sid}:{label}",
                })

        email_enabled = _is_true_text(
            str(
                os.environ.get(
                    "SENSORIUS_EMAIL_ENABLED",
                    _env_map_with_defaults().get("SENSORIUS_EMAIL_ENABLED", "false"),
                )
                or ""
            )
        )
        return {
            "actors": actors,
            "email_enabled": email_enabled,
            "executor_switch_id": "__system__",
            "calendar_timezone": str(settings.get_setting("Astral", "TIMEZONE", "") or settings.get_setting("Time", "TZ", "UTC") or "UTC"),
        }


    def _resolve_automation_target(switch_id: str, switch_key: str) -> tuple[str, str, int | None]:
        sid = switch_id
        suffix = str(switch_key or "").strip()
        if "::" in suffix:
            sid, suffix = suffix.split("::", 1)
            sid = str(sid or switch_id).strip() or switch_id
            suffix = str(suffix or "").strip()
        label = suffix
        channel_index = None
        try:
            switch_mgr = SwitchSettingsManager("switch_settings")
            doc = switch_mgr.load(sid) or {}
            sw_map = doc.get("Switch") or {}
            for idx in range(1, 33):
                cand_label = str(sw_map.get(f"SWITCH_{idx}_LABEL", "") or "").strip()
                cand_channel_id = str(sw_map.get(f"SWITCH_{idx}_CHANNEL_ID", "") or "").strip()
                if not cand_label:
                    continue
                if suffix.lower() == cand_label.lower() or (cand_channel_id and suffix.lower() == cand_channel_id.lower()):
                    label = cand_label
                    channel_index = idx
                    break
        except Exception:
            pass
        return sid, label, channel_index


    async def _broadcast_automation_states(app: FastAPI, mgr, switch_id: str, switch_keys: set[str]) -> None:
        if not hasattr(app.state, "switch_broadcast"):
            return
        for switch_key in (switch_keys or set()):
            state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
            sid, label, _channel_index = _resolve_automation_target(switch_id, switch_key)
            await app.state.switch_broadcast({
                "type": "automation_toggle",
                "switch_id": sid,
                "label": label,
                "enabled": bool(state.get("enabled_any", False)),
            })


    async def _activate_automation_rules(request, manager, hostname, rules, commit):
        """Serialize UI activation and prepare switch baselines before persistence."""
        if not hasattr(request.app.state, "automation_activation_lock"):
            request.app.state.automation_activation_lock = asyncio.Lock()
        controllers = services.switch_controllers
        try:
            return await activate_with_baselines(
                manager, hostname, rules, controllers, commit,
                lock=request.app.state.automation_activation_lock,
                mqtt_ingest=services.mqtt_ingest,
            )
        finally:
            route_state.switch_status_payload = None
            route_state.switch_status_until = 0.0


    @router.post("/advanced/automations/enable", response_class=JSONResponse)
    async def api_enable_advanced_automation(
        request: Request,
        switch_id: str = Form(...),
        rule_id: str = Form(...),
        enabled: str = Form("true"),  # accept str, coerce below
    ):
        from .saiAutomationManager import AutomationManager
        try:
            truthy = str(enabled).strip().lower() in {"1", "true", "on", "yes"}
            mgr = AutomationManager("automation_settings")
            previous = (mgr.load(switch_id).get("Advanced") or {}).get(rule_id)
            rules = {rule_id: previous} if truthy and previous and not previous.get("enabled", True) else {}
            ok = await _activate_automation_rules(
                request, mgr, switch_id, rules,
                lambda: mgr.set_rule_enabled(switch_id, section="Advanced", rule_id=rule_id, enabled=truthy),
            )
            if ok:
                data = mgr.load(switch_id) or {}
                adv = (data.get("Advanced") or {})
                payload = adv.get(rule_id) if isinstance(adv, dict) else None
                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                # Sync per-channel override flags based on aggregate enabled state
                try:
                    switch_mgr = SwitchSettingsManager("switch_settings")
                    for switch_key in switch_keys:
                        state = mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                        eff_enabled = bool(state.get("enabled_any", False))
                        sid, label, channel_index = _resolve_automation_target(switch_id, switch_key)
                        if sid and channel_index:
                            override_val = (not eff_enabled)
                            switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_val)
                            try:
                                sc = services.switch_controllers
                                if isinstance(sc, dict):
                                    for ctrl in sc.values():
                                        if getattr(ctrl, "switch_id", None) == sid:
                                            if isinstance(getattr(ctrl, "override_script", None), dict):
                                                ctrl.override_script[label] = override_val
                                elif sc and getattr(sc, "switch_id", None) == sid:
                                    if isinstance(getattr(sc, "override_script", None), dict):
                                        sc.override_script[label] = override_val
                            except Exception:
                                pass
                except Exception:
                    pass

                await _broadcast_automation_states(request.app, mgr, switch_id, switch_keys)
            return {"ok": bool(ok)}
        except AutomationActivationError as exc:
            return JSONResponse({"ok": False, "error": exc.public_message}, status_code=409)
        except Exception as exc:
            printDM(f"/advanced/automations/enable error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": "Failed to update automation enablement."}, status_code=500)


    @router.post("/advanced/automations/delete", response_class=JSONResponse)
    async def api_delete_advanced_automation(
        request: Request,
        switch_id: str = Form(...),
        rule_id: str = Form(...),
    ):
        from .saiAutomationManager import AutomationManager
        try:
            mgr = AutomationManager("automation_settings")
            data = mgr.load(switch_id) or {}
            adv = (data.get("Advanced") or {})
            payload = adv.get(rule_id) if isinstance(adv, dict) else None
            switch_keys: set[str] = set()
            if isinstance(payload, dict):
                script_json = payload.get("script_json", "")
                try:
                    script = json.loads(str(script_json))
                    for action in (script.get("actions") or []):
                        if not isinstance(action, dict):
                            continue
                        switch_key = (action.get("switch_key") or "").strip()
                        if switch_key:
                            switch_keys.add(switch_key)
                except Exception:
                    pass

            ok = mgr.delete_rule(switch_id, section="Advanced", rule_id=rule_id)
            if ok:
                await _broadcast_automation_states(request.app, mgr, switch_id, switch_keys)
            return {"ok": bool(ok)}
        except Exception as exc:
            printDM(f"/advanced/automations/delete error: {exc}", location="saiWebRoutes")
            return JSONResponse({"error": "Unable to delete Advanced automation."}, status_code=500)


    @router.post("/submit-advanced-trigger")
    async def submit_advanced_trigger(request: Request):
        """
        Persists an Advanced trigger script to automation_settings/automations.toml
        Accepts form or JSON payloads.

        Expected fields:
          - type            (required)
          - switch_id       (required)
          - channel         (e.g., "1") OR switch_selector (fallback)
          - rule_id         (optional; default: "SWITCH_<channel>_Advanced"; made unique if collides)
          - script_json     (required) JSON string built in the Switch Automations pane
          - enabled         (optional; default: true)
        """
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        def norm_switch_id(raw: str) -> str:
            s = (raw or "").strip().replace(" ", "-")
            return "".join(ch for ch in s if ch.isalnum() or ch in "-_").lower() or "unknown-switch"

        def parse_bool(v: str | None, default: bool = True) -> bool:
            if v is None:
                return default
            return str(v).strip().lower() in ("1", "true", "on", "yes")

        def error_response(message: str, status_code: int = 400):
            if request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"ok": False, "error": message}, status_code=status_code)
            return HTMLResponse(f"<h3>{escape(message)}</h3><a href='/'>Return</a>", status_code=status_code)

        async def read_payload():
            ctype = request.headers.get("content-type", "")
            if ctype.startswith("application/json"):
                j = await request.json()
                return {k: ("" if v is None else v) for k, v in j.items()}
            form = await request.form()
            return {k: ("" if v is None else v) for k, v in form.items()}

        # ---------- read & basic validation ----------
        payload = await read_payload()

        raw_switch_id = str(payload.get("switch_id", "")).strip()
        if not raw_switch_id:
            return HTMLResponse("<h3>Missing switch_id</h3><a href='/'>Return</a>", status_code=400)
        switch_id = norm_switch_id(raw_switch_id)

        # channel can come as 'channel' or 'switch_selector'
        channel_str = str(payload.get("channel", payload.get("switch_selector", "1"))).strip() or "1"
        try:
            channel = int(channel_str)
        except Exception:
            channel = 1

        # script JSON required
        script_json_raw = str(payload.get("script_json", "")).strip()
        if not script_json_raw:
            return HTMLResponse("<h3>Missing script_json</h3><a href='/'>Return</a>", status_code=400)

        # small size guard (64KB)
        if len(script_json_raw.encode("utf-8")) > 65536:
            return HTMLResponse("<h3>script_json too large</h3><a href='/'>Return</a>", status_code=413)

        enabled = parse_bool(str(payload.get("enabled", "true")))
        rule_id = str(payload.get("rule_id", "")).strip()

        # Load switch settings early; needed for action switch-key normalization.
        settings_mgr = SwitchSettingsManager("switch_settings")
        sw_doc = settings_mgr.load(switch_id) or {}
        switch_map = sw_doc.get("Switch") or {}

        # ---------- normalize/compact script ----------
        # Try to parse & normalize a bit; if it fails, store raw string (your runtime can validate later)
        is_json_request = request.headers.get("content-type", "").startswith("application/json")

        compact_script = script_json_raw
        try:
            parsed = json.loads(script_json_raw) if script_json_raw else {}

            def _num(v, typ=float, default=None):
                try:
                    return typ(v)
                except Exception:
                    return default

            # CONDITIONS (your UI already matches these keys)
            normalized_conditions = []
            save_anchor_epoch = int(time.time())
            for c in parsed.get("conditions", []) or []:
                cond_type = str(c.get("type", "")).strip().lower()
                if cond_type == "sensor":
                    try:
                        c = normalize_condition_inputs(c)
                    except (ValueError, TypeError) as exc:
                        printDM(f"Invalid automation sensor condition: {exc}", location=MODULE)
                        return error_response("Invalid sensor condition. Check the value, hysteresis, and units.", status_code=400)

                # normalize days-of-week for time-of-day conditions (0–6 = Mon–Sun)
                raw_days = c.get("days") or []
                days_norm: list[int] = []
                for d in raw_days:
                    try:
                        n = int(d)
                    except Exception:
                        continue
                    if 0 <= n <= 6:
                        days_norm.append(n)

                duration_min = _num(c.get("duration_min"), int, None)
                freq_hours = _num(c.get("freq_hours"), int, None)
                period_min = _num(c.get("period_min"), int, None)
                if period_min is None and isinstance(freq_hours, int) and freq_hours > 0:
                    period_min = freq_hours * 60
                anchor_epoch = _num(c.get("anchor_epoch"), int, None)
                if cond_type == "timer":
                    if duration_min is None or duration_min <= 0:
                        return error_response("Timer duration must be at least 1 minute.", status_code=400)
                    if period_min is None or period_min <= 0:
                        return error_response("Timer Every value is invalid.", status_code=400)
                    if duration_min >= period_min:
                        return error_response("Timer duration must be less than Every.", status_code=400)
                    if period_min < 60:
                        anchor_epoch = save_anchor_epoch

                astral_event_raw = str(c.get("astral_event", c.get("event", "sunrise"))).strip().lower()
                astral_event_aliases = {
                    "sunrise-sunset": "sunrise_to_sunset",
                    "sunset-sunrise": "sunset_to_sunrise",
                }
                astral_event = astral_event_aliases.get(astral_event_raw, astral_event_raw)
                if astral_event not in {"sunrise", "sunset", "sunrise_to_sunset", "sunset_to_sunrise"}:
                    astral_event = "sunrise"

                normalized_condition = {
                    "type":   cond_type,  # 'sensor' / 'time' / 'astral' / 'timer' / 'or'
                    "sensor": str(c.get("sensor",  c.get("sensor_id", ""))).strip(),
                    "metric": str(c.get("metric",  "")).strip(),
                    "op":     str(c.get("op",      ">")).strip(),
                    "value":  _num(c.get("value"), float, None),
                    "hyst":   _num(c.get("hyst"),  float, None),
                    "start":  str(c.get("start",   "")).strip(),
                    "end":    str(c.get("end",     "")).strip(),
                    "astral_event": astral_event,
                    "offset_min": _num(c.get("offset_min", c.get("offset_minutes")), int, 0),
                    # new optional fields
                    "days":        days_norm or None,
                    "duration_min": duration_min,
                    "freq_hours":   freq_hours,
                    "period_min":   period_min,
                    "anchor_epoch": anchor_epoch,
                }
                if cond_type == "calendar":
                    from .saiCalendarAutomation import LUNAR_PHASES, normalize_calendar_datetime
                    from .saiSeasonCalendar import SEASON_EVENTS

                    kind = str(c.get("calendar_kind", "bd_transitions")).strip().lower()
                    event = str(c.get("calendar_event", "all")).strip().lower()
                    choices = {"bd_transitions": {"all", "earth", "water", "air", "fire"},
                               "lunar_events": {"all", *LUNAR_PHASES},
                               "season": {"all", *SEASON_EVENTS}, "date_time": {"all"}}
                    if kind not in choices or event not in choices[kind]:
                        return error_response("Invalid Calendar event selection.", status_code=400)
                    if kind == "date_time":
                        tz_name = str(c.get("calendar_timezone") or settings.get_setting("Astral", "TIMEZONE", "")
                                      or settings.get_setting("Time", "TZ", "UTC") or "UTC")
                        try:
                            target = normalize_calendar_datetime(c.get("calendar_local", ""), tz_name)
                        except (ValueError, KeyError, TypeError) as exc:
                            printDM(f"Invalid automation calendar target: {exc}", location=MODULE)
                            return error_response("Invalid Calendar date/time or timezone.", status_code=400)
                        normalized_condition["calendar_local"] = str(c.get("calendar_local"))
                        normalized_condition["calendar_timezone"] = tz_name
                        normalized_condition["calendar_at"] = target
                    normalized_condition["calendar_kind"] = kind
                    normalized_condition["calendar_event"] = event
                    normalized_condition["offset_min"] = 0
                if cond_type in {"bd_transitions", "calendar"}:
                    normalized_condition["executor_switch_id"] = norm_switch_id(
                        str(c.get("executor_switch_id", switch_id) or switch_id)
                    )
                normalized_conditions.append(normalized_condition)

            # ACTIONS: accept UI shape (switch_label/set) OR legacy (switch/state/delay)
            raw_actions = parsed.get("actions", [])
            if isinstance(raw_actions, dict):  # legacy single-action shape
                raw_actions = [raw_actions]

            normalized_actions = []
            label_to_channel_id: dict[str, str] = {}
            channel_num_to_label: dict[int, str] = {}
            try:
                for n in range(1, 33):
                    label = str((switch_map or {}).get(f"SWITCH_{n}_LABEL", "") or "").strip()
                    channel_id = str((switch_map or {}).get(f"SWITCH_{n}_CHANNEL_ID", "") or "").strip()
                    if label:
                        channel_num_to_label[n] = label
                        if channel_id:
                            label_to_channel_id[label.lower()] = channel_id
            except Exception:
                pass

            def _automation_action_key(raw_key: str) -> str:
                text = str(raw_key or "").strip()
                if not text:
                    return ""
                if "::" in text:
                    sid_part, suffix_part = text.split("::", 1)
                    sid_part = str(sid_part or switch_id).strip() or switch_id
                    suffix_part = str(suffix_part or "").strip()
                else:
                    sid_part = switch_id
                    suffix_part = text

                m = re.fullmatch(r"CH(\d+)", suffix_part, flags=re.IGNORECASE)
                if m:
                    label = channel_num_to_label.get(int(m.group(1)), "")
                    suffix_part = label or suffix_part

                channel_id = label_to_channel_id.get(suffix_part.lower(), "")
                suffix_final = channel_id or suffix_part
                return f"{sid_part}::{suffix_final}" if sid_part and suffix_final else ""

            for a in (raw_actions or []):
                action_type = str(a.get("type", "switch") or "switch").strip().lower()
                if action_type == "none":
                    normalized_actions.append({
                        "type": "none",
                        "executor_switch_id": norm_switch_id(
                            str(a.get("executor_switch_id", switch_id) or switch_id)
                        ),
                    })
                    continue
                if action_type == "notify":
                    notify_to = str(a.get("to", "") or "").strip()
                    executor_switch_id = norm_switch_id(
                        str(a.get("executor_switch_id", switch_id) or switch_id)
                    )
                    if "@" not in notify_to or "\r" in notify_to or "\n" in notify_to:
                        return error_response("Notify actions require a valid To email address.", status_code=400)
                    normalized_actions.append({
                        "type": "notify",
                        "to": notify_to,
                        "executor_switch_id": executor_switch_id,
                    })
                    continue
                raw_set = a.get("set", a.get("state", "off"))
                set_on = str(raw_set).strip().lower() in {"on", "true", "1"}
                switch_key = str(a.get("switch_key", a.get("switch_label", ""))).strip()
                switch_key = _automation_action_key(switch_key)
                raw_revert_action = str(a.get("revert_action", "") or "").strip().lower()
                revert_action = "do_nothing" if raw_revert_action == "do_nothing" else "previous_state"

                normalized_actions.append({
                    "type":       "switch",
                    "switch_key": switch_key,
                    "switch":     _num(a.get("switch"), int, None),  # optional numeric channel (legacy)
                    "set":        set_on,
                    "revert_action": revert_action,
                    "delay_s":    _num(a.get("delay_s", a.get("delay")), int, 0) or 0,
                })

            try:
                # Rewrite any action switch_key that references CHn or labels -> canonical switch_id::channel_id
                for a in normalized_actions:
                    if str(a.get("type", "switch") or "switch").strip().lower() == "switch":
                        a["switch_key"] = _automation_action_key(a.get("switch_key") or "")
            except Exception:
                pass

            normalized_logic = str(parsed.get("logic", "AND")).upper()
            if normalized_logic not in ("AND", "OR"):
                normalized_logic = "AND"

            normalized = {
                "logic": normalized_logic,
                "conditions": normalized_conditions,
                "actions": normalized_actions
            }

            compact_payload = {
                "name":       parsed.get("name", ""),
                "enabled":    bool(parsed.get("enabled", True)),
                "conditions": normalized_conditions,
                "actions":    normalized_actions,  #
                #"logic": normalized_logic,  # include only if your runtime still needs it
            }
            compact_script = json.dumps(compact_payload, separators=(",", ":"))
            printDM(f"[{MODULE}] Parsed Advanced script: {compact_script}", location=MODULE)
        except Exception as e:
            if '"value_unit"' in script_json_raw or '"hyst_unit"' in script_json_raw:
                printDM(f"Invalid unit-aware automation: {e}", location=MODULE)
                return error_response("Invalid unit-aware automation. Check the condition values and units.", status_code=400)
            printDM(f"[{MODULE}] Advanced script JSON parse failed; storing raw. Error: {e}", location=MODULE)

        # ---------- verify/adjust channel against settings ----------
        # derive available indices from keys when CHANNELS missing
        indices = []
        for k in switch_map.keys():
            m = re.match(r"^SWITCH_(\d+)$", str(k))
            if m:
                try:
                    indices.append(int(m.group(1)))
                except Exception:
                    pass
        if not indices:
            # fallback to CHANNELS or at least [1]
            ch_count = 0
            try:
                ch_count = int(switch_map.get("CHANNELS", 0))
            except Exception:
                ch_count = 0
            indices = list(range(1, (ch_count or 1) + 1))

        if channel not in indices:
            # clamp to a valid one (prefer 1)
            channel = indices[0] if indices else 1
            printDM(f"[{MODULE}] Adjusted channel to {channel} for {switch_id}", location=MODULE)

        # ---------- rule_id handling ----------
        incoming_rule_id = rule_id
        if not rule_id:
            rule_id = f"SWITCH_{channel}_Advanced"

        # only auto-generated ids should be uniquified; explicit ids from the UI
        # represent an edit-in-place of an existing automation.
        def _unique_rule_id(existing_ids: set[str], base: str) -> str:
            if base not in existing_ids:
                return base
            i = 2
            while True:
                cand = f"{base}_{i}"
                if cand not in existing_ids:
                    return cand
                i += 1

        try:
            trig_mgr = AutomationManager("automation_settings")
            existing = trig_mgr.load(switch_id) or {}
            existing_advanced = (existing or {}).get("Advanced", {}) or {}
            existing_ids = set(existing_advanced.keys())
            final_rule_id = rule_id if incoming_rule_id else _unique_rule_id(existing_ids, rule_id)
            previous_rule = (
                existing_advanced.get(final_rule_id)
                if isinstance(existing_advanced, dict)
                else None
            )
            await _activate_automation_rules(
                request, trig_mgr, switch_id,
                {final_rule_id: {"script_json": compact_script}}
                if enabled and json.loads(compact_script).get("enabled", True) else {},
                lambda: trig_mgr.upsert_advanced_rule(
                    hostname=switch_id,
                    rule_id=final_rule_id,
                    enabled=enabled,
                    script=compact_script,
                ),
            )

            printDM(f"[{MODULE}] Saved Advanced trigger {rule_id} -> {final_rule_id} for {switch_id}", location=MODULE)
            # The system-wide editor persists under "__system__", while actions
            # target real switch IDs. Reload each old/new action target so edits
            # take effect without restarting Sensorius.
            def _rule_target_switch_ids(rule_payload: object) -> set[str]:
                if not isinstance(rule_payload, dict):
                    return set()
                script_payload = rule_payload.get("script_json", "")
                try:
                    script_doc = (
                        script_payload
                        if isinstance(script_payload, dict)
                        else json.loads(str(script_payload))
                    )
                except Exception:
                    return set()
                target_ids: set[str] = set()
                for action in (script_doc.get("actions") or []):
                    if not isinstance(action, dict):
                        continue
                    action_key = str(action.get("switch_key", "") or "").strip()
                    if "::" not in action_key:
                        continue
                    target_id = action_key.split("::", 1)[0].strip()
                    if target_id:
                        target_ids.add(target_id)
                return target_ids

            reload_switch_ids = _rule_target_switch_ids(previous_rule)
            reload_switch_ids.update(
                _rule_target_switch_ids({"script_json": compact_script})
            )
            if switch_id != "__system__":
                reload_switch_ids.add(switch_id)

            try:
                sc = services.switch_controllers

                eval_controllers: list[object] = []
                switch_mgr = SwitchSettingsManager("switch_settings")
                sensor_source = services.sensor_map
                sensor_candidates = (
                    sensor_source.values()
                    if isinstance(sensor_source, dict)
                    else sensor_source
                )

                for target_switch_id in sorted(reload_switch_ids):
                    found_ctrl = next(
                        (
                            ctrl
                            for ctrl in sc.values()
                            if str(getattr(ctrl, "switch_id", "") or "").strip().lower()
                            == target_switch_id.lower()
                        ),
                        None,
                    )

                    if found_ctrl is None:
                        sw_doc = switch_mgr.load(target_switch_id) or {}
                        if isinstance(sw_doc, dict) and (sw_doc.get("Switch") or {}):
                            from .saiSwitch import build_switch_controller

                            sw_loc = str(
                                (sw_doc.get("Switch") or {}).get(
                                    "SWITCH_LOCATION", ""
                                )
                                or ""
                            ).strip().lower()
                            sensor_match = None
                            for candidate in (sensor_candidates or []):
                                try:
                                    if (
                                        str(
                                            getattr(candidate, "location", "") or ""
                                        ).strip().lower()
                                        == sw_loc
                                    ):
                                        sensor_match = candidate
                                        break
                                except Exception:
                                    continue

                            candidate_ctrl = build_switch_controller(
                                switch_settings=sw_doc,
                                supervisor=None,
                                sensor=sensor_match,
                                data_logger=services.data_logger,
                                dashboard_broadcast=_switch_broadcast,
                            )
                            if bool(getattr(candidate_ctrl, "is_present", False)):
                                sc[target_switch_id] = candidate_ctrl
                                found_ctrl = candidate_ctrl

                    if found_ctrl is None:
                        continue

                    rules_cache = getattr(found_ctrl, "_rules_cache", None)
                    if isinstance(rules_cache, dict):
                        rules_cache["mtime"] = None
                    eval_controllers.append(found_ctrl)

                    monitor_name = f"{target_switch_id} Controladora Monitor"
                    supervisor = services.supervisor
                    supervised_names = getattr(supervisor, "_task_names", set())
                    existing_task = route_state.dynamic_switch_monitor_tasks.get(
                        target_switch_id
                    )
                    if (
                        monitor_name not in supervised_names
                        and (existing_task is None or existing_task.done())
                    ):
                        route_state.dynamic_switch_monitor_tasks[target_switch_id] = (
                            asyncio.create_task(
                                found_ctrl.run_controladora_monitor(
                                    getattr(found_ctrl, "sensor", None)
                                ),
                                name=f"{monitor_name} (dynamic)",
                            )
                        )
                        printDM(
                            f"[{MODULE}] started dynamic switch monitor for "
                            f"{target_switch_id}",
                            location=MODULE,
                        )

                # Evaluate immediately; the monitor remains the steady-state path.
                for eval_ctrl in eval_controllers:
                    current_values_map = getattr(eval_ctrl, "values", {}) or {}
                    eval_advanced = getattr(
                        eval_ctrl, "_evaluate_and_apply_advanced", None
                    )
                    if callable(eval_advanced):
                        eval_advanced(current_values_map)

                route_state.switch_status_payload = None
                route_state.switch_status_until = 0.0
            except Exception as _reload_exc:
                if DEBUG:
                    printDM(
                        f"[{MODULE}] live automation reload failed: {_reload_exc}",
                        location=MODULE,
                    )

            # Sync per-channel override flags based on aggregate enabled state.
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                payload = None
                try:
                    data = trig_mgr.load(switch_id) or {}
                    adv = (data.get("Advanced") or {})
                    payload = adv.get(final_rule_id) if isinstance(adv, dict) else None
                except Exception:
                    payload = None

                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                for switch_key in switch_keys:
                    state = trig_mgr.get_advanced_state_for_switch_key(switch_id, switch_key)
                    eff_enabled = bool(state.get("enabled_any", False))
                    sid, label, channel_index = _resolve_automation_target(switch_id, switch_key)
                    if sid and channel_index:
                        override_val = (not eff_enabled)
                        switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_val)
                        try:
                            sc = services.switch_controllers
                            if isinstance(sc, dict):
                                for ctrl in sc.values():
                                    if getattr(ctrl, "switch_id", None) == sid and isinstance(getattr(ctrl, "override_script", None), dict):
                                        ctrl.override_script[label] = override_val
                            elif sc and getattr(sc, "switch_id", None) == sid and isinstance(getattr(sc, "override_script", None), dict):
                                sc.override_script[label] = override_val
                        except Exception:
                            pass
            except Exception:
                pass

            # Broadcast updated automation state so UI button reflects enabled/disabled without refresh.
            try:
                app = request.app
                from .saiAutomationManager import AutomationManager
                mgr = AutomationManager("automation_settings")
                try:
                    data = mgr.load(switch_id) or {}
                    adv = (data.get("Advanced") or {}) if isinstance(data, dict) else {}
                    payload = adv.get(final_rule_id) if isinstance(adv, dict) else None
                except Exception:
                    payload = None

                switch_keys: set[str] = set()
                if isinstance(payload, dict):
                    script_json = payload.get("script_json", "")
                    try:
                        script = json.loads(str(script_json))
                        for action in (script.get("actions") or []):
                            if not isinstance(action, dict):
                                continue
                            switch_key = (action.get("switch_key") or "").strip()
                            if switch_key:
                                switch_keys.add(switch_key)
                    except Exception:
                        pass

                await _broadcast_automation_states(app, mgr, switch_id, switch_keys)
            except Exception:
                pass
        except AutomationActivationError as e:
            return error_response(e.public_message, status_code=409)
        except Exception as e:
            printDM(f"[{MODULE}] ⚠️ Failed to save Advanced trigger {rule_id} for {switch_id}: {e}", location=MODULE)
            if is_json_request:
                return JSONResponse({"ok": False, "error": "Failed saving Advanced trigger."}, status_code=500)
            return HTMLResponse("<h3>Failed saving Advanced trigger.</h3><a href='/'>Return</a>", status_code=500)

        # Success response (JSON for JSON requests; redirect for browser form posts)
        if is_json_request:
            return JSONResponse({"ok": True, "switch_id": switch_id, "rule_id": final_rule_id})
        return RedirectResponse(url="/", status_code=303)


    @router.get("/switch/history")
    def switch_history_api(switch_key: str = Query(..., min_length=1), hours: int = Query(24)):
        """Return all stored transitions for a short switch-history window."""
        if hours not in (1, 6, 24):
            raise HTTPException(status_code=422, detail="History window must be 1, 6, or 24 hours.")
        until = time.time()
        since = until - hours * 3600
        events = services.data_logger.get_switch_history(switch_key, since, until)
        return {"events": events, "start_epoch": since, "end_epoch": until}


    @router.get("/switch-status-update")
    async def switch_status_api():
        """
        Returns a JSON object keyed by *UI* switch key "<switch_id>::<label>".
          {
            "sensoria-hub-0::Light": {
              "state": <bool>,
              "time": ["On 2025-08-11 12:34:56", ...]  # up to 10, chronological
            },
            "switch-4oz31s::Fan": { ... }
          }
        """

        now = time.monotonic()
        if route_state.switch_status_payload is not None and now < route_state.switch_status_until:
            return JSONResponse(route_state.switch_status_payload)

        ctrl_by_switch_id: dict[str, object] = {}
        try:
            if services.switch_controllers and isinstance(services.switch_controllers, dict):
                for ctrl in services.switch_controllers.values():
                    sid = str(getattr(ctrl, "switch_id", "") or "").strip()
                    if sid:
                        ctrl_by_switch_id[sid] = ctrl
        except Exception:
            ctrl_by_switch_id = {}

        def _timer_snapshot(sid: str, label: str) -> dict:
            try:
                ctrl = ctrl_by_switch_id.get(str(sid or "").strip())
                getter = getattr(ctrl, "get_auto_off_status", None)
                if callable(getter):
                    return dict(getter(label) or {})
            except Exception:
                pass
            return {
                "timer_seconds": 0,
                "timer_enabled": False,
                "timer_deadline_epoch": None,
                "timer_remaining_s": 0,
            }
        def _build_switch_status_payload_sync(
            local_entries: list[dict[str, object]],
            remote_cache: dict[str, dict],
            identity_rows: list[dict[str, object]],
        ) -> dict[str, dict]:
            states: dict[str, dict] = {}

            def _event_origin_tag(source: object) -> str:
                raw = str(source or "").strip()
                src = raw.lower()
                if not src:
                    return ""
                if src.startswith("mqtt-auto:"):
                    detail = raw.split(":", 1)[1].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src.startswith("mqtt-manual"):
                    return "manual"
                if src.startswith("auto/rule:"):
                    detail = raw.split(":", 1)[1].strip()
                    if detail.lower().endswith("/mqtt"):
                        detail = detail[:-5].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src == "ui" or any(token in src for token in ("manual", "/ui", "ui/", "user")):
                    return "manual"
                if any(token in src for token in ("auto", "rule", "timer", "automation", "schedule")):
                    return "auto"
                return ""

            def _format_events(switch_key: str, sensor_id: str | None, limit: int = 10) -> list[str]:
                evs = services.data_logger.get_last_switch_events(
                    switch_key,
                    sensor_id=sensor_id,
                    limit=limit,
                    include_source=True,
                )
                out: list[str] = []
                for state_str, ts, source in evs:
                    label = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                    origin_tag = _event_origin_tag(source)
                    suffix = f" ({origin_tag})" if origin_tag else ""
                    out.append(f"{label} {ts}{suffix}")
                return out

            def _format_events_remote(switch_key: str, limit: int = 10) -> list[str]:
                return _format_events(switch_key, None, limit=limit)

            known_channel_ids_by_sid: dict[str, set[str]] = {}
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                ch = _switch_channel_id_from_identity_row(row)
                if not sid or not ch:
                    continue
                known_channel_ids_by_sid.setdefault(sid, set()).add(ch.lower())

            def _db_key_for_label(sid: str, label: str) -> str:
                try:
                    target_sid = (sid or "").strip().lower()
                    target_label = (label or "").strip().lower()
                    for row in (identity_rows or []):
                        rsid = str(row.get("switch_id", "")).strip().lower()
                        rlab = str(row.get("label", "")).strip().lower()
                        if rsid == target_sid and rlab == target_label:
                            sk = str(row.get("switch_key", "")).strip()
                            if sk:
                                return sk
                except Exception:
                    pass
                try:
                    return _switch_key(sid, label)
                except Exception:
                    return f"{sid}::{label}"

            def _cache_state_for(sid: str, label: str, db_key: str) -> bool | None:
                try:
                    ch_map = remote_cache.get(sid, {}) or {}
                    human_state = ch_map.get(label)
                    if human_state is None:
                        ch_id = _resolve_channel_id_from_label(sid, label) or ""
                        human_state = ch_map.get(ch_id)
                    if human_state is None:
                        return None
                    txt = str(human_state).strip().lower()
                    if txt == "on":
                        return True
                    if txt == "off":
                        return False
                except Exception:
                    pass
                return None

            def _remote_liveness_payload(sid: str) -> dict:
                try:
                    sid_text = str(sid or "").strip()
                    if not sid_text:
                        return {}
                    dev_map = getattr(services.mqtt_ingest, "device_type", {}) or {}
                    host_to_peer_ids = getattr(services.mqtt_ingest, "host_to_peer_ids", {}) or {}
                    looks_remote = (
                        sid_text.startswith("switch-")
                        or str(dev_map.get(sid_text) or "").strip().lower() == "nodus"
                        or sid_text in (remote_cache or {})
                        or any(sid_text in (peers or []) for peers in host_to_peer_ids.values())
                    )
                    if not looks_remote:
                        return {}
                    getter = getattr(services.mqtt_ingest, "get_nodus_liveness", None)
                    if not callable(getter):
                        return {}
                    snapshot = getter(sid_text, device_type="switch")
                    state = str(snapshot.get("state") or "unknown").strip().lower()
                    return {
                        "availability": state,
                        "online": state == "online",
                        "liveness_reason": str(snapshot.get("reason") or ""),
                        "last_seen_s": snapshot.get("last_seen_s"),
                        "last_heartbeat_s": snapshot.get("last_heartbeat_s"),
                    }
                except Exception:
                    return {}

            for entry in local_entries:
                switch_id = str(entry.get("switch_id", "") or "").strip()
                label = str(entry.get("label", "") or "").strip()
                ui_key = str(entry.get("ui_key", "") or "").strip()
                db_key = str(entry.get("db_key", "") or "").strip() or ui_key
                sensor_lineage = str(entry.get("sensor_lineage", "") or "").strip() or None
                live_bool = bool(entry.get("live_bool", False))
                events = _format_events(db_key, sensor_lineage, limit=10)
                payload = {"state": live_bool, "time": events}
                if DEBUG and switch_id.lower() == "sensoria-hub-0" and label.lower() == "fan":
                    printDM(
                        f"[switch-status-update] {ui_key} live={live_bool} last_state={bool(entry.get('last_state', False))} db_key={db_key} events_head={(events[-1] if events else 'none')}",
                        location=MODULE,
                    )
                states[db_key] = dict(payload)
                if ui_key and ui_key not in states:
                    states[ui_key] = dict(payload)

            seen_ui_keys: set[str] = set()
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                label = str(row.get("label", "")).strip()
                db_key = str(row.get("switch_key", "")).strip()
                if not (sid and label and db_key):
                    continue
                ui_key = f"{sid}::{label}"
                cached_bool = _cache_state_for(sid, label, db_key)
                if cached_bool is not None:
                    latest_bool = cached_bool
                else:
                    latest = services.data_logger.get_latest_switch_state(db_key)
                    latest_bool = (latest == "On") if latest is not None else False
                events = _format_events_remote(db_key, limit=10) or _format_events(db_key, None, limit=10)
                payload = {"state": latest_bool, "time": events}
                payload.update(_remote_liveness_payload(sid))
                command_status = getattr(services.mqtt_ingest, "get_switch_command_status", None)
                if callable(command_status):
                    payload.update(command_status(sid, label, _switch_channel_id_from_identity_row(row)))
                states[db_key] = dict(payload)
                states[ui_key] = dict(payload)
                seen_ui_keys.add(ui_key)
                ch_id = _switch_channel_id_from_identity_row(row)
                alias_key = f"{ch_id}::{label}" if ch_id else ""
                if alias_key and alias_key not in states:
                    states[alias_key] = dict(payload)
                    seen_ui_keys.add(alias_key)

            for remote_switch_id, ch_map in (remote_cache or {}).items():
                if not isinstance(ch_map, dict):
                    continue
                for channel_label, human_state in ch_map.items():
                    if str(channel_label or "").strip().lower() in known_channel_ids_by_sid.get(str(remote_switch_id), set()):
                        continue
                    ui_key = f"{remote_switch_id}::{channel_label}"
                    if ui_key in seen_ui_keys:
                        continue
                    db_key = _db_key_for_label(remote_switch_id, channel_label)
                    cached_bool = _cache_state_for(remote_switch_id, channel_label, db_key)
                    if cached_bool is not None:
                        latest_bool = cached_bool
                    else:
                        latest = services.data_logger.get_latest_switch_state(db_key)
                        latest_bool = (latest == "On") if latest is not None else (str(human_state).lower() == "on")
                    events = _format_events_remote(db_key, limit=10) or _format_events(db_key, None, limit=10)
                    payload = {"state": latest_bool, "time": events}
                    payload.update(_remote_liveness_payload(remote_switch_id))
                    command_status = getattr(services.mqtt_ingest, "get_switch_command_status", None)
                    if callable(command_status):
                        channel = _resolve_channel_id_from_label(remote_switch_id, channel_label) or ""
                        payload.update(command_status(remote_switch_id, channel_label, channel))
                    if db_key:
                        states[db_key] = dict(payload)
                    states[ui_key] = payload

            return states

        try:
            local_entries: list[dict[str, object]] = []
            timer_snapshots: dict[str, dict] = {}

            if services.switch_controllers and isinstance(services.switch_controllers, dict):
                for ctrl in services.switch_controllers.values():
                    if not getattr(ctrl, "is_present", False):
                        continue
                    if not isinstance(getattr(ctrl, "last_state", None), dict):
                        continue

                    switch_id = str(getattr(ctrl, "switch_id", "") or "").strip()
                    if not switch_id:
                        continue
                    sensor_lineage = f"Switch_{switch_id}"
                    for label, is_on in ctrl.last_state.items():
                        try:
                            db_key = ctrl._switch_key(label)
                        except Exception:
                            db_key = _switch_key(switch_id, label)
                        ui_key = f"{switch_id}::{label}"
                        try:
                            live_bool = bool(ctrl.get_state(label))
                        except Exception:
                            live_bool = bool(is_on)
                        timer_info = _timer_snapshot(switch_id, label)
                        timer_snapshots[ui_key] = dict(timer_info)
                        canonical_key = str(db_key or "").strip() or ui_key
                        timer_snapshots[canonical_key] = dict(timer_info)
                        local_entries.append({
                            "switch_id": switch_id,
                            "label": label,
                            "ui_key": ui_key,
                            "db_key": db_key,
                            "sensor_lineage": sensor_lineage,
                            "live_bool": live_bool,
                            "last_state": bool(is_on),
                        })

            remote_cache = copy.deepcopy(getattr(services.mqtt_ingest, "_switch_state_cache", {}) or {})
            identity_rows = await asyncio.to_thread(lambda: list(services.data_logger.get_switch_identities() or []))
            for row in (identity_rows or []):
                sid = str(row.get("switch_id", "")).strip()
                label = str(row.get("label", "")).strip()
                db_key = str(row.get("switch_key", "")).strip()
                if not (sid and label):
                    continue
                ui_key = f"{sid}::{label}"
                timer_info = _timer_snapshot(sid, label)
                timer_snapshots[ui_key] = dict(timer_info)
                if db_key:
                    timer_snapshots[db_key] = dict(timer_info)
                if db_key and "::" in db_key:
                    ch_id = _switch_channel_id_from_identity_row(row)
                    alias_key = f"{ch_id}::{label}" if ch_id else ""
                    if alias_key:
                        timer_snapshots[alias_key] = dict(timer_info)
            for remote_switch_id, ch_map in remote_cache.items():
                if not isinstance(ch_map, dict):
                    continue
                for channel_label in ch_map.keys():
                    ui_key = f"{remote_switch_id}::{channel_label}"
                    timer_snapshots.setdefault(ui_key, dict(_timer_snapshot(remote_switch_id, str(channel_label or ""))))

            states = await asyncio.to_thread(
                _build_switch_status_payload_sync,
                local_entries,
                remote_cache,
                identity_rows,
            )
            for key, timer_info in timer_snapshots.items():
                if key in states:
                    states[key].update(timer_info)

            for ctrl in ctrl_by_switch_id.values():
                if not getattr(ctrl, "is_ecowitt", False):
                    continue
                for label in ctrl.get_switch_names():
                    for key in (ctrl._switch_key(label), f"{ctrl.switch_id}::{label}",
                                f"{ctrl.channel_id_for_label[label]}::{label}"):
                        states.setdefault(key, {}).update(
                            state=ctrl.get_state(label), online=ctrl.available,
                            availability="online" if ctrl.available else "offline",
                            confirmed=ctrl.confirmed, error=ctrl.last_error,
                        )

            route_state.switch_status_payload = states
            route_state.switch_status_until = time.monotonic() + _SWITCH_STATUS_CACHE_TTL_SEC
            return JSONResponse(states)

        except Exception as e:
            printDM(f"switch-status-update error: {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)


    @router.post("/switch/toggle")
    async def toggle_switch(
        request: Request,
        switch_name: str = Query(...),        # legacy: label-only still supported
        switch_key: str | None = Query(None), # new: "switch_id::channel_id"
        switch_id: str | None = Query(None),  # new: switch_id sent separately
        ):
        """
        Toggle a switch identified by either:
          1) switch_key="switch_id::channel_id"  (preferred)
          2) switch_id + switch_name        (explicit device + label)
          3) switch_name="<label>"          (legacy; ambiguous if duplicates exist)
        """
        import time
        _require_protected_access(request, require_csrf=True)

        def _norm_label(s: str | None) -> str | None:
            return s.strip() if s else None

        def _norm_switch_id(s: str | None) -> str | None:
            return s.strip() if s else None

        def _ctrl_switch_id(ctrl) -> str | None:
            val = getattr(ctrl, "switch_id", None)
            if isinstance(val, str) and val.strip():
                return val.strip()
            return None

        def _ctrl_labels(ctrl) -> list[str]:
            """
            Support both full SwitchController objects and remote presenter objects.
            """
            try:
                getter = getattr(ctrl, "get_switch_names", None)
                if callable(getter):
                    raw = getter() or []
                else:
                    raw = getattr(ctrl, "switches", []) or []
                return [str(s).strip() for s in raw if str(s).strip()]
            except Exception:
                return []

        def _looks_remote(ctrl) -> bool:
            # Heuristics that cover MQTTSwitch and your remote ids
            return (
                bool(getattr(ctrl, "switch_topics", None)) or
                bool(getattr(getattr(ctrl, "switch", None), "switch_topics", None)) or
                str(getattr(ctrl, "switch_id", "")).startswith("switch-")
            )

        def _remote_liveness_snapshot(target_id: str | None, *, channel_id: str | None = None) -> dict:
            try:
                getter = getattr(services.mqtt_ingest, "get_nodus_liveness", None)
                if callable(getter):
                    return dict(getter(str(target_id or channel_id or "").strip(), device_type="switch") or {})
            except Exception:
                pass
            return {"state": "unknown", "reason": "liveness_unavailable"}

        def _remote_liveness_block_response(target_id: str | None, *, channel_id: str | None = None):
            snapshot = _remote_liveness_snapshot(target_id, channel_id=channel_id)
            state = str(snapshot.get("state") or "unknown").strip().lower()
            if state == "online":
                return None
            return JSONResponse(
                {
                    "error": "device_offline",
                    "message": "Nodus device is not reporting; wait for heartbeat or data before toggling.",
                    "switch_id": str(target_id or ""),
                    "channel_id": str(channel_id or ""),
                    "availability": state,
                    "liveness_reason": str(snapshot.get("reason") or ""),
                    "last_seen_s": snapshot.get("last_seen_s"),
                    "last_heartbeat_s": snapshot.get("last_heartbeat_s"),
                },
                status_code=503,
            )

        # even if the db does not have a state value, set the state
        def _desired_toggle_from_db(
            data_logger,
            switch_id: str | None,
            label: str,
            ctrl,
            *,
            prefer_live_current: bool = False,
            live_current: bool | None = None,
        ) -> bool:
            """
            Returns desired new_state:
            - For remote/Nodus switches, flip the live/cache state first.
            - Else if DB has a latest state (using the controller's canonical DB key): flip it.
            - Else: flip the *actual controller* state so the first click always changes something.
            """
            if prefer_live_current and live_current is not None:
                new_state = not bool(live_current)
                if DEBUG:
                    printDM(
                        f"[toggle_switch] live state {switch_id}::{label}={bool(live_current)!r} -> new_state={new_state}",
                        location="saiWebRoutes",
                    )
                return new_state

            # 1) Prefer DB if present, using ctrl._switch_key() so the key matches what logging uses
            try:
                switch_key = None
                try:
                    # Canonical DB identity: uses saiDataLogger.build_switch_key under the hood
                    switch_key = ctrl._switch_key(label)
                except Exception:
                    # Fallback for older controllers: best-effort label-based key
                    if switch_id:
                        switch_key = f"{switch_id}::{label}"

                if switch_key:
                    last = data_logger.get_latest_switch_state(switch_key)  # "On" | "Off" | None
                    if last is not None:
                        last_on = (isinstance(last, str) and last.strip().lower() == "on")
                        new_state = (not last_on)
                        if DEBUG:
                            printDM(
                                f"[toggle_switch] DB says {switch_key}={last!r} → new_state={new_state}",
                                location="saiWebRoutes",
                            )
                        return new_state
            except Exception as e:
                printDM(
                    f"[toggle_switch] DB lookup failed for {switch_id}::{label}: {e}",
                    location="saiWebRoutes",
                )

            # 2) Fallback: flip the controller’s live state
            try:
                live_on = bool(ctrl.get_state(label))
                return (not live_on)
            except Exception:
                # Last-ditch default: turn ON
                return True

        try:
            label_raw     = _norm_label(switch_name)
            key_raw       = _norm_label(switch_key)
            switch_id_raw = _norm_switch_id(switch_id)
            key_suffix_raw = None

            if DEBUG:
                printDM(f"[toggle_switch] Requested: '{switch_name}', key={switch_key}, switch_id={switch_id}", location=MODULE)

            if not label_raw and not key_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            # Resolve from switch_key if provided
            if key_raw and "::" in key_raw:
                sid_part, label_part = key_raw.split("::", 1)
                switch_id_raw = _norm_switch_id(sid_part) or switch_id_raw
                key_suffix_raw = _norm_label(label_part)
                label_raw     = _norm_label(label_part) or label_raw

            if not label_raw:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            try:
                identity_rows = list(services.data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows = []

            def _label_from_key_suffix(sid_value: str | None, suffix_value: str | None) -> str | None:
                sid_l = str(sid_value or "").strip().lower()
                suffix_l = str(suffix_value or "").strip().lower()
                if not sid_l or not suffix_l:
                    return None
                for row in identity_rows:
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    if r_sid != sid_l:
                        continue
                    r_label = str(row.get("label", "")).strip()
                    r_channel = _switch_channel_id_from_identity_row(row)
                    if suffix_l in {r_label.lower(), r_channel.lower()} and r_label:
                        return r_label
                return None

            resolved_label = _label_from_key_suffix(switch_id_raw, key_suffix_raw)
            if resolved_label:
                label_raw = resolved_label
            label_q_lower = label_raw.lower()

            def _switch_id_matches(ctrl, wanted_sid: str | None, label: str) -> bool:
                """
                Match explicit switch_id against controller id and known aliases:
                - host switch_id (e.g. switch-en1n8i)
                - channel_id from switch_key suffix (e.g. S1-en1n8i)
                """
                if not wanted_sid:
                    return True

                wanted_l = wanted_sid.lower()
                ctrl_sid = _ctrl_switch_id(ctrl)
                if ctrl_sid and ctrl_sid.lower() == wanted_l:
                    return True

                aliases: set[str] = set()
                if ctrl_sid:
                    aliases.add(ctrl_sid.lower())

                try:
                    ch_map = getattr(ctrl, "channel_id_for_label", {}) or {}
                    channel_id = str(ch_map.get(label, "") or "").strip().lower()
                    if channel_id:
                        aliases.add(channel_id)
                except Exception:
                    pass

                label_l = (label or "").strip().lower()
                for row in identity_rows:
                    r_label = str(row.get("label", "")).strip().lower()
                    if r_label != label_l:
                        continue
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    r_channel = _switch_channel_id_from_identity_row(row).lower()

                    # Bridge either direction if controller id is host-id or channel-id.
                    if ctrl_sid and r_sid == ctrl_sid and r_channel:
                        aliases.add(r_channel)
                    if ctrl_sid and r_channel and r_channel == ctrl_sid and r_sid:
                        aliases.add(r_sid)

                return wanted_l in aliases

            # ---- Find matching controllers ----
            sc_map = services.switch_controllers if isinstance(services.switch_controllers, dict) else {}
            matches: list[tuple[object, str]] = []

            for ctrl in sc_map.values():
                ctrl_labels = _ctrl_labels(ctrl)
                if not ctrl_labels:
                    if DEBUG:
                        printDM("[toggle_switch] skipping switch with no labels", location=MODULE)
                    continue

                match_label = next((lbl for lbl in ctrl_labels if (lbl or "").lower() == label_q_lower), None)
                if not match_label:
                    continue

                if switch_id_raw and not _switch_id_matches(ctrl, switch_id_raw, match_label):
                    continue

                matches.append((ctrl, match_label))

            if not matches:
                # Direct channel-ID fallback for remote Nodus switches (e.g., S1-xxxxxx).
                try:
                    looks_channel = bool(re.match(r"^S\d+-[A-Za-z0-9._-]+$", str(switch_id_raw or "").strip()))
                    if looks_channel and services.mqtt_ingest:
                        channel_id = str(switch_id_raw or "").strip()
                        resolved_sid = ""
                        resolved_db_key = ""
                        resolved_label = label_raw or channel_id
                        for row in identity_rows:
                            r_key = str(row.get("switch_key", "") or "").strip()
                            r_sid = str(row.get("switch_id", "") or "").strip()
                            r_label = str(row.get("label", "") or "").strip()
                            r_channel = _switch_channel_id_from_identity_row(row)
                            if not r_channel:
                                continue
                            if r_channel.lower() != channel_id.lower():
                                continue
                            if label_raw and r_label and r_label.lower() != label_raw.lower():
                                continue
                            resolved_sid = r_sid or resolved_sid
                            resolved_db_key = r_key or resolved_db_key
                            resolved_label = r_label or resolved_label
                            if resolved_sid and resolved_db_key:
                                break

                        current_on = None
                        try:
                            cache = getattr(services.mqtt_ingest, "_switch_state_cache", {}) or {}
                            if resolved_sid:
                                raw_state = (cache.get(resolved_sid, {}) or {}).get(channel_id)
                                if raw_state is None:
                                    raw_state = (cache.get(resolved_sid, {}) or {}).get(channel_id.lower())
                                if raw_state is not None:
                                    current_on = (str(raw_state).strip().lower() == "on")
                            if current_on is None:
                                for _sid, chmap in cache.items():
                                    if not isinstance(chmap, dict):
                                        continue
                                    raw_state = chmap.get(channel_id)
                                    if raw_state is None:
                                        raw_state = chmap.get(channel_id.lower())
                                    if raw_state is not None:
                                        current_on = (str(raw_state).strip().lower() == "on")
                                        break
                        except Exception:
                            current_on = None

                        if current_on is None and resolved_db_key:
                            try:
                                last = services.data_logger.get_latest_switch_state(resolved_db_key)
                                if last is not None:
                                    current_on = (str(last).strip().lower() == "on")
                            except Exception:
                                current_on = None

                        new_state = (not current_on) if current_on is not None else True
                        blocked = _remote_liveness_block_response(resolved_sid or "", channel_id=channel_id)
                        if blocked is not None:
                            return blocked
                        ok = bool(services.mqtt_ingest.set_switch_by_channel_id(resolved_sid or "", channel_id, new_state, reason="manual"))
                        if ok:
                            ts = time.time()
                            # Remote fallback path: do not persist UI-originated events.
                            # sw_events should reflect only confirmed MQTT state/event messages.
                            # Invalidate short-lived switch status cache after a state change request.
                            route_state.switch_status_payload = None
                            route_state.switch_status_until = 0.0
                            return {"state": bool(new_state), "time": ts}
                except Exception as e:
                    printDM(f"[toggle_switch] channel fallback failed: {e}", location=MODULE)

                if DEBUG:
                    printDM(f"[toggle_switch] No match found for: label='{label_raw}', switch_id='{switch_id_raw}'", location=MODULE)
                return JSONResponse({"error": "switch_not_found"}, status_code=404)

            if len(matches) > 1 and not switch_id_raw:
                options = []
                for ctrl, _ml in matches:
                    try:
                        options.append({
                            "location": getattr(ctrl, "location", None),
                            "labels": _ctrl_labels(ctrl),
                            "switch_id": getattr(ctrl, "switch_id", None),
                        })
                    except Exception:
                        pass
                return JSONResponse(
                    {
                        "error": "ambiguous_switch",
                        "message": "Multiple devices have this label. Provide switch_key='switch_id::channel_id' or pass switch_id.",
                        "options": options,
                    },
                    status_code=409,
                )

            # Target resolved
            ctrl, matched_label = matches[0]
            if getattr(ctrl, "_automation_preparing", False):
                return JSONResponse(
                    {"error": "automation_preparing", "message": "Wait for automation baseline preparation to finish."},
                    status_code=423,
                )
            # Use the controller-resolved id for command publish. The incoming
            # switch_id can be an alias (e.g. channel_id) used only for matching.
            sid = _ctrl_switch_id(ctrl) or switch_id_raw or getattr(ctrl, "switch_id", None)

            # Read current state; fall back to controller cache if needed
            try:
                current = bool(ctrl.get_state(matched_label))
            except Exception:
                current = bool((getattr(ctrl, "last_state", {}) or {}).get(matched_label, False))

            # Decide path: direct GPIO vs remote/MQTT. Remote toggles should
            # invert the live Nodus cache/controller state before consulting
            # historical DB rows, because old bad parses or missed retained
            # events can leave sw_events stale.
            remote = _looks_remote(ctrl)

            # Manual toggles are blocked while any Advanced automation for this switch is enabled.
            try:
                from .saiAutomationManager import AutomationManager
                am = AutomationManager("automation_settings")
                automation_switch_id = _ctrl_switch_id(ctrl) or sid or ""
                try:
                    channel_id = str((getattr(ctrl, "channel_id_for_label", {}) or {}).get(matched_label, "") or "").strip()
                except Exception:
                    channel_id = ""
                suffix = channel_id or matched_label
                automation_switch_key = (
                    f"{automation_switch_id}::{suffix}" if automation_switch_id else f"::{suffix}"
                )
                automation_state = am.get_advanced_state_for_switch_key(automation_switch_id, automation_switch_key)
                if bool(automation_state.get("enabled_any", False)):
                    return JSONResponse(
                        {
                            "error": "automation_enabled",
                            "message": "Automation is enabled for this switch. Disable automation before toggling manually.",
                            "switch_id": automation_switch_id,
                            "label": matched_label,
                        },
                        status_code=423,
                    )
            except Exception:
                pass

            new_state = _desired_toggle_from_db(
                services.data_logger,
                sid,
                matched_label,
                ctrl,
                prefer_live_current=remote or bool(getattr(ctrl, "is_ecowitt", False)),
                live_current=current,
            )
            response_state = bool(new_state)

            ok = False

            def _event_origin_tag(source: object) -> str:
                raw = str(source or "").strip()
                src = raw.lower()
                if not src:
                    return ""
                if src.startswith("mqtt-auto:"):
                    detail = raw.split(":", 1)[1].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src.startswith("mqtt-manual"):
                    return "manual"
                if src.startswith("auto/rule:"):
                    detail = raw.split(":", 1)[1].strip()
                    if detail.lower().endswith("/mqtt"):
                        detail = detail[:-5].strip()
                    return f"auto - {detail}" if detail else "auto"
                if src == "ui" or any(token in src for token in ("manual", "/ui", "ui/", "user")):
                    return "manual"
                if any(token in src for token in ("auto", "rule", "timer", "automation", "schedule")):
                    return "auto"
                return ""

            def _recent_events_payload(limit: int = 10) -> list[str]:
                if remote or not sid:
                    return []
                try:
                    db_key = ctrl._switch_key(matched_label)
                except Exception:
                    db_key = f"{sid}::{matched_label}"
                sensor_lineage = f"Switch_{sid}" if sid else None
                rows = services.data_logger.get_last_switch_events(
                    db_key,
                    sensor_id=sensor_lineage,
                    limit=limit,
                    include_source=True,
                )
                events: list[str] = []
                for state_str, ts_text, source in rows:
                    label_text = "On" if str(state_str).lower() in ("on", "true", "1") else "Off"
                    origin_tag = _event_origin_tag(source)
                    suffix = f" ({origin_tag})" if origin_tag else ""
                    events.append(f"{label_text} {ts_text}{suffix}")
                return events

            def _timer_response_payload(state_value: bool, ts_value, *, note: str | None = None) -> dict:
                payload = {
                    "state": bool(state_value),
                    "time": ts_value if isinstance(ts_value, str) else "",
                    "switch_id": sid or "",
                    "label": matched_label,
                    "ui_key": f"{sid}::{matched_label}" if sid else "",
                }
                events = _recent_events_payload()
                if events:
                    payload["events"] = events
                try:
                    getter = getattr(ctrl, "get_auto_off_status", None)
                    if callable(getter):
                        payload.update(dict(getter(matched_label) or {}))
                except Exception:
                    pass
                if remote:
                    status_getter = getattr(services.mqtt_ingest, "get_switch_command_status", None)
                    channel = str((getattr(ctrl, "channel_id_for_label", {}) or {}).get(matched_label, "") or "")
                    if callable(status_getter):
                        payload.update(status_getter(sid or "", matched_label, channel))
                    else:
                        payload.update(pending=True, requested_state=bool(new_state))
                if note:
                    payload["note"] = note
                return payload

            if getattr(ctrl, "is_ecowitt", False):
                if not ctrl.available:
                    return JSONResponse({"error": "plug_unavailable", "message": ctrl.last_error or "Plug is offline or gateway polling is disabled."}, status_code=503)
                ok = ctrl.set_state(matched_label, new_state, force=True)
                if ok:
                    ok = await asyncio.shield(ctrl.command_task)
                    if not ok:
                        return JSONResponse({"error": "plug_confirmation_failed", "message": ctrl.last_error}, status_code=502)
                response_state = bool(ctrl.get_state(matched_label))
            elif not remote:
                # Direct GPIO on this Pi
                try:
                    ok = bool(ctrl.set_state(matched_label, new_state, force=True))
                except Exception as e:
                    printDM(f"[toggle_switch] ctrl.set_state (direct) failed: {e}", location=MODULE)
            else:
                # Remote (Pico2 W via MQTT) — publish via injected mqtt_ingest
                if not services.mqtt_ingest:
                    return JSONResponse({"error": "mqtt_not_ready", "detail": "ingest_not_injected"}, status_code=503)

                if getattr(services.mqtt_ingest, "client", None) is None:
                    return JSONResponse({"error": "mqtt_not_ready", "detail": "ingest_client_none"}, status_code=503)

                # Primary: use ingest helper (finds correct base topic and emits {"set": "..."} JSON)
                if sid:
                    try:
                        channel_id = str((getattr(ctrl, "channel_id_for_label", {}) or {}).get(matched_label, "") or "").strip()
                    except Exception:
                        channel_id = ""
                    blocked = _remote_liveness_block_response(sid, channel_id=channel_id)
                    if blocked is not None:
                        return blocked
                    try:
                        ok = bool(services.mqtt_ingest.set_switch(sid, matched_label, new_state, event_origin="manual"))
                    except Exception as e:
                        printDM(f"[toggle_switch] ingest.set_switch error: {e}", location=MODULE)
                if ok:
                    # Publication is a request, not a device state confirmation.
                    response_state = bool(ctrl.get_state(matched_label))

            # ...after we've tried to set the state (ok = ctrl.set_state(...) or MQTT path)...
            if not ok:
                # Distinguish a guard/no-op from a real failure by checking the *effective* state
                try:
                    effective = bool(ctrl.get_state(matched_label))
                except Exception:
                    effective = bool((getattr(ctrl, "last_state", {}) or {}).get(matched_label, False))

                note = None
                # If the effective state equals the current state we started from,
                # it’s either a no-op (asked to set same state) or blocked by min-on/off guard.
                if effective == current:
                    # Best-effort hint: if we asked to flip but it didn't, likely guard-window.
                    note = "guard_window" if new_state != current else "noop"
                    # Return 200 so the UI treats this as handled (no error toast).
                    ts_map = getattr(ctrl, "last_set_time", {}) or {}
                    ts = ts_map.get(matched_label, time.time())
                    return JSONResponse(_timer_response_payload(effective, ts, note=note), status_code=200)

                # Otherwise, this really did fail (hardware/driver issue)
                reason = "mqtt_not_ready" if remote else "failed_to_toggle"
                return JSONResponse({"error": reason}, status_code=503 if reason == "mqtt_not_ready" else 500)

            # Persist SWITCH_n_LAST_STATE
            try:
                if sid and not remote and not getattr(ctrl, "is_ecowitt", False):
                    mgr = SwitchSettingsManager("switch_settings")
                    # Prefer controller helper if available
                    idx = None
                    try:
                        idx = ctrl.get_channel_index(matched_label)
                    except Exception:
                        idx = None
                    if not idx:
                        # Fallback: walk ordered names
                        ordered = list(ctrl.get_switch_names() or [])
                        idx = next((i + 1 for i, nm in enumerate(ordered) if (nm or "").strip().lower() == label_q_lower), None)
                    if idx:
                        mgr.update_setting(sid, f"SWITCH_{idx}_LAST_STATE", bool(new_state))
            except Exception as e:
                printDM(f"[toggle_switch] persist failed for '{matched_label}': {e}", location=MODULE)

            # Prefer controller’s recorded set time if available
            ts_map = getattr(ctrl, "last_set_time", {}) or {}
            ts = ts_map.get(matched_label, time.time())
            # Persist UI-originated switch events only for local/direct controllers.
            # For remote/Nodus, history should be written only from confirmed MQTT event/state ingest.
            if not remote and not getattr(ctrl, "is_ecowitt", False):
                try:
                    if sid:
                        try:
                            db_key = ctrl._switch_key(matched_label)
                        except Exception:
                            # Fallback for older controllers
                            db_key = f"{sid}::{matched_label}"

                        services.data_logger.log_switch_event(
                            switch_key=db_key,
                            is_on=bool(new_state),
                            source="ui",
                            sensor_id=f"Switch_{sid}",
                        )
                except Exception as e:
                    printDM(f"[toggle_switch] failed to log sw_event for {matched_label}: {e}", location=MODULE)

            # Let controller optionally record/log an event
            try:
                rec = getattr(ctrl, "record_event", None)
                if callable(rec) and not remote:
                    rec(matched_label, "on" if new_state else "off", ts)
            except Exception:
                pass

            # Invalidate short-lived switch status cache after a state change.
            route_state.switch_status_payload = None
            route_state.switch_status_until = 0.0

            return _timer_response_payload(response_state, ts)

        except Exception as e:
            printDM(f"[toggle_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)


    @router.post("/switch/timer")
    async def set_switch_timer(
        request: Request,
        switch_id: str = Query(...),
        switch_name: str = Query(...),
    ):
        try:
            _require_protected_access(request, require_csrf=True)
            data = await request.json()
        except Exception:
            data = {}

        try:
            seconds = int(data.get("seconds", 0) or 0)
        except Exception:
            return JSONResponse({"error": "invalid_seconds"}, status_code=400)

        if seconds != 0 and not (30 <= seconds <= 9999):
            return JSONResponse({"error": "invalid_seconds", "detail": "Use 0 or 30-9999 seconds."}, status_code=400)

        sid = str(switch_id or "").strip()
        label_raw = str(switch_name or "").strip()
        if not sid or not label_raw:
            return JSONResponse({"error": "bad_request"}, status_code=400)

        sc_map = services.switch_controllers if isinstance(services.switch_controllers, dict) else {}
        match_ctrl = None
        match_label = ""
        for ctrl in sc_map.values():
            ctrl_sid = str(getattr(ctrl, "switch_id", "") or "").strip()
            if ctrl_sid != sid:
                continue
            try:
                labels = list(ctrl.get_switch_names() or [])
            except Exception:
                labels = list(getattr(ctrl, "switches", []) or [])
            found = next((lbl for lbl in labels if str(lbl or "").strip().lower() == label_raw.lower()), None)
            if found:
                match_ctrl = ctrl
                match_label = str(found).strip()
                break

        if not match_ctrl or not match_label:
            return JSONResponse({"error": "switch_not_found"}, status_code=404)

        setter = getattr(match_ctrl, "set_auto_off_seconds", None)
        getter = getattr(match_ctrl, "get_auto_off_status", None)
        if not callable(setter) or not callable(getter):
            return JSONResponse({"error": "timer_not_supported"}, status_code=400)

        applied = int(setter(match_label, seconds))
        try:
            current_state = bool(match_ctrl.get_state(match_label))
        except Exception:
            current_state = bool((getattr(match_ctrl, "last_state", {}) or {}).get(match_label, False))

        payload = {
            "ok": True,
            "switch_id": sid,
            "label": match_label,
            "ui_key": f"{sid}::{match_label}",
            "state": current_state,
            "timer_seconds": applied,
        }
        try:
            payload.update(dict(getter(match_label) or {}))
        except Exception:
            pass

        route_state.switch_status_payload = None
        route_state.switch_status_until = 0.0
        return JSONResponse(payload)


    @router.post("/switch/override")
    async def override_switch(
        request: Request,
        switch_name: str | None = Query(None),
        switch_key: str | None = Query(None),
        switch_id: str | None = Query(None),
    ):
        from .saiSwitchSettingsManager import SwitchSettingsManager
        from .saiAutomationManager import AutomationManager

        try:
            _require_protected_access(request, require_csrf=True)
            data = await request.json()
            desired_rule_enabled = bool(data.get("enabled", False))  # ← interpret as RULE state

            label_q   = (switch_name or "").strip()
            key_q     = (switch_key or "").strip()
            switch_id_q = (switch_id or "").strip().lower() if switch_id else None
            key_suffix_q = ""

            if DEBUG:
                printDM(f"[override_switch] name='{label_q}', key='{key_q}', switch_id='{switch_id_q}', rule.enabled={desired_rule_enabled}", location=MODULE)

            # Resolve from switch_key if present
            if key_q and "::" in key_q:
                sid_part, lbl_part = key_q.split("::", 1)
                switch_id_q = (sid_part or "").strip().lower() or switch_id_q
                key_suffix_q = (lbl_part or "").strip()
                label_q     = (lbl_part or "").strip() or label_q

            if not label_q:
                return JSONResponse({"error": "bad_switch_name"}, status_code=400)

            try:
                identity_rows_for_override = list(services.data_logger.get_switch_identities() or [])
            except Exception:
                identity_rows_for_override = []
            if switch_id_q and key_suffix_q:
                suffix_l = key_suffix_q.lower()
                for row in identity_rows_for_override:
                    r_sid = str(row.get("switch_id", "")).strip().lower()
                    if r_sid != switch_id_q:
                        continue
                    r_label = str(row.get("label", "") or "").strip()
                    r_channel = _switch_channel_id_from_identity_row(row)
                    if suffix_l in {r_label.lower(), r_channel.lower()} and r_label:
                        label_q = r_label
                        break

            label_lower = label_q.lower()

            # Find the one controller that matches
            def _ctrl_labels(ctrl) -> list[str]:
                try:
                    getter = getattr(ctrl, "get_switch_names", None)
                    if callable(getter):
                        raw = getter() or []
                    else:
                        raw = getattr(ctrl, "switches", []) or []
                    return [str(s).strip() for s in raw if str(s).strip()]
                except Exception:
                    return []

            matches = []
            for ctrl in (services.switch_controllers or {}).values():
                ctrl_labels = _ctrl_labels(ctrl)
                if not ctrl_labels:
                    if DEBUG:
                        printDM("[override_switch] skipping switch with no labels", location=MODULE)
                    continue

                match_label = next((lbl for lbl in ctrl_labels if lbl.lower() == label_lower), None)
                if not match_label:
                    continue

                ctrl_sid = getattr(ctrl, "switch_id", None)
                if switch_id_q and (not ctrl_sid or ctrl_sid.lower() != switch_id_q):
                    continue

                matches.append((ctrl, match_label))

            if not matches:
                return JSONResponse({"error": "switch_not_found"}, status_code=404)
            if len(matches) > 1 and not switch_id_q:
                options = []
                for ctrl, _ in matches:
                    options.append({
                        "switch_id": getattr(ctrl, "switch_id", None),
                        "location": getattr(ctrl, "location", None),
                        "labels": _ctrl_labels(ctrl),
                    })
                return JSONResponse({"error": "ambiguous_switch", "options": options}, status_code=409)

            ctrl, matched_label = matches[0]

            if getattr(ctrl, "_automation_preparing", False):
                return JSONResponse(
                    {"error": "automation_preparing", "message": "Wait for automation baseline preparation to finish."},
                    status_code=423,
                )

            # Resolve channel index for SWITCH_{n}_OVERRIDE_SCRIPT
            try:
                switch_mgr = SwitchSettingsManager("switch_settings")
                sid = getattr(ctrl, "switch_id", None)
                channel_index = None
                if sid:
                    ordered = switch_mgr.get_switch_channel_names(sid)  # ['Fan','Light',...]
                    channel_index = next((i + 1 for i, nm in enumerate(ordered) if (nm or '').strip().lower() == label_lower), None)
                # Fallback: parse switch.toml directly if needed
                if sid and not channel_index:
                    try:
                        doc = switch_mgr.load(sid) or {}
                        sw_map = doc.get("Switch") or {}
                        for k, v in sw_map.items():
                            if not str(k).startswith("SWITCH_"):
                                continue
                            parts = str(k).split("_")
                            if len(parts) != 2 or not parts[1].isdigit():
                                continue
                            if str(v).strip().lower() == label_lower:
                                channel_index = int(parts[1])
                                break
                    except Exception:
                        channel_index = None
                # Fallback: map label -> channel_id from switch_ids, then match SWITCH_n_ID
                if sid and not channel_index:
                    try:
                        channel_id = None
                        for row in identity_rows_for_override:
                            if str(row.get("switch_id", "")).strip().lower() != sid.lower():
                                continue
                            if str(row.get("label", "")).strip().lower() != label_lower:
                                continue
                            channel_id = _switch_channel_id_from_identity_row(row)
                            if channel_id:
                                break
                        if channel_id:
                            doc = switch_mgr.load(sid) or {}
                            sw_map = doc.get("Switch") or {}
                            for k, v in sw_map.items():
                                if not str(k).startswith("SWITCH_") or not str(k).endswith("_CHANNEL_ID"):
                                    continue
                                parts = str(k).split("_")
                                if len(parts) != 4 or not parts[1].isdigit():
                                    continue
                                if str(v).strip() == channel_id:
                                    channel_index = int(parts[1])
                                    break
                    except Exception:
                        channel_index = None
            except Exception as e:
                printDM(f"[override_switch] failed resolving channel index: {e}", location=MODULE)
                channel_index = None

            # --- Persist BOTH states atomically: Advanced rule + switch override flag ---
            try:
                am = AutomationManager()
                sid = getattr(ctrl, "switch_id", None) or ""
                # Canonical Advanced switch_key
                switch_key_full = (key_q or "").strip()
                if not switch_key_full:
                    # fall back to constructing it from sid + label
                    switch_key_full = f"{sid}::{matched_label}" if sid else f"::{matched_label}"

                # 1) Update Advanced rule enabled flag
                before = am.get_advanced_state_for_switch_key(sid, switch_key_full)
                stored_rules = (am.load(sid).get("Advanced") or {})
                rules = {
                    rid: stored_rules[rid] for rid in before.get("rule_ids", [])
                    if rid in stored_rules and not stored_rules[rid].get("enabled", True)
                } if desired_rule_enabled else {}
                updated = await _activate_automation_rules(
                    request, am, sid, rules,
                    lambda: am.set_advanced_enabled_for_switch_key(sid, switch_key_full, desired_rule_enabled),
                )
                if not updated:
                    # No matching Advanced rule for this switch_key -> report to client
                    return JSONResponse({"error": "automation_rule_not_found"}, status_code=404)

                agg_state = am.get_advanced_state_for_switch_key(sid, switch_key_full)
                effective_rule_enabled = bool(agg_state.get("enabled_any", False))

                # 2) Update switch.toml override to the inverse of rule.enabled
                override_value = (not effective_rule_enabled)
                if sid and channel_index:
                    switch_mgr.update_setting(sid, f"SWITCH_{channel_index}_OVERRIDE_SCRIPT", override_value)

                # 3) Update in-memory map if present
                try:
                    override_map = getattr(ctrl, "override_script", None)
                    if isinstance(override_map, dict):
                        override_map[matched_label] = override_value
                except Exception:
                    pass

                # Invalidate short-lived switch status cache after a state-affecting update.
                route_state.switch_status_payload = None
                route_state.switch_status_until = 0.0

            except AutomationActivationError as e:
                return JSONResponse({"error": e.public_message}, status_code=409)
            except Exception as e:
                printDM(f"[override_switch] persist failed for '{matched_label}': {e}", location=MODULE)
                return JSONResponse({"error": "persist_failed"}, status_code=500)

            # notify any listeners that an automation rule changed
            try:
                app = request.app
                if hasattr(app.state, "switch_broadcast"):
                    await app.state.switch_broadcast({
                        "type": "automation_toggle",
                        "switch_id": sid,
                        "label": matched_label,
                        "enabled": bool(effective_rule_enabled),
                    })
            except Exception as e:
                printDM(f"[override_switch] broadcast failed: {e}", location=MODULE)

            # Return both states so UI can reflect the RULE state
            return {
                "status": "ok",
                "enabled": effective_rule_enabled,
                "override": (not effective_rule_enabled),
            }

        except Exception as e:
            printDM(f"[override_switch] ERROR for '{switch_name}': {e}", location=MODULE)
            return JSONResponse({"error": "internal_error"}, status_code=500)


def register_switch_events(router: APIRouter, *, app, services: RuntimeServices,
                           debug: bool = False) -> DashboardBroadcast:
    """Register switch WebSocket delivery and bind runtime producer callbacks."""
    DEBUG = debug
    _SWITCH_SOCKETS: Set[WebSocket] = set()

    async def _switch_broadcast(payload: Dict[str, Any]) -> None:
        if DEBUG:
            try:
                printDM(
                    f"[switch-ws] broadcast type={payload.get('type')} key={payload.get('key')} ui_key={payload.get('ui_key', '')} clients={len(_SWITCH_SOCKETS)}",
                    location=MODULE,
                )
            except Exception:
                pass
        stale: list[WebSocket] = []
        for ws in list(_SWITCH_SOCKETS):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            try:
                _SWITCH_SOCKETS.discard(ws)
            except Exception:
                pass

    @router.websocket("/ws/switch-updates")
    async def ws_switch_updates(ws: WebSocket):
        await ws.accept()
        _SWITCH_SOCKETS.add(ws)
        if DEBUG:
            printDM(f"[switch-ws] client connected total={len(_SWITCH_SOCKETS)}", location=MODULE)
        try:
            # keep alive; client never needs to send data
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _SWITCH_SOCKETS.discard(ws)
            if DEBUG:
                printDM(f"[switch-ws] client disconnected total={len(_SWITCH_SOCKETS)}", location=MODULE)

    # Keep the async route API while injecting notifications into runtime services.
    app.state.switch_broadcast = _switch_broadcast
    services.bind_dashboard_broadcast(_switch_broadcast)
    return _switch_broadcast
