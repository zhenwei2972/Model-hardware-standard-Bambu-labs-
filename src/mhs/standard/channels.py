"""Named read/write channels, with their limits attached.

A channel is one thing about the device that can be observed or set:
``nozzle.temperature``, ``job.state``, ``light.chamber``. Collapsing the device
onto this vocabulary is what lets an agent operate hardware it has never seen -
it enumerates the channels and their declared bounds instead of learning a
per-vendor API.

Limits live here, next to the channel, and are checked before anything reaches
the hardware.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import MHSError


class SafetyViolation(MHSError):
    """A write was rejected because it falls outside the channel's declared limits."""


class Access(str, Enum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"

    @property
    def readable(self) -> bool:
        return self in (Access.READ, Access.READ_WRITE)

    @property
    def writable(self) -> bool:
        return self in (Access.WRITE, Access.READ_WRITE)


@dataclass(frozen=True)
class SafetyLimit:
    """The bounds a write must satisfy, and why they exist.

    ``rationale`` is part of the contract, not decoration: an agent that is told
    *why* a limit exists can reason about alternatives instead of retrying.
    """

    minimum: float | None = None
    maximum: float | None = None
    allowed: tuple[str, ...] | None = None
    requires_confirmation: bool = False
    rationale: str | None = None

    def check(self, channel: str, value: Any) -> None:
        """Raise :class:`SafetyViolation` if ``value`` is out of bounds."""
        if self.allowed is not None:
            if str(value) not in self.allowed:
                raise SafetyViolation(
                    f"{channel}: {value!r} is not allowed "
                    f"(expected one of {', '.join(self.allowed)})",
                    hint=self.rationale,
                )
            return
        if self.minimum is not None or self.maximum is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                raise SafetyViolation(f"{channel}: expected a number, got {value!r}") from None
            if self.minimum is not None and numeric < self.minimum:
                raise SafetyViolation(
                    f"{channel}: {numeric:g} is below the safe minimum {self.minimum:g}",
                    hint=self.rationale,
                )
            if self.maximum is not None and numeric > self.maximum:
                raise SafetyViolation(
                    f"{channel}: {numeric:g} exceeds the safe maximum {self.maximum:g}",
                    hint=self.rationale,
                )

    def to_dict(self) -> dict:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "allowed": list(self.allowed) if self.allowed else None,
            "requires_confirmation": self.requires_confirmation,
            "rationale": self.rationale,
        }


@dataclass
class Channel:
    """One observable or settable property of a device."""

    name: str
    access: Access
    description: str
    value_type: str = "number"  # number | string | boolean | object | binary
    unit: str | None = None
    limit: SafetyLimit | None = None
    tags: tuple[str, ...] = ()
    reader: Callable[[], Awaitable[Any]] | None = None
    writer: Callable[[Any], Awaitable[Any]] | None = None
    #: Set when the underlying hardware cannot currently serve this channel.
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.access.readable and self.reader is None and self.unavailable_reason is None:
            raise ValueError(f"channel {self.name!r} is readable but has no reader")
        if self.access.writable and self.writer is None and self.unavailable_reason is None:
            raise ValueError(f"channel {self.name!r} is writable but has no writer")

    def validate(self, value: Any) -> None:
        if self.limit is not None:
            self.limit.check(self.name, value)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "access": self.access.value,
            "description": self.description,
            "value_type": self.value_type,
            "unit": self.unit,
            "tags": list(self.tags),
            "limit": self.limit.to_dict() if self.limit else None,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class ChannelTable:
    """The channels one device exposes, keyed by name."""

    channels: dict[str, Channel] = field(default_factory=dict)

    def add(self, channel: Channel) -> Channel:
        self.channels[channel.name] = channel
        return channel

    def get(self, name: str) -> Channel:
        try:
            return self.channels[name]
        except KeyError:
            near = [c for c in self.channels if name.lower() in c.lower()]
            raise SafetyViolation(
                f"unknown channel {name!r}",
                hint=("Did you mean: " + ", ".join(sorted(near)) + "?") if near else
                     "Call list_channels to see what this device exposes.",
            ) from None

    def names(self) -> list[str]:
        return sorted(self.channels)

    def to_list(self) -> list[dict]:
        return [self.channels[name].to_dict() for name in self.names()]
