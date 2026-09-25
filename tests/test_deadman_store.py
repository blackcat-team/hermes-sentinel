"""Deterministic tests for the Stage H1B-1 dead-man state store.

Covers the authoritative H1B-1 store contract: the absent-file
canonical fresh start (the H1A INITIAL status), the strict
fail-closed read (corrupt / malformed / unsupported-schema /
non-UTF-8 documents and oversized files NEVER silently become
UNKNOWN), the deterministic H1A roundtrip, the atomic same-directory
replacement (temporary file -> flush/fsync -> os.replace, no partial
target exposure, no temporary debris on any failure path), the
symmetric bounded size limit on reads and writes, and the H1B-1
architectural boundaries. All filesystem interaction runs against
real per-test temporary directories — no repository files, no
network, no real clock.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# src-layout bootstrap: allows running the suite without installing
# the package (stdlib unittest has no pythonpath support; pytest gets
# the same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel import deadman_store  # noqa: E402
from hermes_sentinel.deadman import (  # noqa: E402
    INITIAL_DEADMAN_STATUS,
    DeadManNotification,
    DeadManNotificationKind,
    DeadManProbeOutcome,
    DeadManState,
    DeadManStatus,
    DeadManTransition,
    acknowledge_deadman_notification,
    advance_deadman_status,
    encode_deadman_status,
)
from hermes_sentinel.deadman_store import (  # noqa: E402
    MAX_DEADMAN_STATE_BYTES,
    DeadManStateStore,
    DeadManStateStoreError,
)

_UTC = timezone.utc
_T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=_UTC)
_MINUTE = timedelta(seconds=60)


def _advance(
    status: DeadManStatus,
    outcome: DeadManProbeOutcome,
    minutes: float,
) -> DeadManStatus:
    """One H1A evaluation at a deterministic explicit moment."""
    return advance_deadman_status(
        status=status,
        outcome=outcome,
        now=_T0 + timedelta(minutes=minutes),
    )


def _confirmed_down_status() -> DeadManStatus:
    """A real DOWN snapshot: three failures confirm the outage."""
    status = INITIAL_DEADMAN_STATUS
    for minutes in (0.0, 1.0, 2.0):
        status = _advance(status, DeadManProbeOutcome.FAILED, minutes)
    self_state = status.state
    assert self_state is DeadManState.DOWN
    return status


def _recovered_up_status() -> DeadManStatus:
    """The richest ledger shape: acknowledged DOWN, pending RECOVERED."""
    status = _confirmed_down_status()
    status = acknowledge_deadman_notification(status=status, notification_id=1)
    status = _advance(status, DeadManProbeOutcome.HEALTHY, 3.0)
    status = _advance(status, DeadManProbeOutcome.HEALTHY, 4.0)
    assert status.state is DeadManState.UP
    assert status.pending_notifications
    return status


def _pathological_backlog_status() -> DeadManStatus:
    """A valid status whose encoding exceeds the bounded size limit.

    Direct canonical construction: a UP snapshot with a contiguous
    run of unacknowledged DOWN intents (the pending kinds grammar
    ``RECOVERED? DOWN*`` holds), sized past the store bound.
    """
    count = 12000
    pending = tuple(
        DeadManNotification(
            notification_id=identifier,
            kind=DeadManNotificationKind.DOWN,
            transition=DeadManTransition(
                from_state=DeadManState.UP,
                to_state=DeadManState.DOWN,
                at=_T0,
            ),
        )
        for identifier in range(1, count + 1)
    )
    return DeadManStatus(
        state=DeadManState.UP,
        state_changed_at=_T0 + _MINUTE,
        pending_notifications=pending,
        next_notification_id=count + 1,
    )


class DeadManStateStoreConstructionTest(unittest.TestCase):
    """The store binds one state-file path, fail-closed."""

    def test_path_is_exposed(self) -> None:
        path = Path("state") / "deadman.json"
        self.assertEqual(DeadManStateStore(path).path, path)

    def test_non_path_rejected(self) -> None:
        for value in ("deadman.json", 42, None, b"deadman.json"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    DeadManStateStore(value)  # type: ignore[arg-type]


class StoreLoadTest(unittest.TestCase):
    """Fail-closed strict reads; absent means canonical fresh start."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "deadman-state.json"
        self.store = DeadManStateStore(self.path)

    def _write_bytes(self, data: bytes) -> None:
        self.path.write_bytes(data)

    def test_absent_file_is_the_canonical_initial_status(self) -> None:
        self.assertEqual(self.store.load(), INITIAL_DEADMAN_STATUS)

    def test_absent_file_is_unknown_not_an_error(self) -> None:
        self.assertIs(self.store.load().state, DeadManState.UNKNOWN)

    def test_valid_down_status_roundtrips(self) -> None:
        status = _confirmed_down_status()
        self.store.save(status)
        self.assertEqual(self.store.load(), status)

    def test_valid_recovered_status_roundtrips(self) -> None:
        status = _recovered_up_status()
        self.store.save(status)
        self.assertEqual(self.store.load(), status)

    def test_garbage_bytes_fail_closed(self) -> None:
        self._write_bytes(b"\x00\x01not json at all")
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("corrupt", str(caught.exception))

    def test_empty_file_fails_closed(self) -> None:
        self._write_bytes(b"")
        with self.assertRaises(DeadManStateStoreError):
            self.store.load()

    def test_wrong_schema_version_fails_closed(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        document["schema_version"] = 3
        self._write_bytes(json.dumps(document).encode("utf-8"))
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("corrupt", str(caught.exception))

    def test_unknown_state_value_fails_closed(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        document["state"] = "paused"
        self._write_bytes(json.dumps(document).encode("utf-8"))
        with self.assertRaises(DeadManStateStoreError):
            self.store.load()

    def test_missing_field_fails_closed(self) -> None:
        document = json.loads(encode_deadman_status(INITIAL_DEADMAN_STATUS))
        del document["next_notification_id"]
        self._write_bytes(json.dumps(document).encode("utf-8"))
        with self.assertRaises(DeadManStateStoreError):
            self.store.load()

    def test_non_utf8_file_fails_closed(self) -> None:
        self._write_bytes(b'{"schema_version": 4, "\xff\xfe": 1}')
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("UTF-8", str(caught.exception))

    def test_oversized_file_fails_closed_before_parsing(self) -> None:
        self._write_bytes(b"x" * (MAX_DEADMAN_STATE_BYTES + 1))
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("bounded size limit", str(caught.exception))

    def test_file_at_exactly_the_limit_still_parses(self) -> None:
        # Whitespace is inert to the strict JSON decoder, so a valid
        # document padded to exactly the bound is still loadable.
        body = encode_deadman_status(INITIAL_DEADMAN_STATUS).encode("utf-8")
        padded = body + b"\n" * (MAX_DEADMAN_STATE_BYTES - len(body))
        self.assertEqual(len(padded), MAX_DEADMAN_STATE_BYTES)
        self._write_bytes(padded)
        self.assertEqual(self.store.load(), INITIAL_DEADMAN_STATUS)

    def test_unreadable_path_fails_closed(self) -> None:
        directory = Path(self._tmp.name) / "a-directory"
        directory.mkdir()
        store = DeadManStateStore(directory)
        with self.assertRaises(DeadManStateStoreError) as caught:
            store.load()
        self.assertIn("cannot read", str(caught.exception))


class _ReadSpy:
    """Wraps one real binary stream, recording every read size.

    Deterministic seam for proving the load path's read bound: the
    stdlib ``_io.BufferedReader`` is an immutable C type whose
    methods cannot be patched, so the spy wraps the stream that the
    (patchable) ``Path.open`` produces instead. Optionally raises a
    synthetic exception from ``read`` to exercise the read failure
    boundary.
    """

    def __init__(self, stream, read_error: BaseException | None = None):
        self._stream = stream
        self._read_error = read_error
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        self.read_sizes.append(size)
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stream.close()


class BoundedStateReadTest(unittest.TestCase):
    """The load path is genuinely bounded (regression coverage).

    The size limit is enforced BY the read itself: at most one byte
    beyond the bound is ever drawn from the file, the unbounded
    whole-file read path is never used, and read failures stay
    bounded store errors.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "deadman-state.json"
        self.store = DeadManStateStore(self.path)

    def _write_bytes(self, data: bytes) -> None:
        self.path.write_bytes(data)

    def _spy_reads(
        self, read_error: BaseException | None = None
    ) -> list[_ReadSpy]:
        """Install the Path.open-wrapping read spy; returns the spies."""
        original_open = Path.open
        spies: list[_ReadSpy] = []

        def spying_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            spy = _ReadSpy(stream, read_error)
            spies.append(spy)
            return spy

        patcher = mock.patch.object(Path, "open", spying_open)
        patcher.start()
        self.addCleanup(patcher.stop)
        return spies

    def test_oversized_file_reads_at_most_limit_plus_one_byte(self) -> None:
        self._write_bytes(b"x" * (MAX_DEADMAN_STATE_BYTES + 4096))
        spies = self._spy_reads()
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("bounded size limit", str(caught.exception))
        # Exactly one read, bounded to limit + 1: the oversized
        # remainder was never drawn into memory.
        self.assertEqual(len(spies), 1)
        self.assertEqual(spies[0].read_sizes, [MAX_DEADMAN_STATE_BYTES + 1])

    def test_valid_file_reads_at_most_limit_plus_one_byte(self) -> None:
        self._write_bytes(
            encode_deadman_status(INITIAL_DEADMAN_STATUS).encode("utf-8")
        )
        spies = self._spy_reads()
        self.assertEqual(self.store.load(), INITIAL_DEADMAN_STATUS)
        self.assertEqual(len(spies), 1)
        self.assertEqual(spies[0].read_sizes, [MAX_DEADMAN_STATE_BYTES + 1])

    def test_load_never_uses_the_unbounded_read_path(self) -> None:
        # Prove the whole-file read API is not on the load path at
        # all: a valid document loads cleanly with read_bytes bombed.
        self._write_bytes(
            encode_deadman_status(INITIAL_DEADMAN_STATUS).encode("utf-8")
        )
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("unbounded whole-file read"),
        ):
            self.assertEqual(self.store.load(), INITIAL_DEADMAN_STATUS)

    def test_read_failure_is_a_bounded_store_error(self) -> None:
        self._write_bytes(b'{"schema_version": 4}')
        self._spy_reads(read_error=OSError("read denied"))
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("cannot read", str(caught.exception))

    def test_file_at_exactly_the_limit_still_loads(self) -> None:
        # One byte beyond the bound decides; exactly-at-limit is not
        # oversized, so the padded-but-valid document still decodes.
        body = encode_deadman_status(INITIAL_DEADMAN_STATUS).encode("utf-8")
        padded = body + b"\n" * (MAX_DEADMAN_STATE_BYTES - len(body))
        self.assertEqual(len(padded), MAX_DEADMAN_STATE_BYTES)
        self._write_bytes(padded)
        self.assertEqual(self.store.load(), INITIAL_DEADMAN_STATUS)

    def test_one_byte_over_the_limit_is_rejected(self) -> None:
        body = encode_deadman_status(INITIAL_DEADMAN_STATUS).encode("utf-8")
        padded = body + b"\n" * (MAX_DEADMAN_STATE_BYTES - len(body) + 1)
        self.assertEqual(len(padded), MAX_DEADMAN_STATE_BYTES + 1)
        self._write_bytes(padded)
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.load()
        self.assertIn("bounded size limit", str(caught.exception))


class StoreSaveTest(unittest.TestCase):
    """Atomic deterministic writes with no partial exposure."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "deadman-state.json"
        self.store = DeadManStateStore(self.path)

    def _directory_entries(self) -> set[str]:
        return {entry.name for entry in self.path.parent.iterdir()}

    def test_save_writes_the_exact_h1a_encoding(self) -> None:
        status = _confirmed_down_status()
        self.store.save(status)
        self.assertEqual(
            self.path.read_bytes(),
            encode_deadman_status(status).encode("utf-8"),
        )

    def test_save_creates_no_debris(self) -> None:
        self.store.save(_confirmed_down_status())
        self.assertEqual(self._directory_entries(), {self.path.name})

    def test_save_replaces_previous_state_atomically(self) -> None:
        first = _confirmed_down_status()
        self.store.save(first)
        second = acknowledge_deadman_notification(
            status=first, notification_id=1
        )
        self.store.save(second)
        self.assertEqual(
            self.path.read_bytes(),
            encode_deadman_status(second).encode("utf-8"),
        )
        self.assertEqual(self._directory_entries(), {self.path.name})

    def test_roundtrip_after_replacement(self) -> None:
        self.store.save(INITIAL_DEADMAN_STATUS)
        status = _recovered_up_status()
        self.store.save(status)
        self.assertEqual(self.store.load(), status)

    def test_failed_replace_leaves_target_untouched_and_cleans_up(
        self,
    ) -> None:
        original = encode_deadman_status(
            INITIAL_DEADMAN_STATUS
        ).encode("utf-8")
        self.path.write_bytes(original)
        with mock.patch(
            "os.replace", side_effect=OSError("replace denied")
        ):
            with self.assertRaises(DeadManStateStoreError) as caught:
                self.store.save(_confirmed_down_status())
        self.assertIn("atomically replace", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self._directory_entries(), {self.path.name})

    def test_failed_fsync_leaves_target_untouched_and_cleans_up(
        self,
    ) -> None:
        original = encode_deadman_status(
            INITIAL_DEADMAN_STATUS
        ).encode("utf-8")
        self.path.write_bytes(original)
        with mock.patch("os.fsync", side_effect=OSError("fsync denied")):
            with self.assertRaises(DeadManStateStoreError) as caught:
                self.store.save(_confirmed_down_status())
        self.assertIn(
            "cannot write the temporary dead-man state file",
            str(caught.exception),
        )
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self._directory_entries(), {self.path.name})

    def test_failed_write_creates_no_target_and_no_debris(self) -> None:
        with mock.patch("os.fsync", side_effect=OSError("fsync denied")):
            with self.assertRaises(DeadManStateStoreError):
                self.store.save(_confirmed_down_status())
        self.assertFalse(self.path.exists())
        self.assertEqual(self._directory_entries(), set())

    def test_missing_parent_directory_fails_closed(self) -> None:
        store = DeadManStateStore(
            Path(self._tmp.name) / "missing" / "deadman-state.json"
        )
        with self.assertRaises(DeadManStateStoreError) as caught:
            store.save(INITIAL_DEADMAN_STATUS)
        self.assertIn("cannot create", str(caught.exception))

    def test_target_replaced_by_a_directory_fails_closed(self) -> None:
        self.path.mkdir()
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.save(_confirmed_down_status())
        self.assertIn("atomically replace", str(caught.exception))
        self.assertTrue(self.path.is_dir())

    def test_oversized_document_rejected_before_touching_target(
        self,
    ) -> None:
        original = encode_deadman_status(
            INITIAL_DEADMAN_STATUS
        ).encode("utf-8")
        self.path.write_bytes(original)
        with self.assertRaises(DeadManStateStoreError) as caught:
            self.store.save(_pathological_backlog_status())
        self.assertIn("bounded size limit", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self._directory_entries(), {self.path.name})

    def test_non_status_input_is_the_encoders_error(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("not a status")  # type: ignore[arg-type]


class StageBoundariesTest(unittest.TestCase):
    """H1B-1 store stays a bounded filesystem adapter."""

    _MODULE_SOURCE = inspect.getsource(deadman_store)

    def test_public_surface_is_bounded(self) -> None:
        self.assertEqual(
            sorted(deadman_store.__all__),
            [
                "DeadManStateStore",
                "DeadManStateStoreError",
                "MAX_DEADMAN_STATE_BYTES",
            ],
        )

    def test_module_imports_no_migration_or_backup_machinery(self) -> None:
        # Precise import check: the store is stdlib path I/O only.
        imported = {
            name
            for name, module in inspect.getmembers(
                deadman_store, inspect.ismodule
            )
        }
        for forbidden in ("shutil", "sqlite3", "subprocess"):
            self.assertNotIn(forbidden, imported)

    def test_no_state_machine_or_acknowledgement_authority(self) -> None:
        for forbidden in (
            "advance_deadman_status",
            "acknowledge_deadman_notification",
        ):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)

    def test_no_clock_or_scheduling(self) -> None:
        for forbidden in (
            "time.time",
            "monotonic(",
            "utcnow",
            "datetime.now",
            "sleep(",
        ):
            self.assertNotIn(forbidden, self._MODULE_SOURCE)

    def test_no_environment_or_logging_access(self) -> None:
        for forbidden in (
            "os.environ",
            "environ[",
            "environ.get",
            "getenv",
            "print(",
            "logging",
            "logger",
        ):
            self.assertNotIn(forbidden.lower(), self._MODULE_SOURCE.lower())


if __name__ == "__main__":
    unittest.main()
