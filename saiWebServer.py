"""Web server controller for starting the Sensorius UI/API service.

Responsibilities:
- configure and launch the FastAPI application
- mount static assets and Jinja templates
- register route modules and inject shared app state (settings, MQTT ingest)
- provide a single async entrypoint to run the web server in the supervisor
"""
import asyncio
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from saiUtils import printDM, debug_enabled
import inspect

MODULE = "saiWebServer"
DEBUG = debug_enabled(MODULE)

class WebServerController:
    def __init__(self, settings, net_mgr, supervisor, gc_mgr, mqtt_ingest):
        self.settings = settings
        self.net_mgr = net_mgr
        self.supervisor = supervisor
        self.gc_mgr = gc_mgr
        self.mqtt_ingest = mqtt_ingest
        self.app = FastAPI()
        self.session_active = False

        # ultra-light health route for readiness checks
        self.app.add_api_route("/healthz", lambda: PlainTextResponse("ok"), methods=["GET"])
        
        # Compression (nice win for templates/js/css)
        self.app.add_middleware(GZipMiddleware, minimum_size=1024)
        # Path defs for web ui components
        base_dir = Path(__file__).resolve().parent
        ui_static_dir = (base_dir / "ui_static").resolve()
        ui_templates_dir = (base_dir / "ui_templates").resolve()

        if not ui_static_dir.exists():
            printDM(f"[{MODULE}] ui_static not found at {ui_static_dir}", location=MODULE)
        if not ui_templates_dir.exists():
            printDM(f"[{MODULE}] ui_templates not found at {ui_templates_dir}", location=MODULE)

        # Static + templates
        self.app.mount("/ui_static", StaticFiles(directory=str(ui_static_dir)), name="ui_static")
        self.templates = Jinja2Templates(directory=str(ui_templates_dir))
        # Make templates available to route modules without circular imports
        self.app.state.templates = self.templates
        
        # non-blocking prewarm at startup
        self.app.add_event_handler("startup", lambda: asyncio.create_task(self._prewarm()))


    async def _prewarm(self):
        """Warm DB pages & compute a first set of values/stats to avoid cold-start latency."""
        try:
            from saiWebRoutes import data_logger as routes_logger, statter as routes_statter

            available = routes_logger.get_available_sensors() or []
            sids = self.settings.get_all_sensor_ids() or available

            cap = 8  # keep it light
            for sid in sids[:cap]:
                # touch hot paths
                _ = routes_logger.get_latest_values(sid)
                maybe_stats = routes_statter.get_24hr_stats(sid)
                if inspect.isawaitable(maybe_stats):
                    await maybe_stats  # handle async statter seamlessly

            if DEBUG:
                printDM(f"Prewarm complete for {len(sids[:cap])} sensors", location=f"{MODULE}._prewarm")
        except Exception as e:
            printDM(f"Prewarm skipped: {e}", location=f"{MODULE}._prewarm")


    async def initialize_server(self):
        from saiWebRoutes import register_routes

        await register_routes(self.app, self.settings, self.net_mgr, self.gc_mgr, self.mqtt_ingest)
        # Make ingest accessible to routes that use request.app.state.mqtt_ingest.
        self.app.state.mqtt_ingest = self.mqtt_ingest
        app = self.app
        @app.get("/favicon.ico")
        async def favicon_root():
            return Response(status_code=204)
    
        if DEBUG:
            printDM("FastAPI routes registered", location=MODULE)

    def run(self, host="0.0.0.0", port=8000):
        if DEBUG:
            printDM(f"Starting FastAPI on {host}:{port}", location=MODULE)
        uvicorn.run(self.app, host=host, port=port)
        
    async def run_async(self):
        import socket
        import uvicorn
        if DEBUG:
            printDM(f"Starting uvicorn", location=MODULE)
        self.config = uvicorn.Config(self.app, host="0.0.0.0", port=8000, log_level="info", access_log=False )
        self.server = uvicorn.Server(self.config)
        
        # wait for actual server readiness
        # This blocks until server exits
        await self.server.serve()

    def is_ready(self):
        return getattr(self.server, "started", False)

async def launch_webview(url: str = "http://127.0.0.1:8000", retries: int = 10, delay: float = 1.0):
    import os, traceback, webview, httpx, asyncio
    from urllib.parse import urljoin, urlencode, quote
    try:
        # Local imports to avoid boot-time cycles
        from saiSwitchSettingsManager import SwitchSettingsManager
        from saiUtils import printDM
    except Exception:
        # tolerate early import issues; we'll just open base url
        SwitchSettingsManager = None

    os.environ["GDK_BACKEND"] = "x11"
    os.environ["DISPLAY"] = ":0"

    if DEBUG:
        printDM("Launching webview...", location="saiWebServer")

    await asyncio.sleep(2)  # small grace, prewarm also runs

    base_url = url.rstrip("/")
    health_url = f"{base_url}/healthz"

    # --- Wait for server readiness ---
    server_ready = False
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(health_url, timeout=3.0)
                if r.status_code == 200:
                    if DEBUG:
                        printDM(f"Web server ready after {attempt+1} attempts", location="saiWebServer")
                    server_ready = True
                    break
        except Exception as e:
            printDM(f"[Attempt {attempt+1}/{retries}] Web view not ready: {e}", location="saiWebServer")
        await asyncio.sleep(delay + attempt * 0.25)

    if not server_ready:
        printDM(f"Web view @ {base_url} not ready after {retries} attempts — skipping GUI", location="saiWebServer")
        return None

    # --- Compute initial route (prefer Advanced modal for first available switch) ---
    initial_url = base_url  

    # --- Launch pywebview ---
    try:
        window = webview.create_window(
            "Sensorius Automatio Instrumentorum",
            initial_url,
            width=1920, height=1000, x=0, y=0,
            resizable=True, frameless=False, confirm_close=True
        )
        if DEBUG:
            printDM(f"Web view created at {initial_url}", location="saiWebServer")
        return window
    except Exception as e:
        printDM(f"Webview failed to start: {e}", location="saiWebServer")
        printDM(traceback.format_exc(), location="saiWebServer")
        return None
