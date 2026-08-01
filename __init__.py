"""Compatibility export for repository-level tooling."""

try:
    from .sensorius import __version__
except ImportError:
    from sensorius import __version__
