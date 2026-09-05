"""Async task supervisor and watchdog orchestration.

Manages background coroutines, restart policies, and liveness heartbeats for
core Sensorius services (sensors, MQTT, web server, GC, etc.). The supervisor
tracks task health, exposes a feed-the-dogs API for workers, and works with the
watchdog monitor to detect stalled loops and trigger recovery actions.
"""

import asyncio
import time
from .saiUtils import printDM, debug_enabled

MODULE = "saiTaskSupervisor"
DEBUG = debug_enabled(MODULE)
CRASH_RESTART_DELAY_SEC = 5.0
CRASH_RESTART_MAX_DELAY_SEC = 60.0
CRASH_BACKOFF_RESET_SEC = 300.0
RETURN_RESTART_DELAY_SEC = 1.0
FEED_LOG_MIN_INTERVAL_SEC = 10.0

class TaskSupervisor:
    """Run, restart, stop, and monitor registered asynchronous tasks."""

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
        self.task_policies = {}
        self.task_issues = {}
        self._last_feed_log = {}
        self._consecutive_crashes = {}

    def add(
        self,
        coro_func,
        *args,
        name="Unnamed",
        fatal_on_timeout=True,
        fatal_on_error=True,
        **kwargs,
    ):
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
        self.task_policies[name] = {
            "fatal_on_timeout": bool(fatal_on_timeout),
            "fatal_on_error": bool(fatal_on_error),
        }

    async def runner(self, func, args, kwargs, name):
        while not self._shutdown:
            try:
                await self.pause_event.wait()
                if self._shutdown:
                    return
                started_mono = time.monotonic()
                if DEBUG:
                    printDM(f"Starting task: {name}", location=f"{__name__}.runner")
                await func(*args, **kwargs)
                self._consecutive_crashes[name] = 0
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
                runtime_s = max(0.0, time.monotonic() - started_mono)
                previous_crashes = 0 if runtime_s >= CRASH_BACKOFF_RESET_SEC else int(
                    self._consecutive_crashes.get(name, 0)
                )
                consecutive_crashes = min(previous_crashes + 1, 16)
                self._consecutive_crashes[name] = consecutive_crashes
                restart_delay_s = min(
                    CRASH_RESTART_DELAY_SEC * (2 ** (consecutive_crashes - 1)),
                    CRASH_RESTART_MAX_DELAY_SEC,
                )
                now = time.monotonic()
                self.failed_tasks[name] = now
                self.report_issue(
                    name,
                    f"Task crashed: {e}; retrying in {restart_delay_s:.0f}s",
                    recommend_restart=True,
                    issue_type="task_crash",
                )
                printDM(
                    f"Task {name} crashed with: {e}; restarting in {restart_delay_s:.0f}s",
                    location=f"{__name__}.runner",
                    level="warning",
                )
                await asyncio.sleep(restart_delay_s)

    async def start(self):
        async with self._start_lock:
            if self._started:
                await self._stop_event.wait()
                return
            self._started = True
            self._shutdown = False
            self._stop_event.clear()
            if DEBUG:
                printDM(
                    f"Supervisor starting {len(self.tasks)} task(s): {[task['name'] for task in self.tasks]}",
                    location=f"{__name__}.start",
                )
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

    def get_task_policy(self, task_name):
        """Return watchdog policy for a supervised task."""
        policy = self.task_policies.get(task_name) or {}
        return {
            "fatal_on_timeout": bool(policy.get("fatal_on_timeout", True)),
            "fatal_on_error": bool(policy.get("fatal_on_error", True)),
        }

    def report_issue(self, task_name, message, *, recommend_restart=True, issue_type="warning"):
        """Record a recoverable runtime issue for UI/status consumers."""
        if not task_name:
            return
        now = time.monotonic()
        current = self.task_issues.get(task_name) or {}
        self.task_issues[task_name] = {
            "task_name": str(task_name),
            "message": str(message or "").strip(),
            "recommend_restart": bool(recommend_restart),
            "issue_type": str(issue_type or "warning"),
            "count": int(current.get("count", 0)) + 1,
            "last_seen_monotonic": now,
            "first_seen_monotonic": float(current.get("first_seen_monotonic", now)),
        }

    def runtime_status_snapshot(self):
        """Return a lightweight snapshot of watchdog policy and recoverable issues."""
        issues = []
        for task_name in sorted(self.task_issues.keys()):
            issue = self.task_issues.get(task_name) or {}
            issues.append(
                {
                    "task_name": task_name,
                    "message": str(issue.get("message", "")),
                    "recommend_restart": bool(issue.get("recommend_restart", True)),
                    "issue_type": str(issue.get("issue_type", "warning")),
                    "count": int(issue.get("count", 0)),
                }
            )
        policies = {
            name: self.get_task_policy(name)
            for name in sorted(self.task_policies.keys())
        }
        return {
            "issues": issues,
            "policies": policies,
        }

    def feedthedogs(self, task_name, error=False):
        """Record liveness without discarding the restart backoff history."""
        if task_name in self.time_to_feedthedogs and task_name != "Watchdog Monitor":
            now = time.monotonic()
            self.time_to_feedthedogs[task_name] = now
            if error:
                self.failed_tasks[task_name] = now
            else:
                self.failed_tasks.pop(task_name, None)
                issue = self.task_issues.get(task_name) or {}
                if issue.get("issue_type") == "task_crash":
                    self.task_issues.pop(task_name, None)
            if DEBUG:
                # Heartbeats are frequent; throttle debug noise per task.
                last = self._last_feed_log.get(task_name, 0.0)
                if error or (now - last) >= FEED_LOG_MIN_INTERVAL_SEC:
                    self._last_feed_log[task_name] = now
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
