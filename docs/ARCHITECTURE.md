# Hermes Sentinel — Architecture Contracts

Status: **authoritative** (fixed at Stage A1). Changes to the FROZEN
sections require an explicit roadmap decision, not a drive-by edit.

Hermes Sentinel is a standalone server-first monitoring service. It
answers one primary question:

> Не умер ли один из моих VDS, и не приближается ли он к проблемному
> состоянию?

(Is one of my VDS boxes dead, or approaching a bad state?)

## 1. Product boundary

In scope (MVP):

- determine whether a server/host is alive;
- determine whether it is reachable from the outside;
- determine whether its heartbeat is fresh;
- determine whether CPU / RAM / swap / load / disk / inodes are healthy;
- detect DOWN and RECOVERED transitions.

Out of scope: everything else — see the roadmap in section 9.

## 2. Server-first MVP boundary

The monitored unit is a **server/host**, not an application. The MVP
must determine, for each configured host:

- alive / not alive;
- externally reachable / not reachable;
- heartbeat fresh / stale;
- resources healthy / breached;
- whether a DOWN or RECOVERED transition has occurred.

Mandatory per-host telemetry (the host reporter payload contract,
`hermes_sentinel.domain.HostTelemetry`):

| Field            | Meaning                                    |
|------------------|--------------------------------------------|
| `timestamp`      | timezone-aware report time                 |
| `uptime_seconds` | host uptime                                |
| `load`           | load average (1/5/15 min windows)          |
| `cpu_percent`    | CPU usage                                  |
| `ram`            | RAM usage (bytes, used/total/percent)      |
| `swap`           | swap usage (bytes, used/total/percent)     |
| `root_filesystem`| filesystem `/` usage (bytes)               |
| `root_inodes`    | inode usage on `/` (counts)                |

Systemd service monitoring is **not** part of the MVP (see section 10,
Stage I). The architecture may support an optional services list, and
`services=[]` is a normal production configuration, but host state
semantics never consult services.

## 3. Observability-only security boundary — FROZEN

Sentinel is **observability only**. The following are permanently
forbidden:

- SSH access to monitored hosts;
- remote shell execution;
- sudo;
- `systemctl start/stop/restart` (or any service manipulation);
- reboot;
- remote file modification;
- deployment actions against monitored hosts;
- remediation of any kind;
- arbitrary command execution.

There are **no control credentials** for monitored hosts and **no
control endpoints**. Sentinel only receives telemetry and performs
read-only external checks.

## 4. Host state semantics

States: `HEALTHY`, `DEGRADED`, `DOWN`
(`hermes_sentinel.domain.HostState`).

Normative rules:

- resource problems may produce `DEGRADED`;
- resource problems alone **never** produce `DOWN`;
- heartbeat lost while the host is still TCP-reachable => `DEGRADED`;
- `DOWN` requires heartbeat lost/stale **and** external TCP failure,
  confirmed by debounce/hysteresis (a configurable number of
  consecutive failed probes);
- service state never participates in `HEALTHY`/`DEGRADED`/`DOWN`.

Transitions (`hermes_sentinel.domain.HostTransition`):

- a **DOWN event** is any transition into `DOWN`;
- a **RECOVERED event** is any transition out of `DOWN`
  (`DOWN -> HEALTHY` or `DOWN -> DEGRADED`).

## 5. Telegram sender-only isolation

Sentinel (Stage E) uses a **dedicated Telegram bot token** and acts
only as a **sender**. It never reads updates (`getUpdates` is not
part of the design). This keeps credential isolation clean; note that
credential isolation is not failure-domain isolation.

## 6. Failure domain

In the MVP the central Sentinel may live on the same VDS as the Hermes
Agent. This gives software/runtime independence but a **shared host
failure domain**: if that VDS dies, both the monitor and the monitored
agent die together. Stage H (EXTERNAL DEAD-MAN, required post-MVP)
must close this gap.

