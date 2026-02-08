"""Async task supervisor and watchdog orchestration.

Manages background coroutines, restart policies, and liveness heartbeats for
core Sensorius services (sensors, MQTT, web server, GC, etc.). The supervisor
tracks task health, exposes a feed-the-dogs API for workers, and works with the
watchdog monitor to detect stalled loops and trigger recovery actions.
"""

import asyncio
import time
from saiUtils import printDM, debug_enabled

MODULE = "saiTaskSupervisor"
DEBUG = debug_enabled(MODULE)
CRASH_RESTART_DELAY_SEC = 5.0
RETURN_RESTART_DELAY_SEC = 1.0

class TaskSupervisor:
    def __init__(self):
        self.paused = False
        self.tasks = []
        self._task_names = set()
        self._runner_tasks = set()
        self._started = False
        self._shutdown = False
        self._start_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.time_to_feedthedogs = {}
        self.failed_tasks = {}

    def add(self, coro_func, *args, name="Unnamed", **kwargs):
        if self._started:
            raise RuntimeError("Cannot add tasks after supervisor has started")
        if name in self._task_names:
            raise ValueError(f"Duplicate supervised task name: {name!r}")
        self._task_names.add(name)
        self.tasks.append({
            "func": coro_func,
            "args": args,
            "kwargs": kwargs,
            "name": name
        })
        self.time_to_feedthedogs[name] = time.monotonic()

    async def runner(self, func, args, kwargs, name):
        while not self._shutdown:
            try:
                await self.pause_event.wait()
                if self._shutdown:
                    return
                if DEBUG:
                    printDM(f"Starting task: {name}", location=f"{__name__}.runner")
                await func(*args, **kwargs)
                if self._shutdown:
                    return
                printDM(
                    f"Task {name} returned; restarting in {RETURN_RESTART_DELAY_SEC:.0f}s",
                    location=f"{__name__}.runner",
                )
                await asyncio.sleep(RETURN_RESTART_DELAY_SEC)
            except asyncio.CancelledError:
                return
            except Exception as e:
                printDM(f"Task {name} crashed with: {e}", location=f"{__name__}.runner")
                await asyncio.sleep(CRASH_RESTART_DELAY_SEC)

    async def start(self):
        async with self._start_lock:
            if self._started:
                await self._stop_event.wait()
                return
            self._started = True
            self._shutdown = False
            self._stop_event.clear()
            self._runner_tasks = {
                asyncio.create_task(
                    self.runner(task["func"], task["args"], task["kwargs"], task["name"]),
                    name=f"TaskSupervisor:{task['name']}",
                )
                for task in self.tasks
            }
        await self._stop_event.wait()

    async def run_forever(self):
        await self.start()

    async def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        self.pause_event.set()
        tasks = list(self._runner_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runner_tasks.clear()
        self._stop_event.set()

    async def cancel_all(self):
        await self.shutdown()

    def feedthedogs(self, task_name, error=False):
        if task_name in self.time_to_feedthedogs and task_name != "Watchdog Monitor":
            now = time.monotonic()
            self.time_to_feedthedogs[task_name] = now
            if error:
                self.failed_tasks[task_name] = now
            else:
                self.failed_tasks.pop(task_name, None)
            if DEBUG:
                msg = f"feedthedogs received for {task_name}"
                if error:
                    msg += " (ERROR)"
                printDM(f"[TaskSupervisor] {msg}", location="TaskSupervisor")
        elif DEBUG:
            printDM(f"Failed to feed the dogs — {task_name} not found", location=f"{__name__}.feedthedogs")

    def pause_all(self):
        self.paused = True
        self.pause_event.clear()
        if DEBUG:
            printDM("All supervised tasks paused", location=f"{__name__}.pause_all")

    def resume_all(self):
        self.paused = False
        self.pause_event.set()
        if DEBUG:
            printDM("All supervised tasks resumed", location=f"{__name__}.resume_all")
