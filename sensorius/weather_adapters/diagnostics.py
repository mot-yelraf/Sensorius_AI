"""Report weather acquisition failures through Sensorius logging.

This bridge preserves concise transport diagnostics when porting vendor readers.
"""

from ..saiUtils import printDM


def report_error(message, *args, **kwargs):
    """Log a transport failure without writing protocol data to standard output."""
    printDM(message % args if args else message, location="weather_adapters", level="warning")
