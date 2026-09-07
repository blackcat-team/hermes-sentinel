"""SQLite persistence foundation for Hermes Sentinel (Stage B1).

Stdlib ``sqlite3`` only — no ORM, no migration framework. Public API:

- :func:`connect` — open/create the database with the schema applied;
- :class:`HeartbeatRecord` — one accepted heartbeat observation;
- :class:`HeartbeatRepository` — minimal insert/latest-lookup API;
- :class:`SchemaCompatibilityError` /
  :class:`UnsupportedSchemaVersionError` — fail-closed bootstrap
  errors raised at schema initialization time, never deferred to
  the first insert.

See the persistence contracts in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from hermes_sentinel.persistence.repository import (
    HeartbeatRecord,
    HeartbeatRepository,
    connect,
)
from hermes_sentinel.persistence.schema import (
    SCHEMA_VERSION,
    SchemaCompatibilityError,
    UnsupportedSchemaVersionError,
    initialize_schema,
    schema_version,
)

__all__ = [
    "SCHEMA_VERSION",
    "HeartbeatRecord",
    "HeartbeatRepository",
    "connect",
    "initialize_schema",
    "schema_version",
    "SchemaCompatibilityError",
    "UnsupportedSchemaVersionError",
]
