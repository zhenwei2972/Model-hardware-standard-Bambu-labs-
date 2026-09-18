"""Implicit-FTPS file access (port 990).

The printer's storage is where sliced ``.3mf`` projects have to live before
``print.project_file`` can start them. Two quirks are handled here: TLS is
*implicit* (wrapped before the greeting, not via ``AUTH TLS``), and the data
channel must resume the control channel's TLS session.
"""

from __future__ import annotations

import contextlib
import ftplib
import logging
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

from ...errors import FileTransferError
from ...models import FileEntry
from .tls import build_ssl_context

log = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._ +()\[\]-]{1,120}$")
PRINTABLE_SUFFIXES = (".3mf", ".gcode")


def sanitize_remote_name(name: str) -> str:
    """Reject anything that could escape the upload directory."""
    candidate = Path(name).name
    if candidate in {"", ".", ".."} or not _SAFE_NAME.match(candidate):
        raise FileTransferError(
            f"unsafe remote filename {name!r}",
            hint="Use letters, digits, spaces and . _ - + ( ) [ ] only.",
        )
    return candidate


class _ImplicitFTPTLS(ftplib.FTP_TLS):
    """FTP_TLS variant that negotiates TLS on connect instead of after AUTH."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sock: ssl.SSLSocket | None = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):  # type: ignore[override]
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = getattr(self.sock, "session", None)
            try:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
            except (ssl.SSLError, ValueError):
                # Some firmware builds do not require session reuse.
                conn = self.context.wrap_socket(conn, server_hostname=self.host)
        return conn, size


@dataclass
class BambuFiles:
    """Blocking FTPS client. Call it from a worker thread."""

    host: str
    access_code: str
    serial: str = ""
    port: int = 990
    tls_mode: str = "verify"
    timeout: float = 30.0

    def _connect(self) -> _ImplicitFTPTLS:
        ftp = _ImplicitFTPTLS()
        ftp.context = build_ssl_context(self.tls_mode, self.serial)
        try:
            ftp.connect(host=self.host, port=self.port, timeout=self.timeout)
            ftp.login(user="bblp", passwd=self.access_code)
            ftp.prot_p()
            ftp.set_pasv(True)
        except (ftplib.all_errors, OSError) as exc:  # type: ignore[misc]
            raise FileTransferError(
                f"FTPS connect to {self.host}:{self.port} failed - {exc}",
                hint="Access Code wrong, or LAN Only Mode / Developer Mode is off.",
            ) from exc
        return ftp

    # -- operations --------------------------------------------------------
    def list_files(self, directory: str = "") -> list[FileEntry]:
        directory = directory.strip("/")
        ftp = self._connect()
        try:
            entries: list[FileEntry] = []
            path = f"/{directory}" if directory else "/"
            try:
                for name, facts in ftp.mlsd(path):
                    if name in {".", ".."}:
                        continue
                    entries.append(
                        FileEntry(
                            name=name,
                            path=f"{directory}/{name}" if directory else name,
                            size_bytes=int(facts["size"]) if facts.get("size", "").isdigit() else None,
                            modified=facts.get("modify"),
                            is_dir=facts.get("type") == "dir",
                        )
                    )
            except (ftplib.error_perm, ftplib.error_proto):
                # MLSD is optional; fall back to a plain LIST.
                lines: list[str] = []
                ftp.retrlines(f"LIST {path}", lines.append)
                entries.extend(_parse_list_line(line, directory) for line in lines if line.strip())
            return [e for e in entries if e is not None]
        finally:
            _quietly_close(ftp)

    def upload(self, local_path: str | Path, remote_path: str) -> FileEntry:
        source = Path(local_path).expanduser()
        if not source.is_file():
            raise FileTransferError(f"{source} does not exist")
        if source.suffix.lower() not in PRINTABLE_SUFFIXES:
            raise FileTransferError(
                f"{source.name}: expected a sliced {' or '.join(PRINTABLE_SUFFIXES)} file",
                hint="Slice the model in Bambu Studio/OrcaSlicer and export the plate first.",
            )
        remote_path = remote_path.strip("/")
        directory, _, name = remote_path.rpartition("/")
        name = sanitize_remote_name(name)

        ftp = self._connect()
        try:
            if directory:
                _ensure_dir(ftp, directory)
            with source.open("rb") as fh:
                ftp.storbinary(f"STOR /{directory}/{name}" if directory else f"STOR /{name}", fh)
        except (ftplib.all_errors, OSError) as exc:  # type: ignore[misc]
            raise FileTransferError(f"upload of {source.name} failed - {exc}") from exc
        finally:
            _quietly_close(ftp)
        return FileEntry(
            name=name,
            path=f"{directory}/{name}" if directory else name,
            size_bytes=source.stat().st_size,
        )

    def delete(self, remote_path: str) -> None:
        remote_path = remote_path.strip("/")
        ftp = self._connect()
        try:
            ftp.delete(f"/{remote_path}")
        except (ftplib.all_errors, OSError) as exc:  # type: ignore[misc]
            raise FileTransferError(f"delete of {remote_path} failed - {exc}") from exc
        finally:
            _quietly_close(ftp)


def _ensure_dir(ftp: ftplib.FTP_TLS, directory: str) -> None:
    built = ""
    for part in directory.strip("/").split("/"):
        built = f"{built}/{part}"
        # Already present, or the firmware forbids mkdir outside /cache.
        with contextlib.suppress(ftplib.error_perm):
            ftp.mkd(built)


def _quietly_close(ftp: ftplib.FTP_TLS) -> None:
    try:
        ftp.quit()
    except Exception:
        with contextlib.suppress(Exception):  # pragma: no cover
            ftp.close()


def _parse_list_line(line: str, directory: str) -> FileEntry | None:
    """Parse a unix-style ``LIST`` line, which is what the printer emits."""
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    perms, size, name = parts[0], parts[4], parts[8]
    if name in {".", ".."}:
        return None
    return FileEntry(
        name=name,
        path=f"{directory}/{name}" if directory else name,
        size_bytes=int(size) if size.isdigit() else None,
        modified=" ".join(parts[5:8]),
        is_dir=perms.startswith("d"),
    )
