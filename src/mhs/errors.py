"""Exception hierarchy shared by every driver.

Drivers translate vendor-specific failures into these so the MCP layer can report
something actionable to the model instead of leaking a paho/ftplib traceback.
"""

from __future__ import annotations


class MHSError(Exception):
    """Base class for all errors raised by this package."""

    hint: str | None = None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint

    def to_dict(self) -> dict[str, str]:
        out = {"error": type(self).__name__, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


class ConfigError(MHSError):
    """Printer configuration is missing or malformed."""


class ConnectionFailed(MHSError):
    """Could not reach the device, or authentication was rejected."""


class NotSupported(MHSError):
    """The device (or its current firmware) does not expose this capability."""


class DeviceBusy(MHSError):
    """The device is in a state where the requested action is not allowed."""


class CommandRejected(MHSError):
    """The device accepted the connection but refused the command."""


class ControlDisabled(MHSError):
    """A write was attempted while the server is running read-only.

    Also raised when Bambu's Authorization Control System blocks LAN control
    because Developer Mode is off.
    """

    hint = (
        "Enable LAN Only Mode + Developer Mode on the printer screen "
        "(Settings > General), or unset MHS_READ_ONLY."
    )


class FileTransferError(MHSError):
    """Upload/download/listing over the device's file interface failed."""


class TimeoutExceeded(MHSError):
    """The device did not reach the expected state in time."""
