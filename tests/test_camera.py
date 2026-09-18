"""Framing for the port-6000 JPEG stream (A1 / A1 mini / P1)."""

from __future__ import annotations

import struct

import pytest

from mhs.drivers.bambu.camera import (
    JPEG_EOI,
    JPEG_SOI,
    BambuCamera,
    build_auth_packet,
    parse_frame_header,
    read_exact,
)
from mhs.errors import ConnectionFailed


def test_auth_packet_layout():
    packet = build_auth_packet("bblp", "12345678")
    assert len(packet) == 80
    size, kind, flags, reserved = struct.unpack("<IIII", packet[:16])
    assert (size, kind, flags, reserved) == (0x40, 0x3000, 0, 0)
    assert packet[16:48] == b"bblp".ljust(32, b"\0")
    assert packet[48:80] == b"12345678".ljust(32, b"\0")


def test_frame_header_parsing():
    assert parse_frame_header(struct.pack("<IIII", 1234, 0, 1, 0)) == 1234
    with pytest.raises(ConnectionFailed):
        parse_frame_header(b"short")
    with pytest.raises(ConnectionFailed):
        parse_frame_header(struct.pack("<IIII", 0, 0, 1, 0))
    with pytest.raises(ConnectionFailed):
        parse_frame_header(struct.pack("<IIII", 99_000_000, 0, 1, 0))


class FakeSocket:
    """Hands data back in small chunks, the way the printer actually does."""

    def __init__(self, payload: bytes, chunk: int = 4096) -> None:
        self.buffer = bytearray(payload)
        self.chunk = chunk
        self.sent = bytearray()
        self.closed = False

    def recv(self, size: int) -> bytes:
        take = min(size, self.chunk, len(self.buffer))
        data = bytes(self.buffer[:take])
        del self.buffer[:take]
        return data

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def settimeout(self, _timeout) -> None: ...

    def close(self) -> None:
        self.closed = True


def jpeg(size: int = 10_000, filler: bytes = b"\x00") -> bytes:
    return JPEG_SOI + filler * (size - 4) + JPEG_EOI


def frame(payload: bytes) -> bytes:
    return struct.pack("<IIII", len(payload), 0, 1, 0) + payload


def test_read_exact_reassembles_chunks():
    sock = FakeSocket(b"x" * 10_000, chunk=4096)
    assert len(read_exact(sock, 10_000)) == 10_000


def test_read_exact_detects_closed_stream():
    with pytest.raises(ConnectionFailed):
        read_exact(FakeSocket(b"short"), 100)


def test_stream_yields_whole_frames_and_authenticates(monkeypatch):
    image_a, image_b = jpeg(9_000, b"\x01"), jpeg(9_000, b"\x02")
    sock = FakeSocket(frame(image_a) + frame(image_b))
    camera = BambuCamera(host="10.0.0.5", access_code="code", serial="SER")
    monkeypatch.setattr(camera, "_connect_socket", lambda: sock)

    frames = list(camera.stream(max_frames=2))
    assert frames == [image_a, image_b]
    assert sock.sent == build_auth_packet("bblp", "code")
    assert sock.closed is True


def test_truncated_frames_are_skipped(monkeypatch):
    broken = b"\xff\xd8" + b"\x00" * 100  # no end-of-image marker
    good = jpeg(5_000)
    sock = FakeSocket(frame(broken) + frame(good))
    camera = BambuCamera(host="10.0.0.5", access_code="code")
    monkeypatch.setattr(camera, "_connect_socket", lambda: sock)
    assert camera.capture() == good
