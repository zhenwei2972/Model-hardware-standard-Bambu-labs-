"""TLS setup shared by the MQTT, FTPS and camera transports.

Bambu printers present a certificate issued by Bambu's own CA whose common name
is the printer's **serial number** - not its IP address. Verifying it therefore
needs two things: the pinned CA bundle shipped next to this module, and SNI set
to the serial rather than to the host we dialled.
"""

from __future__ import annotations

import ssl

from . import CA_BUNDLE


class _SNIOverrideContext(ssl.SSLContext):
    """SSLContext that forces a fixed SNI/hostname during the handshake.

    Libraries such as paho and ftplib call ``wrap_socket(server_hostname=host)``
    with the address we connected to. Overriding it here means the pinned
    certificate can still be validated against the serial number.
    """

    sni_hostname: str | None = None

    def wrap_socket(self, sock, *args, server_hostname=None, **kwargs):  # type: ignore[override]
        return super().wrap_socket(
            sock, *args, server_hostname=self.sni_hostname or server_hostname, **kwargs
        )


def build_ssl_context(tls_mode: str = "verify", serial: str | None = None) -> ssl.SSLContext:
    """Return an SSL context for one of the three supported trust modes.

    ``verify``   pin Bambu's CA and require CN == serial (recommended)
    ``ca_only``  pin Bambu's CA, accept any CN (older certs, renamed printers)
    ``insecure`` verify nothing - only acceptable on a LAN you fully control
    """
    if tls_mode == "insecure":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ctx = _SNIOverrideContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(CA_BUNDLE))
    ctx.verify_mode = ssl.CERT_REQUIRED
    # Bambu's CA omits the keyUsage extension, which Python 3.13's strict
    # profile rejects. Everything else about the chain is still checked.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

    if tls_mode == "verify" and serial:
        ctx.check_hostname = True
        ctx.sni_hostname = serial
    else:
        ctx.check_hostname = False
    return ctx
