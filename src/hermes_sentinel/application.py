"""Central application composition root (Stage E7).

The minimal boundary that wires the already-accepted B1–E6 components
into ONE owned Sentinel application — construction and bounded
resource ownership only:

    build_application(settings: CentralSettings) -> SentinelApplication
                |
    ONE B1 SQLite connection (connect(settings.database_path))
                |
    ONE HeartbeatRepository on exactly that connection
                |
    HeartbeatIngestor -> AuthenticatedHeartbeatAdapter (B3)
                      -> HeartbeatHttpAdapter (B4)
                      -> create_heartbeat_http_server (B5, bound)
                |
    HealthEngine on the SAME repository
    TelegramSender -> NotificationCoordinator (E3) -> MonitoringCycle (E4)
                |
    SentinelRuntime(E5) + listener + connection
                |
    SentinelApplication (owns all three)

E7 owns construction and the cleanup of the two bound resources it
opens. Existing constructor contracts remain authoritative: no auth,
health semantics, Telegram semantics, runtime scheduling, persistence
logic or configuration validation is duplicated, re-validated or
reinterpreted here.

Contract points (see docs/ARCHITECTURE.md section 28):

- **Composition only, no environment access**: ``build_application``
  consumes an already-loaded :class:`CentralSettings` value. It never
  reads the process environment, never calls
  ``load_central_settings``, and never re-opens E6 validation — a
  future process/entrypoint unit passes the environment to E6 and the
  resulting settings here;
- **Exactly two side effects**: opening/initializing the configured
  SQLite database through the accepted B1 ``connect()`` and binding
  the configured B5 heartbeat listener through the accepted
  ``create_heartbeat_http_server()``. Nothing else happens:
  ``build_application`` does not start the runtime, does not call
  ``serve_forever()``, does not service a request, does not execute a
  monitoring cycle, does not perform a Telegram send or a TCP
  reachability probe, installs no signal handler and deploys nothing.
  ``TelegramSender`` construction may build its accepted stdlib
  opener — that is not a Telegram request;
- **One shared persistence session**: exactly one SQLite connection
  and exactly one ``HeartbeatRepository`` are constructed, and BOTH
  the heartbeat write path (``HeartbeatIngestor``) and the monitoring
  read path (``HealthEngine``) receive that SAME repository instance
  — preserving the accepted single-threaded architecture: heartbeat
  HTTP write -> repository; monitoring health read -> the same
  repository. No duplicate repository, no second connection, no
  second sender, no parallel component graph;
- **Fail-safe construction**: if a failure occurs after the database
  is opened, the connection is closed before the failure escapes; if
  it occurs after the listener is bound, ``server_close()`` is
  attempted and the connection is closed. The ORIGINAL construction
  failure always remains the primary escaping failure — a secondary
  cleanup failure during rollback never replaces it (rollback
  failures are swallowed for exactly this reason; only an explicit
  :meth:`SentinelApplication.close` after a successful build surfaces
  cleanup failures). No retries;
- **Minimal owned lifecycle**: ``run_forever(should_stop=None)`` is a
  thin delegation to the accepted E5
  ``SentinelRuntime.run_forever()`` with the stop predicate passed
  through exactly as supplied — no new scheduling, retry, error
  translation or loop semantics; runtime exceptions propagate
  unchanged. ``close()`` closes the owned B5 listener through
  ``server_close()`` (deliberately NOT ``shutdown()``: E5 never runs
  ``serve_forever()`` and owns no serve-loop thread) and then the
  owned SQLite connection, attempting BOTH cleanups even if the
  first fails and never converting a cleanup failure into silent
  success. The context-manager protocol calls that same ``close()``
  on exit;
- **No new concurrency**: the accepted single-thread model is
  preserved — no ``ThreadingHTTPServer``, no threads, no asyncio, no
  multiprocessing, no executors, no background workers. E7 only
  wires the accepted serial B5 server into the accepted cooperative
  E5 runtime;
- **No new error hierarchy**: expected constructor/bind/database
  errors propagate from their accepted owners unchanged after any
  rollback cleanup;
- **Secret-safe repr**: :class:`SentinelApplication` exposes no
  settings and no secret material in its repr.

Out of scope for E7: implicit process-environment access, .env
loading, CLI/entrypoints/``__main__``, console scripts, signal
handling (SIGTERM/SIGINT), daemonization, PID files, the central
Sentinel systemd service, deployment scripts, TLS termination,
reverse proxy configuration, access/application logging frameworks,
metrics, retry/backoff, queues, incident persistence, dedupe/flap
suppression, schema changes/migrations, service monitoring, Hermes
integration, remote remediation and Stage F production hardening.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from http.server import HTTPServer
from types import TracebackType

from hermes_sentinel.health_engine import HealthEngine
from hermes_sentinel.http_api import HeartbeatHttpAdapter
from hermes_sentinel.http_server import create_heartbeat_http_server
from hermes_sentinel.ingestion import HeartbeatIngestor
from hermes_sentinel.monitoring import MonitoringCycle
from hermes_sentinel.notifications import NotificationCoordinator
from hermes_sentinel.persistence import HeartbeatRepository, connect
from hermes_sentinel.runtime import SentinelRuntime, StopPredicate
from hermes_sentinel.settings import CentralSettings
from hermes_sentinel.telegram import TelegramSender
from hermes_sentinel.wire import AuthenticatedHeartbeatAdapter

__all__ = [
    "SentinelApplication",
    "build_application",
]


def _close_rolling_back(close: Callable[[], None]) -> None:
    """Attempt exactly one rollback cleanup, swallowing its failure.

    A secondary cleanup failure must never replace the ORIGINAL
    construction failure that is already escaping, so it is swallowed
    here — by contrast, an explicit ``close()`` after a successful
    build always surfaces cleanup failures.
    """
    try:
        close()
    except BaseException:
        pass


class SentinelApplication:
    """The owned central Sentinel application (Stage E7).

    The composition result of :func:`build_application`: the composed
    E5 runtime plus the two bound resources the composition opened
    (the B5 heartbeat listener and the B1 SQLite connection). The
    application owns exactly their cleanup; it starts nothing by
    itself and adds no scheduling, retry or error translation.
    """

    __slots__ = ("_connection", "_heartbeat_server", "_runtime")

    def __init__(
        self,
        *,
        runtime: SentinelRuntime,
        heartbeat_server: HTTPServer,
        connection: sqlite3.Connection,
    ) -> None:
        self._connection = connection
        self._heartbeat_server = heartbeat_server
        self._runtime = runtime

    @property
    def runtime(self) -> SentinelRuntime:
        """The composed E5 runtime this application delegates to."""
        return self._runtime

    @property
    def heartbeat_server(self) -> HTTPServer:
        """The owned bound B5 heartbeat listener."""
        return self._heartbeat_server

    @property
    def connection(self) -> sqlite3.Connection:
        """The owned B1 SQLite connection."""
        return self._connection

    def run_forever(self, should_stop: StopPredicate | None = None) -> None:
        """Thin delegation to the accepted E5 runtime loop.

        The stop predicate (including ``None``) passes through exactly
        as supplied. No new scheduling, retry, error translation or
        loop semantics; runtime exceptions propagate unchanged.
        """
        self._runtime.run_forever(should_stop)

    def close(self) -> None:
        """Close both owned resources; never swallow cleanup failures.

        The listener is closed through the standard B5
        ``server_close()`` — ``shutdown()`` is deliberately NOT
        called: the accepted E5 loop never runs ``serve_forever()``
        and owns no serve-loop thread. Both cleanups are attempted
        even if the first one fails, and a cleanup failure propagates
        (when both fail, the later one surfaces with the earlier
        attached as context).
        """
        try:
            self._heartbeat_server.server_close()
        finally:
            self._connection.close()

    def __enter__(self) -> SentinelApplication:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        # Secret safety: owned collaborator types only — no settings,
        # no tokens, no addresses, no configuration values.
        return (
            f"<{type(self).__name__}"
            f" runtime={type(self._runtime).__name__}"
            f" heartbeat_server={type(self._heartbeat_server).__name__}"
            f" connection={type(self._connection).__name__}>"
        )


def build_application(settings: CentralSettings) -> SentinelApplication:
    """Compose the accepted B1–E6 components into one application.

    Performs composition only: opens the configured database, binds
    the configured listener, constructs each accepted collaborator
    exactly once and returns the application that owns the runtime,
    the listener and the connection. The application is NOT started —
    calling :meth:`SentinelApplication.run_forever` (or entering it as
    a context manager and calling ``run_forever`` inside) is the
    caller's explicit decision.

    If construction fails after a resource has been opened, rollback
    closes the opened resource(s) and the ORIGINAL construction
    failure escapes unchanged.
    """
    # Side effect 1 of 2: open (or create) and initialize the
    # configured SQLite database through the accepted B1 connect().
    connection = connect(settings.database_path)
    try:
        repository = HeartbeatRepository(connection=connection)
        ingestor = HeartbeatIngestor(
            config=settings.config, repository=repository
        )
        authenticated = AuthenticatedHeartbeatAdapter(
            config=settings.config,
            credentials=settings.credentials,
            ingestor=ingestor,
        )
        http_adapter = HeartbeatHttpAdapter(wire=authenticated)
        # Side effect 2 of 2: bind the configured B5 heartbeat
        # listener (bind failures raise before a server exists).
        heartbeat_server = create_heartbeat_http_server(
            adapter=http_adapter,
            host=settings.listen_host,
            port=settings.listen_port,
        )
        try:
            # The write path above and the read path below observe the
            # SAME repository instance on the SAME connection.
            engine = HealthEngine(
                config=settings.config, repository=repository
            )
            sender = TelegramSender(settings=settings.telegram)
            coordinator = NotificationCoordinator(sender=sender)
            monitoring_cycle = MonitoringCycle(
                config=settings.config,
                engine=engine,
                coordinator=coordinator,
            )
            runtime = SentinelRuntime(
                heartbeat_server=heartbeat_server,
                monitoring_cycle=monitoring_cycle,
                monitor_interval_seconds=settings.monitor_interval_seconds,
                poll_interval_seconds=settings.poll_interval_seconds,
            )
            return SentinelApplication(
                runtime=runtime,
                heartbeat_server=heartbeat_server,
                connection=connection,
            )
        except BaseException:
            # The listener is already bound: attempt its cleanup, then
            # let the outer boundary close the connection.
            _close_rolling_back(heartbeat_server.server_close)
            raise
    except BaseException:
        _close_rolling_back(connection.close)
        raise
