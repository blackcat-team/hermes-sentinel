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

    HTTPServer + BaseHTTPRequestHandler (Stage B5)
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

## 14. Heartbeat HTTP listener (Stage B5)

A minimal stdlib bridge binds the B4 request semantics to a real
network endpoint (`hermes_sentinel.http_server`): the server is
exactly `http.server.HTTPServer` (serial — NOT
`ThreadingHTTPServer`), and ALL raw HTTP parsing is the stdlib
`BaseHTTPRequestHandler` parser. B5 implements NO custom raw HTTP
protocol parser of its own:

    TCP client (reporter, plaintext)
                |
     HTTPServer + BaseHTTPRequestHandler (B5 bridge:
     stdlib raw HTTP parsing only)
                |
     HeartbeatHttpAdapter.handle(HttpRequest) -> HttpResponse   (B4)
                |
     AuthenticatedHeartbeatAdapter (B3) -> B2 -> B1 -> SQLite

B5 owns only the transport/framing bridge; routing, method,
header, body, JSON and authentication semantics stay exclusively
in B4/B3 — B5 implements no second HTTP semantics layer and never
inspects the token.

**Plaintext boundary (FROZEN)**: B5 is a plaintext *backend*
listener. It is NOT a production Internet-facing endpoint.
Production reporters reach Sentinel through outbound HTTPS only;
TLS termination and network exposure belong to later production
hardening/deployment stages (Stage F). TLS is deliberately not
implemented here.

- server API: `create_heartbeat_http_server(adapter, host, port)`
  returns a ready-to-serve `HTTPServer` subclass; `port=0` is
  allowed (ephemeral port discovery via `server_address`);
  `serve_forever`/`shutdown`/`server_close` stay the standard
  lifecycle; there is no global singleton;
- one request per connection: after the response the connection is
  closed (`Connection: close` always). No keep-alive, no
  pipelining. B5 deliberately introduces NO production connection
  timeout policy — that is Stage F / deployment hardening scope;
- routing stays in B4. For a wrong path OR a wrong method the body
  is NOT read (including announced-but-unsent bodies): the request
  is handed to B4 with `body=b""` and B4 decides 404/405;
- ANY syntactically accepted method reaches B4 (generic `do_*`
  dispatch via `__getattr__`): e.g. `BREW /v1/heartbeat` becomes
  B4's 405 + `Allow: POST` — never the stdlib default 501;
- ANY `Expect` header on the exact heartbeat POST is rejected:
  417, empty body, no interim 100, body never read, B4/B3/B2/B1
  never invoked (zero persistence rows). PRESENCE ALONE is
  authoritative — expectation values are never inspected,
  normalized or parsed. The stdlib `handle_expect_100` hook only
  covers the specific `100-continue` case, so the framing path
  detects ANY Expect presence via the multiplicity-preserving
  stdlib `headers.get_all`. On a wrong route/method Expect never
  makes the server wait for the body and never disturbs the
  authoritative B4 404/405 behavior (no 100 is ever sent);
- framing is required only for the exact `POST /v1/heartbeat`: a
  missing Content-Length is 411; a duplicated (case-insensitively,
  via the multiplicity-preserving stdlib `headers.get_all`) or
  invalid value — anything but plain `[0-9]+` after surrounding
  OWS: negative, `+10`, comma lists, decimal, hex, empty, internal
  whitespace, Unicode digits — is 400;
- `Transfer-Encoding` present (with or without Content-Length) is
  400; chunked decoding is NOT implemented and the body is never
  read;
- pre-read size limit: a Content-Length whose NUMERIC value is
  above `MAX_HEARTBEAT_BODY_BYTES` (the B4 limit, 16384) is
  answered 413 immediately — WITHOUT reading the oversized body
  off the socket and WITHOUT converting the digit string with an
  unbounded `int()` (Python 3.11+'s integer-string digit safety
  limit makes that an uncaught `ValueError`; the comparison uses
  the significant decimal representation, so leading zeroes are
  numerically insignificant: `"0"*5000 + "1"` is 1, never 413 for
  textual length). `16384` is not rejected for size alone; `0` is
  forwarded to B4 as `body=b""` (B4 answers 400);
- with a valid Content-Length <= 16384 exactly the declared bytes
  are read; a short EOF / client half-close before the declared
  size is 400 and B4 is never called with a partial body; bytes
  beyond the declared length are never read;
- the B4 `HttpRequest` receives the stdlib-parsed headers as
  `tuple[tuple[str, str], ...]`; multiplicity is preserved for
  Content-Type and X-Sentinel-Token (duplicates stay visible as
  multiple pairs — B4 owns the duplicate semantics). B5 works only
  with the semantic header values the stdlib parser produced: no
  raw-header reconstruction, no token stripping/normalization/
  case-folding;
- response emission relays the actual B4 status/headers/body plus
  `Connection: close`. B5-owned statuses (400/411/413/417/500) are
  always empty-body with `Content-Length: 0`. The stdlib HTML
  error pages are never used: `send_error` is overridden to a
  sanitized empty-body emission (parser-selected statuses are
  kept; no raw request line, header/token/body values or exception
  text is ever reflected);
