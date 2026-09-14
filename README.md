# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage C1 (Linux Host Telemetry Collector Core,
`scripts/sentinel-report.sh`) on top of the Stage B5 heartbeat HTTP
server bridge, the Stage B4 heartbeat HTTP request adapter, the Stage
B3 authenticated heartbeat wire boundary, the Stage B2 heartbeat
ingestion core and the Stage B1 SQLite persistence foundation.

Stage C1 is a one-shot bash collector that reads local Linux
telemetry (/proc, df) and prints exactly one B3 heartbeat JSON
document. It performs no network delivery — that is Stage C2, and
the systemd timer/packaging is Stage C3.

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
