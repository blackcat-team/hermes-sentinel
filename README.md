# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage C3 (systemd Reporter Timer / Packaging)
on top of the Stage C2 HTTPS one-shot reporter transport
(`scripts/sentinel-report.sh`), the Stage C1 Linux host telemetry
collector, the Stage B5 heartbeat HTTP server bridge, the Stage B4
heartbeat HTTP request adapter, the Stage B3 authenticated heartbeat
wire boundary, the Stage B2 heartbeat ingestion core and the Stage B1
SQLite persistence foundation.

Stage C now consists of the C1 collector, the C2 HTTPS one-shot
transport and the C3 systemd timer/packaging: executing
`scripts/sentinel-report.sh` is the complete one-shot reporter (the
exact C1 heartbeat JSON, exactly ONE outbound HTTPS POST via `curl`,
token in the `X-Sentinel-Token` header, only HTTP 204 is success, no
retries), and Stage C3 packages it for a monitored Ubuntu host as
declarative systemd units (`packaging/systemd/**` — a oneshot
`hermes-sentinel-reporter.service` plus a ~60-second
`hermes-sentinel-reporter.timer`) with a dedicated unprivileged
runtime user, an `EnvironmentFile` configuration boundary and the
operator runbook [docs/REPORTER_DEPLOYMENT.md](docs/REPORTER_DEPLOYMENT.md).
Deployment is a local operator action — this repository state is
packaging only, not a claim of a deployed or production-verified
installation.

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