- internal 500 boundary: B4 deliberately propagates unexpected
  application exceptions. B5 catches `Exception` — ONLY around the
  `HeartbeatHttpAdapter.handle` call, never `BaseException` — and
  answers a deterministic empty-body 500 with `Content-Length: 0`.
  The caught application exception is deliberately NOT logged at
  all (no `logger.exception`, no traceback, no `exc_info`, no
  exception text — Stage F owns observability), so no exception
  message, traceback or potentially sensitive internal data is
  ever emitted. An internal failure is never turned into a 400;
- logging: the standard `BaseHTTPRequestHandler` access logging is
  disabled (`log_message`/`log_error` overridden to emit nothing),
  and the caught application exception around the B4 dispatch is
  never logged either. No path, query, request line, headers,
  token, body, exception message or traceback is ever printed.
  Observability belongs to Stage F;
- version disclosure: no `Server`/`Date` headers carrying
  `BaseHTTP/...` or `Python/...` are emitted — responses use
  `send_response_only` plus explicit safe headers, never the plain
  `send_response`;
- unapproved contracts are deliberately absent: no custom 431 head
  cap, no custom 505 version policy, no custom
  UTF-8/surrogateescape header parser, no custom HTTP grammar, no
  product connection timeout policy. Any status the stdlib parser
  itself generates is sanitized in its response, without adding an
  alternative protocol semantics of our own.

Out of scope for B5: TLS (plaintext backend listener — see the
frozen plaintext boundary above), keep-alive/pipelining, chunked
transfer, compression, access logging, rate limiting, credential
loaders, token rotation, replay protection and deployment
hardening.

## 15. Host reporter collector (Stage C1)

The monitored node's side of the heartbeat contract is a single
one-shot bash script, `scripts/sentinel-report.sh` (Ubuntu Linux /
bash target). The frozen reporter architecture stays lightweight:

    systemd timer (Stage C3)
                |
     one-shot sentinel-report.sh (this stage)
                 |  read local Linux telemetry
                 |  print one heartbeat JSON document
     outbound HTTPS POST (Stage C2 — not part of C1)

C1 performs **no network delivery of any kind** and requires **no
Python, jq or other extra packages** on the monitored host — only
ordinary `/proc`, `df`, `date`, `sleep`, `awk`, `printf` and standard
bash builtins/coreutils. There is deliberately **no second Python
implementation of the collector**: Python appears only in the test
harness (fixture provider, JSON parser, B3 contract oracle).

- **Node identity is configuration, never hostname inference**: the
  script requires `SENTINEL_NODE` and fails non-zero before producing
  any payload when it is absent or invalid. The documented reporter
  constraint on node text is deterministic: groups of ASCII letters,
  digits, dot, underscore and hyphen, separated by single spaces (no
  leading/trailing whitespace, quotes, backslashes, control
  characters or other symbols). An accepted value is emitted verbatim
  as the wire field `node` — no case folding, no trimming, no silent
  mutation ("Prod", "Hermes", "VPN-1", "VPN-2" all stay distinct).
  Tokens are not part of C1 and no secrets exist in this stage.
- **Sources and semantics** (all fail-closed — a measurement is never
  invented, clamped or normalized):
  - `uptime_seconds`: first field of `/proc/uptime`, finite and >= 0;
  - `load` 1/5/15: first three fields of `/proc/loadavg`;
  - `cpu_percent`: interval measurement from the first aggregate
    `cpu` line of `/proc/stat`, two samples 1 second apart, over
    user/nice/system/idle/iowait/irq/softirq/steal (guest and
    guest_nice are never counted a second time): idle_all = idle +
    iowait, non_idle = user + nice + system + irq + softirq + steal,
    cpu_percent = 100 * (total_delta - idle_delta) / total_delta.
    Decreasing counters, total_delta <= 0, or a non-finite / out of
    [0, 100] result fail closed;
  - `ram`: `MemTotal`/`MemAvailable` from `/proc/meminfo`, KiB
    converted to bytes exactly, used = (MemTotal - MemAvailable) —
    deliberately not MemFree. Physical RAM is never an absent
    resource (unlike swap): `MemTotal == 0` fails closed. Duplicate
    or missing required keys, wrong units, MemAvailable outside
    [0, MemTotal] and non-finite (overflowed) values fail closed;
  - `swap`: `SwapTotal`/`SwapFree` (KiB -> bytes); SwapTotal == 0
    (with SwapFree == 0) is the absent resource 0/0/0, inconsistent
    input fails closed;
  - `root_fs`: exactly the `/` filesystem via `LC_ALL=C df -P -B1 /`
    (bytes); `root_inodes`: `LC_ALL=C df -Pi /` (counts). df output is
    validated mode-aware against the expected C-locale structure:
    the header's semantic fields must match (`Filesystem` /
    variable block-size label / `Used` / `Available` / `Capacity` /
    `Mounted on` for bytes; `Filesystem` / `Inodes` / `IUsed` /
    `IFree` / `IUse%` / `Mounted on` for inodes), and the data row
    must have exactly the six `-P` columns with non-negative integer
    total/used/available, an integer 0..100 capacity token with a
    literal `%`, mount exactly `/`, used <= total and available <=
    total. The df capacity value is never trusted as telemetry —
    percent is recomputed from used/total. The script forces
    `LC_ALL=C` so df and number formatting are locale-independent.
