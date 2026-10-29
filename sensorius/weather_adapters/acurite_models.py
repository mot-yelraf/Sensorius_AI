"""Configuration profiles for AcuRite's shared PC Connect live USB protocol.

Profiles describe user-selected hardware and mode instructions. They do not
identify a console from its shared USB ID or override report calibration data.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsoleProfile:
    """A console family and the live USB mode its operator must select."""

    key: str
    label: str
    aliases: tuple[str, ...]
    requires_mode4_selection: bool

    @property
    def mode_help(self) -> str:
        """Explain the console-side setting without implying software can change it."""
        if self.requires_mode4_selection:
            return (
                "Select USB MODE or PC CONNECT MODE 4 on the console before enabling. "
                "Recheck this setting after a console power loss. Sensorius does not change the console mode."
            )
        return "The 01536-RX / 06056 uses USB mode 4 automatically; no console mode selection is needed."


CONSOLE_PROFILES = (
    ConsoleProfile("01536", "01536-RX / 06056", ("01536", "01536-RX", "06056"), False),
    ConsoleProfile("01025", "01025", ("01025",), True),
    ConsoleProfile("01035", "01035", ("01035",), True),
    ConsoleProfile("01036", "01036 / 01092 / 06006", ("01036", "01092", "06006"), True),
    ConsoleProfile("01525", "01525", ("01525",), True),
    ConsoleProfile(
        "02032",
        "02032 / 02033 / 01093 / 06037",
        ("02032", "02032C", "02033", "01093", "06037"),
        True,
    ),
)


def console_profile(value: str) -> ConsoleProfile:
    """Validate a model/alias and return its canonical family profile."""
    if isinstance(value, str):
        model = value.strip().upper()
        for profile in CONSOLE_PROFILES:
            if model in profile.aliases:
                return profile
    raise ValueError("Unsupported AcuRite USB console family")
