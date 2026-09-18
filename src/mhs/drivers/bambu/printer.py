"""The Bambu Lab driver: turns three LAN transports into one `Printer`."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ...config import PrinterConfig
from ...device import Printer
from ...errors import CommandRejected, ConnectionFailed, DeviceBusy
from ...models import (
    Capability,
    FileEntry,
    PrinterInfo,
    PrinterStatus,
    PrintOptions,
    PrintState,
)
from . import commands
from .camera import BambuCamera
from .files import BambuFiles, sanitize_remote_name
from .mqtt import BambuMqtt
from .state import BambuState

log = logging.getLogger(__name__)

#: Build volumes, so a caller can sanity-check a model before slicing.
BUILD_VOLUMES = {
    "a1 mini": (180, 180, 180),
    "a1": (256, 256, 256),
    "p1p": (256, 256, 256),
    "p1s": (256, 256, 256),
    "x1c": (256, 256, 256),
    "x1e": (256, 256, 256),
}

_BASE_CAPABILITIES = (
    Capability.START_PRINT,
    Capability.PAUSE_RESUME,
    Capability.STOP,
    Capability.FILE_LIST,
    Capability.FILE_UPLOAD,
    Capability.TEMPERATURE_CONTROL,
    Capability.RAW_GCODE,
    Capability.LIGHT,
    Capability.SPEED,
    Capability.AMS,
    Capability.SKIP_OBJECTS,
    Capability.CALIBRATION,
    Capability.CAMERA_SNAPSHOT,
)

#: Do not hammer the A1/P1 MCU: OpenBambuAPI warns that frequent `pushall`
#: requests make the printer lag during a print.
_MIN_PUSHALL_INTERVAL = 300.0
_STALE_AFTER = 90.0


class BambuPrinter(Printer):
    """A Bambu Lab printer reached over the LAN."""

    driver_name = "bambu"

    def __init__(self, config: PrinterConfig) -> None:
        super().__init__(config.printer_id, config.model)
        self.config = config
        self.state = BambuState(config.printer_id)
        self._mqtt = BambuMqtt(
            host=config.host,
            serial=config.serial,
            access_code=config.access_code,
            port=config.mqtt_port,
            tls_mode=config.tls_mode,
            on_report=self.state.apply,
            on_connected=self._resync,
        )
        self._files = BambuFiles(
            host=config.host,
            access_code=config.access_code,
            serial=config.serial,
            port=config.ftps_port,
            tls_mode=config.tls_mode,
        )
        self._camera = BambuCamera(
            host=config.host,
            access_code=config.access_code,
            serial=config.serial,
            port=config.camera_port,
            tls_mode=config.tls_mode,
        )
        self._last_pushall = 0.0
        self._connect_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self, wait_for_status: float = 10.0) -> None:
        async with self._connect_lock:
            if self._mqtt.connected:
                return
            await asyncio.to_thread(self._mqtt.connect)
            self.state.connected = True
            deadline = time.monotonic() + wait_for_status
            while not self.state.print and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
            if not self.state.print:
                log.warning("%s: connected but no telemetry yet", self.printer_id)

    async def disconnect(self) -> None:
        self.state.connected = False
        await asyncio.to_thread(self._mqtt.disconnect)

    async def _ensure_connected(self) -> None:
        if not self._mqtt.connected:
            await self.connect()

    def _resync(self) -> None:
        """Ask for a full status object right after (re)connecting.

        Runs on the MQTT network thread, so it publishes directly.
        """
        self._last_pushall = time.monotonic()
        self._mqtt.publish(commands.push_all(self._mqtt.next_sequence()), qos=0)
        self._mqtt.publish(commands.get_version(self._mqtt.next_sequence()), qos=0)

    async def _refresh(self, force: bool = False) -> None:
        """Request a full status object, rate-limited."""
        now = time.monotonic()
        if not force and now - self._last_pushall < _MIN_PUSHALL_INTERVAL:
            return
        self._last_pushall = now
        seq = self._mqtt.next_sequence()
        await asyncio.to_thread(self._mqtt.publish, commands.push_all(seq), 0)
        await asyncio.to_thread(self._mqtt.publish, commands.get_version(self._mqtt.next_sequence()), 0)

    # -- introspection -----------------------------------------------------
    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return _BASE_CAPABILITIES

    async def info(self) -> PrinterInfo:
        return PrinterInfo(
            printer_id=self.printer_id,
            driver=self.driver_name,
            model=self.config.model,
            host=self.config.host,
            serial=self.config.serial,
            firmware=self.state.firmware,
            build_volume_mm=BUILD_VOLUMES.get(self.config.model.strip().lower()),
            capabilities=self.capabilities,
        )

    async def status(self) -> PrinterStatus:
        await self._ensure_connected()
        self.state.connected = self._mqtt.connected
        if self.state.is_stale(_STALE_AFTER):
            # Telemetry dried up (printer rebooted, or we missed the full object).
            await self._refresh(force=True)
            await asyncio.sleep(1.0)
        return self.state.to_status(stale_after=_STALE_AFTER)

    # -- files -------------------------------------------------------------
    async def list_files(self, directory: str = "") -> list[FileEntry]:
        return await asyncio.to_thread(self._files.list_files, directory or self.config.upload_dir)

    async def upload_file(self, local_path: str, remote_name: str | None = None) -> FileEntry:
        name = sanitize_remote_name(remote_name or Path(local_path).name)
        target_dir = self.config.upload_dir.strip("/")
        remote_path = f"{target_dir}/{name}" if target_dir else name
        return await asyncio.to_thread(self._files.upload, local_path, remote_path)

    async def delete_file(self, remote_path: str) -> None:
        await asyncio.to_thread(self._files.delete, remote_path)

    # -- job control -------------------------------------------------------
    async def start_print(self, remote_path: str, options: PrintOptions | None = None) -> dict:
        options = options or PrintOptions()
        status = await self.status()
        if not status.state.accepts_new_job:
            raise DeviceBusy(
                f"{self.printer_id} is {status.state.value}; stop or finish the current job first"
            )
        blocking = [a for a in status.alerts if a.severity in {"fatal", "serious"}]
        if blocking:
            raise DeviceBusy(
                f"{self.printer_id} reports {blocking[0].code}; clear it before printing",
                hint=blocking[0].url,
            )
        payload = commands.project_file(
            remote_path,
            plate=options.plate,
            use_ams=options.use_ams,
            ams_mapping=options.ams_mapping,
            bed_leveling=options.bed_leveling,
            flow_calibration=options.flow_calibration,
            vibration_calibration=options.vibration_calibration,
            layer_inspect=options.layer_inspect,
            timelapse=options.timelapse,
            job_name=options.job_name,
            sequence_id=self._mqtt.next_sequence(),
        )
        result = await asyncio.to_thread(self._mqtt.publish_and_wait, payload)
        result["file"] = remote_path
        result["plate"] = options.plate
        return result

    async def _simple_command(self, builder) -> dict:
        await self._ensure_connected()
        payload = builder(self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def pause_print(self) -> dict:
        return await self._simple_command(commands.pause)

    async def resume_print(self) -> dict:
        return await self._simple_command(commands.resume)

    async def stop_print(self) -> dict:
        return await self._simple_command(commands.stop)

    # -- optional controls -------------------------------------------------
    async def set_temperature(self, nozzle: float | None = None, bed: float | None = None) -> dict:
        await self._ensure_connected()
        payload = commands.set_temperature(nozzle, bed, self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def set_light(self, on: bool, node: str = "chamber_light") -> dict:
        await self._ensure_connected()
        payload = commands.led_control(on, node, self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def set_speed(self, level: int) -> dict:
        await self._ensure_connected()
        payload = commands.set_speed(level, self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def send_gcode(self, gcode: str) -> dict:
        await self._ensure_connected()
        payload = commands.gcode_line(gcode, self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def skip_objects(self, object_ids: list[int]) -> dict:
        await self._ensure_connected()
        payload = commands.skip_objects(object_ids, self._mqtt.next_sequence())
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def calibrate(self, **kwargs: bool) -> dict:
        status = await self.status()
        if status.state.is_active:
            raise DeviceBusy(f"{self.printer_id} is printing; calibration needs an idle printer")
        payload = commands.calibration(sequence_id=self._mqtt.next_sequence(), **kwargs)
        return await asyncio.to_thread(self._mqtt.publish_and_wait, payload)

    async def snapshot(self) -> bytes:
        try:
            return await asyncio.to_thread(self._camera.capture)
        except ConnectionFailed:
            raise
        except OSError as exc:  # pragma: no cover - socket edge cases
            raise ConnectionFailed(f"camera capture failed: {exc}") from exc

    async def stream_frames(self, count: int, interval: float = 1.0):
        """Yield ``count`` JPEG frames, at most one every ``interval`` seconds."""
        if count < 1:
            raise CommandRejected("count must be >= 1")
        for index in range(count):
            if index:
                await asyncio.sleep(interval)
            yield await self.snapshot()

    async def wait_until_idle(self, timeout: float = 3600.0) -> PrinterStatus:
        return await self.wait_for_state(
            {PrintState.IDLE, PrintState.FINISHED, PrintState.FAILED}, timeout=timeout, poll_interval=5.0
        )