- **Output contract**: success is exit 0 with exactly one complete
  heartbeat JSON document on stdout (the exact Stage B3 wire payload
  — `node`, `reported_at`, `uptime_seconds`, `load`, `cpu_percent`,
  `ram`, `swap`, `root_fs`, `root_inodes` and nothing else; metrics
  are JSON numbers, never numeric strings). Every collection failure
  is a non-zero exit with **no partial JSON on stdout** and a short
  generic stderr diagnostic that never dumps `/proc` contents. All
  measurements are collected and validated first; a **final
  pre-render numeric gate** then rejects any token that is not a
  plain non-negative decimal (`^[0-9]+([.][0-9]+)?$` for fractional
  metrics, `^[0-9]+$` for byte/count values) or a fixed-format UTC
  `reported_at` — so a non-finite awk result (inf/nan), an exponent
  form, a sign or an empty token can never reach the document. Only
  then is the JSON rendered exactly once via a single `printf` with
  only pre-validated allowlisted values, so no unsafe shell
  interpolation of arbitrary data into JSON can occur.
- **`reported_at`** is host UTC time in timezone-aware ISO 8601
  (`YYYY-MM-DDTHH:MM:SS+00:00`, e.g. `2026-09-10T08:00:00+00:00`).
  The reporter owns only `reported_at`; the central `received_at`
  remains B2 responsibility. All arithmetic lives in awk (IEEE
  doubles), never in shell integer arithmetic, so huge-but-finite
  kernel counters cannot overflow.
- **Testability**: metric parsing/calculation lives in small bash
  functions (`parse_uptime`, `parse_loadavg`, `read_cpu_sample`,
  `compute_cpu_percent`, `parse_ram_usage`, `parse_swap_usage`,
  `parse_root_usage`, `validate_node_identity`,
  `require_metric_decimal`/`require_metric_count`/
  `require_metric_timestamp`), and the script is
  sourceable without executing `main`. Documented deterministic test
  seams (`SENTINEL_PROC_UPTIME`, `SENTINEL_PROC_LOADAVG`,
  `SENTINEL_PROC_MEMINFO`, `SENTINEL_PROC_STAT_A`/`_B`,
  `SENTINEL_DF_BYTES_FILE`, `SENTINEL_DF_INODES_FILE`) redirect the
  sources to synthetic fixtures under `tests/fixtures/reporter/**`
  so tests never depend on the developer machine's live `/proc`.
  At least one integration test proves the accepted B3 decoder
  (`decode_heartbeat_payload`) accepts the produced payload.

Out of scope for C1: any HTTP/HTTPS delivery, endpoint/token
configuration, retry/backoff, TLS, systemd unit/timer, deployment,
secret loaders, health thresholds, heartbeat freshness, TCP
reachability, incidents, Telegram and Hermes integration (Stages C2,
C3, D, E and F).

## 16. HTTPS one-shot reporter transport (Stage C2)

Stage C2 turns the C1 collector into the complete one-shot reporter.
Executing `scripts/sentinel-report.sh` IS the production reporter
path — there is deliberately no second reporter, no daemon, no
Python runtime on the monitored host and no local spool/queue:

    systemd timer (Stage C3)
                |
     one-shot sentinel-report.sh (C1 collection + C2 transport)
                  |  validate transport configuration
                  |  collect the exact C1 payload
                  |  deterministic size gate
                  |  ONE outbound HTTPS POST
                  |  require HTTP 204
     exit (success: exit 0, empty stdout)

- **Transport dependency**: exactly one monitored-host transport
  dependency is added — `curl`. No Python, no jq, no wget fallback,
  no alternate HTTP client. A missing curl is a non-zero failure with
  empty stdout and a short generic stderr diagnostic.
- **Collection authority**: the accepted C1 collector is refactored
  into a sourceable `collect_payload` function — the ONLY collection
  logic. The executable path (`main`) validates transport
  configuration first (cheap failures never collect), then invokes
  `collect_payload`, applies the size gate and performs the single
  `send_heartbeat`. No production bypass variable (TEST_MODE /
  SENTINEL_COLLECT_ONLY / SENTINEL_DISABLE_NETWORK) exists: C1
  fixture tests source the real script and call the real collection
  function, while the executable always attempts delivery after a
  successful collection.
