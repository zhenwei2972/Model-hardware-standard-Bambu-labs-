"""Configuration loading.

Resolution order (first hit wins per printer):

1. ``--config path.toml`` / ``MHS_CONFIG`` TOML file
2. ``~/.config/mhs/config.toml``
3. environment variables (``BAMBU_HOST`` / ``BAMBU_ACCESS_CODE`` / ``BAMBU_SERIAL``)

Credentials are never written back to disk by this package, and the repository's
``.gitignore`` excludes ``config.toml`` so an access code cannot be committed by
accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import ConfigError

DEFAULT_CONFIG_PATHS = (
    Path(os.environ.get("MHS_CONFIG", "")) if os.environ.get("MHS_CONFIG") else None,
    Path.cwd() / "config.toml",
    Path.home() / ".config" / "mhs" / "config.toml",
)


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class PrinterConfig:
    """One entry of the ``[printers.*]`` table."""

    printer_id: str
    driver: str = "bambu"
    model: str = "A1 mini"
    host: str = ""
    serial: str = ""
    access_code: str = ""
    # "verify"  -> pin Bambu's CA and require the cert CN to equal the serial (default)
    # "ca_only" -> pin the CA but skip hostname matching (older/odd certificates)
    # "insecure"-> no verification at all; only for a trusted, isolated LAN
    tls_mode: str = "verify"
    upload_dir: str = "cache"
    camera_port: int = 6000
    mqtt_port: int = 8883
    ftps_port: int = 990
    extra: dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.driver == "mock":
            return
        missing = [k for k in ("host", "serial", "access_code") if not getattr(self, k)]
        if missing:
            raise ConfigError(
                f"printer '{self.printer_id}' is missing: {', '.join(missing)}",
                hint="Printer screen: Settings > Network shows IP + Access Code; "
                "the serial is on the sticker or in Bambu Studio > Device.",
            )
        if self.tls_mode not in {"verify", "ca_only", "insecure"}:
            raise ConfigError(f"printer '{self.printer_id}': tls_mode must be verify|ca_only|insecure")


@dataclass
class Settings:
    """Whole-process configuration."""

    printers: dict[str, PrinterConfig] = field(default_factory=dict)
    default_printer: str | None = None
    read_only: bool = False
    allow_raw_gcode: bool = False
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "share" / "mhs")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mhs.sqlite3"

    @property
    def capture_dir(self) -> Path:
        return self.state_dir / "captures"

    def get(self, printer_id: str | None = None) -> PrinterConfig:
        if not self.printers:
            raise ConfigError(
                "no printers configured",
                hint="Create config.toml (see examples/config.example.toml) or set "
                "BAMBU_HOST, BAMBU_SERIAL and BAMBU_ACCESS_CODE.",
            )
        if printer_id is None:
            if self.default_printer:
                return self.printers[self.default_printer]
            if len(self.printers) == 1:
                return next(iter(self.printers.values()))
            raise ConfigError(
                f"several printers configured ({', '.join(sorted(self.printers))}); name one"
            )
        try:
            return self.printers[printer_id]
        except KeyError:
            raise ConfigError(
                f"unknown printer '{printer_id}' (have: {', '.join(sorted(self.printers)) or 'none'})"
            ) from None


def _printer_from_table(printer_id: str, table: dict) -> PrinterConfig:
    known = {f for f in PrinterConfig.__dataclass_fields__ if f not in {"printer_id", "extra"}}
    kwargs = {k: v for k, v in table.items() if k in known}
    extra = {k: v for k, v in table.items() if k not in known}
    return PrinterConfig(printer_id=printer_id, extra=extra, **kwargs)


def load_settings(path: str | Path | None = None, env: dict | None = None) -> Settings:
    """Build :class:`Settings` from a TOML file and/or the environment."""
    env = dict(os.environ if env is None else env)
    settings = Settings()

    candidates = [Path(path)] if path else [p for p in DEFAULT_CONFIG_PATHS if p]
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as fh:
                raw = tomllib.load(fh)
            for pid, table in (raw.get("printers") or {}).items():
                settings.printers[pid] = _printer_from_table(pid, table)
            server = raw.get("server") or {}
            settings.default_printer = server.get("default_printer")
            settings.read_only = _as_bool(server.get("read_only"), False)
            settings.allow_raw_gcode = _as_bool(server.get("allow_raw_gcode"), False)
            if server.get("state_dir"):
                settings.state_dir = Path(server["state_dir"]).expanduser()
            break

    # Environment: a single printer described inline, handy for `docker run -e ...`
    if env.get("BAMBU_HOST"):
        pid = env.get("BAMBU_PRINTER_ID", "a1mini")
        settings.printers[pid] = PrinterConfig(
            printer_id=pid,
            driver=env.get("BAMBU_DRIVER", "bambu"),
            model=env.get("BAMBU_MODEL", "A1 mini"),
            host=env["BAMBU_HOST"],
            serial=env.get("BAMBU_SERIAL", ""),
            access_code=env.get("BAMBU_ACCESS_CODE", ""),
            tls_mode=env.get("BAMBU_TLS_MODE", "verify"),
            upload_dir=env.get("BAMBU_UPLOAD_DIR", "cache"),
        )
        settings.default_printer = settings.default_printer or pid

    if _as_bool(env.get("MHS_MOCK")):
        settings.printers["mock"] = PrinterConfig(printer_id="mock", driver="mock", model="MHS Mock A1 mini")
        settings.default_printer = settings.default_printer or "mock"

    if env.get("MHS_READ_ONLY") is not None:
        settings.read_only = _as_bool(env.get("MHS_READ_ONLY"))
    if env.get("MHS_ALLOW_RAW_GCODE") is not None:
        settings.allow_raw_gcode = _as_bool(env.get("MHS_ALLOW_RAW_GCODE"))
    if env.get("MHS_STATE_DIR"):
        settings.state_dir = Path(env["MHS_STATE_DIR"]).expanduser()

    if settings.default_printer and settings.default_printer not in settings.printers:
        raise ConfigError(f"default_printer '{settings.default_printer}' is not defined")
    for cfg in settings.printers.values():
        cfg.validate()
    return settings
