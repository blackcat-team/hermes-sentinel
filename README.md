# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage E (incidents + Telegram) on top of the
Stage D health engine, the Stage C host reporter and the Stage B
heartbeat pipeline.

Stage C is CLOSED in repository: the Stage C1 Linux telemetry
collector, the Stage C2 HTTPS one-shot transport and the Stage C3
systemd timer/packaging. Executing `scripts/sentinel-report.sh` is
the complete one-shot
reporter (the exact C1 heartbeat JSON, exactly ONE outbound HTTPS
POST via `curl`, token in the `X-Sentinel-Token` header, only HTTP
204 is success, no retries), packaged for a monitored Ubuntu host as
declarative systemd units (`packaging/systemd/**` — a oneshot
`hermes-sentinel-reporter.service` plus a ~60-second
`hermes-sentinel-reporter.timer`) with a dedicated unprivileged
runtime user, an `EnvironmentFile` configuration boundary and the
operator runbook [docs/REPORTER_DEPLOYMENT.md](docs/REPORTER_DEPLOYMENT.md).
Deployment is a local operator action — this repository state is
packaging only, not a claim of a deployed or production-verified
installation.

Stage D1 is CLOSED_GREEN: the deterministic health signals core
(`src/hermes_sentinel/health.py`) — heartbeat freshness
MISSING/FRESH/STALE evaluated on the central `received_at` time
axis, and resource threshold breach signals
(CPU/RAM/swap/disk/inodes/load5, equality is a breach, canonical
breach order). It is a pure signal layer with no state decisions.

Stage D2 is CLOSED_GREEN: the external TCP reachability evidence
primitive (`src/hermes_sentinel/reachability.py`) — one bounded
probe that answers exactly "can Sentinel establish a TCP connection
to the configured external target?" via the standard-library
`socket.create_connection`. It returns only REACHABLE / UNREACHABLE
raw evidence with no state decisions, no debounce/hysteresis and no
memory between calls.

Stage D3 is CLOSED_GREEN: the pure deterministic host-state state
machine (`src/hermes_sentinel/state_resolver.py`). It combines the
already-computed D1/D2 evidence (heartbeat freshness, resource
assessment, TCP reachability) with the explicit previous resolution
and the existing confirmation settings into
HEALTHY/DEGRADED/DOWN with debounce/hysteresis: HEALTHY iff fresh +
reachable + resources clear outside DOWN hysteresis; DOWN requires
MISSING/STALE heartbeat AND unreachable TCP confirmed by exactly
`down_confirmations` consecutive qualifying observations; a
confirmed DOWN is held until exactly `recovery_confirmations`
consecutive REACHABLE probes (the recovery target is recomputed from
current evidence). The immutable `HostStateResolution` is both the
resolved state and the explicit memory fed into the next pure call —
no engine, no per-host registry, no transitions, no clock and no I/O
in D3.

Stage D4 is CLOSED_GREEN: the bounded health engine
`HealthEngine.evaluate_host(host)` composes the frozen D1/D2/D3
contracts and the B1 repository read into one immutable
`HostHealthEvaluation` per call, in exactly the documented order:
configured-host check (`UnknownHostError` before any activity), one
central clock call validated as truly timezone-aware
(`InvalidClockResultError` otherwise, never coerced), exactly one
`latest_heartbeat` read (D4 never writes), D1 freshness, D1
resources (public `resources=None` when no heartbeat exists — the
D3 resolver internally receives the neutral non-degrading input),
exactly one D2 TCP probe, D3 resolution against the remembered
per-host previous resolution, and a `HostTransition` ONLY when DOWN
is entered or left (first evaluation never emits; ordinary
HEALTHY <-> DEGRADED changes never emit). The per-host resolver
memory is process-local and committed only after the entire
evaluation succeeds — any failure leaves the remembered state
exactly intact, and a restart resets every host. Stage D is
CLOSED_GREEN.

Stage E1 is CLOSED_GREEN: the pure incident domain layer
(`src/hermes_sentinel/incidents.py`) projecting the frozen Stage D
`HostTransition` onto the incident vocabulary — a DOWN event (any
transition into DOWN) maps to `IncidentKind.DOWN`, a RECOVERED event
(any transition out of DOWN) maps to `IncidentKind.RECOVERED`, and
ordinary `HEALTHY <-> DEGRADED` changes map to `None`. The immutable
`Incident` wraps the canonical `HostTransition` directly and
duplicates none of its facts.

Stage E2 is CLOSED_GREEN: the bounded Telegram sender primitive
(`src/hermes_sentinel/telegram.py`) renders the accepted E1
`Incident` as one deterministic plain-text message and performs
exactly one outbound Telegram Bot API `sendMessage` POST to the
fixed `https://api.telegram.org` origin with a sender-only bot —
secret-safe `TelegramDeliveryError` failures, redirects refused, no
retries, HTTP 200 + `"ok": true` required for success, no
`getUpdates`.

Stage E3 is CLOSED_GREEN: the bounded notification coordinator
(`src/hermes_sentinel/notifications.py`) exists as a thin composition
layer. One confirmed D4 `HostTransition` goes in, the E1
`incident_from_transition()` mapper stays the sole
incident-classification authority, and an incident-worthy transition
reaches the injected sender — any object structurally satisfying the
minimal `IncidentSender.send(incident)` protocol, which the E2
`TelegramSender` does unmodified — exactly once. A successful send
returns that exact same `Incident`; a sender failure propagates
unchanged with no retry, no dedupe and no persistence, and the
coordinator owns no transport, health evaluation, configuration
loading or state.

