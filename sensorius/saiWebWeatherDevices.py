"""Register weather device setup independently of hardware acquisition.

Settings mutations follow the protected-access checks used by device routes;
USB reads remain in supervised ingestion, never in an HTTP request handler.
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
    """Expose AcuRite setup/status while keeping route handlers thin."""
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

    @router.post("/acurite/disable")
    async def disable(request: Request):
        return await mutate(request, disable=True)