## 7. Independence from Hermes — FROZEN

- Sentinel is not a part of Hermes Agent;
- Hermes Agent is absent from the monitoring/alert path;
- Sentinel does not depend on the Hermes virtual environment;
- Sentinel does not depend on the Hermes Gateway;
- Sentinel and Hermes are independently restartable;
- the number of Sentinel files inside the Hermes core checkout is 0;
- the number of Hermes core changes caused by Sentinel is 0;
- do not design changes to `gateway/run.py`, `gateway/platforms/base.py`
  or any other Hermes core code.

This separation is **update-neutral**: updating either system must
never require touching the other.

## 8. Authoritative roadmap

```
A FOUNDATION
B HEARTBEAT + SQLITE
C HOST REPORTER
D HOST HEALTH ENGINE + EXTERNAL REACHABILITY
E INCIDENTS + TELEGRAM
--- MVP ---
F PRODUCTION HARDENING
G HERMES READ-ONLY INTEGRATION (optional)
H SENTINEL EXTERNAL DEAD-MAN (required post-MVP)
I OPTIONAL SERVICE MONITORING / HISTORY / UI
```

Stage A1 delivers only the foundation: the Python package skeleton
under `src/hermes_sentinel`, the typed domain/config contracts, and
this document. Heartbeat API, SQLite persistence, network probes,
the health engine, incidents and Telegram delivery are implemented by
stages B–E and are deliberately absent here.

## 9. Stage A1 scope guardrails

- No speculative infrastructure; no heavy dependencies.
- No early implementation of future stages.
- Only ordinary tracked project files are touched (README, docs,
  pyproject, `src/hermes_sentinel`, tests, `.env.example`).
- `.kilo/`, `.blackcat/`, `.kilocodeignore`, Git configuration, the
  Hermes repository and deployments are never modified.

## 10. Persistence foundation (Stage B1)

Heartbeat observations are persisted in SQLite via the Python stdlib
`sqlite3` module — no ORM and no migration framework. The contract
(`hermes_sentinel.persistence`):

- one row per accepted heartbeat in `heartbeat_observation`;
- node identity is an explicit `node` column; every lookup filters
  by it, so telemetry of different servers can never mix;
- a node identity must be a non-empty string after `strip()`;
  whitespace-only identities are rejected at the persistence
  boundary. Names are stored and compared verbatim — no
  normalization, no case folding ("Prod" and "prod" stay distinct);
- both the server-reported timestamp and the central received
  timestamp are stored;
- the full mandatory `HostTelemetry` payload (uptime, load 1/5/15,
  CPU, RAM, swap, filesystem `/`, inodes on `/`) is stored per
  observation without field loss;
- timezone-aware datetimes are canonically serialized as UTC ISO 8601
  text, so textual ordering in SQLite is chronological and
  unambiguous; reads restore timezone-aware datetimes with the same
  instant (offsets are normalized to UTC);
- "latest heartbeat" is deterministic: `ORDER BY received_at DESC,
  id DESC` — on a full timestamp tie the row inserted last wins;
- all statements are parameterized; SQL text is assembled only from
  module-level constants, never from user values;
- schema bootstrap is deterministic and idempotent, safe on repeated
  application restarts, and **fail-closed**: compatibility of any
  pre-existing database is proven at bootstrap time (PRAGMA
  introspection against the canonical column/index contract), never
  deferred to the first insert. A database whose stored objects are
  incompatible with the canonical B1 schema, or whose
  `user_version` is newer than supported, is explicitly rejected
  before any write — never silently accepted, downgraded or
  destructively altered;
- retention cleanup and full migrations are out of scope for B1.

Stage B1 does not compute HEALTHY / DEGRADED / DOWN and does not
decide heartbeat freshness; those belong to stages B/D.

## 11. Heartbeat ingestion core (Stage B2)

