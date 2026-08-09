"""Static data publisher for the Orbit application."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("orbit-data")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

__all__ = ["__version__"]
