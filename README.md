# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage D3 (host state resolver +
debounce/hysteresis) on top of the Stage D1/D2 health signal and
reachability primitives, the Stage C host reporter and the Stage B
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

Stage D3 is the current stage: the pure deterministic host-state
state machine (`src/hermes_sentinel/state_resolver.py`). It combines
the already-computed D1/D2 evidence (heartbeat freshness, resource
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
in D3. Health engine orchestration (D4) — evidence acquisition,
per-host retention of previous resolutions and `HostTransition`
emission — is not started.

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
