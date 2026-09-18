"""Roborock driver (Saros 10 / 10R / Z70, and the wider V1 vacuum family).

Unlike the Bambu driver, this one does **not** reimplement the wire protocol.
Roborock's is genuinely hard - four message versions, AES in ECB, CBC and GCM,
HMAC-derived MQTT credentials, and a compressed binary map format - and
`python-roborock` already implements and maintains all of it, is what Home
Assistant uses, and ships a device simulator this driver's tests run against.
Re-deriving that blind, against hardware nobody here can test on, would be a
worse engineering decision than taking the dependency.

What this package adds is the part that library deliberately leaves open: the
MHS surface (channels, enforced limits, a device descriptor), room and
coordinate handling that an agent can actually aim with, and named locations
that survive a restart.
"""

from __future__ import annotations

#: Product ids that identify a Saros, for nicer model naming.
SAROS_MODELS = {
    "roborock.vacuum.a147": "Saros 10",
    "roborock.vacuum.a144": "Saros 10R",
    "roborock.vacuum.a143": "Saros R10",
}
