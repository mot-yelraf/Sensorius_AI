"""Garbage collection manager for long-running Sensorius services.

This module provides a lightweight, watchdog-friendly GC loop suitable for
always-on deployments. It is designed to:

1. Run periodic GC with jitter to avoid synchronized load spikes.
2. Escalate to full generation collection on a configurable cadence.
3. Expose simple runtime stats for observability and troubleshooting.
4. Support graceful stop/cancel behavior during service shutdown.

Environment overrides:
- ``SENSORIUS_GC_ENABLED``: ``1|true|yes|on`` (default: enabled)
- ``SENSORIUS_GC_INTERVAL_SEC``: base interval between GC cycles
- ``SENSORIUS_GC_JITTER_SEC``: random +/- jitter applied to interval
- ``SENSORIUS_GC_MIN_SLEEP_SEC``: lower bound for sleep duration
- ``SENSORIUS_GC_FULL_EVERY_N``: run full gen-2 collect every N cycles
"""

import asyncio
import gc
import os
import random
import time
from typing import Any

from .saiUtils import printDM, debug_enabled

MODULE = "saiGarbageCollection"
DEBUG = debug_enabled(MODULE)

class GCManager:
    def __init__(
        self,
        interval_sec: float = 29,
        supervisor=None,
        *,
        jitter_sec: float = 0.7,
        min_sleep_sec: float = 1.0,
        full_collect_every_n: int = 10,
        enabled: bool = True,
    ):
        self.enabled = self._env_bool("SENSORIUS_GC_ENABLED", enabled)
        self.interval = self._env_float("SENSORIUS_GC_INTERVAL_SEC", interval_sec, minimum=0.1)
        self.jitter_sec = self._env_float("SENSORIUS_GC_JITTER_SEC", jitter_sec, minimum=0.0)
        self.min_sleep_sec = self._env_float("SENSORIUS_GC_MIN_SLEEP_SEC", min_sleep_sec, minimum=0.1)
        self.full_collect_every_n = self._env_int(
            "SENSORIUS_GC_FULL_EVERY_N", full_collect_every_n, minimum=1
        )
        self.supervisor = supervisor
        self._stop_event = asyncio.Event()
        self._cycle = 0
        self.last_gc_info: dict[str, Any] = {}

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_float(name: str, default: float, minimum: float) -> float:
        raw = os.getenv(name)
        try:
            val = float(raw) if raw is not None else float(default)
        except Exception:
            val = float(default)
        return max(minimum, val)

    @staticmethod
    def _env_int(name: str, default: int, minimum: int) -> int:
        raw = os.getenv(name)
        try:
            val = int(raw) if raw is not None else int(default)
        except Exception:
            val = int(default)
        return max(minimum, val)

    async def run(self):
        if DEBUG:
            printDM(
                (
                    "GC Manager started "
                    f"(enabled={self.enabled}, interval={self.interval:.2f}s, "
                    f"jitter={self.jitter_sec:.2f}s, full_every={self.full_collect_every_n})"
                ),
                location="GCManager.run",
            )

        while not self._stop_event.is_set():
            try:
                if self.enabled:
                    self._cycle += 1
                    generation = 2 if (self._cycle % self.full_collect_every_n == 0) else 0
                    t0 = time.monotonic()
                    before_count = gc.get_count()
                    collected = gc.collect(generation)
                    after_count = gc.get_count()
                    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
                    self.last_gc_info = {
                        "cycle": self._cycle,
                        "generation": generation,
                        "collected": int(collected),
                        "duration_ms": elapsed_ms,
                        "before_count": before_count,
                        "after_count": after_count,
                    }
                    if DEBUG:
                        printDM(
                            (
                                f"GC cycle={self._cycle} gen={generation} collected={collected} "
                                f"duration_ms={elapsed_ms} before={before_count} after={after_count}"
                            ),
                            location="GCManager.run",
                        )
            except Exception as e:
                printDM(f"Exception during GC: {e}", location="GCManager.run")

            if self.supervisor:
                self.supervisor.feedthedogs("GC Manager")

            sleep_for = max(
                self.min_sleep_sec,
                self.interval + random.uniform(-self.jitter_sec, self.jitter_sec),
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                if DEBUG:
                    printDM("GC Manager cancelled", location="GCManager.run")
                break

        if DEBUG:
            printDM("GC Manager stopped", location="GCManager.run")

    def stop(self):
        """Request the GC loop to stop on its next wake-up."""
        self._stop_event.set()

    def force_collect(self, reason: str = "manual", generation: int = 2) -> int:
        """Run immediate GC and return collected object count."""
        if DEBUG:
            printDM(f"Forcing GC: {reason}", location="GCManager.force_collect")
        t0 = time.monotonic()
        collected = gc.collect(generation)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        self.last_gc_info = {
            "cycle": self._cycle,
            "generation": generation,
            "collected": int(collected),
            "duration_ms": elapsed_ms,
            "forced": True,
            "reason": reason,
        }
        return int(collected)

    def gc_stats(self):
        """Return interpreter GC stats when available; else ``None``."""
        try:
            return gc.get_stats() if hasattr(gc, "get_stats") else None
        except Exception:
            return None

    def freeMem(self):
        """Backward-compatible alias for historical API."""
        return self.gc_stats()

    def freezeMem(self):
        """Attempt to freeze GC-tracked objects if supported by runtime."""
        if hasattr(gc, "freeze"):
            try:
                gc.freeze()
                return "frozen"
            except Exception:
                return "freeze failed"
        return "freeze not supported"
