"""Compatibility launcher and import alias for the packaged application."""

import sys

from sensorius import app as _app


if __name__ == "__main__":
    _app.run_application()
else:
    sys.modules[__name__] = _app
