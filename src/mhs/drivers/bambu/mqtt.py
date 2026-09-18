"""LAN MQTT transport.

paho runs its own network thread, so everything here is thread-safe and the
driver bridges into asyncio with ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable

import paho.mqtt.client as mqtt

from ...errors import ConnectionFailed, TimeoutExceeded
from .tls import build_ssl_context

log = logging.getLogger(__name__)

_AUTH_HINT = (
    "Check the Access Code on the printer screen (Settings > Network). "
    "If the code is right but commands are ignored, enable LAN Only Mode + "
    "Developer Mode: since firmware 01.05 Bambu's Authorization Control System "
    "blocks third-party control in cloud mode."
)


class BambuMqtt:
    """Thin wrapper around a paho client for one printer."""

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        *,
        port: int = 8883,
        tls_mode: str = "verify",
        on_report: Callable[[dict], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        keepalive: int = 60,
    ) -> None:
        self.host = host
        self.port = port
        self.serial = serial
        self.access_code = access_code
        self.tls_mode = tls_mode
        self.keepalive = keepalive
        self._on_report = on_report
        self._on_connected = on_connected

        self.report_topic = f"device/{serial}/report"
        self.request_topic = f"device/{serial}/request"

        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._connect_error: str | None = None
        self._sequence_lock = threading.Lock()
        self._sequence = 0
        self._pending: dict[str, list] = {}  # sequence_id -> [Event, report|None]
        self._pending_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self, timeout: float = 15.0) -> None:
        if self._client is not None and self.connected:
            return
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"mhs-{uuid.uuid4().hex[:8]}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set("bblp", self.access_code)
        client.tls_set_context(build_ssl_context(self.tls_mode, self.serial))
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        self._connect_error = None

        try:
            client.connect(self.host, self.port, keepalive=self.keepalive)
        except OSError as exc:
            self._client = None
            raise ConnectionFailed(
                f"cannot reach {self.host}:{self.port} - {exc}",
                hint="Is the printer on the same network and powered on?",
            ) from exc
        client.loop_start()

        if not self._connected.wait(timeout):
            detail = self._connect_error or "no CONNACK within timeout"
            self.disconnect()
            raise ConnectionFailed(f"MQTT connect to {self.host} failed: {detail}", hint=_AUTH_HINT)

    def disconnect(self) -> None:
        client, self._client = self._client, None
        self._connected.clear()
        if client is not None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()

    # -- paho callbacks (network thread) -----------------------------------
    def _on_connect(self, client: mqtt.Client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self._connect_error = str(reason_code)
            log.warning("MQTT connect refused by %s: %s", self.host, reason_code)
            return
        client.subscribe(self.report_topic, qos=0)
        self._connected.set()
        log.info("MQTT connected to %s, subscribed to %s", self.host, self.report_topic)
        if self._on_connected is not None:
            # Re-sync after a reconnect: the cached state may have drifted while
            # we were away, and the A1/P1 only send deltas from here on.
            try:
                self._on_connected()
            except Exception:  # pragma: no cover - never kill the network thread
                log.exception("on_connected callback failed")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected.clear()
        log.info("MQTT disconnected from %s (%s)", self.host, reason_code)

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.debug("dropping non-JSON message on %s", message.topic)
            return
        self._resolve_pending(payload)
        if self._on_report is not None:
            try:
                self._on_report(payload)
            except Exception:  # pragma: no cover - never kill the network thread
                log.exception("on_report callback failed")

    def _resolve_pending(self, payload: dict) -> None:
        for body in payload.values():
            if not isinstance(body, dict):
                continue
            seq = body.get("sequence_id")
            if seq is None:
                continue
            with self._pending_lock:
                slot = self._pending.get(str(seq))
            if slot is not None:
                slot[1] = body
                slot[0].set()

    # -- publishing --------------------------------------------------------
    def next_sequence(self) -> int:
        with self._sequence_lock:
            self._sequence += 1
            return self._sequence

    def publish(self, payload: dict, qos: int = 1) -> None:
        if self._client is None or not self.connected:
            raise ConnectionFailed(f"not connected to {self.host}", hint=_AUTH_HINT)
        info = self._client.publish(self.request_topic, json.dumps(payload), qos=qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionFailed(f"publish failed (rc={info.rc})")

    def publish_and_wait(self, payload: dict, timeout: float = 6.0) -> dict:
        """Publish and wait for the report echoing the same ``sequence_id``.

        A missing acknowledgement is reported rather than raised: it is the usual
        symptom of the Authorization Control System dropping control commands, and
        the caller can surface that as a hint.
        """
        body = next(iter(payload.values()))
        sequence_id = str(body.get("sequence_id"))
        event = threading.Event()
        slot = [event, None]
        with self._pending_lock:
            self._pending[sequence_id] = slot
        try:
            self.publish(payload)
            if not event.wait(timeout):
                return {
                    "acknowledged": False,
                    "sequence_id": sequence_id,
                    "command": body.get("command"),
                    "hint": _AUTH_HINT,
                }
            report = slot[1] or {}
            result = str(report.get("result", "")).lower()
            return {
                "acknowledged": True,
                "sequence_id": sequence_id,
                "command": report.get("command", body.get("command")),
                "result": report.get("result", "unknown"),
                "success": result in {"success", ""},
                "reason": report.get("reason") or None,
            }
        finally:
            with self._pending_lock:
                self._pending.pop(sequence_id, None)

    def wait_until_connected(self, timeout: float = 15.0) -> None:
        if not self._connected.wait(timeout):
            raise TimeoutExceeded(f"MQTT not connected to {self.host} after {timeout:.0f}s")