Heartbeats reach persistence through a transport-independent
application operation (`hermes_sentinel.ingestion.HeartbeatIngestor`):

    reporter / future HTTP transport
                |
    HeartbeatIngestor
                |
    B1 HeartbeatRepository -> SQLite

B2 is deliberately not an HTTP/API transport. The contract:

- input is the Stage A `HostTelemetry`; no competing telemetry model;
- the reported node must be a configured Sentinel node, looked up via
  the existing `SentinelConfig.host()` contract — verbatim,
  case-sensitive, no normalization or case folding. An unknown node
  fails closed with an explicit error **before any write**;
- the central `received_at` moment is assigned by the Sentinel
  ingestion layer via an injectable clock (production default:
  timezone-aware UTC). The reporter never defines the authoritative
  receive moment. A clock result must be truly timezone-aware
  (`tzinfo is not None` AND `utcoffset() is not None`), matching the
  B1 timestamp rule — otherwise the observation is rejected without
  a write;
- the server-reported `telemetry.timestamp` is a separate time axis
  (`reported_at`), stored alongside `received_at` and never replaced
  by it;
- server-reported telemetry passes through ingestion unmodified;
- persistence goes exclusively through the B1 `HeartbeatRepository`;
  repository failures propagate and are never masked as successful
  receipts;
- a successful insert returns a minimal typed receipt: node,
  persistence observation id, `received_at`;
- every accepted heartbeat is a separate observation — B2 defines no
  deduplication/idempotency protocol;
- `services` (present or empty) never participates in ingestion
  semantics.

Stage B2 does not compute HEALTHY / DEGRADED / DOWN, does not decide
heartbeat freshness, and applies no clock-skew acceptance policy;
those belong to stages B/D. HTTP transport, reporter authentication
and tokens belong to later stages.

## 12. Authenticated heartbeat wire contract (Stage B3)

An external wire/auth boundary sits in front of the B2 ingestion core
(`hermes_sentinel.wire`):

    future HTTP POST (Stage B4)
                |
    AuthenticatedHeartbeatAdapter.handle(payload, token)
                |
    HostTelemetry -> HeartbeatIngestor (B2) -> SQLite (B1)

B3 is deliberately transport-neutral: the adapter accepts an already
decoded `Mapping[str, object]` plus a separately presented node token.
JSON bytes/string parsing, Content-Type/header semantics and the HTTP
server lifecycle belong to Stage B4 and are absent here.

Wire payload contract — exactly the mandatory telemetry MVP fields
and nothing else: `node`, `reported_at` (ISO 8601, timezone-aware),
`uptime_seconds`, `load` (`one`/`five`/`fifteen`), `cpu_percent`,
`ram`, `swap`, `root_fs`, `root_inodes` (each `used`/`total`/
`percent`). The payload never carries `received_at` (exclusive
responsibility of the B2 central clock), health state, token or
incident data.

Decoding is strict and fail-closed (`decode_heartbeat_payload`):
non-mapping payloads, missing or unknown/extra fields (top-level or
nested — no wire versioning yet, so extras are rejected), wrong
scalar types, numeric strings, bools-as-numbers, malformed or naive
`reported_at`, and invalid/whitespace-only `node` are all rejected
before ingestion. No silent coercions; values are decoded into the
typed Stage A domain models so their invariants apply.

Authentication is per-node and fail-closed: each monitored node has
its own secret token; the presented token is verified against the
credential of the claimed node only (never "any known token"), with
the stdlib constant-time `hmac.compare_digest` (verbatim, no
stripping/normalization; node identity stays case-sensitive like
B1/B2). Unknown nodes, configured nodes without a credential, wrong,
empty, whitespace-only or non-string tokens all fail with the same
generic external `HeartbeatAuthenticationError` — no identity
enumeration, no secret disclosure. A credential set
(`NodeCredentials`) is an in-memory runtime mapping, rejects a
duplicate token assigned to two different nodes at construction, and
never exposes token values in `repr()` or error texts. Tokens are
never logged, never persisted in SQLite, never included in receipts
or exception messages. Authentication and wire decode failures
happen strictly before the B2 clock and any write; repository/B2
failures after a successful authentication propagate unchanged.

