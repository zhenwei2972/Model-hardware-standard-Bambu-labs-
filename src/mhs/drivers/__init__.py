"""Driver registry.

A driver is any :class:`mhs.device.Printer` subclass registered under a name that
can appear as ``driver = "..."`` in config.toml.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import PrinterConfig
from ..device import Printer
from ..errors import ConfigError

_REGISTRY: dict[str, Callable[[PrinterConfig], Printer]] = {}


def register(name: str, factory: Callable[[PrinterConfig], Printer]) -> None:
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def build(config: PrinterConfig) -> Printer:
    """Instantiate the driver named by ``config.driver``."""
    try:
        factory = _REGISTRY[config.driver]
    except KeyError:
        raise ConfigError(
            f"unknown driver '{config.driver}' (available: {', '.join(available())})"
        ) from None
    return factory(config)


def _install_builtin_drivers() -> None:
    from .bambu.printer import BambuPrinter
    from .mock import MockPrinter

    register("bambu", BambuPrinter)
    register("mock", MockPrinter)


_install_builtin_drivers()
