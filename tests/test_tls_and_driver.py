"""TLS trust modes and the Bambu driver's non-networked surface."""

from __future__ import annotations

import ssl

import pytest

from mhs.config import PrinterConfig
from mhs.drivers import build
from mhs.drivers.bambu import CA_BUNDLE
from mhs.drivers.bambu.tls import build_ssl_context
from mhs.models import Capability


def test_ca_bundle_ships_with_the_package():
    text = CA_BUNDLE.read_text()
    assert text.count("BEGIN CERTIFICATE") >= 2  # BBL CA and BBL CA2 (RSA + ECC)


def test_verify_mode_pins_the_ca_and_uses_the_serial_for_sni():
    ctx = build_ssl_context("verify", serial="01P00A000000000")
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.sni_hostname == "01P00A000000000"
    assert not (ctx.verify_flags & ssl.VERIFY_X509_STRICT)  # BBL CA omits keyUsage
    assert ctx.get_ca_certs(), "CA bundle was not loaded"


def test_ca_only_keeps_verification_but_drops_hostname_matching():
    ctx = build_ssl_context("ca_only", serial="01P00A000000000")
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


def test_insecure_is_explicitly_unverified():
    ctx = build_ssl_context("insecure")
    assert ctx.verify_mode is ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_without_a_serial_falls_back_to_ca_only():
    ctx = build_ssl_context("verify", serial=None)
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


@pytest.fixture
def bambu():
    return build(
        PrinterConfig(
            printer_id="a1mini",
            driver="bambu",
            model="A1 mini",
            host="192.0.2.10",
            serial="01P00A000000000",
            access_code="12345678",
        )
    )


def test_driver_wires_every_transport_to_the_same_credentials(bambu):
    assert bambu._mqtt.report_topic == "device/01P00A000000000/report"
    assert bambu._mqtt.request_topic == "device/01P00A000000000/request"
    for transport in (bambu._mqtt, bambu._files, bambu._camera):
        assert transport.host == "192.0.2.10"
        assert transport.access_code == "12345678"
    assert (bambu._mqtt.port, bambu._files.port, bambu._camera.port) == (8883, 990, 6000)


async def test_driver_info_reports_the_build_volume(bambu):
    info = await bambu.info()
    assert info.build_volume_mm == (180, 180, 180)
    assert info.serial == "01P00A000000000"
    assert Capability.CAMERA_SNAPSHOT in info.capabilities


async def test_unreachable_printer_fails_with_a_useful_hint(bambu, monkeypatch):
    """A socket error must surface as ConnectionFailed with something to act on."""
    import paho.mqtt.client as mqtt

    from mhs.errors import ConnectionFailed

    def refuse(*_args, **_kwargs):
        raise OSError("No route to host")

    monkeypatch.setattr(mqtt.Client, "connect", refuse)
    with pytest.raises(ConnectionFailed) as excinfo:
        await bambu.connect()
    assert "192.0.2.10:8883" in excinfo.value.message
    assert excinfo.value.hint


def test_mqtt_sequence_ids_increment(bambu):
    assert [bambu._mqtt.next_sequence() for _ in range(3)] == [1, 2, 3]


def test_reconnect_triggers_a_full_status_resync(bambu, monkeypatch):
    """The A1/P1 only send deltas, so a reconnect must re-request everything."""
    published: list[dict] = []
    monkeypatch.setattr(bambu._mqtt, "publish", lambda payload, qos=1: published.append(payload))

    bambu._resync()

    commands_sent = [next(iter(p.values()))["command"] for p in published]
    assert commands_sent == ["pushall", "get_version"]