- **Transport configuration**: `SENTINEL_ENDPOINT` (mandatory, the
  FULL heartbeat endpoint URL such as
  `https://sentinel.example/v1/heartbeat`) and `SENTINEL_TOKEN`
  (mandatory reporter token). The endpoint must use HTTPS — `http://`
  is rejected, as are empty values and any value containing
  whitespace or control characters — and is used VERBATIM: no path is
  added or normalized, no URL is derived from the hostname, no
  service discovery. The token is constrained to the documented
  transport-safe reporter alphabet (one or more characters from
  `A-Z a-z 0-9 . _ ~ -`): no whitespace, no CR/LF, no control
  characters, no quotes, no trimming/case-folding/normalization, and
  deliberately no entropy or minimum-length policy (that belongs to
  deployment hardening).
- **Exactly one request attempt**: no retry, no retry-after, no
  backoff, no loop. Redirects are never followed (no
  `--location`/`-L`): a 3xx response is a failure, not a second
  request. Success requires the final HTTP status to be EXACTLY
  `204` — 200/201/202, 3xx, 4xx and 5xx all fail. A single attempt
  is bounded by fixed limits (connect timeout 10 s, overall request
  timeout 20 s); timeout tuning belongs to Stage F.
- **HTTPS-only / TLS**: `curl --proto '=https'` prevents any
  non-HTTPS protocol use; default certificate and hostname
  verification stay enabled; `--insecure`/`-k` are never used.
  Ambient curl configuration is isolated: `--disable` is passed as
  the FIRST curl option, so host- or user-level curl config files
  (`CURL_HOME/.curlrc`, XDG config, `~/.curlrc`) are never read and
  can never inject `--insecure`, `--location`, `--retry`, extra
  headers or tracing into the reporter request — the frozen runtime
  invariants hold for the EFFECTIVE invocation, not merely for the
  script's explicit argv (curl only honors the config-disabling
  option in first position, hence the placement). No certificate
  pinning, custom production CA loader or client certificate
  support (later deployment/hardening concerns).
- **Request contract**: `POST <SENTINEL_ENDPOINT>` with
  `Content-Type: application/json` and `X-Sentinel-Token: <exact
  reporter token>` headers, and the EXACT C1 JSON as the body — no
  `received_at`, `state`, `health`, `services`, transport metadata,
  token in JSON, or semantic re-serialization/mutation of the C1
  payload. The `Expect: 100-continue` header curl would otherwise
  emit is suppressed via curl's explicit header-suppression semantics
  (`-H 'Expect:'` — transmission suppressed, NOT an empty header on
  the wire), because the accepted B5 rejects ANY Expect header with
  417. The body length is known: `--data-binary` produces ordinary
  `Content-Length` framing — never `Transfer-Encoding: chunked`, no
  compression.
- **Payload size gate**: before curl is invoked the payload length
  must be `> 0` and `<= 16384` bytes (exactly the accepted B4 server
  limit; deterministic because the C1 payload is ASCII-only).
  Oversized or empty input fails non-zero without invoking curl and
  is never truncated.
- **Token secret safety**: the token never enters the JSON, is never
  printed to stdout/stderr, is never persisted, never written to a
  temporary file and never appears in curl argv. It reaches curl
  only through the protected stdin header path (`--header @-` fed by
  `printf 'X-Sentinel-Token: %s\n'`). Before any child process is
  spawned the exported `SENTINEL_TOKEN` is captured into a
  non-exported local variable and removed from the child
  environment; shell xtrace is disabled for the whole
  configuration/transport path so `bash -x` can never print it.
- **Output safety**: curl's response body, progress meter, raw
  diagnostics and headers never reach reporter stdout — `--silent`,
  `--output /dev/null`, only `--write-out '%{http_code}'` is
  captured, and raw curl stderr is suppressed so the reporter emits
  its own short generic failure. `--verbose`/`--trace` are never
  used. On success: exit 0, empty stdout. On ANY failure (missing
  curl, invalid/missing endpoint or token, collection failure, empty
  or oversized payload, DNS/TCP/TLS failure, curl non-zero exit,
  HTTP != 204): non-zero exit, empty stdout, one short generic
  stderr diagnostic that never contains the token, the payload or
  the response body. A failed heartbeat is simply not delivered —
  no local persistence, no retry queue.

Out of scope for C2: systemd service/timer units, `/etc` install
paths, deployment commands, secret-file provisioning, token
rotation, retries, local spool/queue, persistent reporter process,
TLS termination on Sentinel, reverse proxy, server deployment,
health engine, TCP reachability checks, incidents, Telegram and
Hermes integration (Stages C3 / D / E / F).

## 17. systemd reporter timer / packaging (Stage C3)

Stage C3 packages the already-accepted C1+C2 reporter for a monitored
Ubuntu host. It adds NO second reporter implementation — only
declarative systemd packaging (`packaging/systemd/**`), an operator
runbook (`docs/REPORTER_DEPLOYMENT.md`) and deterministic packaging
tests. The final host-side pipeline:

    systemd timer (hermes-sentinel-reporter.timer)
                 |
     systemd oneshot service (hermes-sentinel-reporter.service)
                 |
      /usr/local/libexec/hermes-sentinel/sentinel-report.sh
                   |  exact C1 collection
                   |  exact C2 single HTTPS POST
      exit

