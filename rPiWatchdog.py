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
import traceback

from rPiUtils import printDM, debug_enabled

MODULE = "rPiWatchdog"
DEBUG = debug_enabled(MODULE)

# ---- user-tunable constants (top) ----
WATCHDOG_LOOP_INTERVAL_SEC = 10.0         # base interval between sweeps
WATCHDOG_JITTER_SEC = 0.8                 # +/- jitter
WATCHDOG_EXIT_DELAY_SEC = 0.75            # small grace after signaling
EXIT_CODE_TIMEOUT = 70                    # distinct exit code for timeout
EXIT_CODE_FAILED_TASK = 71                # distinct exit code for explicit failure
POLITE_SHUTDOWN = False

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


async def WatchdogMonitor(supervisor, timeout: int = 71):
    """
    Watchdog task to monitor all supervised tasks except itself.
    Triggers a forced exit if a task hasn't fed the dogs within the timeout,
    or if a task is marked as failed via feedthedogs(..., error=True).

    EXIT CODES:
      - EXIT_CODE_TIMEOUT (70)     when a heartbeat times out
      - EXIT_CODE_FAILED_TASK (71) when a task sets an error flag
    """
    jitter = WATCHDOG_JITTER_SEC
    base_interval = WATCHDOG_LOOP_INTERVAL_SEC

    while True:
        now = time.monotonic()

        # Snapshot keys so dict changes during iteration won’t explode the loop
        try:
            heartbeat_items = list(getattr(supervisor, "time_to_feedthedogs", {}).items())
        except Exception:
            heartbeat_items = []

        # 1) Explicit failures first (they should short-circuit)
        try:
            failed = set(getattr(supervisor, "failed_tasks", {}) or {})
        except Exception:
            failed = set()

        for task_name in failed:
            if str(task_name).startswith("Watchdog"):
                continue
            # Immediate hard exit on explicit failure
            await _force_process_exit(
                reason=f"Task '{task_name}' marked as failed",
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

            if elapsed > timeout:
                await _force_process_exit(
                    reason=f"'{task_name}' not fed in {int(elapsed)}s (timeout={timeout}s)",
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
                printDM("[Watchdog] Cancelled — exiting task", location=MODULE)
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
            await asyncio.sleep(0)  # yield to loop
