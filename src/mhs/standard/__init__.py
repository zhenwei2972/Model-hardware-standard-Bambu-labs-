"""Alignment with Anthropic's Model Hardware Standard (MHS).

MHS was announced as a research preview on 27 August 2026. As of this writing
the full specification and reference implementation are not public, so this
package implements the four properties the announcement describes rather than
claiming conformance to a published schema:

1. a small, uniform set of primitives - ``read`` and ``write`` over named
   channels - instead of a bespoke method per device;
2. a device description an agent can enumerate and consult before acting;
3. natural-language tags recording what the device is and what it is for;
4. safety limits held in the driver and checked before execution, not left to
   the model's judgement or to prompt text.

When the specification is open-sourced, :mod:`mhs.standard.descriptor` is the
single place that has to change to emit the official format.
"""

from .channels import Access, Channel, SafetyLimit
from .descriptor import build_descriptor, descriptor_markdown

__all__ = ["Access", "Channel", "SafetyLimit", "build_descriptor", "descriptor_markdown"]
