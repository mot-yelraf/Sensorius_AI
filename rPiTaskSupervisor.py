"""Async task supervisor and watchdog orchestration.

Manages background coroutines, restart policies, and liveness heartbeats for
core Sensorius services (sensors, MQTT, web server, GC, etc.). The supervisor
tracks task health, exposes a feed-the-dogs API for workers, and works with the
watchdog monitor to detect stalled loops and trigger recovery actions.
"""

import asyncio
import time
from rPiUtils import printDM, debug_enabled

MODULE = "rPiTaskSupervisor"
DEBUG = debug_enabled(MODULE)

class TaskSupervisor:
    def __init__(self):
        self.paused = False
        self.tasks = []
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.time_to_feedthedogs = {}
        self.failed_tasks = {}

    def add(self, coro_func, *args, name="Unnamed", **kwargs):
        self.tasks.append({
            "func": coro_func,
            "args": args,
            "kwargs": kwargs,
            "name": name
        })
        self.time_to_feedthedogs[name] = time.monotonic()

    async def runner(self, func, args, kwargs, name):
        while True:
            try:
                await self.pause_event.wait()
                if DEBUG:
                    printDM(f"Starting task: {name}", location=f"{__name__}.runner")
                await func(*args, **kwargs)
            except Exception as e:
                printDM(f"Task {name} crashed with: {e}", location=f"{__name__}.runner")
                await asyncio.sleep(5)

    async def start(self):
        await asyncio.gather(
            *(self.runner(task["func"], task["args"], task["kwargs"], task["name"]) for task in self.tasks)
        )

    def feedthedogs(self, task_name, error=False):
        if task_name in self.time_to_feedthedogs and task_name != "Watchdog Monitor":
            now = time.monotonic()
            self.time_to_feedthedogs[task_name] = now
            if DEBUG:
                msg = f"feedthedogs received for {task_name}"
                if error:
                    msg += " (ERROR)"
                printDM(f"[TaskSupervisor] {msg}", location="TaskSupervisor")
        elif DEBUG:
            printDM(f"Failed to feed the dogs — {task_name} not found", location=f"{__name__}.feedthedogs")

    def pause_all(self):
        self.pause_event.clear()
        if DEBUG:
            printDM("All supervised tasks paused", location=f"{__name__}.pause_all")

    def resume_all(self):
        self.pause_event.set()
        if DEBUG:
            printDM("All supervised tasks resumed", location=f"{__name__}.resume_all")
