"""Launch Sensorius or expose the packaged application through a legacy import.

Executing this module delegates to :func:`sensorius.app.run_application`;
importing it aliases the module object to :mod:`sensorius.app` for compatibility.
"""

import sys

from sensorius import app as _app


if __name__ == "__main__":
    _app.run_application()
else:
    sys.modules[__name__] = _app
