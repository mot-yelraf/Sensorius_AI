"""Provide compatibility exports for repository-level tooling.

The repository root forwards package metadata so callers receive the same
version whether they import the root module or the installed package.
"""

try:
    from .sensorius import __version__
except ImportError:
    from sensorius import __version__
