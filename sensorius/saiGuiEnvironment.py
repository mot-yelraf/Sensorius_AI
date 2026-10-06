"""Configure native GUI rendering before WebKit initializes.

Pi/Trixie uses software painting and avoids DMA-BUF buffers because newer
WebKitGTK/Mesa combinations can corrupt the desktop surface. Explicit
operator environment values remain authoritative.
"""

import os
from pathlib import Path
import platform
import sys


def configure_gui_environment() -> None:
    """Apply conservative Linux rendering defaults before importing pywebview."""
    if not sys.platform.startswith("linux"):
        return
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    os.environ.setdefault("GDK_BACKEND", "wayland,x11")
    try:
        model = Path("/proc/device-tree/model").read_text()
        release = platform.freedesktop_os_release()
    except (OSError, UnicodeError):
        return
    if "Raspberry Pi" in model and release.get("VERSION_CODENAME") == "trixie":
        os.environ.setdefault("WEBKIT_SKIA_ENABLE_CPU_RENDERING", "1")
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
