"""External TCP reachability probe (Stage D2).

A deliberately small, bounded primitive that answers exactly one
question: can Sentinel establish a TCP connection to the configured
external target?

Contract points (see docs/ARCHITECTURE.md section 19):

- the result is raw external evidence only:
  ``TcpReachability.REACHABLE`` / ``TcpReachability.UNREACHABLE`` —
  never a host state decision and never a state transition;
- the sole network dependency is the Python standard library
  ``socket`` module; ``socket.create_connection`` is the only
  connection primitive used;
- one invocation performs exactly one application-level
  ``create_connection`` call with the verbatim target
  ``(tcp_host, tcp_port)`` and the configured ``timeout_seconds``
  as the connection timeout (address resolution inside
  ``create_connection`` may internally consider multiple resolved
  addresses — that is still one logical probe);
- a successful TCP connect is sufficient evidence: no bytes are
  exchanged and no application protocol handshake is performed; the
  opened socket is closed before the function returns;
- normal network failures of the ``OSError`` family (connection
  refused, connect timeout, name resolution failure, network or
  host unreachable) map to ``UNREACHABLE`` and are never leaked to
  callers as exceptions;
- unexpected non-network programming or runtime defects are not
  swallowed: they propagate unchanged, and control-flow exceptions
  are never caught;
- there is no confirmation counting and no memory between calls:
  debounce/hysteresis counters are a Stage D3 concern and
  orchestration a Stage D4 concern;
- the probe is stateless: no persistence, no notification delivery
  and no mutation of global socket state.
"""

from __future__ import annotations

import socket
from enum import Enum

from hermes_sentinel.config import ExternalCheckSettings

__all__ = [
    "TcpReachability",
    "probe_tcp_reachability",
]


class TcpReachability(Enum):
    """Raw external TCP reachability evidence for one probe call.

    This is deliberately not a host state: mapping reachability
    evidence to a host state is a later stage D responsibility that
    also weighs heartbeat freshness and confirmation counters.
    """

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


def probe_tcp_reachability(
    *,
    settings: ExternalCheckSettings,
) -> TcpReachability:
    """Probe external TCP reachability of the configured target.

    Performs exactly one application-level connection attempt to
    ``(settings.tcp_host, settings.tcp_port)`` using the standard
    library ``socket.create_connection`` primitive with
    ``settings.timeout_seconds`` as the connection timeout.

    - success (TCP connection established) -> ``REACHABLE``; the
      opened socket is closed before returning and no bytes are
      exchanged;
    - any ``OSError`` raised by the connection attempt (connection
      refused, connect timeout, name resolution failure, network or
      host unreachable) -> ``UNREACHABLE`` — normal network failure
      is never leaked to callers as an exception;
    - anything else propagates unchanged: programming defects and
      control-flow exceptions are not converted into evidence.

    The result is one instantaneous raw observation. Confirmation
    counting, debounce/hysteresis and host state resolution belong
    to later stage D units; this function keeps no state between
    calls and never mutates the settings object or any global
    socket state.
    """
    try:
        connection = socket.create_connection(
            (settings.tcp_host, settings.tcp_port),
            timeout=settings.timeout_seconds,
        )
    except OSError:
        return TcpReachability.UNREACHABLE
    try:
        # A successful TCP connect is the only evidence needed: no
        # bytes are exchanged and no application protocol runs.
        return TcpReachability.REACHABLE
    finally:
        connection.close()
