# Hermes-sentinel

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question: is one of my VDS boxes dead, or
approaching a bad state?

Sentinel is **observability only**: it receives telemetry and performs
read-only external checks. It never executes commands on, deploys to,
or modifies monitored hosts.

## Status

Initial development — Stage C2 (HTTPS One-shot Reporter Transport,
`scripts/sentinel-report.sh`) on top of the Stage C1 Linux host
telemetry collector, the Stage B5 heartbeat HTTP server bridge, the
Stage B4 heartbeat HTTP request adapter, the Stage B3 authenticated
heartbeat wire boundary, the Stage B2 heartbeat ingestion core and
the Stage B1 SQLite persistence foundation.

Stage C2 makes executing `scripts/sentinel-report.sh` the complete
one-shot reporter: it collects the exact C1 heartbeat JSON and
performs exactly ONE outbound HTTPS POST (`curl` is the single added
monitored-host dependency) to the configured endpoint
(`SENTINEL_ENDPOINT`, HTTPS-only, used verbatim) with the reporter
token in the `X-Sentinel-Token` header — never in the payload, never
in curl argv. Only HTTP 204 is success; redirects are never followed;
there is no retry loop, no daemon and no local spool. The systemd
timer/packaging is Stage C3.

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
