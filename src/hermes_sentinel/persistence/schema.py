"""Deterministic fail-closed SQLite schema bootstrap for Hermes Sentinel.

Stage B1 persistence foundation. The schema stores heartbeat
observations: one row per accepted heartbeat report, keyed by an
explicit node identity so telemetry of different servers can never
be mixed accidentally.

Bootstrap contract (docs/ARCHITECTURE.md, persistence section):

- deterministic: the DDL is a fixed constant, executed verbatim;
- idempotent: repeated bootstrap of the canonical schema is safe
  (e.g. application restart) and never duplicates or destroys it;
- FAIL-CLOSED: compatibility is proven at bootstrap time, never
  deferred to the first insert —

  1. a new empty database gets the canonical table, index and
     ``user_version = SCHEMA_VERSION``;
  2. a database with ``user_version == SCHEMA_VERSION`` must already
     contain the canonical schema (validated via PRAGMA
     introspection); anything else raises
     :class:`SchemaCompatibilityError` BEFORE any write;
  3. a database with a newer ``user_version`` raises
     :class:`UnsupportedSchemaVersionError` — no downgrade, no
     modification;
  4. an unversioned (``user_version == 0``) database with an already
     existing ``heartbeat_observation`` table is accepted ONLY after
     the table proves exactly compatible with the canonical column
     contract; otherwise :class:`SchemaCompatibilityError`;
  5. no destructive ALTER/DROP/recreate is ever performed on an
     unknown schema;
  6. validation happens before any write, so a failed bootstrap
     never leaves a partially accepted schema or version behind.

Only stdlib ``sqlite3`` is used; no ORM, no migration framework.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "SCHEMA_VERSION",
    "SchemaCompatibilityError",
    "UnsupportedSchemaVersionError",
    "initialize_schema",
    "schema_version",
    "heartbeat_table_exists",
]


class SchemaCompatibilityError(RuntimeError):
    """Existing database objects are incompatible with the canonical
    B1 schema. Raised at bootstrap time (fail-closed), before any
    write is performed."""


class UnsupportedSchemaVersionError(RuntimeError):
    """The database schema version is newer than this build supports.
    The database is never downgraded or modified."""


#: Version of the heartbeat schema. Bumped only by an explicit
#: roadmap decision; ``PRAGMA user_version`` records it in the
# database file so future stages can detect the layout they face.
SCHEMA_VERSION = 1

_TABLE_NAME = "heartbeat_observation"
_INDEX_NAME = "idx_heartbeat_node_received"

_CREATE_HEARTBEAT_TABLE = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    uptime_seconds REAL NOT NULL,
    load_one REAL NOT NULL,
    load_five REAL NOT NULL,
    load_fifteen REAL NOT NULL,
    cpu_percent REAL NOT NULL,
    ram_used REAL NOT NULL,
    ram_total REAL NOT NULL,
    ram_percent REAL NOT NULL,
    swap_used REAL NOT NULL,
    swap_total REAL NOT NULL,
    swap_percent REAL NOT NULL,
    root_fs_used REAL NOT NULL,
    root_fs_total REAL NOT NULL,
    root_fs_percent REAL NOT NULL,
    root_inode_used REAL NOT NULL,
    root_inode_total REAL NOT NULL,
    root_inode_percent REAL NOT NULL
)
"""

_CREATE_LATEST_INDEX = f"""
CREATE INDEX IF NOT EXISTS {_INDEX_NAME}
    ON {_TABLE_NAME} (node, received_at, id)
"""

# SCHEMA_VERSION is a module-level int constant defined in this file,
# never a user value, so interpolation here cannot inject SQL.
_SET_USER_VERSION = f"PRAGMA user_version = {SCHEMA_VERSION}"

