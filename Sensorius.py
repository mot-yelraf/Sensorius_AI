"""Launch Sensorius or expose the packaged application through a legacy import.

Executing this module delegates to :func:`sensorius.app.run_application`;
importing it aliases the module object to :mod:`sensorius.app` for compatibility.
"""

import sys

if __name__ == "__main__" and sys.argv[1:] == ["--acurite-hid-worker"]:
    from sensorius.weather_adapters.acurite_hid import main
    main()
    raise SystemExit(0)

from sensorius import app as _app


if __name__ == "__main__":
    _app.run_application()
else:
    sys.modules[__name__] = _app
