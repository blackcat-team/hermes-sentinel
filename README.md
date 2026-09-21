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

Stage E4 is the current stage: the minimal synchronous one-shot
monitoring cycle (`src/hermes_sentinel/monitoring.py`) now exists in
this candidate. `MonitoringCycle(config, engine, coordinator).run()`
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
processed — no retry, no backoff, no error swallowing. The
long-running daemon/runtime loop, scheduling/polling, the central
Sentinel systemd unit, configuration/Telegram settings wiring and
production hardening are still NOT implemented: those are later Stage
E/F roadmap units. Stage E is not complete and the MVP is not
complete.

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
