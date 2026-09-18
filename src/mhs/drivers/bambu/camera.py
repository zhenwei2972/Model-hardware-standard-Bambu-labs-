"""Chamber camera client for the A1/A1 mini/P1 series (TCP 6000).

Those printers do not run RTSP: they expose a bespoke TLS socket that streams
1280x720 JPEG frames at roughly 1 fps after an 80-byte login packet.
(The X1/H2 series serve ``rtsps://<ip>:322/streaming/live/1`` instead.)

Frames are plain JPEG, which is exactly what a vision model wants - so this is
the piece that makes "look at the print and tell me what is wrong" possible.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from ...errors import ConnectionFailed, TimeoutExceeded
from .tls import build_ssl_context

log = logging.getLogger(__name__)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
_AUTH_PAYLOAD_SIZE = 0x40
_AUTH_TYPE = 0x3000
_HEADER_SIZE = 16
_MAX_FRAME_BYTES = 8 * 1024 * 1024


def build_auth_packet(username: str, access_code: str) -> bytes:
    """80-byte login packet: 4x uint32 little-endian header, then 2x 32-byte creds."""
    return struct.pack(
        "<IIII32s32s",
        _AUTH_PAYLOAD_SIZE,
        _AUTH_TYPE,
        0,
        0,
        username.encode("ascii"),
        access_code.encode("ascii"),
    )


def parse_frame_header(header: bytes) -> int:
    """Return the JPEG payload size announced by a 16-byte frame header."""
    if len(header) != _HEADER_SIZE:
        raise ConnectionFailed(f"short camera header ({len(header)} bytes)")
    payload_size, _itrack, _flags, _reserved = struct.unpack("<IIII", header)
    if not 0 < payload_size <= _MAX_FRAME_BYTES:
        raise ConnectionFailed(f"implausible camera frame size {payload_size}")
    return payload_size


def read_exact(sock, count: int) -> bytes:
    """Read exactly ``count`` bytes; the stream arrives in ~4 KiB chunks."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise ConnectionFailed("camera closed the connection mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass
class BambuCamera:
    """Blocking camera client. Call it from a worker thread."""

    host: str
    access_code: str
    serial: str = ""
    port: int = 6000
    tls_mode: str = "verify"
    timeout: float = 10.0

    def _connect_socket(self):
        """Open the TLS socket. Split out so tests can supply a fake."""
        context = build_ssl_context(self.tls_mode, self.serial)
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock = context.wrap_socket(raw, server_hostname=self.host)
        sock.settimeout(self.timeout)
        return sock

    def _open(self):
        try:
            sock = self._connect_socket()
            sock.sendall(build_auth_packet("bblp", self.access_code))
            return sock
        except OSError as exc:
            raise ConnectionFailed(
                f"camera connect to {self.host}:{self.port} failed - {exc}",
                hint="Turn on LAN Mode Liveview on the printer screen "
                "(Settings > General) and check the Access Code.",
            ) from exc

    def stream(self, max_frames: int | None = None) -> Iterator[bytes]:
        """Yield JPEG frames until ``max_frames`` or the caller stops iterating."""
        sock = self._open()
        try:
            produced = 0
            while max_frames is None or produced < max_frames:
                try:
                    header = read_exact(sock, _HEADER_SIZE)
                    frame = read_exact(sock, parse_frame_header(header))
                except TimeoutError as exc:
                    raise TimeoutExceeded(
                        f"no camera frame from {self.host} within {self.timeout:.0f}s"
                    ) from exc
                if not (frame.startswith(JPEG_SOI) and frame.endswith(JPEG_EOI)):
                    log.debug("discarding truncated camera frame (%d bytes)", len(frame))
                    continue
                produced += 1
                yield frame
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    def capture(self) -> bytes:
        """Grab a single JPEG frame."""
        for frame in self.stream(max_frames=1):
            return frame
        raise ConnectionFailed(f"camera on {self.host} produced no frames")
