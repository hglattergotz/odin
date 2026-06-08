"""Odin — headless Claude Code task orchestrator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("odin")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0+unknown"
