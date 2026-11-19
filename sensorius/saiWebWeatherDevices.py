"""Register weather device setup independently of hardware acquisition.

Settings mutations follow the protected-access checks used by device routes;
USB reads run in bounded service workers, serialized with supervised ingestion.
"""

import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from .saiAcuRite import AcuRiteUsbIngest
from .saiUtils import printDM


_PUBLIC_CONFIGURATION_ERRORS = {message: message for message in (
    "Expected an AcuRite configuration object.",
    "Unsupported AcuRite USB console family",
    "Invalid USB serial number.",
    "Data retrieval interval must be 60–3600 seconds.",
)}


def register_weather_device_routes(router, *, services, settings_factory, require_protected_access, invalidate_dashboard_caches):
    """Expose independent weather-console setup/status with thin route handlers."""
    def service(request):
        if services.acurite_service is None:
            services.acurite_service = AcuRiteUsbIngest(
                settings=settings_factory(), data_logger=services.data_logger,
                supervisor=services.supervisor,
            )
            request.app.state.acurite_service = services.acurite_service
        return services.acurite_service

    @router.get("/acurite/status")
    async def status(request: Request):
        return JSONResponse(await asyncio.to_thread(service(request).status))

    async def mutate(request, *, disable=False):
        require_protected_access(request, require_csrf=True)
        current = service(request)
        try:
            if disable:
                await current.disable_device()
            else:
                payload = await request.json()
                await current.save_configuration(payload)
            invalidate_dashboard_caches()
            return JSONResponse({"ok": True, **await asyncio.to_thread(current.status)})
        except (ValueError, TypeError) as exc:
            message = _PUBLIC_CONFIGURATION_ERRORS.get(
                str(exc), "Invalid AcuRite configuration. Check the console family, USB serial and retrieval interval."
            )
            return JSONResponse({"ok": False, "error": message}, status_code=400)
        except Exception as exc:
            printDM(f"AcuRite setup failed: {exc}", location=__name__)
            return JSONResponse({"ok": False, "error": "Unable to save AcuRite settings. Check the Sensorius log."}, status_code=500)

    @router.post("/acurite/save")
    async def save(request: Request):
        return await mutate(request)

    @router.post("/acurite/test")
    async def test(request: Request):
        require_protected_access(request, require_csrf=True)
        return await _test_console(request, lambda: service(request), "AcuRite USB")

    @router.post("/acurite/disable")
    async def disable(request: Request):
        return await mutate(request, disable=True)

    for kind in ("davis", "oregon"):
        _register_usb_routes(router, kind, services, require_protected_access, invalidate_dashboard_caches)


async def _test_console(request, get_service, label):
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("enabled") is not True:
            return JSONResponse({"ok": False, "error": "Select Enable before testing the console."}, status_code=400)
        current = get_service()
        if current is None:
            return JSONResponse({"ok": False, "error": "Console service unavailable."}, status_code=503)
        if await current.test_retrieval(payload):
            return JSONResponse({"ok": True, "message": f"{label} data retrieval succeeded."})
        return JSONResponse({"ok": False, "error": f"{label} data retrieval failed: no valid live data returned. Check the connection, console settings and USB access."}, status_code=502)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Invalid console configuration. Check the model, serial port, baud rate, USB serial and retrieval interval (60–3600 seconds)."}, status_code=400)
    except Exception as exc:
        printDM(f"{label} test failed: {exc}", location=__name__)
        return JSONResponse({"ok": False, "error": f"{label} data retrieval failed. Check USB access and the Sensorius log."}, status_code=502)


def _register_usb_routes(router, kind, services, require_access, invalidate):
    from .saiUsbWeather import FAMILIES

    async def status(request: Request):
        current = getattr(services, f"{kind}_service", None)
        if current is None:
            return JSONResponse({"enabled": False, "state": "disabled", "label": FAMILIES[kind][1]})
        return JSONResponse(await asyncio.to_thread(current.status))

    async def mutate(request, disable=False):
        require_access(request, require_csrf=True)
        current = getattr(services, f"{kind}_service", None)
        if current is None:
            return JSONResponse({"ok": False, "error": "Console service unavailable."}, status_code=503)
        try:
            if disable:
                await current.disable_device()
            else:
                await current.save_configuration(await request.json())
            invalidate()
            return JSONResponse({"ok": True, **await asyncio.to_thread(current.status)})
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Invalid console configuration. Check the model, local serial port, baud rate, USB serial and retrieval interval (60–3600 seconds)."}, status_code=400)
        except Exception as exc:
            printDM(f"{kind} setup failed: {exc}", location=__name__)
            return JSONResponse({"ok": False, "error": "Unable to save console settings. Check the Sensorius log."}, status_code=500)

    async def save(request: Request):
        return await mutate(request)

    async def disable(request: Request):
        return await mutate(request, disable=True)

    async def test(request: Request):
        require_access(request, require_csrf=True)
        return await _test_console(request, lambda: getattr(services, f"{kind}_service", None), FAMILIES[kind][1])

    router.add_api_route(f"/{kind}/status", status, methods=["GET"])
    router.add_api_route(f"/{kind}/save", save, methods=["POST"])
    router.add_api_route(f"/{kind}/test", test, methods=["POST"])
    router.add_api_route(f"/{kind}/disable", disable, methods=["POST"])
