import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from saiAutomationManager import AutomationManager, enable_trigger as automation_enable_trigger
from saiAutomationManager import remove_trigger as automation_remove_trigger
from saiAutomationManager import save_automations
from saiSwitchTriggerManager import SwitchTriggerManager, enable_trigger as switch_enable_trigger
from saiSwitchTriggerManager import remove_trigger as switch_remove_trigger
from saiSwitchTriggerManager import save_triggers


@dataclass(frozen=True)
class ManagerAdapter:
    name: str
    manager_cls: type
    filename: str
    save_fn: Callable[[Any, str, dict], None]
    enable_fn: Callable[[Any, str, str, str, bool], bool]
    remove_fn: Callable[[Any, str, str, str], bool]


def adapters() -> list[ManagerAdapter]:
    return [
        ManagerAdapter(
            name="automation_manager",
            manager_cls=AutomationManager,
            filename="automations.toml",
            save_fn=save_automations,
            enable_fn=automation_enable_trigger,
            remove_fn=automation_remove_trigger,
        ),
        ManagerAdapter(
            name="switch_trigger_manager",
            manager_cls=SwitchTriggerManager,
            filename="automations.toml",
            save_fn=save_triggers,
            enable_fn=switch_enable_trigger,
            remove_fn=switch_remove_trigger,
        ),
    ]


def make_manager(adapter: ManagerAdapter, tmp_path: Path):
    return adapter.manager_cls(base_dir=str(tmp_path))