Out of scope for B3: HTTP listener, headers, JSON byte parsing, TLS,
credential file/env loaders, token rotation, rate limiting, replay
protection, clock-skew policy, deduplication, heartbeat freshness
and HEALTHY/DEGRADED/DOWN computation.

## 13. Heartbeat HTTP request contract (Stage B4)

A minimal stdlib-only HTTP request adapter sits in front of the B3
wire boundary (`hermes_sentinel.http_api`):

    future HTTP listener (Stage B5)
                |
    HeartbeatHttpAdapter.handle(HttpRequest) -> HttpResponse
                |
    strict UTF-8 JSON Mapping + X-Sentinel-Token header value
                |
    AuthenticatedHeartbeatAdapter (B3) -> B2 -> B1

B4 starts no network listener and parses no raw HTTP bytes: the
socket, connection lifecycle and wire-format parsing belong to a
later stage. B4 defines only the request/response semantics of the
single heartbeat endpoint over a minimal typed request/response
model (`HttpRequest`/`HttpResponse` — header pairs are tuples, not
dicts, so duplicate headers stay deterministically detectable).

Endpoint: exactly `POST /v1/heartbeat` — no path normalization of
any kind (trailing slash, query string and percent-encoded
alternates are 404); the right path with a wrong method is 405 plus
`Allow: POST` (methods are case-sensitive). The reporter token
travels in its own `X-Sentinel-Token` header (never in the JSON
payload), must appear exactly once (case-insensitive name matching),
and is forwarded to B3 verbatim — B3 stays authoritative for token
shape, comparison and per-node binding.

Body: raw bytes with a hard limit of 16 KiB
(`MAX_HEARTBEAT_BODY_BYTES`). Larger is 413, empty is 400;
socket-level pre-read enforcement is a Stage B5 responsibility.
Content-Type must be exactly `application/json`, optionally
`; charset=utf-8` (tokens compared case-insensitively):
missing/unsupported is 415, malformed or duplicated is 400 — no
permissive guessing. A malformed request structure (wrong field
types, CR/LF or other control characters in header names/values)
fails closed with 400.

JSON decoding is strict: invalid UTF-8, malformed JSON, non-object
roots, duplicate object keys at any nesting depth, the
NaN/Infinity/-Infinity constants and pathologically deep nesting
that exhausts the stdlib parser recursion budget (`RecursionError`)
are all 400 — deep nesting is malformed/unprocessable client JSON
at the B4 parsing boundary, not an internal server failure. The
decoded mapping reaches B3 without semantic mutation; wire schema
validation stays in B3.

Responses are deterministic: success is 204 with an empty body (the
B3 receipt is not exposed to the HTTP surface); known client
failures map to 404/405 (+`Allow: POST`)/413/415/400/401 — always
with an empty body and `Content-Length: 0`, never containing
exception text, token material or node existence details.
`HttpRequest.__repr__` is a compile-time constant (never header
values, body content or any caller-controlled field repr — secret
safety and totality on hostile values). After successful HTTP-level
parsing B4 catches only the two expected
B3 external input errors (`MalformedHeartbeatPayloadError` -> 400,
`HeartbeatAuthenticationError` -> 401); the only additional catch
is `RecursionError`, and only inside the bounded JSON parsing
boundary (client 400). Any other internal exception — repository,
B2 or B3 internals, `MemoryError` — propagates unchanged to the
future server boundary, which owns the
generic 500 mapping and logging policy.

Out of scope for B4: socket bind/listen, raw HTTP parsing,
WSGI/ASGI frameworks, TLS, Content-Length pre-read enforcement,
keep-alive, timeouts, credential loaders, token rotation, replay
protection and rate limiting.
