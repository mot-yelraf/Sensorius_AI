"""Web server controller for starting the Sensorius UI/API service.

Responsibilities:
- configure and launch the FastAPI application
- mount static assets and Jinja templates
- register route modules and inject shared app state (settings, MQTT ingest)
- provide a single async entrypoint to run the web server in the supervisor
"""
import asyncio
import os
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
        self.host = str(os.environ.get("SENSORIUS_HTTP_HOST") or self.settings.get_setting("Network", "HTTPHOST", "0.0.0.0"))
        try:
            self.port = int(os.environ.get("SENSORIUS_HTTP_PORT") or self.settings.get_setting("Network", "HTTPPORT", 8000))
        except Exception:
            self.port = 8000

        # ultra-light health route for readiness checks
        self.app.add_api_route("/healthz", lambda: PlainTextResponse("ok"), methods=["GET"])
        
        # Compression (nice win for templates/js/css)
        self.app.add_middleware(GZipMiddleware, minimum_size=1024)
        # Path defs for web ui components
        base_dir = Path(__file__).resolve().parent
        ui_static_dir = (base_dir / "ui_static").resolve()
        ui_templates_dir = (base_dir / "ui_templates").resolve()

        if not ui_static_dir.exists():
            raise FileNotFoundError(f"[{MODULE}] Required ui_static directory not found at {ui_static_dir}")
        if not ui_templates_dir.exists():
            raise FileNotFoundError(f"[{MODULE}] Required ui_templates directory not found at {ui_templates_dir}")

        # Static + templates
        self.app.mount("/ui_static", StaticFiles(directory=str(ui_static_dir)), name="ui_static")
        self.templates = Jinja2Templates(directory=str(ui_templates_dir))
        # Make templates available to route modules without circular imports
        self.app.state.templates = self.templates
        
        # Schedule prewarm without returning the Task; Starlette awaits
        # awaitable startup handler results.
        self.app.add_event_handler("startup", self._schedule_prewarm)

    def _schedule_prewarm(self) -> None:
        asyncio.create_task(self._prewarm())


    async def _prewarm(self):
        """Warm DB pages & compute a first set of values/stats to avoid cold-start latency."""
        try:
            from saiWebRoutes import data_logger as routes_logger, statter as routes_statter

            available = await asyncio.to_thread(routes_logger.get_available_sensors)
            available = available or []
            sids = self.settings.get_all_sensor_ids() or available

            cap = 8  # keep it light
            for sid in sids[:cap]:
                # touch hot paths
                _ = await asyncio.to_thread(routes_logger.get_latest_values, sid)
                maybe_stats = await asyncio.to_thread(routes_statter.get_24hr_stats, sid)
                if inspect.isawaitable(maybe_stats):
                    await maybe_stats  # handle async statter seamlessly
                await asyncio.sleep(0)

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

    def run(self, host=None, port=None):
        host = host or self.host
        port = int(port if port is not None else self.port)
        if DEBUG:
            printDM(f"Starting FastAPI on {host}:{port}", location=MODULE)
        uvicorn.run(self.app, host=host, port=port)
        
    async def run_async(self, host=None, port=None):
        import uvicorn
        host = host or self.host
        port = int(port if port is not None else self.port)
        if DEBUG:
            printDM(f"Starting uvicorn on {host}:{port}", location=MODULE)
        self.config = uvicorn.Config(self.app, host=host, port=port, log_level="info", access_log=False)
        self.server = uvicorn.Server(self.config)
        
        # wait for actual server readiness
        # This blocks until server exits
        await self.server.serve()

    def is_ready(self):
        server = getattr(self, "server", None)
        return bool(getattr(server, "started", False))

async def launch_webview(url: str = "http://127.0.0.1:8000", retries: int = 10, delay: float = 1.0):
    import os, sys, traceback, httpx, asyncio
    try:
        import webview
    except Exception as e:
        from saiUtils import printDM
        printDM(f"pywebview not available: {e} — continuing headless", location="saiWebServer")
        return None
    from saiUtils import printDM

    if sys.platform.startswith("linux"):
        # Keep caller-provided values; only provide conservative defaults.
        os.environ.setdefault("GDK_BACKEND", "wayland,x11")
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            printDM("DISPLAY/WAYLAND_DISPLAY not set; skipping GUI.", location="saiWebServer")
            return None

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
        def _int_env(name: str, default: int) -> int:
            raw_value = os.environ.get(name)
            if raw_value is None:
                return default
            try:
                return int(raw_value)
            except ValueError:
                return default

        window = webview.create_window(
            "Sensorius Automatio Instrumentorum",
            initial_url,
            width=_int_env("SENSORIUS_GUI_WIDTH", 1920),
            height=_int_env("SENSORIUS_GUI_HEIGHT", 1000),
            x=_int_env("SENSORIUS_GUI_X", 0),
            y=_int_env("SENSORIUS_GUI_Y", 48),
            resizable=True, frameless=False, confirm_close=True
        )
        if DEBUG:
            printDM(f"Web view created at {initial_url}", location="saiWebServer")
        return window
    except Exception as e:
        printDM(f"Webview failed to start: {e}", location="saiWebServer")
        printDM(traceback.format_exc(), location="saiWebServer")
        return None
