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


def default_config_paths(env: dict | None = None) -> list[Path]:
    """Where to look for config.toml, in order.

    Computed per call rather than at import: ``MHS_CONFIG`` and the working
    directory can both change after this module is loaded, and a constant
    captured at import silently ignores either.
    """
    env = dict(os.environ if env is None else env)
    candidates = []
    if env.get("MHS_CONFIG"):
        candidates.append(Path(env["MHS_CONFIG"]).expanduser())
    candidates.append(Path.cwd() / "config.toml")
    candidates.append(Path.home() / ".config" / "mhs" / "config.toml")
    return candidates


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DeviceConfig:
    """One entry of the ``[devices.*]`` (or legacy ``[printers.*]``) table."""

    device_id: str
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

    #: Drivers whose whole configuration lives in `extra`, validated by the
    #: driver itself rather than here.
    SELF_VALIDATING = frozenset({"mock", "mock_vacuum", "roborock"})

    def validate(self) -> None:
        if self.driver in self.SELF_VALIDATING:
            return
        missing = [k for k in ("host", "serial", "access_code") if not getattr(self, k)]
        if missing:
            raise ConfigError(
                f"printer '{self.device_id}' is missing: {', '.join(missing)}",
                hint="Printer screen: Settings > Network shows IP + Access Code; "
                "the serial is on the sticker or in Bambu Studio > Device.",
            )
        if self.tls_mode not in {"verify", "ca_only", "insecure"}:
            raise ConfigError(f"printer '{self.device_id}': tls_mode must be verify|ca_only|insecure")


@dataclass
class Settings:
    """Whole-process configuration."""

    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    default_device: str | None = None
    read_only: bool = False
    allow_raw_gcode: bool = False
    #: Slicer binary to use; None searches the PATH for a known one.
    slicer_binary: str | None = None
    #: Profile files handed to the slicer before any per-slice override.
    slicer_profiles: tuple[str, ...] = ()
    state_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "share" / "mhs")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "mhs.sqlite3"

    @property
    def capture_dir(self) -> Path:
        return self.state_dir / "captures"

    def get(self, device_id: str | None = None) -> DeviceConfig:
        if not self.devices:
            raise ConfigError(
                "no devices configured",
                hint="Create config.toml (see examples/config.example.toml) or set "
                "BAMBU_HOST, BAMBU_SERIAL and BAMBU_ACCESS_CODE.",
            )
        if device_id is None:
            if self.default_device:
                return self.devices[self.default_device]
            if len(self.devices) == 1:
                return next(iter(self.devices.values()))
            raise ConfigError(
                f"several devices configured ({', '.join(sorted(self.devices))}); name one"
            )
        try:
            return self.devices[device_id]
        except KeyError:
            raise ConfigError(
                f"unknown device '{device_id}' (have: {', '.join(sorted(self.devices)) or 'none'})"
            ) from None


def _device_from_table(device_id: str, table: dict) -> DeviceConfig:
    known = {f for f in DeviceConfig.__dataclass_fields__ if f not in {"device_id", "extra"}}
    kwargs = {k: v for k, v in table.items() if k in known}
    extra = {k: v for k, v in table.items() if k not in known}
    return DeviceConfig(device_id=device_id, extra=extra, **kwargs)


def load_settings(path: str | Path | None = None, env: dict | None = None) -> Settings:
    """Build :class:`Settings` from a TOML file and/or the environment."""
    env = dict(os.environ if env is None else env)
    settings = Settings()

    candidates = [Path(path).expanduser()] if path else default_config_paths(env)
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as fh:
                raw = tomllib.load(fh)
            # `[devices.*]` is the current spelling; `[printers.*]` still works.
            for section in ("devices", "printers"):
                for pid, table in (raw.get(section) or {}).items():
                    settings.devices[pid] = _device_from_table(pid, table)
            server = raw.get("server") or {}
            settings.default_device = server.get("default_device") or server.get("default_printer")
            settings.read_only = _as_bool(server.get("read_only"), False)
            settings.allow_raw_gcode = _as_bool(server.get("allow_raw_gcode"), False)
            if server.get("state_dir"):
                settings.state_dir = Path(server["state_dir"]).expanduser()

            slicer = raw.get("slicer") or {}
            settings.slicer_binary = slicer.get("binary")
            profiles = slicer.get("profiles") or []
            settings.slicer_profiles = tuple(str(p) for p in profiles)
            break

    # Environment: a single printer described inline, handy for `docker run -e ...`
    if env.get("BAMBU_HOST"):
        pid = env.get("BAMBU_PRINTER_ID", "a1mini")
        settings.devices[pid] = DeviceConfig(
            device_id=pid,
            driver=env.get("BAMBU_DRIVER", "bambu"),
            model=env.get("BAMBU_MODEL", "A1 mini"),
            host=env["BAMBU_HOST"],
            serial=env.get("BAMBU_SERIAL", ""),
            access_code=env.get("BAMBU_ACCESS_CODE", ""),
            tls_mode=env.get("BAMBU_TLS_MODE", "verify"),
            upload_dir=env.get("BAMBU_UPLOAD_DIR", "cache"),
        )
        settings.default_device = settings.default_device or pid

    # Simulated devices, so every tool can be tried before touching hardware.
    if _as_bool(env.get("MHS_MOCK")):
        settings.devices["mock"] = DeviceConfig(
            device_id="mock", driver="mock", model="MHS Mock A1 mini"
        )
        settings.default_device = settings.default_device or "mock"
    if _as_bool(env.get("MHS_MOCK_VACUUM")):
        settings.devices["mock-vacuum"] = DeviceConfig(
            device_id="mock-vacuum", driver="mock_vacuum", model="Saros 10"
        )
        settings.default_device = settings.default_device or "mock-vacuum"

    if env.get("MHS_READ_ONLY") is not None:
        settings.read_only = _as_bool(env.get("MHS_READ_ONLY"))
    if env.get("MHS_ALLOW_RAW_GCODE") is not None:
        settings.allow_raw_gcode = _as_bool(env.get("MHS_ALLOW_RAW_GCODE"))
    if env.get("MHS_STATE_DIR"):
        settings.state_dir = Path(env["MHS_STATE_DIR"]).expanduser()
    if env.get("MHS_SLICER"):
        settings.slicer_binary = env["MHS_SLICER"]
    if env.get("MHS_SLICER_PROFILES"):
        settings.slicer_profiles = tuple(
            p for p in env["MHS_SLICER_PROFILES"].split(os.pathsep) if p
        )

    if settings.default_device and settings.default_device not in settings.devices:
        raise ConfigError(f"default_device '{settings.default_device}' is not defined")
    for cfg in settings.devices.values():
        cfg.validate()
    return settings


#: Previous name for :class:`DeviceConfig`, kept so existing imports keep working.
PrinterConfig = DeviceConfig
