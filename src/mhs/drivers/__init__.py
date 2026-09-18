"""Driver registry.

A driver is any :class:`mhs.device.Printer` subclass registered under a name that
can appear as ``driver = "..."`` in config.toml.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from ..config import DeviceConfig
from ..device import Device
from ..errors import ConfigError

_REGISTRY: dict[str, Callable[..., Device]] = {}


def register(name: str, factory: Callable[..., Device]) -> None:
    _REGISTRY[name] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)


def build(config: DeviceConfig, state_dir=None) -> Device:
    """Instantiate the driver named by ``config.driver``.

    ``state_dir`` is offered to drivers that need somewhere to keep a token or
    cache; drivers that do not take it are called with the config alone, so a
    driver never has to accept an argument it has no use for.
    """
    try:
        factory = _REGISTRY[config.driver]
    except KeyError:
        raise ConfigError(
            f"unknown driver '{config.driver}' (available: {', '.join(available())})"
        ) from None
    if state_dir is not None and "state_dir" in inspect.signature(factory).parameters:
        return factory(config, state_dir=state_dir)
    return factory(config)


def _install_builtin_drivers() -> None:
    from .bambu.printer import BambuPrinter
    from .mock import MockPrinter
    from .mock_vacuum import MockVacuum
    from .roborock.vacuum import RoborockVacuum

    register("bambu", BambuPrinter)
    register("mock", MockPrinter)
    register("mock_vacuum", MockVacuum)
    register("roborock", RoborockVacuum)


_install_builtin_drivers()
