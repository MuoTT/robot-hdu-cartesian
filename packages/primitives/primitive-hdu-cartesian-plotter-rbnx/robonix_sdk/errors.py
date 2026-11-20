# SPDX-License-Identifier: MIT
"""Error surface of the vendored SDK.

The upstream SDK raises three kinds of exceptions:

* :class:`robonix_sdk.ProtocolError` for malformed frames, CRC failures
  and non-OK ACKs;
* built-in :class:`TimeoutError` when an ACK does not arrive;
* built-in :class:`ConnectionError` when the socket dies.

This module only re-exports :class:`ProtocolError` for callers that want
a dedicated import path. There is intentionally no ``PlcError`` class:
the PLC reports failures through the ACK ``status``/``err`` fields, which
the client converts into ``ProtocolError`` with the raw values attached.
"""

from .client import ProtocolError

__all__ = ["ProtocolError"]