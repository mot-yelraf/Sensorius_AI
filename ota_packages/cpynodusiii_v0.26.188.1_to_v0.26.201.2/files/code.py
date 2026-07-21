"""Run the cPyNodus_III application on CircuitPython boards.

On host Python, this module defers to the standard-library ``code`` module so
test tooling and local scripts behave normally. On CircuitPython, it starts the
main async application, records fatal tracebacks when possible, and requests a
soft reload after an unhandled runtime failure.
"""

import sys

if getattr(sys.implementation, "name", "") != "circuitpython":
    import os

    _stdlib_code = os.path.join(os.path.dirname(os.__file__), "code.py")
    with open(_stdlib_code, "r", encoding="utf-8") as _handle:
        exec(compile(_handle.read(), _stdlib_code, "exec"), globals(), globals())
else:
    import asyncio
    import sys
    import time

    from cpynodus_iii.core.autoreload import disable_auto_reload
    from cpynodus_iii.core.reboot_log import append_reboot_traceback

    def _stamp():
        try:
            now = time.localtime()
        except Exception:
            now = None
        if now is not None:
            try:
                if int(now[0]) >= 2023:
                    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                        int(now[0]),
                        int(now[1]),
                        int(now[2]),
                        int(now[3]),
                        int(now[4]),
                        int(now[5]),
                    )
            except Exception:
                pass
        try:
            elapsed = max(0, int(time.monotonic()))
        except Exception:
            elapsed = 0
        return "{}s".format(elapsed)

    disable_auto_reload()

    def _arm_ota_boot_health():
        from cpynodus_iii.ota.state import (
            FwUpdateState,
            load_ota_state,
            save_ota_state,
        )

        state_path = "./_ota/state.json"
        state = load_ota_state(state_path)
        phase = str(getattr(state, "phase", "") or "")
        if phase == "applied_pending_boot":
            save_ota_state(
                FwUpdateState(
                    prior_profile=getattr(state, "prior_profile", "") or "",
                    package_id=getattr(state, "package_id", "") or "",
                    phase="boot_pending",
                ),
                state_path,
            )
            return True
        if phase == "boot_pending":
            from cpynodus_iii.ota.http import recover_interrupted_ota_apply

            error = recover_interrupted_ota_apply(".")
            if error:
                raise RuntimeError("ota_boot_rollback_failed:{}".format(error))
            print("{} ota phase=boot_rollback status=restored".format(_stamp()))
        return False

    try:
        ota_first_boot_armed = _arm_ota_boot_health()
        from cpynodus_iii.app import main

        asyncio.run(main(ota_first_boot_armed=ota_first_boot_armed))
    except Exception as exc:
        append_reboot_traceback(exc)
        print_exception = getattr(sys, "print_exception", None)
        if callable(print_exception):
            print_exception(exc)
        else:
            raise
        try:
            import supervisor

            reload_runtime = getattr(supervisor, "reload", None)
            if callable(reload_runtime):
                print(
                    "{} runtime action=reload reason=unhandled_exception:{}".format(
                        _stamp(),
                        type(exc).__name__,
                    )
                )
                reload_runtime()
        except Exception:
            pass
        raise
