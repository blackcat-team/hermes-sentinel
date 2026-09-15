# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage D1 (deterministic health signals core)
on top of the Stage C host reporter and the Stage B heartbeat
pipeline.

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

Stage D1 adds the deterministic health signals core
(`src/hermes_sentinel/health.py`): heartbeat freshness
MISSING/FRESH/STALE evaluated on the central `received_at` time
axis, and resource threshold breach signals
(CPU/RAM/swap/disk/inodes/load5, equality is a breach, canonical
breach order). It is a pure signal layer: it does not yet decide
HEALTHY/DEGRADED/DOWN, does not yet probe external TCP reachability
and applies no debounce/hysteresis — those belong to the remaining
Stage D units (D2 external TCP probe, D3 host state resolver with
debounce/hysteresis, D4 health engine orchestration), which are not
started.

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
