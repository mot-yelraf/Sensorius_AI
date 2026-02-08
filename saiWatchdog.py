"""Watchdog monitor for detecting stalled loops and service liveness.

Consumes heartbeat updates from the TaskSupervisor and logs or escalates when
workers stop feeding within their expected intervals. Used to surface stuck
sensor loops, MQTT tasks, or other background services.
"""
import asyncio
import os
import sys
import time
import random
import signal

from saiUtils import printDM, debug_enabled

MODULE = "saiWatchdog"
DEBUG = debug_enabled(MODULE)

# ---- user-tunable constants (top) ----
WATCHDOG_LOOP_INTERVAL_SEC = 10.0         # base interval between sweeps
WATCHDOG_JITTER_SEC = 0.8                 # +/- jitter
WATCHDOG_EXIT_DELAY_SEC = 0.75            # small grace after signaling
EXIT_CODE_TIMEOUT = 70                    # distinct exit code for timeout
EXIT_CODE_FAILED_TASK = 71                # distinct exit code for explicit failure
POLITE_SHUTDOWN = False


def _env_float(name: str, default: float, minimum: float) -> float:
    """Read a positive float from env, falling back to default on invalid input."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        printDM(f"[Watchdog] Invalid {name}={raw!r}; using default={default}", location=MODULE, level="warning")
        return default
    if value < minimum:
        printDM(
            f"[Watchdog] {name} below minimum ({minimum}); clamping from {value} to {minimum}",
            location=MODULE,
            level="warning",
        )
        return minimum
    return value


def _load_runtime_config(timeout: float | int) -> tuple[float, float, float]:
    """
    Compute watchdog runtime settings from defaults + optional env overrides.
    """
    # Keep timeout default backwards-compatible when env is not set.
    timeout_sec = _env_float("SENSORIUS_WATCHDOG_TIMEOUT_SEC", float(timeout), 1.0)
    loop_interval = _env_float(
        "SENSORIUS_WATCHDOG_LOOP_INTERVAL_SEC", WATCHDOG_LOOP_INTERVAL_SEC, 0.25
    )
    jitter = _env_float("SENSORIUS_WATCHDOG_JITTER_SEC", WATCHDOG_JITTER_SEC, 0.0)
    # Prevent pathological "always <= 0 sleep" when jitter is larger than interval.
    if jitter >= loop_interval:
        old = jitter
        jitter = max(0.0, loop_interval * 0.8)
        printDM(
            f"[Watchdog] Jitter {old} too large for interval {loop_interval}; using {jitter}",
            location=MODULE,
            level="warning",
        )
    return timeout_sec, loop_interval, jitter

async def _force_process_exit(reason: str, code: int) -> None:
    """
    Make a best effort to stop cleanly, then HARD-exit the interpreter.
    This works on Linux, macOS, and Windows.
    """
    try:
        printDM(f"[Watchdog] EXIT({code}): {reason}", location=MODULE)
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass

    if POLITE_SHUTDOWN:
        # 1) Try to cancel other asyncio tasks (best-effort, short grace)
        try:
            loop = asyncio.get_running_loop()
            this_task = asyncio.current_task(loop=loop)
            for task in list(asyncio.all_tasks(loop)):
                if task is not this_task:
                    task.cancel()
            # Give tasks a tiny window to handle CancelledError and flush logs
            await asyncio.sleep(0.25)
        except Exception:
            pass

        # 2) On POSIX, send SIGTERM to self (lets some frameworks hook shutdown)
        try:
            if hasattr(signal, "SIGTERM"):
                os.kill(os.getpid(), signal.SIGTERM)
                await asyncio.sleep(WATCHDOG_EXIT_DELAY_SEC)
        except Exception:
            pass

        # 3) Flush stdio if we can (so logs aren’t lost)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        pass

    # 4) Guaranteed process termination (no exceptions, no cleanup hooks)
    os._exit(int(code))


async def WatchdogMonitor(supervisor, timeout: float | int = 71):
    """
    Watchdog task to monitor all supervised tasks except itself.
    Triggers a forced exit if a task hasn't fed the dogs within the timeout,
    or if a task is marked as failed via feedthedogs(..., error=True).

    EXIT CODES:
      - EXIT_CODE_TIMEOUT (70)     when a heartbeat times out
      - EXIT_CODE_FAILED_TASK (71) when a task sets an error flag
    """
    timeout_sec, base_interval, jitter = _load_runtime_config(timeout)
    warned_missing_state = False

    while True:
        now = time.monotonic()

        # Snapshot keys so dict changes during iteration won’t explode the loop
        try:
            heartbeat_map = getattr(supervisor, "time_to_feedthedogs", {})
            heartbeat_items = list(heartbeat_map.items()) if isinstance(heartbeat_map, dict) else []
        except Exception:
            heartbeat_items = []

        if not heartbeat_items and not warned_missing_state:
            printDM(
                "[Watchdog] No heartbeat entries found; waiting for supervisor registrations",
                location=MODULE,
                level="warning",
            )
            warned_missing_state = True
        elif heartbeat_items and warned_missing_state:
            warned_missing_state = False
            printDM("[Watchdog] Heartbeat monitoring active", location=MODULE)

        # 1) Explicit failures first (they should short-circuit)
        try:
            failed_tasks = getattr(supervisor, "failed_tasks", {}) or {}
            failed_items = (
                list(failed_tasks.items())
                if isinstance(failed_tasks, dict)
                else [(name, None) for name in failed_tasks]
            )
        except Exception:
            failed_items = []

        for task_name, failed_at in failed_items:
            if str(task_name).startswith("Watchdog"):
                continue
            # Immediate hard exit on explicit failure
            if isinstance(failed_at, (int, float)):
                age = max(0.0, now - float(failed_at))
                reason = f"Task '{task_name}' marked as failed ({age:.1f}s ago)"
            else:
                reason = f"Task '{task_name}' marked as failed"
            await _force_process_exit(
                reason=reason,
                code=EXIT_CODE_FAILED_TASK
            )
            # _force_process_exit() never returns

        # 2) Heartbeat timeouts
        for task_name, last_beat in heartbeat_items:
            if str(task_name).startswith("Watchdog"):
                continue

            try:
                elapsed = now - float(last_beat)
            except Exception:
                elapsed = float("inf")

            if elapsed > timeout_sec:
                await _force_process_exit(
                    reason=f"'{task_name}' not fed in {int(elapsed)}s (timeout={int(timeout_sec)}s)",
                    code=EXIT_CODE_TIMEOUT
                )
                # never returns

        # 3) Sleep with a little jitter to avoid lockstep
        sleep_for = base_interval + random.uniform(-jitter, jitter)
        try:
            await asyncio.sleep(max(1.0, sleep_for))
        except asyncio.CancelledError:
            # If the watchdog itself is being cancelled as part of shutdown, exit calmly
            try:
                printDM("[Watchdog] Cancelled - exiting task", location=MODULE)
            except Exception:
                pass
            return
        except Exception as e:
            # Don't let odd sleep errors kill the watchdog; try again shortly
            try:
                printDM(f"[Watchdog] sleep error: {e}", location=MODULE)
            except Exception:
                pass
            await asyncio.sleep(1.0)
