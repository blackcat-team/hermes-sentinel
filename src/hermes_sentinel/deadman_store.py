"""Atomic dead-man state-file store (Stage H1B-1).

The bounded filesystem half of the accepted Stage H1A persistence
boundary: reading and writing the dead-man canonical operational
snapshot as ONE state file. The serialization semantics stay entirely
with the frozen H1A helpers — this store adds only file I/O, size
bounds and atomic replacement, and it never re-decides, repairs or
resets any persisted state.

Contract points (see docs/ARCHITECTURE.md section 32):

READ:

- an absent state file is the canonical fresh start: the H1A
  ``INITIAL_DEADMAN_STATUS`` (UNKNOWN) — no file has ever named an
  outage, so nothing has been lost;
- a present file is decoded STRICTLY through the accepted H1A
  ``decode_deadman_status`` (schema v4); a corrupt, malformed or
  unsupported document surfaces as the bounded
  :class:`DeadManStateStoreError` and is NEVER silently reset to
  UNKNOWN — a damaged liveness ledger must stop the runtime, not
  erase it;
- the read itself is bounded: at most one byte beyond
  ``MAX_DEADMAN_STATE_BYTES`` is ever read, so an oversized file is
  rejected before any parsing attempt and its remainder is never
  read into memory (a pre-read stat is never the sole enforcement
  because the file size can change);
- read failures (permissions, I/O) surface as bounded store errors.

WRITE:

- the status is serialized deterministically through the accepted H1A
  ``encode_deadman_status`` and written with atomic replacement in
  the SAME directory: create a uniquely named temporary file, write,
  flush, ``fsync``, then ``os.replace`` onto the target — the target
  is never partially exposed and a failed write leaves the previous
  target content untouched;
- the temporary file is removed on every write failure path, so no
  debris accumulates;
- parent-directory durability is attempted best-effort where the
  platform supports it (POSIX ``O_DIRECTORY`` fsync) and silently
  skipped where it does not (for example Windows) — portability over
  a durability claim that cannot be made everywhere;
- the serialized document must stay within the same bounded size
  limit as reads: a status too large to ever be read back is
  rejected before the target is touched, keeping the store
  symmetric and the persisted state always loadable;
- every filesystem failure surfaces as the bounded
  :class:`DeadManStateStoreError`; the store holds no secrets.

There is deliberately no migration framework, no locking, no backup
rotation and no schema negotiation: schema v4 is current, and the
future H1B orchestration owns run cadence and directory provisioning
(the store never creates parent directories).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hermes_sentinel.deadman import (
    INITIAL_DEADMAN_STATUS,
    DeadManStatus,
    DeadManStateDecodeError,
    decode_deadman_status,
    encode_deadman_status,
)

__all__ = [
    "MAX_DEADMAN_STATE_BYTES",
    "DeadManStateStore",
    "DeadManStateStoreError",
]

#: The bounded size of the dead-man state file, applied identically
#: to reads (reject oversized input before parsing) and writes
#: (reject a document that could never be read back). The canonical
#: snapshot is a few hundred bytes; a full mebibyte bounds even a
#: pathological unacknowledged-intent backlog.
MAX_DEADMAN_STATE_BYTES = 1_048_576


class DeadManStateStoreError(Exception):
    """A bounded dead-man state-file store failure.

    Messages name the store action and the state-file path plus a
    short reason (or the strict H1A decode reason) — the persisted
    dead-man state carries no secrets by design, and neither does
    the failure text.
    """


def _remove_quietly(path: Path) -> None:
    """Best-effort temporary-file removal; never masks a failure."""
    try:
        path.unlink()
    except OSError:
        pass


def _sync_directory_best_effort(directory: Path) -> None:
    """Best-effort parent-directory durability, where portable.

    fsyncing the directory makes the rename itself durable on POSIX;
    platforms without ``O_DIRECTORY`` (for example Windows) skip the
    step entirely rather than attempt a semantics they cannot honor.
    Every failure here is deliberately swallowed: directory sync is
    strictly best-effort and never turns a completed atomic replace
    into a reported write failure.
    """
    directory_flags = getattr(os, "O_DIRECTORY", None)
    if directory_flags is None:
        return
    try:
        fd = os.open(directory, os.O_RDONLY | directory_flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class DeadManStateStore:
    """Bounded atomic filesystem adapter for the H1A dead-man state.

    ``load()`` returns the strict H1A decode of the state file, or
    the canonical initial status when no file exists; ``save()``
    atomically replaces the file with the deterministic H1A encoding
    of the given status. The store performs no validation of its own
    beyond size bounds — schema, type and integrity checking stays
    authoritative in the accepted H1A decode boundary.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError(
                "path must be a pathlib.Path, got"
                f" {type(path).__name__}"
            )
        self._path = path

    @property
    def path(self) -> Path:
        """The dead-man state-file path (operator configuration)."""
        return self._path

    def load(self) -> DeadManStatus:
        """Load the persisted dead-man status strictly, fail-closed.

        An absent file is the canonical fresh start and returns
        ``INITIAL_DEADMAN_STATUS``. The file is read through a
        bounded read of at most one byte beyond the size limit, so
        an oversized, unreadable, non-UTF-8, corrupt, malformed or
        schema-unsupported file raises
        :class:`DeadManStateStoreError` — a damaged ledger is never
        silently coerced to a healthy baseline, and an oversized
        file is never fully read.
        """
        try:
            with self._path.open("rb") as handle:
                # The bounded read is the authoritative size
                # enforcement: at most one byte beyond the limit is
                # ever drawn, so an oversized file is detected here
                # and its remainder is never read into memory (a
                # pre-read stat could race a growing file and is
                # never the sole check).
                raw = handle.read(MAX_DEADMAN_STATE_BYTES + 1)
        except FileNotFoundError:
            return INITIAL_DEADMAN_STATUS
        except OSError as error:
            raise DeadManStateStoreError(
                f"cannot read dead-man state file {self._path}: {error}"
            ) from None
        if len(raw) > MAX_DEADMAN_STATE_BYTES:
            raise DeadManStateStoreError(
                f"dead-man state file {self._path} exceeds the bounded"
                f" size limit ({len(raw)} >"
                f" {MAX_DEADMAN_STATE_BYTES} bytes)"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DeadManStateStoreError(
                f"dead-man state file {self._path} is not valid UTF-8:"
                f" {error}"
            ) from None
        try:
            return decode_deadman_status(text)
        except DeadManStateDecodeError as error:
            raise DeadManStateStoreError(
                f"corrupt dead-man state file {self._path}: {error}"
            ) from None

    def save(self, status: DeadManStatus) -> None:
        """Persist the dead-man status with atomic replacement.

        The status is encoded deterministically by the accepted H1A
        encoder; the bytes are written to a uniquely named temporary
        file in the SAME directory, flushed and fsynced, then moved
        onto the target with ``os.replace`` — readers observe either
        the complete previous state or the complete new state, never
        a partial file. Any write failure removes the temporary file,
        leaves the previous target content untouched and raises
        :class:`DeadManStateStoreError`. A document beyond the
        bounded size limit is rejected before the target is touched.
        """
        # A wrong input type is an explicit programmer error owned by
        # the accepted H1A encoder, not a store failure.
        data = encode_deadman_status(status).encode("utf-8")
        if len(data) > MAX_DEADMAN_STATE_BYTES:
            raise DeadManStateStoreError(
                "serialized dead-man state exceeds the bounded size"
                f" limit ({len(data)} > {MAX_DEADMAN_STATE_BYTES}"
                " bytes): refusing to write a state that could never"
                " be read back"
            )
        parent = self._path.parent
        try:
            fd, temporary_name = tempfile.mkstemp(
                dir=parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
        except OSError as error:
            raise DeadManStateStoreError(
                f"cannot create a temporary dead-man state file in"
                f" {parent}: {error}"
            ) from None
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            _remove_quietly(temporary_path)
            raise DeadManStateStoreError(
                f"cannot write the temporary dead-man state file"
                f" {temporary_path}: {error}"
            ) from None
        try:
            os.replace(temporary_path, self._path)
        except OSError as error:
            _remove_quietly(temporary_path)
            raise DeadManStateStoreError(
                f"cannot atomically replace the dead-man state file"
                f" {self._path}: {error}"
            ) from None
        _sync_directory_best_effort(parent)