- **Unit names and install paths (frozen)**: the units install as
  `/etc/systemd/system/hermes-sentinel-reporter.service` and
  `/etc/systemd/system/hermes-sentinel-reporter.timer` (root:root,
  0644); the reporter executable installs as
  `/usr/local/libexec/hermes-sentinel/sentinel-report.sh`
  (root:root, 0755); the configuration installs as
  `/etc/hermes-sentinel/reporter.env` (root:root, 0600, directory
  `/etc/hermes-sentinel` root:root 0750).
- **Dedicated runtime identity**: the reporter runs as the system
  account `hermes-sentinel-reporter` (group `hermes-sentinel-reporter`)
  — no login shell, no home, no sudo, no extra groups, no
  capabilities, and never root. The runtime user neither owns nor
  can modify the executable.
- **EnvironmentFile boundary**: all reporter configuration
  (`SENTINEL_NODE`, `SENTINEL_ENDPOINT`, `SENTINEL_TOKEN`) is
  supplied by the systemd manager from `reporter.env`. The reporter
  process never needs filesystem read permission on that file; no
  token, endpoint or node value ever appears in the unit files, in
  unit commands or in command-line arguments. The packaged example
  (`packaging/systemd/reporter.env.example`) uses only synthetic
  values, and its token placeholder is deliberately invalid under
  the C2 token validation so an unedited example fails closed before
  network delivery.
- **Timer semantics**: `OnBootSec=30s`, `OnUnitActiveSec=60s`,
  `AccuracySec=1s`, `RandomizedDelaySec=0`, targeting exactly
  `hermes-sentinel-reporter.service`, enabled via
  `WantedBy=timers.target` — approximately one report per minute
  with a short initial boot delay. No `OnCalendar`, no
  `Persistent=true` (missed heartbeats are deliberately not
  replayed: a heartbeat represents current host state, not
  historical backlog), no retry loops, no extra timer targets.
- **No immediate retry**: a failed oneshot heartbeat is a service
  failure (`Restart=no`; C2 non-zero exit is never broadened or
  wrapped). The next scheduled timer activation is the next NORMAL
  measurement attempt — sampling cadence, not a transport retry.
- **Non-overlap / singleton**: one fixed service unit is the
  singleton execution boundary — no templated `@.service` instances,
  no `systemd-run`, no background or parallel launches. Because the
  unit is oneshot, systemd never starts a second instance while one
  is active; the C2 timeouts (connect 10 s, request 20 s) plus
  `TimeoutStartSec=30s` bound each run.
