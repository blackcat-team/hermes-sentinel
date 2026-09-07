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