#: Canonical column contract of ``heartbeat_observation``:
#: (name, declared type, notnull, pk) in exact declaration order.
#: This is the compatibility yardstick for fail-closed validation.
_CANONICAL_COLUMNS: tuple[tuple[str, str, int, int], ...] = (
    ("id", "INTEGER", 0, 1),
    ("node", "TEXT", 1, 0),
    ("reported_at", "TEXT", 1, 0),
    ("received_at", "TEXT", 1, 0),
    ("uptime_seconds", "REAL", 1, 0),
    ("load_one", "REAL", 1, 0),
    ("load_five", "REAL", 1, 0),
    ("load_fifteen", "REAL", 1, 0),
    ("cpu_percent", "REAL", 1, 0),
    ("ram_used", "REAL", 1, 0),
    ("ram_total", "REAL", 1, 0),
    ("ram_percent", "REAL", 1, 0),
    ("swap_used", "REAL", 1, 0),
    ("swap_total", "REAL", 1, 0),
    ("swap_percent", "REAL", 1, 0),
    ("root_fs_used", "REAL", 1, 0),
    ("root_fs_total", "REAL", 1, 0),
    ("root_fs_percent", "REAL", 1, 0),
    ("root_inode_used", "REAL", 1, 0),
    ("root_inode_total", "REAL", 1, 0),
    ("root_inode_percent", "REAL", 1, 0),
)

#: Canonical index contract: covering index for the deterministic
#: latest-heartbeat lookup (node, received_at, id).
_CANONICAL_INDEX_COLUMNS: tuple[str, ...] = ("node", "received_at", "id")


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Bootstrap the canonical B1 schema on ``connection``, fail-closed.

    See the module docstring for the full decision table. All writes
    (DDL, ``PRAGMA user_version``) happen only after compatibility of
    any pre-existing objects has been proven, inside one transaction:
    a failed validation raises before any modification, and a failed
    write rolls back completely.

    No user-supplied values are interpolated into any statement.
    """
    version = schema_version(connection)
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"database schema version {version} is newer than the"
            f" supported version {SCHEMA_VERSION}; refusing to"
            " downgrade or modify the schema"
        )

    table_present = heartbeat_table_exists(connection)
    if table_present and _observed_columns(connection) != _CANONICAL_COLUMNS:
        raise SchemaCompatibilityError(
            f"table {_TABLE_NAME!r} does not match the canonical B1"
            " column contract (missing, extra, reordered or mistyped"
            " columns); refusing to accept the database"
        )

    index_on_table = table_present and _index_on_table(connection)
    if index_on_table and _index_columns(connection) != (
        _CANONICAL_INDEX_COLUMNS
    ):
        raise SchemaCompatibilityError(
            f"index {_INDEX_NAME!r} does not match the canonical B1"
            " index contract; refusing to accept the database"
        )

    if version == SCHEMA_VERSION:
        # The version already claims canonical B1: require the full
        # canonical schema to be present, otherwise fail explicitly
        # instead of silently trusting the version stamp.
        if not table_present or not index_on_table:
            raise SchemaCompatibilityError(
                f"user_version={SCHEMA_VERSION} but the canonical B1"
                " schema objects are missing"
            )
        return  # fully validated; nothing to write

    # version == 0: fresh or unversioned database. Any pre-existing
    # table/index has already passed exact compatibility validation
    # above; create only what is missing and stamp the version.
    try:
        connection.execute("BEGIN")
        if not table_present:
            connection.execute(_CREATE_HEARTBEAT_TABLE)
        if not index_on_table:
            connection.execute(_CREATE_LATEST_INDEX)
        connection.execute(_SET_USER_VERSION)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema version recorded in the database file."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def heartbeat_table_exists(connection: sqlite3.Connection) -> bool:
    """True when the heartbeat table is present (test helper)."""
    row = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master"
        " WHERE type = 'table' AND name = ?",
        (_TABLE_NAME,),
    ).fetchone()
    return int(row[0]) == 1


def _observed_columns(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, int, int], ...]:
    """Read the live column layout of the heartbeat table.

    PRAGMA arguments cannot be parameterized; the table name is a
    module-level constant, never a user value. Declared types are
    compared case-insensitively (SQLite type affinity is
    case-insensitive); everything else must match exactly.
    """
    rows = connection.execute(f"PRAGMA table_info({_TABLE_NAME})").fetchall()
    return tuple(
        (str(row[1]), str(row[2] or "").upper(), int(row[3]), int(row[5]))
        for row in rows
    )


def _index_on_table(connection: sqlite3.Connection) -> bool:
    """True when the canonical index exists on the heartbeat table."""
    rows = connection.execute(f"PRAGMA index_list({_TABLE_NAME})").fetchall()
    return any(str(row[1]) == _INDEX_NAME for row in rows)


def _index_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Read the column order of the canonical index, if present."""
    rows = connection.execute(f"PRAGMA index_info({_INDEX_NAME})").fetchall()
    return tuple(str(row[2]) for row in rows)
