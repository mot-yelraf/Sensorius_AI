"""Backport multi-digit GPIO chip selection for the pinned Blinka release.

Blinka 8.58.1 truncates gpiochip15 to gpiochip5. This narrowly scoped,
idempotent repair preserves the installed file before applying the upstream
suffix-parsing correction; other releases remain untouched.
"""

from importlib import metadata
from pathlib import Path
import os
import tempfile


OLD = 'lgpio.gpiochip_open(int(dev.name[-1]))'
NEW = 'lgpio.gpiochip_open(int(dev.name[len("gpiochip"):]))'


def repair_file(path: Path) -> bool:
    """Repair the known expression once, retaining an exclusive original backup."""
    source = path.read_text(encoding="utf-8")
    if NEW in source and OLD not in source:
        return False
    if source.count(OLD) != 1:
        raise RuntimeError(f"Unrecognized Blinka GPIO code in {path}; refusing to alter it.")
    corrected = source.replace(OLD, NEW, 1)
    compile(corrected, str(path), "exec")
    backup = path.with_name(path.name + ".sensorius-gpio-backup")
    if not backup.exists():
        with backup.open("x", encoding="utf-8") as handle:
            handle.write(source)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(corrected)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main() -> None:
    """Repair only the supported pinned version in this Python environment."""
    try:
        distribution = metadata.distribution("Adafruit-Blinka")
    except metadata.PackageNotFoundError:
        return  # Dependency reconciliation installs it before retrying.
    if distribution.version != "8.58.1":
        return
    path = Path(distribution.locate_file("adafruit_blinka/microcontroller/generic_linux/lgpio_pin.py"))
    if repair_file(path):
        print(f"Applied Blinka 8.58.1 multi-digit GPIO correction: {path}")
    else:
        print("Blinka 8.58.1 multi-digit GPIO correction already present.")


if __name__ == "__main__":
    main()
