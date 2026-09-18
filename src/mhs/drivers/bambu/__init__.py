"""Bambu Lab LAN driver (A1 mini, A1, P1P/P1S, X1 series).

Three independent transports, all authenticated with the printer's LAN
*Access Code* and all TLS-protected by Bambu's own CA:

* MQTT  ``mqtts://<ip>:8883``  telemetry + commands  (see :mod:`.mqtt`)
* FTPS  ``ftps://<ip>:990``    implicit TLS file transfer (see :mod:`.files`)
* Camera``tcp://<ip>:6000``    JPEG frame stream (see :mod:`.camera`)
"""

from __future__ import annotations

from pathlib import Path

CA_BUNDLE = Path(__file__).with_name("bambu_ca.pem")