- **Security hardening** (strong but boring and portable):
  `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
  `ProtectSystem=strict`, `ProtectHome`, `ProtectKernelTunables`,
  `ProtectKernelModules`, `ProtectControlGroups`,
  `RestrictSUIDSGID`, `LockPersonality`, empty
  `CapabilityBoundingSet`/`AmbientCapabilities`, `UMask=0077`,
  journal-directed stdout/stderr, and a direct `ExecStart` with no
  shell wrapper, no sudo/su and no curl in the unit. Network access
  is deliberately NOT restricted (the reporter needs outbound HTTPS
  and DNS); deeper hardening (address-family/system-call filtering
  etc.) may come with Stage F Linux runtime evidence.
- **Local operator deployment only**: deployment is an operator
  action performed locally on the monitored host (see
  `docs/REPORTER_DEPLOYMENT.md`). Sentinel itself never deploys,
  SSHes, installs files, invokes systemctl or executes remote
  commands — the observability-only boundary of section 3 is
  unchanged. There is no automated installer: packaging is
  declarative files plus documentation.

Stage C3 completes Stage C (HOST REPORTER): C1 collector + C2 HTTPS
one-shot transport + C3 systemd timer/packaging. Repository
packaging only — an actual deployment on a monitored host is a
separate operator action and is not claimed here.

Out of scope for C3: heartbeat freshness evaluation, TCP
reachability, HEALTHY/DEGRADED/DOWN, thresholds, hysteresis
(Stage D); incidents and Telegram (Stage E); TLS termination,
reverse proxy, server deployment, advanced reporter hardening, rate
limiting, credential rotation, production observability (Stage F).

## 18. Deterministic health signals core (Stage D1)

Stage D1 adds a pure, deterministic signal layer
(`hermes_sentinel.health`). It answers exactly two questions and
nothing else: is the latest accepted heartbeat MISSING / FRESH /
STALE, and which configured resource thresholds are breached. It
does not decide HEALTHY / DEGRADED / DOWN.

- **Central time authority**: heartbeat freshness is evaluated
  exclusively against the central `HeartbeatRecord.received_at` time
  axis assigned by the Stage B2 ingestion clock. The reporter-side
  `telemetry.timestamp` (`reported_at`) is a separate time axis and
  never participates in freshness.
- **Freshness statuses**: `HeartbeatFreshness.MISSING` — no accepted
  heartbeat exists at all; `age_seconds` is `None` (a missing
  heartbeat is never converted into a fake timestamp or a host
  state). `FRESH` / `STALE` carry a finite, non-negative
  `age_seconds = now - latest_received_at`.
- **Equality boundary**: `stale_after_seconds` is the sole freshness
  authority. `0 <= age <= stale_after_seconds` is FRESH — exact
  threshold equality stays FRESH; `age > stale_after_seconds` is
  STALE. No grace periods exist.
- **`expected_interval_seconds` does not itself change status**: an
  age above the expected interval but below the stale threshold
  remains FRESH. No intermediate status (LATE/WARNING/...) is
  invented from the expected interval alone.
- **Clock safety**: `now` and a non-None `latest_received_at` must
  be truly timezone-aware (`tzinfo is not None` AND
  `utcoffset() is not None`); effectively naive values are rejected
  with `ValueError`. Offset-aware timestamps with different UTC
  offsets compare by instant. A `latest_received_at` in the future
  relative to `now` means the central clock moved backwards or the
  evidence is inconsistent — it fails closed with `ValueError` (no
  clamping to zero, no `abs()`, no reporter-timestamp substitution).
  The module never reads a wall clock: all time enters explicitly
  as function arguments.
- **Resource breach boundary**: a metric breaches when
  `metric >= configured threshold` — equality is a breach. Exact
  mappings: CPU (`cpu_percent`), RAM (`ram.percent`), SWAP
  (`swap.percent`), DISK (`root_filesystem.percent`), INODES
  (`root_inodes.percent`), LOAD5 (`load.five` vs `load5_max`).
  There are deliberately no load1/load15 thresholds.
- **LOAD5 is optional**: `thresholds.load5_max is None` disables the
  load check entirely — no LOAD5 breach can be emitted regardless of
  the observed load average.
- **Absent swap** (the normal domain shape `used=0 / total=0 /
  percent=0`, e.g. swap disabled) is not special-cased: under a
  normal positive swap threshold it is simply not breached.
- **Deterministic breach order**: `ResourceAssessment.breaches` is
  an immutable tuple in the canonical order CPU, RAM, SWAP, DISK,
  INODES, LOAD5 — never a set or list.
- **Signal-only boundary**: resource issues (and freshness
  statuses) are facts, not state decisions. Resource breaches may
  later produce DEGRADED but never DOWN; D1 performs no
  HEALTHY/DEGRADED/DOWN resolution and emits no `HostTransition`.
- **Purity**: no I/O, no networking, no repository/SQLite access, no
  wall-clock reads, no caches or global evaluation state; inputs are
  never mutated and results are immutable.

D1 owns no external TCP reachability probe, no debounce/hysteresis
and no orchestration: Stage D2 (external TCP reachability probe) now
provides the external TCP reachability evidence, Stage D3 (host
state resolver + debounce/hysteresis) now provides the pure
host-state resolution in section 20 below, and Stage D4 (health
engine orchestration) now provides the runtime composition in
section 21 below.

## 19. External TCP reachability probe (Stage D2)

Stage D2 adds exactly one bounded external evidence primitive
(`hermes_sentinel.reachability`). It answers exactly one question —
can Sentinel establish a TCP connection to the configured external
target? — and nothing else. It does not decide
HEALTHY / DEGRADED / DOWN.

- **Evidence-only boundary**: `probe_tcp_reachability` returns only
  `TcpReachability.REACHABLE` or `TcpReachability.UNREACHABLE` —
  raw external evidence, never a host state and never a state
  transition. REACHABLE never implies a healthy host and
  UNREACHABLE never implies a down host: mapping evidence to host
  state (weighing heartbeat freshness and confirmation counters) is
  Stage D3 responsibility.
- **Configuration**: the probe consumes only
  `ExternalCheckSettings.tcp_host`, `tcp_port` and
  `timeout_seconds`. `tcp_host` / `tcp_port` are passed verbatim to
  the connection primitive — no lowercasing, stripping, URL parsing
  or substitution; configuration validation stays owned by
  `ExternalCheckSettings`. `down_confirmations` /
  `recovery_confirmations` belong to D3 and are deliberately not
  interpreted here.
- **Standard primitive**: the sole network dependency is the Python
  standard library `socket` module. `socket.create_connection` is
  the only connection primitive; no third-party networking package
  and no generic network abstraction framework exists.
- **One probe per call**: one invocation performs exactly one
  application-level `create_connection` call with the target
  `(tcp_host, tcp_port)` and the configured `timeout_seconds` as
  the connection timeout. `create_connection` may internally
  consider multiple resolved addresses; that is still one logical
  probe, and address iteration is never reimplemented locally.
- **Success semantics**: a successful TCP connect is sufficient
  evidence — no bytes are sent, no application protocol handshake
  and no TLS handshake is performed, and no second timeout exists.
  The opened socket is closed before the function returns
  (deterministic resource cleanup).
- **Failure semantics**: normal network failures of the `OSError`
  family — connection refused, connect timeout
  (`socket.timeout` / `TimeoutError`), name resolution failure
  (`socket.gaierror`), network/host unreachable — map to
  `UNREACHABLE` and are never leaked to D3/D4 callers as
  exceptions.
- **Error boundary**: only the `OSError` family is caught.
  Unexpected non-network programming or runtime defects propagate
  unchanged; `KeyboardInterrupt` / `SystemExit` are never swallowed
  (no `BaseException` handling).
- **No retry, no state**: no application-level retry loop, no
  sleep/backoff, no confirmation counters (failure/success streaks
  are D3 concerns), no memory between calls — one call is one
  instantaneous logical observation. The probe never mutates the
  settings object and never mutates global socket state
  (`socket.setdefaulttimeout` is never called).
- **No persistence / no incidents**: no SQLite access, no cached
  probe state, no incident generation, no Telegram dependency.

Out of scope for D2: host state resolution, debounce/hysteresis,
`HostTransition` emission, health engine orchestration, incidents
and Telegram (Stages D3, D4 and E). D2 itself performs no host-state
resolution and no orchestration; Stage D3 (host state resolver +
debounce/hysteresis) now provides the pure state resolution in
section 20 below, while Stage D4 (health engine orchestration) now
provides the runtime composition in section 21 below.

## 20. Host state resolver and hysteresis (Stage D3)

Stage D3 adds the pure deterministic host-state state machine
(`hermes_sentinel.state_resolver`). It consumes ONLY the
already-computed D1/D2 evidence — `HeartbeatFreshness`,
`ResourceAssessment`, `TcpReachability` — plus the explicit previous
D3 resolution and the existing confirmation settings
(`ExternalCheckSettings`). It performs no heartbeat calculation, no
resource calculation, no TCP probing, no repository query and no
wall-clock access.

- **Pure resolution with explicit memory**: the public
  `resolve_host_state(previous, freshness, resources, reachability,
  settings)` is keyword-only and pure. `HostStateResolution` is BOTH
  the current resolved `HostState` and the minimal explicit D3
  memory (the bounded confirmation streaks `down_failures` /
  `recovery_successes`) to feed into the next evaluation. It is an
  immutable frozen slotted dataclass; invalid memory shapes (bool or
  negative counters, a nonzero `down_failures` in DOWN, a nonzero
  `recovery_successes` outside DOWN) fail closed with `ValueError`.
  `previous=None` means first evaluation (no established state, both
  streaks 0). There is no mutable module state and no hidden
  cross-call memory.
- **Base instantaneous state** (outside confirmed DOWN hysteresis):
  HEALTHY iff heartbeat FRESH AND TCP REACHABLE AND no resource
  threshold breached. Every other non-DOWN evidence combination is
  DEGRADED (FRESH + REACHABLE + resource breach; MISSING/STALE +
  REACHABLE; FRESH + UNREACHABLE). Resource issues never produce
  DOWN.
- **DOWN qualification**: a DOWN-confirmation observation is exactly
  heartbeat MISSING or STALE (the lost-heartbeat qualifying family)
  AND TCP UNREACHABLE. Resource state does not participate in DOWN
  qualification.
- **DOWN debounce**: while not in confirmed DOWN, each qualifying
  observation increments the consecutive `down_failures` streak by
  exactly 1; the state stays DEGRADED until exactly
  `down_confirmations` consecutive qualifying observations confirm
  DOWN on that evaluation (threshold equality confirms; the streak
  resets to 0 once DOWN is confirmed). Any break of the combined
  predicate (a FRESH heartbeat OR a REACHABLE probe) resets the
  pending down streak to 0; alternating MISSING/STALE does not break
  it while TCP stays UNREACHABLE.
- **Confirmed DOWN hold**: once previous state is DOWN, D3 stays
  DOWN until recovery hysteresis confirms. Heartbeat freshness alone
  does NOT release confirmed DOWN; resource state alone does NOT
  release confirmed DOWN. While DOWN: UNREACHABLE remains DOWN and
  resets the recovery streak; REACHABLE increments the consecutive
  `recovery_successes` streak by exactly 1.
- **Recovery hysteresis**: exactly `recovery_confirmations`
  consecutive REACHABLE TCP observations exit DOWN on the confirming
  evaluation (threshold equality exits; both counters reset to 0).
  A single UNREACHABLE observation resets the recovery streak to 0
  while the host remains DOWN. Heartbeat or resource changes never
  break a successful TCP streak.
- **Recovery target**: the state after leaving DOWN is recomputed
  from the CURRENT evidence via the base instantaneous rule —
  FRESH + clear resources → HEALTHY; FRESH + resource breach →
  DEGRADED; MISSING/STALE → DEGRADED.
- **Settings consumption**: D3 consumes ONLY
  `settings.down_confirmations` and `settings.recovery_confirmations`
  (both `>= 1` by the existing config contract).
  `tcp_host` / `tcp_port` / `timeout_seconds` belong to D2 and are
  never read here.
- **No transitions, no identity, no time**: D3 constructs no
  `HostTransition` and carries no host name and no timestamp — it
  has no clock and no host identity. D4 owns orchestration, the
  per-host retention of previous D3 resolutions and transition
  timestamp creation (comparing `previous.state` vs the new
  resolution state at the confirmed engine evaluation time).
- **Purity boundary**: no I/O, no socket, no persistence, no
  logging, no environment reads, no clock calls, no mutable module
  state; inputs are never mutated. D3 is deliberately not a runtime
  manager: no engine/tracker/registry/scheduler/loop object exists
  here — D4 owns runtime composition.

Out of scope for D3: obtaining evidence (heartbeat/resource
evaluation, TCP probing), per-host state retention, orchestration,
`HostTransition` creation, incidents and Telegram (Stages D1, D2, D4
and E). D4 (health engine orchestration) owns exactly those runtime
composition concerns and is documented in section 21 below; it
remains deliberately absent from the pure D3 resolver.

## 21. Health engine orchestration (Stage D4)

Stage D4 adds the bounded orchestration layer
(`hermes_sentinel.health_engine`) that composes the frozen D1/D2/D3
contracts and the B1 repository read into one deterministic per-host
health evaluation. It re-opens none of the lower contracts: D1/D2/D3
functions are called verbatim as the source of truth, and no
freshness, breach, probe or hysteresis logic is duplicated here.

- **Public result**: `HostHealthEvaluation` is an immutable frozen
  slotted dataclass carrying the evaluated `host`, the single
  validated `evaluated_at` clock moment, the propagated D1
  `freshness` (`HeartbeatFreshnessResult`), the propagated D1
  `resources` (`ResourceAssessment` or `None`), the propagated D2
  `reachability` (`TcpReachability`), the propagated D3
  `resolution` (`HostStateResolution`) and the optional confirmed
  `transition` (`HostTransition` or `None`).
- **Engine**: `HealthEngine(config, repository, clock=utc_now)` with
  one public operation `evaluate_host(host)`. The accepted B2
  central clock (`hermes_sentinel.ingestion.utc_now` /
  `Clock`) is the default injectable time authority.
- **Exact evaluation order** (normative, one full evaluation per
  call): (1) the host must be configured — otherwise
  `UnknownHostError` before any clock, repository or TCP activity;
  (2) the clock is called exactly once; (3) the clock result must be
  a truly timezone-aware `datetime` — otherwise
  `InvalidClockResultError` before any repository read (never
  silently coerced); (4) `repository.latest_heartbeat(host)` is
  called exactly once — the ONLY repository access, D4 never
  writes; (5) D1 freshness is evaluated against the single clock
  moment; (6) with a heartbeat the D1 resource assessment is
  computed and used both publicly and internally, without one the
  public `resources` is `None` while the D3 resolver receives the
  neutral non-degrading `ResourceAssessment(breaches=())` input it
  requires (resources never participate in DOWN qualification, so
  absent telemetry can invent neither DEGRADED-with-cause nor
  DOWN); (7) exactly one D2 TCP probe supplies the reachability
  evidence; (8) the D3 resolver resolves the new state from the
  evidence plus the previously remembered per-host resolution; (9)
  a transition is created only when DOWN was entered or left; (10)
  the complete immutable evaluation is constructed; (11) ONLY then
  is the new resolution committed to the per-host memory; (12) the
  result is returned.
- **Errors**: `HealthEngineError` is the bounded base;
  `UnknownHostError` and `InvalidClockResultError` subclass it and
  are distinct from the accepted B2 ingestion errors. Lower-layer
  exceptions (for example the D1 fail-closed `ValueError` on
  inconsistent clock evidence, repository failures or non-network
  probe defects) propagate unchanged — there is no broad
  exception-wrapping hierarchy.
- **Transitions**: emitted ONLY when an established previous
  resolution is entered or left by DOWN on this evaluation, stamped
  `at` the confirming evaluation's clock moment. The first
  evaluation of a host never emits — even when the initial resolved
  state is DOWN — and ordinary HEALTHY <-> DEGRADED changes never
  emit. D3 hysteresis semantics (the DOWN debounce, the confirmed
  DOWN hold and the recovery streak) remain authoritative and are
  never reimplemented here.
- **Process-local per-host memory**: the engine remembers only the
  last fully committed `HostStateResolution` per host. The commit
  is all-or-nothing from the engine's point of view: any failure
  before successful result construction (clock validation,
  repository read, freshness/resource assessment, TCP probe,
  resolver execution, result construction) leaves the previously
  remembered per-host resolution exactly intact. A process restart
  naturally resets every host to `previous=None`; nothing is
  persisted.
- **Read-only persistence boundary**: D4 performs no SQLite writes,
  no `latest_received_at` updates, no schema changes and no
  incident/notification records. The usage model is the B-stage
  single service thread (`HealthEngine`, like
  `HeartbeatRepository`, is not thread-safe).

Out of scope for D4: incidents, Telegram delivery,
scheduler/polling loops, service monitoring, retries/backoff,
HTTP endpoints, remote remediation (SSH/shell/systemctl/reboot)
and any new persistence (Stages E, F, H, I).
