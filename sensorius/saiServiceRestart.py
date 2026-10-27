"""Identify and restart the service that owns this Sensorius process.

Only enabled automatic services whose reported process matches this instance
are eligible. Probes never change service configuration or elevate privileges.
"""

from dataclasses import dataclass
import os
import platform
import plistlib
from pathlib import Path
import re
import subprocess
import sys


@dataclass(frozen=True)
class RestartService:
    """Describe a verified automatic service and its restart command."""

    name: str
    command: tuple[str, ...]
    detached: bool = False


def _output(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=8)
    return result.stdout.strip() if result.returncode == 0 else ""


def find_restart_service() -> RestartService | None:
    """Find an enabled automatic service that owns the current process."""
    system = platform.system()
    pid = os.getpid()
    try:
        if system == "Linux":
            for scope in ([], ["--user"]):
                base = ["systemctl", *scope]
                output = _output([*base, "show", "sensorius.service", "--property=MainPID,ActiveState,UnitFileState"])
                fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
                if (fields.get("MainPID") == str(pid) and fields.get("ActiveState") == "active"
                        and fields.get("UnitFileState") == "enabled"):
                    return RestartService(
                        "Sensorius systemd " + ("user service" if scope else "system service"),
                        (*base, "--no-ask-password", "--no-block", "restart", "sensorius.service"),
                    )
        elif system == "Darwin":
            for domain, directory in (
                (f"gui/{os.getuid()}", Path.home() / "Library/LaunchAgents"),
                ("system", Path("/Library/LaunchDaemons")),
            ):
                disabled = _output(["launchctl", "print-disabled", domain])
                for label in ("com.sensorius.app", "com.sensorius.sensorius"):
                    path = directory / f"{label}.plist"
                    if not path.is_file():
                        continue
                    with path.open("rb") as source:
                        config = plistlib.load(source)
                    if not (config.get("RunAtLoad") or config.get("KeepAlive")):
                        continue
                    if not disabled or re.search(r'"' + re.escape(label) + r'"\s*=>\s*true', disabled):
                        continue
                    target = f"{domain}/{label}"
                    output = _output(["launchctl", "print", target])
                    if re.search(r"^\s*pid = " + str(pid) + r"\s*$", output, re.MULTILINE):
                        return RestartService("Sensorius launchd service", ("launchctl", "kickstart", "-k", target))
        elif system == "Windows":
            # Match a known boot/login task to this process's task-engine ancestry.
            executable = sys.executable.replace("'", "''")
            launcher = str(Path(__file__).resolve().parents[1] / "Sensorius.py").replace("'", "''")
            script = f"""
$ErrorActionPreference = 'Stop'
$ancestors = @({pid})
$current = {pid}
for ($i = 0; $i -lt 12 -and $current -gt 0; $i++) {{
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$current"
    $current = [int]$p.ParentProcessId
    $ancestors += $current
}}
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$root = $scheduler.GetFolder('\\')
foreach ($task in $scheduler.GetRunningTasks(0)) {{
    if ($task.Name -notin @('SensoriusAutoStart', 'SensoriusStartup', 'SensoriusStartupUser')) {{ continue }}
    $engine = [int]$task.EnginePID
    if ($engine -le 0 -or $engine -notin $ancestors) {{ continue }}
    $registered = $root.GetTask($task.Name)
    $automatic = @($registered.Definition.Triggers | Where-Object {{ $_.Enabled -and $_.Type -in @(8, 9) }})
    $actions = @($registered.Definition.Actions | Where-Object {{ $_.Path -eq '{executable}' -and $_.Arguments.IndexOf('{launcher}', [StringComparison]::OrdinalIgnoreCase) -ge 0 }})
    if ($registered.Enabled -and $automatic.Count -gt 0 -and $actions.Count -gt 0) {{ $task.Name; break }}
}}
"""
            task = _output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
            if task in {"SensoriusAutoStart", "SensoriusStartup", "SensoriusStartupUser"}:
                restart = f"""
$ErrorActionPreference = 'Stop'
Start-Sleep -Seconds 1
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$task = $scheduler.GetFolder('\\').GetTask('{task}')
$task.Stop(0)
for ($i = 0; $i -lt 30 -and $task.State -eq 4; $i++) {{ Start-Sleep -Milliseconds 200 }}
if ($task.State -eq 4) {{ throw 'Sensorius task did not stop' }}
$task.Run($null) | Out-Null
"""
                return RestartService("Sensorius scheduled task", (
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", restart,
                ), detached=True)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        return None
    return None


def restart_service(service: RestartService) -> None:
    """Submit a restart request to the owning service manager."""
    if service.detached:
        # Break out of the task's job so stopping it cannot kill its restarter.
        subprocess.Popen(
            service.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_BREAKAWAY_FROM_JOB,
        )
        return
    result = subprocess.run(service.command, capture_output=True, text=True, timeout=10)
    if result.returncode and service.command[:2] == ("systemctl", "--no-ask-password"):
        # Existing passwordless service-management grants may be available.
        result = subprocess.run(("sudo", "-n", *service.command), capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Service manager rejected the restart")