Stage E4 is CLOSED_GREEN: the minimal synchronous one-shot
monitoring cycle (`src/hermes_sentinel/monitoring.py`). `MonitoringCycle(config, engine, coordinator).run()`
iterates the configured hosts in `SentinelConfig.hosts` tuple order,
evaluates each host exactly once through the accepted D4
`HealthEngine.evaluate_host`, passes each exact
`evaluation.transition` to the accepted E3
`NotificationCoordinator.notify_transition` exactly once, and returns
an ordered tuple of immutable `HostMonitoringResult` values pairing
each exact evaluation with the exact `Incident | None` the
coordinator returned (object identity preserved; a `None` transition
stays `None`). An empty host configuration is a valid no-op. Failures
are deliberately simple: an exception from the engine or coordinator
propagates unchanged and the remaining hosts of that run are not
processed — no retry, no backoff, no error swallowing.

Stage E5 is CLOSED_GREEN: the single-threaded cooperative central
runtime loop (`src/hermes_sentinel/runtime.py`).
`SentinelRuntime(heartbeat_server, monitoring_cycle,
monitor_interval_seconds, monotonic=...,
poll_interval_seconds=...).run_forever(should_stop=...)` lets ONE
thread serve the accepted B5 serial heartbeat server through its
standard one-request `handle_request()` mechanism while running the
accepted E4 `MonitoringCycle.run()` on an injected monotonic
schedule: the first cycle is due immediately, the next is anchored to
each cycle's completion time (a long delay performs exactly one
cycle — no catch-up bursts), at most one heartbeat request is
serviced per cooperative iteration, and the accept-wait timeout is
clamped to be non-negative and never above the poll interval or the
remaining time to the next cycle — so continuous heartbeat traffic
cannot starve monitoring. Interval values are validated fail-fast; a
stop predicate is injectable for deterministic tests and later
lifecycle wiring (no signal handling yet); exceptions propagate
unchanged with no retry. All collaborators are injected — the
runtime constructs nothing and closes nothing.

Stage E6 is CLOSED_GREEN: the bounded central application
settings loader (`src/hermes_sentinel/settings.py`).
`load_central_settings(env)` — the caller supplies
the environment mapping explicitly, never an implicit `os.environ`
read — snapshots it and strictly parses it into one immutable
`CentralSettings` value holding exactly the already-accepted
configuration objects the composition root needs:
`SentinelConfig` (from the `SENTINEL_HOSTS_JSON` host array — strict
JSON with no unknown or duplicate keys, no bool-as-number and no
non-finite constants), `NodeCredentials` (from
`SENTINEL_NODE_TOKENS_JSON`, with the credential node set required
to match the configured hosts exactly and duplicate tokens rejected
through the accepted credential contract), `TelegramSettings`
(including the optional message thread id and timeout), the database
path, the listen host/port and the E5 monitor/poll intervals. Every
malformed or missing input fails through the bounded
`CentralSettingsError` with secret-safe messages that never echo
node tokens or the Telegram bot token. The loader constructs no
server, repository, engine, sender, coordinator, cycle or runtime.

Stage E7 is CLOSED_GREEN: the minimal central application
composition root (`src/hermes_sentinel/application.py`).
`build_application(settings)` consumes one already-loaded
`CentralSettings` value (it never reads the process environment or
re-validates settings) and constructs the accepted B1–E6 components
exactly once each into ONE owned application: one B1 SQLite
connection, ONE `HeartbeatRepository` shared by both the B2
ingestor and the D4 health engine, the B3/B4/B5 heartbeat listener
chain bound to the configured host/port, and the E2/E3/E4/E5
monitoring-and-runtime chain — returned as a `SentinelApplication`
owning the composed runtime, the bound listener and the connection.
Its only two side effects are the database open and the listener
bind: nothing is started, served, monitored or sent by construction.
`run_forever(should_stop=...)` is thin delegation to the accepted E5
runtime; `close()` (or context-manager exit) closes the owned
listener and connection without swallowing cleanup failures; a
construction failure after a resource is opened rolls back every
opened resource while the original failure still escapes.

Stage E8 is the current stage: the minimal central process lifecycle
/ entrypoint (`src/hermes_sentinel/process.py`) now exists in this
candidate. `run_process(env)` connects the accepted layers — the
exact environment mapping to E6 `load_central_settings`, the exact
resulting settings to E7 `build_application`, the composed
application to one `run_forever` call — and installs cooperative
SIGTERM/SIGINT handlers that do nothing but mark a process-owned
stop request (the accepted E5 loop notices it at its next bounded
loop boundary and returns cooperatively; no force-kill
second-signal policy). Previous signal handlers are captured and
restored on every exit path, with partial-installation rollback;
application cleanup is guaranteed by the E7 context-manager
lifecycle and E8 owns no resource cleanup of its own. Startup and
runtime failures propagate unchanged — no retries, no exit-code
translation, nothing swallowed. After installing the package the
central process is launchable through the one console entrypoint
`hermes-sentinel` (`hermes_sentinel.process:main`), which is also
the only place production code implicitly reads the process
environment. This is a process boundary, not a production
deployment claim: the central Sentinel systemd unit,
EnvironmentFile packaging, deployment tooling and Stage F production
hardening remain outstanding later units. Stage E is not complete
and the MVP is not complete.

Stage B5 is a plaintext backend listener
(`http.server.HTTPServer` + `BaseHTTPRequestHandler`, stdlib raw
HTTP parsing only). It is not a production Internet-facing
endpoint: production reporters use outbound HTTPS; TLS termination
belongs to later hardening.

- Architecture contracts (product, security, host state semantics,
  persistence, roadmap): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Development environment and verification commands:
  [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
- Contribution rules: [CONTRIBUTING.md](CONTRIBUTING.md)

## Runtime

Python 3.11, src layout: package code lives in `src/hermes_sentinel`.
