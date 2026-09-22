"""Central process lifecycle / entrypoint (Stage E8).

The minimal process boundary that connects the already-accepted
layers into ONE launchable central Sentinel process:

    main() -> run_process(os.environ)
                    |
    E6 load_central_settings(env)  (exactly once, the live mapping)
                    |
    E7 build_application(settings) (exactly once, context-managed)
                    |
    cooperative SIGTERM/SIGINT stop REQUEST (flag only)
                    |
    SentinelApplication.run_forever(stop_predicate)  (exactly once)
                    |
    guaranteed E7 application cleanup + previous signal handlers
    restored on every exit path

E8 owns ONLY the process boundary: the single implicit environment
read in ``main``, the process-owned cooperative stop request, and the
temporary installation/restoration of the two stop-signal handlers.
It duplicates no configuration parsing (E6 remains the authority that
snapshots and validates the mapping), no application construction (E7
remains the sole resource owner), no runtime scheduling (E5 remains
the loop) and no cleanup ownership (the application's context-manager
exit performs the owned cleanup).

Contract points (see docs/ARCHITECTURE.md section 29):

- **Minimal public surface**: ``run_process(env)`` is the testable
  process boundary — it receives the environment mapping EXPLICITLY
  and never reads ``os.environ`` itself; ``main()`` is the one
  production place that passes the live ``os.environ`` object through
  to ``run_process`` (which hands it to E6 unmodified — no copy, no
  parse, no environment files, no aliases, no defaults);
- **Exact delegation**: ``load_central_settings`` is called exactly
  once with the exact supplied mapping object, and the exact returned
  ``CentralSettings`` value is passed to ``build_application``
  exactly once;
- **Stop REQUEST semantics only**: the SIGTERM/SIGINT handlers
  perform no I/O, no logging, no cleanup, no application call, no
  process exit and raise nothing — they only mark the process-owned
  stop request. The stop predicate handed to the accepted runtime is
  False before any stop signal, True after the first SIGTERM or
  SIGINT, stable True thereafter and safe under repeated delivery;
  the accepted E5 loop notices the request at its next bounded loop
  boundary and returns cooperatively. No force-kill / second-signal
  policy exists here;
- **Temporary signal ownership**: the previous SIGTERM/SIGINT
  handlers are captured, the E8 handlers are installed only for the
  duration of ``run_process`` in the calling thread (no signal
  thread, no self-pipe machinery, no exit-time hook registration),
  and the previous handlers are restored when ``run_process`` exits
  — normal runtime return, runtime exception, or
  application-construction failure. A partially completed
  installation (one signal changed, the next failing) restores
  everything already changed before the ORIGINAL installation
  failure propagates; during such rollback a secondary restore
  failure never replaces the original failure;
- **Guaranteed application cleanup, E7-owned**: the application is
  used exactly as ``with build_application(settings) as application:
  application.run_forever(stop_request)`` — the E7 context-manager
  exit closes the owned B5 listener and B1 SQLite connection on
  every path. This module performs no direct listener, server or
  database cleanup of its own and adds no cleanup abstraction
  around E7 resources;
- **Unchanged error semantics**: startup settings failures
  (``CentralSettingsError``), E7 construction/database/bind failures,
  runtime failures and ordinary signal API failures propagate
  unchanged from their accepted owners — no retry, no backoff, no
  broad translation to an exit code, no catch-and-print framework.
  ``main``/``run_process`` return normally only when the accepted
  runtime returns normally (a graceful cooperative stop); an
  uncaught failure stays uncaught so a later launcher can observe it
  naturally.

Out of scope for E8: the central Sentinel systemd service,
EnvironmentFile packaging, deployment scripts/runbooks, daemonization,
PID files, privilege dropping, TLS termination, reverse proxy,
firewall configuration, logging frameworks, metrics, retries/backoff,
queueing, incident persistence, schema changes, service monitoring,
Hermes integration, backup implementation, dead-man monitoring and
Stage F hardening — those remain later stages.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable, Mapping
from types import FrameType
from typing import Any

from hermes_sentinel.application import build_application
from hermes_sentinel.settings import load_central_settings

__all__ = [
    "main",
    "run_process",
]

#: The two cooperative stop signals E8 handles, in installation order.
_STOP_SIGNALS: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)

#: A captured previous signal handler: a Python handler callable, a
#: stdlib sentinel (an int enum such as SIG_DFL/SIG_IGN), a raw int,
#: or None (``getsignal``'s full result shape).
_PreviousHandler = Callable[[int, FrameType | None], Any] | int | None


class _StopRequest:
    """The process-owned cooperative stop request (Stage E8).

    One boolean flag and nothing else. ``request()`` is the only
    mutation and is what a stop-signal handler runs; calling the
    object is the stop predicate handed to the accepted E5/E7 runtime,
    so the exact predicate the runtime polls is the exact state the
    installed signal handlers flip. Never reset within one
    ``run_process`` call: repeated signal delivery stays True.
    """

    __slots__ = ("_requested",)

    def __init__(self) -> None:
        self._requested = False

    def request(self) -> None:
        """Mark the process stop request as requested (idempotent)."""
        self._requested = True

    def __call__(self) -> bool:
        """The stop predicate: True once any stop signal arrived."""
        return self._requested


def _install_stop_handlers(
    handler: Callable[[int, FrameType | None], object],
) -> list[tuple[int, _PreviousHandler]]:
    """Install ``handler`` for both stop signals; return what to restore.

    Each (signum, previous handler) pair is recorded only AFTER that
    signal's installation actually succeeded, so a failure at any
    step (capture or install) leaves ``installed`` holding exactly
    the signals that were really changed: those are restored — with
    secondary restore failures swallowed, the original installation
    failure stays primary (the E7 rollback precedent) — before the
    original failure propagates. Runs in the calling thread.
    """
    installed: list[tuple[int, _PreviousHandler]] = []
    try:
        for signum in _STOP_SIGNALS:
            previous_handler = signal.getsignal(signum)
            signal.signal(signum, handler)
            installed.append((signum, previous_handler))
    except BaseException:
        _restore_stop_handlers(installed, swallow_failures=True)
        raise
    return installed


def _restore_stop_handlers(
    installed: list[tuple[int, _PreviousHandler]], *, swallow_failures: bool
) -> None:
    """Restore every captured previous handler, newest install first.

    Both restorations are attempted even if the first fails (the E7
    close() precedent). With ``swallow_failures`` the first restore
    failure is remembered but suppressed — used while an original
    failure is already escaping, which must stay primary; without it
    (the normal exit path) that restore failure propagates.
    """
    failure: BaseException | None = None
    for signum, previous_handler in reversed(installed):
        try:
            signal.signal(signum, previous_handler)
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None and not swallow_failures:
        raise failure


def run_process(env: Mapping[str, str]) -> None:
    """Run one central Sentinel process against ``env``.

    The complete process flow: load the settings through the accepted
    E6 loader exactly once (the exact mapping object supplied — never
    a copy or a re-read), install the two cooperative stop-request
    signal handlers, compose the application through the accepted E7
    root exactly once, run it until the accepted runtime returns
    cooperatively (the stop predicate flips when SIGTERM/SIGINT
    arrives), then restore the previous signal handlers. The E7
    application cleanup is guaranteed by the application's own
    context-manager exit on every path. Returns normally only when
    the accepted runtime returns normally; every other failure
    propagates unchanged from its accepted owner.
    """
    settings = load_central_settings(env)
    stop_request = _StopRequest()

    def on_stop_signal(signum: int, frame: FrameType | None) -> None:
        # Stop REQUEST only: no I/O, no logging, no cleanup, no
        # application call, no process exit, nothing raised.
        stop_request.request()

    installed = _install_stop_handlers(on_stop_signal)
    try:
        with build_application(settings) as application:
            application.run_forever(stop_request)
    except BaseException:
        # The original failure stays primary: the (mandatory) signal
        # restoration runs best-effort alongside an escaping failure.
        _restore_stop_handlers(installed, swallow_failures=True)
        raise
    _restore_stop_handlers(installed, swallow_failures=False)


def main() -> None:
    """The console entrypoint: pass the live process environment through.

    The ONLY place in production code where the process environment is
    implicitly read: the live ``os.environ`` object itself is handed to
    ``run_process`` (and through it to the E6 loader, which snapshots
    and validates it) — never copied, parsed or filtered here.
    """
    run_process(os.environ)
