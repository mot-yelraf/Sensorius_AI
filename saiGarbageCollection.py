"""Periodic garbage collection and memory housekeeping utilities."""

import gc
import random
import asyncio
from saiUtils import printDM, debug_enabled

MODULE = "saiGarbageCollection"
DEBUG = debug_enabled(MODULE)

class GCManager:
    def __init__(self, interval_sec=29, supervisor=None):
        self.interval = interval_sec
        self.supervisor = supervisor

    async def run(self):
        if DEBUG:
            printDM("GC Manager started", location="GCManager.run")

        while True:
            try:
                before = gc.get_stats()[0]['collected'] if hasattr(gc, 'get_stats') else gc.mem_free()
                gc.collect()
                after = gc.get_stats()[0]['collected'] if hasattr(gc, 'get_stats') else gc.mem_free()
                delta = after - before
                if DEBUG:
                    printDM(f"Collected delta: {delta}", location="GCManager.run")
            except Exception as e:
                printDM(f"Exception during GC: {e}", location="GCManager.run")

            if self.supervisor:
                self.supervisor.feedthedogs("GC Manager")

            await asyncio.sleep(self.interval + random.uniform(-0.7, 0.7))

    def force_collect(self, reason="manual"):
        if DEBUG:
            printDM(f"Forcing GC: {reason}", location="GCManager.force_collect")
        gc.collect()

    def freeMem(self):
        # gc.get_stats is CPython 3.4+ feature but not available on all platforms
        try:
            return gc.get_stats() if hasattr(gc, 'get_stats') else None
        except Exception:
            return None

    def freezeMem(self):
        # No direct analog to CircuitPython's gc.freeze in CPython
        return "freeze not supported"
