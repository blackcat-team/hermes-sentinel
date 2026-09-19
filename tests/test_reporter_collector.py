"""Deterministic fixture-driven tests for the Stage C1 host reporter.

Production calculation authority is ``scripts/sentinel-report.sh``
(bash). This module is only a test harness: it provides fixtures,
executes the real shell collector under the real bash available on
this machine, parses the resulting JSON with the stdlib and proves
the accepted Stage B3 decoder (``decode_heartbeat_payload``) accepts
the payload. There is deliberately NO second Python implementation of
the collector here.

All collector inputs are synthetic deterministic fixtures under
``tests/fixtures/reporter`` or adversarial temp files written by these
tests — never the developer machine's live /proc values.

Stage C2 note: the production executable now delivers the payload via
HTTPS. These C1 tests therefore source the real script and invoke its
sourceable ``collect_payload`` function (the single collection
authority) directly — see ``_run_collector``. Transport behaviour is
covered by ``tests/test_reporter_transport.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from unittest import mock

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.wire import decode_heartbeat_payload  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sentinel-report.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "reporter"

# --- synthetic fixture values (single source of truth for assertions) ---

UPTIME_EXPECTED = 758901.33
LOAD_ONE = 0.28
LOAD_FIVE = 0.32
LOAD_FIFTEEN = 0.25

# CPU: sample A idle_all=820 (800+20), non_idle=170 -> total 990;
#      sample B idle_all=850, non_idle=260 -> total 1110;
#      delta_total=120, delta_idle=30 -> 100*90/120 = 75.00.
# guest/guest_nice columns (30->60) are present and must be ignored:
# wrongly counting guest would give 80.00 instead of 75.00.
CPU_EXPECTED = 75.0
CPU_GUEST_WRONG_VALUE = 80.0

MEM_TOTAL_KIB = 8035328
MEM_AVAILABLE_KIB = 4567890
MEM_FREE_KIB = 1234567  # deliberately different from MemAvailable
RAM_TOTAL_BYTES = MEM_TOTAL_KIB * 1024
RAM_USED_BYTES = (MEM_TOTAL_KIB - MEM_AVAILABLE_KIB) * 1024
RAM_USED_IF_MEMFREE = (MEM_TOTAL_KIB - MEM_FREE_KIB) * 1024

SWAP_TOTAL_KIB = 2097148
SWAP_FREE_KIB = 1048574
SWAP_TOTAL_BYTES = SWAP_TOTAL_KIB * 1024
SWAP_USED_BYTES = (SWAP_TOTAL_KIB - SWAP_FREE_KIB) * 1024

DF_BYTES_USED = 12345678901
DF_BYTES_TOTAL = 20511336448
DF_INODES_USED = 456789
DF_INODES_TOTAL = 1310720

TOP_LEVEL_KEYS = {
    "node",
    "reported_at",
    "uptime_seconds",
    "load",
    "cpu_percent",
    "ram",
    "swap",
    "root_fs",
    "root_inodes",
}
RESOURCE_KEYS = {"used", "total", "percent"}
LOAD_KEYS = {"one", "five", "fifteen"}
REPORTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

# --- real bash discovery (native POSIX, MSYS/Git Bash or WSL) ------------


def _bash_candidates() -> list[str]:
    """Candidate bash executables in deterministic priority order."""
    candidates: list[str] = []
    on_path = shutil.which("bash")
    if on_path is not None:
        candidates.append(on_path)
    git = shutil.which("git")
    if git is not None:
        git_root = Path(git).resolve().parent.parent
        candidates.append(str(git_root / "bin" / "bash.exe"))
        candidates.append(str(git_root / "usr" / "bin" / "bash.exe"))
    candidates.extend(
        [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Windows\System32\bash.exe",
        ]
    )
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen or not Path(candidate).exists():
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _probe_bash(bash: str) -> str:
    """Return the `uname -s` of a working bash, or an empty string."""
    try:
        proc = subprocess.run(
            [bash, "-c", "uname -s"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _classify_bash(uname: str, host_os: str) -> str:
    """Classify a probed bash for path handling.

    ``uname`` is the probed ``uname -s`` output of the bash; ``host_os``
    is the ``os.name`` of the Python test process driving it.  A bare
    ``uname -s == Linux`` cannot distinguish native Linux bash from WSL
    bash — both report ``Linux``.  The decisive context is the host of
    the test process itself: WSL bash is only reachable from a
    Windows-hosted (``nt``) Python, while a POSIX-hosted Python that
    selected a Linux bash is running NATIVE bash whose paths are
    already POSIX and must never be routed through ``wslpath``.
    """
    if "MINGW" in uname or "MSYS" in uname or "CYGWIN" in uname:
        return "msys"
    if "Linux" in uname:
        return "wsl" if host_os == "nt" else "posix"
    return ""


def _discover_bash() -> tuple[str | None, str]:
    for candidate in _bash_candidates():
        uname = _probe_bash(candidate)
        if not uname:
            continue
        flavor = _classify_bash(uname, os.name)
        if flavor:
            return candidate, flavor
    return None, ""


BASH, BASH_FLAVOR = _discover_bash()
_WSL_PATH_CACHE: dict[str, str] = {}


def setUpModule() -> None:
    if BASH is None:
        raise RuntimeError(
            "C1 runtime verification requires a real bash on this "
            "machine; none was found on PATH or in standard Git/WSL "
            "locations (probed: "
            + ", ".join(_bash_candidates())
            + ")"
        )


def _bash() -> str:
    assert BASH is not None
    return BASH


def _bash_path(path: Path, flavor: str | None = None) -> str:
    """Path form usable as a bash command argument.

    ``flavor`` overrides the discovered ``BASH_FLAVOR`` (deterministic
    helper-level testing).  Native POSIX bash receives native POSIX
    paths untranslated; MSYS/Git Bash accepts the POSIX spelling of a
    resolved Windows path; only actual WSL bash needs ``wslpath``
    translation of the POSIX-spelled Windows path.
    """
    resolved = path.resolve()
    active = BASH_FLAVOR if flavor is None else flavor
    if active in ("msys", "posix"):
        return resolved.as_posix()
    key = str(resolved)
    if key in _WSL_PATH_CACHE:
        return _WSL_PATH_CACHE[key]
    proc = subprocess.run(
        [_bash(), "-c", f"wslpath -a '{resolved.as_posix()}'"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"cannot translate path for WSL bash: {key}")
    converted = proc.stdout.strip()
    _WSL_PATH_CACHE[key] = converted
    return converted


def _env_path(path: Path) -> str:
    """Path form usable as an environment-variable seam value."""
    return path.resolve().as_posix()


def _fixture_env(
    node: str | None = "Prod", **overrides: str | None
) -> dict[str, str]:
    """Environment for one collector run against the nominal fixtures.

    ``None`` values remove the variable entirely (used to prove the
    missing-node / missing-source failure paths).
    """
    values: dict[str, str | None] = {
        "SENTINEL_NODE": node,
        "SENTINEL_PROC_UPTIME": _env_path(FIXTURES / "proc_uptime.txt"),
        "SENTINEL_PROC_LOADAVG": _env_path(FIXTURES / "proc_loadavg.txt"),
        "SENTINEL_PROC_MEMINFO": _env_path(FIXTURES / "proc_meminfo.txt"),
        "SENTINEL_PROC_STAT_A": _env_path(FIXTURES / "proc_stat_a.txt"),
        "SENTINEL_PROC_STAT_B": _env_path(FIXTURES / "proc_stat_b.txt"),
        "SENTINEL_DF_BYTES_FILE": _env_path(FIXTURES / "df_root_bytes.txt"),
        "SENTINEL_DF_INODES_FILE": _env_path(FIXTURES / "df_root_inodes.txt"),
    }
    values.update(overrides)
    env = dict(os.environ)
    for key, value in values.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    if BASH_FLAVOR == "wsl":
        # WSL forwards only variables listed in WSLENV; the /p flag
        # translates each Windows path value into its WSL form.
        parts = [
            key if key == "SENTINEL_NODE" else f"{key}/p"
            for key, value in values.items()
            if value is not None
        ]
        inherited = env.get("WSLENV", "")
        env["WSLENV"] = ":".join(parts + ([inherited] if inherited else []))
    return env


def _run_collector(
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Bounded Stage C2 harness adaptation: executing the script now
    performs HTTPS delivery, so the C1 fixture tests exercise the
    ACTUAL production collection authority instead — the real script
    is sourced (a no-op at source time by design) and its sourceable
    ``collect_payload`` function is invoked directly. No collection
    logic is duplicated and no production bypass variable exists."""
    code = f'source "{_bash_path(SCRIPT)}"\ncollect_payload\n'
    return subprocess.run(
        [_bash(), "-c", code],
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _run_bash_code(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@contextlib.contextmanager
def _temp_fixture(content: str) -> Iterator[Path]:
    """Adversarial fixture file with guaranteed LF line endings."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fixture.txt"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        yield path


def _successful_payload(
    node: str | None = "Prod",
    **overrides: str | None,
) -> dict[str, object]:
    proc = _run_collector(_fixture_env(node=node, **overrides))
    if proc.returncode != 0:
        raise AssertionError(
            f"collector failed unexpectedly: rc={proc.returncode} "
            f"stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


# --- file purity / environment -------------------------------------------


class CollectorEnvironmentTest(unittest.TestCase):
    def test_script_shebang_is_env_bash(self) -> None:
        first_line = SCRIPT.read_bytes().split(b"\n", 1)[0]
        self.assertEqual(first_line, b"#!/usr/bin/env bash")

    def test_script_and_fixtures_use_unix_line_endings(self) -> None:
        """CRLF would break bash parsing and /proc-style fixtures."""
        paths = [SCRIPT, *sorted(FIXTURES.iterdir())]
        self.assertGreater(len(paths), 1)
        for path in paths:
            self.assertNotIn(b"\r", path.read_bytes(), msg=str(path))

    def test_real_bash_is_available(self) -> None:
        """Runtime evidence: a real bash executes on this machine."""
        self.assertIsNotNone(BASH)
        proc = _run_bash_code("uname -s")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.strip())


# --- bash flavor classification / path semantics (CI incident regression) --


class BashFlavorSemanticsTest(unittest.TestCase):
    """Deterministic helper-level proofs of the bash flavor contract.

    Regression for the first GitHub Actions run: native Ubuntu bash
    also reports ``uname -s == Linux``, and classifying it as WSL
    routed native POSIX paths through ``wslpath`` (absent on the
    runner), failing 109 tests.  Classification must therefore use the
    test-process host context, and native POSIX paths must reach bash
    untranslated.
    """

    _WSL_KEY = str(Path("E:/repo/scripts/sentinel-report.sh").resolve())

    def setUp(self) -> None:
        _WSL_PATH_CACHE.pop(self._WSL_KEY, None)
        self.addCleanup(_WSL_PATH_CACHE.pop, self._WSL_KEY, None)

    def test_native_linux_bash_on_posix_host_is_posix_not_wsl(self) -> None:
        """The incident: a Linux-reporting bash driven by a POSIX-hosted
        Python is NATIVE bash, never WSL."""
        self.assertEqual(_classify_bash("Linux", "posix"), "posix")

    def test_linux_bash_from_windows_host_is_wsl(self) -> None:
        self.assertEqual(_classify_bash("Linux", "nt"), "wsl")

    def test_msys_family_classifies_msys_on_both_hosts(self) -> None:
        for uname in (
            "MINGW64_NT-10.0-26200",
            "MSYS_NT-10.0-26200",
            "CYGWIN_NT-10.0-26200",
        ):
            for host_os in ("nt", "posix"):
                self.assertEqual(_classify_bash(uname, host_os), "msys")

    def test_unclassifiable_uname_is_rejected_fail_closed(self) -> None:
        self.assertEqual(_classify_bash("Darwin", "posix"), "")
        self.assertEqual(_classify_bash("", "nt"), "")

    def test_discovered_flavor_matches_classifier_for_local_bash(self) -> None:
        """The module-level discovery agrees with the classifier for the
        bash actually selected on this machine (msys here, posix on the
        GitHub Actions Linux runner)."""
        if BASH is None:
            self.skipTest("no real bash discovered on this machine")
        self.assertEqual(
            BASH_FLAVOR, _classify_bash(_probe_bash(_bash()), os.name)
        )

    def test_posix_flavor_passes_path_through_with_no_translator(self) -> None:
        """Native POSIX path handling performs ZERO subprocess work — no
        wslpath, no cygpath — and the path reaches bash in its resolved
        POSIX spelling (on a Linux host: byte-identical native path)."""

        def forbidden(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(
                "posix flavor must not invoke any path-translator subprocess"
            )

        native = Path(
            "/home/runner/work/hermes-sentinel/hermes-sentinel"
            "/scripts/sentinel-report.sh"
        )
        with mock.patch.object(subprocess, "run", side_effect=forbidden):
            converted = _bash_path(native, flavor="posix")
        self.assertEqual(converted, native.resolve().as_posix())
        self.assertNotIn("\\", converted)

    def test_msys_flavor_keeps_posix_drive_spelling(self) -> None:
        """Existing MSYS contract preserved: the POSIX spelling of the
        resolved (Windows) path, exactly as before the fix."""
        self.assertEqual(
            _bash_path(SCRIPT, flavor="msys"), SCRIPT.resolve().as_posix()
        )

    def test_wsl_flavor_still_translates_via_wslpath_subprocess(self) -> None:
        """Existing WSL contract preserved: the wsl branch routes the
        POSIX-spelled Windows path through a real ``wslpath`` invocation
        of the discovered bash (captured here, never executed) and
        caches the translation."""
        source = Path("E:/repo/scripts/sentinel-report.sh")
        converted = "/mnt/e/repo/scripts/sentinel-report.sh"
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=converted + "\n", stderr=""
        )
        with mock.patch.object(
            subprocess, "run", return_value=fake
        ) as runner:
            first = _bash_path(source, flavor="wsl")
            second = _bash_path(source, flavor="wsl")
        self.assertEqual(first, converted)
        self.assertEqual(second, converted)
        runner.assert_called_once()  # the second call hits the cache
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], _bash())
        self.assertIn("wslpath", argv[-1])
        self.assertIn(source.resolve().as_posix(), argv[-1])


# --- success contract + B3 oracle ----------------------------------------


class SuccessPayloadTest(unittest.TestCase):
    """The exact fixture payload is accepted by the accepted B3 decoder."""

    def test_fixture_payload_accepted_by_b3_decoder(self) -> None:
        payload = _successful_payload()
        # The accepted Stage B3 decoder is the oracle — not a local
        # duplicate schema.
        telemetry = decode_heartbeat_payload(payload)
        self.assertEqual(telemetry.host, "Prod")
        self.assertEqual(telemetry.uptime_seconds, UPTIME_EXPECTED)
        self.assertEqual(telemetry.load.one, LOAD_ONE)
        self.assertEqual(telemetry.load.five, LOAD_FIVE)
        self.assertEqual(telemetry.load.fifteen, LOAD_FIFTEEN)
        self.assertEqual(telemetry.cpu_percent, CPU_EXPECTED)
        self.assertEqual(telemetry.ram.used, RAM_USED_BYTES)
        self.assertEqual(telemetry.ram.total, RAM_TOTAL_BYTES)
        self.assertEqual(telemetry.swap.used, SWAP_USED_BYTES)
        self.assertEqual(telemetry.swap.total, SWAP_TOTAL_BYTES)
        self.assertEqual(telemetry.root_filesystem.used, DF_BYTES_USED)
        self.assertEqual(telemetry.root_filesystem.total, DF_BYTES_TOTAL)
        self.assertEqual(telemetry.root_inodes.used, DF_INODES_USED)
        self.assertEqual(telemetry.root_inodes.total, DF_INODES_TOTAL)
        self.assertIsNotNone(telemetry.timestamp.tzinfo)
        self.assertIsNotNone(telemetry.timestamp.utcoffset())

    def test_stdout_is_exactly_one_json_document(self) -> None:
        proc = _run_collector(_fixture_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        # Exactly one line terminated by a single newline, pure JSON,
        # no diagnostic prose, no BOM.
        self.assertTrue(proc.stdout.startswith("{"))
        self.assertTrue(proc.stdout.endswith("}\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertNotIn("sentinel-report", proc.stdout)
        self.assertNotIn("\ufeff", proc.stdout)
        json.loads(proc.stdout)

    def test_exact_top_level_and_nested_key_sets(self) -> None:
        """Independent key-set assertion — no extra fields anywhere."""
        payload = _successful_payload()
        self.assertEqual(set(payload), TOP_LEVEL_KEYS)
        load = payload["load"]
        assert isinstance(load, dict)
        self.assertEqual(set(load), LOAD_KEYS)
        for name in ("ram", "swap", "root_fs", "root_inodes"):
            block = payload[name]
            assert isinstance(block, dict)
            self.assertEqual(set(block), RESOURCE_KEYS)

    def test_no_forbidden_fields_anywhere(self) -> None:
        payload = _successful_payload()
        for forbidden in (
            "received_at",
            "token",
            "state",
            "services",
            "health",
            "hostname",
            "kernel",
            "ip",
        ):
            self.assertNotIn(forbidden, payload)
        raw = json.dumps(payload)
        for forbidden in ("received_at", "token", "state", "services"):
            self.assertNotIn(forbidden, raw)

    def test_metric_values_are_json_numbers_not_strings(self) -> None:
        payload = _successful_payload()
        numeric_paths: list[tuple[str, ...]] = [("uptime_seconds",), ("cpu_percent",)]
        for group in ("load",):
            numeric_paths.extend((group, key) for key in LOAD_KEYS)
        for group in ("ram", "swap", "root_fs", "root_inodes"):
            numeric_paths.extend((group, key) for key in RESOURCE_KEYS)
        for path in numeric_paths:
            value: object = payload
            for part in path:
                assert isinstance(value, dict)
                value = value[part]
            self.assertIsInstance(value, (int, float), msg=str(path))
            self.assertNotIsInstance(value, bool, msg=str(path))

    def test_all_percentages_stay_within_bounds(self) -> None:
        payload = _successful_payload()
        for group in ("ram", "swap", "root_fs", "root_inodes"):
            block = payload[group]
            assert isinstance(block, dict)
            percent = block["percent"]
            assert isinstance(percent, (int, float))
            self.assertGreaterEqual(percent, 0.0, msg=group)
            self.assertLessEqual(percent, 100.0, msg=group)
        cpu = payload["cpu_percent"]
        assert isinstance(cpu, (int, float))
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)

    def test_reported_at_is_timezone_aware_utc_iso8601(self) -> None:
        payload = _successful_payload()
        reported_at = payload["reported_at"]
        assert isinstance(reported_at, str)
        self.assertRegex(reported_at, REPORTED_AT_RE)
        parsed = datetime.fromisoformat(reported_at)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertIsNotNone(parsed.utcoffset())
        self.assertEqual(parsed.utcoffset().total_seconds(), 0.0)


# --- uptime / load --------------------------------------------------------


class UptimeLoadTest(unittest.TestCase):
    def test_uptime_parsed_from_proc_uptime(self) -> None:
        payload = _successful_payload()
        self.assertEqual(payload["uptime_seconds"], UPTIME_EXPECTED)

    def test_load_1_5_15_parsed_from_proc_loadavg(self) -> None:
        payload = _successful_payload()
        load = payload["load"]
        assert isinstance(load, dict)
        self.assertEqual(load["one"], LOAD_ONE)
        self.assertEqual(load["five"], LOAD_FIVE)
        self.assertEqual(load["fifteen"], LOAD_FIFTEEN)

    def test_uptime_malformed_value_fails(self) -> None:
        with _temp_fixture("not-a-number 1.0\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_uptime_nan_text_fails(self) -> None:
        with _temp_fixture("nan 1.0\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_uptime_negative_value_fails(self) -> None:
        with _temp_fixture("-100.0 5.0\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_uptime_extra_noise_line_fails(self) -> None:
        with _temp_fixture("100.0 5.0\nnoise\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_uptime_missing_file_fails(self) -> None:
        missing = _env_path(FIXTURES / "does-not-exist.txt")
        proc = _run_collector(_fixture_env(SENTINEL_PROC_UPTIME=missing))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_uptime_empty_file_fails(self) -> None:
        with _temp_fixture("") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_loadavg_negative_value_fails(self) -> None:
        with _temp_fixture("0.10 -1.5 0.20 1/1 1\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_LOADAVG=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


# --- CPU -------------------------------------------------------------------


class CpuTest(unittest.TestCase):
    def test_cpu_percent_formula_from_two_known_samples(self) -> None:
        payload = _successful_payload()
        self.assertEqual(payload["cpu_percent"], CPU_EXPECTED)

    def test_guest_counters_not_double_counted(self) -> None:
        """The fixtures carry guest 30->60: counting guest again would
        produce 80.00 instead of the correct 75.00."""
        payload = _successful_payload()
        self.assertEqual(payload["cpu_percent"], CPU_EXPECTED)
        self.assertNotEqual(payload["cpu_percent"], CPU_GUEST_WRONG_VALUE)

    def test_cpu_counter_regression_fails(self) -> None:
        # user decreases from 100 (sample A) to 90 -> counters must
        # not decrease; fail closed.
        regressed = "cpu  90 0 80 830 20 5 15 10 60 0\ncpu0 45 0 40 415 10 2 7 5 30 0\n"
        with _temp_fixture(regressed) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_STAT_B=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_cpu_total_delta_zero_fails(self) -> None:
        # Identical samples: total_delta == 0 is a division-by-zero
        # path and must fail closed, never produce a number.
        sample_a = _env_path(FIXTURES / "proc_stat_a.txt")
        proc = _run_collector(_fixture_env(SENTINEL_PROC_STAT_B=sample_a))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_cpu_missing_aggregate_line_fails(self) -> None:
        with _temp_fixture("cpu0 50 0 25 400 10 2 5 2 15 0\nintr 1\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_STAT_A=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_cpu_short_line_fails(self) -> None:
        with _temp_fixture("cpu 100 0 50 800 20 5 10\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_STAT_A=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_cpu_non_integer_counter_fails(self) -> None:
        with _temp_fixture("cpu 100.5 0 50 800 20 5 10 5 30 0\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_STAT_A=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_cpu_huge_finite_counters_do_not_overflow(self) -> None:
        """No shell integer arithmetic: huge-but-finite jiffy counters
        (10^15 scale, far beyond 32-bit) still yield a valid percent."""
        sample_a = "cpu 1000000000000000 1000000000000000 1000000000000000 1000000000000000 1000000000000000 1000000000000000 1000000000000000 1000000000000000 0 0\n"
        sample_b = "cpu 1000000000001000 1000000000000000 1000000000000500 1000000000007000 1000000000000000 1000000000000000 1000000000000500 1000000000000000 0 0\n"
        with _temp_fixture(sample_a) as fixture_a, _temp_fixture(
            sample_b
        ) as fixture_b:
            proc = _run_collector(
                _fixture_env(
                    SENTINEL_PROC_STAT_A=_env_path(fixture_a),
                    SENTINEL_PROC_STAT_B=_env_path(fixture_b),
                )
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        cpu = payload["cpu_percent"]
        assert isinstance(cpu, (int, float))
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)


# --- RAM -------------------------------------------------------------------


class RamTest(unittest.TestCase):
    def test_ram_uses_memavailable_not_memfree(self) -> None:
        payload = _successful_payload()
        ram = payload["ram"]
        assert isinstance(ram, dict)
        self.assertEqual(ram["used"], RAM_USED_BYTES)
        self.assertNotEqual(ram["used"], RAM_USED_IF_MEMFREE)

    def test_ram_kib_converted_to_bytes_exactly(self) -> None:
        payload = _successful_payload()
        ram = payload["ram"]
        assert isinstance(ram, dict)
        self.assertEqual(ram["total"], MEM_TOTAL_KIB * 1024)
        self.assertEqual(ram["used"], (MEM_TOTAL_KIB - MEM_AVAILABLE_KIB) * 1024)

    def test_ram_percent_matches_formula(self) -> None:
        payload = _successful_payload()
        ram = payload["ram"]
        assert isinstance(ram, dict)
        percent = ram["percent"]
        assert isinstance(percent, (int, float))
        expected = RAM_USED_BYTES / RAM_TOTAL_BYTES * 100.0
        self.assertAlmostEqual(percent, expected, delta=0.011)

    def test_ram_missing_memavailable_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemFree:         {MEM_FREE_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_ram_missing_memtotal_fails(self) -> None:
        content = (
            f"MemFree:         {MEM_FREE_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_meminfo_duplicate_required_key_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
            f"SwapTotal:       {SWAP_TOTAL_KIB} kB\n"
            f"SwapFree:        {SWAP_FREE_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_ram_memavailable_exceeds_memtotal_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_TOTAL_KIB + 1} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_meminfo_malformed_value_fails(self) -> None:
        content = "MemTotal:        12x kB\nMemAvailable:    1 kB\n"
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_meminfo_wrong_unit_fails(self) -> None:
        content = "MemTotal:        123 MB\nMemAvailable:    1 kB\n"
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_ram_huge_finite_values_convert_exactly(self) -> None:
        total_kib = 4_000_000_000_000  # 4 TiB-scale, still exact in KiB->bytes
        content = (
            f"MemTotal:        {total_kib} kB\n"
            f"MemAvailable:    {total_kib // 2} kB\n"
            f"SwapTotal:       {SWAP_TOTAL_KIB} kB\n"
            f"SwapFree:        {SWAP_FREE_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        ram = payload["ram"]
        assert isinstance(ram, dict)
        self.assertEqual(ram["total"], total_kib * 1024)
        self.assertEqual(ram["used"], (total_kib - total_kib // 2) * 1024)


# --- swap ------------------------------------------------------------------


class SwapTest(unittest.TestCase):
    def test_swap_normal_usage(self) -> None:
        payload = _successful_payload()
        swap = payload["swap"]
        assert isinstance(swap, dict)
        self.assertEqual(swap["used"], SWAP_USED_BYTES)
        self.assertEqual(swap["total"], SWAP_TOTAL_BYTES)
        self.assertAlmostEqual(
            swap["percent"],  # type: ignore[arg-type]
            SWAP_USED_BYTES / SWAP_TOTAL_BYTES * 100.0,
            delta=0.011,
        )

    def test_swap_absent_is_zero_zero_zero(self) -> None:
        payload = _successful_payload(
            SENTINEL_PROC_MEMINFO=_env_path(
                FIXTURES / "proc_meminfo_no_swap.txt"
            )
        )
        swap = payload["swap"]
        assert isinstance(swap, dict)
        self.assertEqual(swap["used"], 0)
        self.assertEqual(swap["total"], 0)
        self.assertEqual(swap["percent"], 0)
        # The absent-swap payload is still valid for the B3 decoder.
        decode_heartbeat_payload(payload)

    def test_swap_free_exceeds_total_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
            f"SwapTotal:       100 kB\n"
            f"SwapFree:        200 kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_swap_total_zero_with_nonzero_free_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
            "SwapTotal:       0 kB\n"
            "SwapFree:        5 kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_swap_missing_swapfree_fails(self) -> None:
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
            f"SwapTotal:       {SWAP_TOTAL_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


# --- root filesystem / inodes ----------------------------------------------


class RootFsTest(unittest.TestCase):
    def test_root_fs_bytes_mapping(self) -> None:
        payload = _successful_payload()
        root_fs = payload["root_fs"]
        assert isinstance(root_fs, dict)
        self.assertEqual(root_fs["used"], DF_BYTES_USED)
        self.assertEqual(root_fs["total"], DF_BYTES_TOTAL)
        expected = DF_BYTES_USED / DF_BYTES_TOTAL * 100.0
        self.assertAlmostEqual(
            root_fs["percent"],  # type: ignore[arg-type]
            expected,
            delta=0.011,
        )

    def test_root_inodes_are_counts_not_bytes(self) -> None:
        payload = _successful_payload()
        root_inodes = payload["root_inodes"]
        assert isinstance(root_inodes, dict)
        self.assertEqual(root_inodes["used"], DF_INODES_USED)
        self.assertEqual(root_inodes["total"], DF_INODES_TOTAL)
        expected = DF_INODES_USED / DF_INODES_TOTAL * 100.0
        self.assertAlmostEqual(
            root_inodes["percent"],  # type: ignore[arg-type]
            expected,
            delta=0.011,
        )

    def test_df_extra_data_line_fails(self) -> None:
        content = (
            "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
            "/dev/sda1      1000 400 600  40% /\n"
            "/dev/sdb1      2000 400 1600  20% /\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_missing_header_fails(self) -> None:
        content = "/dev/sda1      1000 400 600  40% /\n"
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_wrong_mount_fails(self) -> None:
        content = (
            "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
            "/dev/sda1      1000 400 600  40% /boot\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_malformed_number_fails(self) -> None:
        content = (
            "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
            "/dev/sda1      1,000 400 600  40% /\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_used_exceeds_total_fails(self) -> None:
        content = (
            "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
            "/dev/sda1      1000 4000 600  40% /\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_missing_file_fails(self) -> None:
        missing = _env_path(FIXTURES / "does-not-exist.txt")
        proc = _run_collector(_fixture_env(SENTINEL_DF_BYTES_FILE=missing))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_df_irregular_spacing_is_accepted(self) -> None:
        """df column padding is not an assumption: arbitrary (even
        inconsistent) whitespace between fields still parses."""
        content = (
            "Filesystem   1-blocks\tUsed      Available   Capacity Mounted on\n"
            "/dev/mapper/vg-root    20511336448   12345678901  8165657547  61% /\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        root_fs = payload["root_fs"]
        assert isinstance(root_fs, dict)
        self.assertEqual(root_fs["used"], DF_BYTES_USED)
        self.assertEqual(root_fs["total"], DF_BYTES_TOTAL)


# --- node identity ---------------------------------------------------------


class NodeIdentityTest(unittest.TestCase):
    def test_missing_node_fails(self) -> None:
        proc = _run_collector(_fixture_env(node=None))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")

    def test_empty_node_fails(self) -> None:
        proc = _run_collector(_fixture_env(node=""))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_whitespace_only_node_fails(self) -> None:
        proc = _run_collector(_fixture_env(node="   "))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_node_invalid_characters_fail(self) -> None:
        """Node text that could break JSON rendering is rejected —
        never silently mutated or escaped into the document."""
        for bad in (
            'Prod"',
            "Prod\\",
            "Prod\tTab",
            "Prod\nNewline",
            "Прод",
            " leading-space",
            "trailing-space ",
            "double  space",
            "Prod;rm-rf",
            "Prod#1",
        ):
            with self.subTest(node=bad):
                proc = _run_collector(_fixture_env(node=bad))
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_node_is_not_case_folded_or_renamed(self) -> None:
        for name in ("Prod", "prod", "Hermes", "VPN-1", "VPN-2", "Prod Web 01"):
            with self.subTest(node=name):
                payload = _successful_payload(node=name)
                self.assertEqual(payload["node"], name)
                telemetry = decode_heartbeat_payload(payload)
                self.assertEqual(telemetry.host, name)

    def test_valid_node_allowlist_shapes_pass(self) -> None:
        for name in ("a", "Prod", "vpn-2.host_01", "A-b_.z9"):
            with self.subTest(node=name):
                payload = _successful_payload(node=name)
                self.assertEqual(payload["node"], name)


# --- fail-closed stdout/stderr contract ------------------------------------


class FailClosedContractTest(unittest.TestCase):
    def _missing_path(self) -> str:
        return _env_path(FIXTURES / "no-such-fixture.txt")

    def test_failure_exit_code_is_nonzero_and_generic(self) -> None:
        proc = _run_collector(
            _fixture_env(SENTINEL_PROC_UPTIME=self._missing_path())
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")

    def test_failed_collection_never_emits_partial_json(self) -> None:
        """Every failure path produces empty stdout — the JSON is
        rendered exactly once, only after full collection."""
        failing_envs: list[dict[str, str]] = [
            _fixture_env(node=None),
            _fixture_env(node="   "),
            _fixture_env(SENTINEL_PROC_UPTIME=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_PROC_LOADAVG=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_PROC_STAT_A=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_PROC_STAT_B=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(FIXTURES / "nope.txt")),
            _fixture_env(SENTINEL_DF_INODES_FILE=_env_path(FIXTURES / "nope.txt")),
        ]
        for index, env in enumerate(failing_envs):
            with self.subTest(case=index):
                proc = _run_collector(env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_stderr_diagnostic_is_short_and_generic(self) -> None:
        proc = _run_collector(
            _fixture_env(SENTINEL_PROC_UPTIME=self._missing_path())
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess(len(proc.stderr), 300)
        # No /proc contents or fixture values are dumped to stderr.
        self.assertNotIn(str(MEM_TOTAL_KIB), proc.stderr)
        self.assertNotIn("cpu ", proc.stderr)
        self.assertNotIn("{", proc.stderr)

    def test_hostile_locale_still_produces_dot_decimals(self) -> None:
        """The collector forces LC_ALL=C: a comma-decimal ambient locale
        can never leak into the JSON numbers."""
        env = _fixture_env()
        env["LC_ALL"] = "ru_RU.UTF-8"
        env["LC_NUMERIC"] = "de_DE.UTF-8"
        proc = _run_collector(env)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        ram = payload["ram"]
        assert isinstance(ram, dict)
        percent = ram["percent"]
        self.assertIsInstance(percent, (int, float))
        # Commas are JSON field separators; a comma *between digits*
        # would be a leaked locale decimal separator.
        self.assertIsNone(re.search(r"\d,\d", proc.stdout))


# --- shell function level tests (sourcing the production script) ----------


class ShellFunctionTest(unittest.TestCase):
    """The production bash functions, sourced and called directly with
    fixture input — no duplicated collector logic."""

    def _source(self, body: str) -> subprocess.CompletedProcess[str]:
        return _run_bash_code(f'source "{_bash_path(SCRIPT)}"\n{body}')

    def test_compute_cpu_percent_function_direct(self) -> None:
        proc = self._source(
            "compute_cpu_percent "
            '"cpu 100 0 50 800 20 5 10 5 30 0" '
            '"cpu 150 0 80 830 20 5 15 10 60 0"'
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout.strip(), "75.00")

    def test_compute_cpu_percent_regressed_counters_exit_nonzero(self) -> None:
        proc = self._source(
            "compute_cpu_percent "
            '"cpu 100 0 50 800 20 5 10 5 30 0" '
            '"cpu 90 0 80 830 20 5 15 10 60 0"'
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_parse_uptime_function_direct(self) -> None:
        proc = self._source(
            f'parse_uptime "{_env_path(FIXTURES / "proc_uptime.txt")}"'
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout.strip(), "758901.33")

    def test_parse_uptime_function_malformed_fails(self) -> None:
        with _temp_fixture("bogus\n") as fixture:
            proc = self._source(f'parse_uptime "{_env_path(fixture)}"')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_validate_node_identity_function(self) -> None:
        proc = self._source(
            "for value in 'Prod' 'VPN-1' 'prod' 'Hermes 01' 'vpn_2.host'; do\n"
            "  if validate_node_identity \"$value\"; then echo \"ok:$value\";"
            " else echo \"bad:$value\"; fi\n"
            "done\n"
            "if validate_node_identity 'x\"y'; then echo 'ok:quote';"
            " else echo 'bad:quote'; fi\n"
            "if validate_node_identity '  '; then echo 'ok:blank';"
            " else echo 'bad:blank'; fi\n"
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(
            [line for line in proc.stdout.splitlines() if line],
            [
                "ok:Prod",
                "ok:VPN-1",
                "ok:prod",
                "ok:Hermes 01",
                "ok:vpn_2.host",
                "bad:quote",
                "bad:blank",
            ],
        )

    def test_bash_syntax_check(self) -> None:
        proc = _run_bash_code(f'bash -n "{_bash_path(SCRIPT)}"')
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "")


# --- zero RAM must fail closed (QA defect 1) -------------------------------


class ZeroRamTest(unittest.TestCase):
    """Physical RAM is never an absent resource: MemTotal == 0 is a
    corrupt measurement and must fail closed. Swap 0/0/0 semantics
    (already covered by SwapTest) stay untouched."""

    def _run_with_meminfo(self, content: str):
        with _temp_fixture(content) as fixture:
            return _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )

    def test_zero_ram_with_zero_available_fails(self) -> None:
        # QA case A.
        proc = self._run_with_meminfo(
            "MemTotal:        0 kB\nMemAvailable:    0 kB\n"
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_zero_ram_with_nonzero_available_fails(self) -> None:
        # QA case B (also violates MemAvailable <= MemTotal).
        proc = self._run_with_meminfo(
            "MemTotal:        0 kB\nMemAvailable:    512 kB\n"
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_normal_ram_still_green_after_zero_fix(self) -> None:
        payload = _successful_payload()
        ram = payload["ram"]
        assert isinstance(ram, dict)
        self.assertEqual(ram["used"], RAM_USED_BYTES)
        self.assertEqual(ram["total"], RAM_TOTAL_BYTES)


# --- finite number gate (QA defect 2) ---------------------------------------


class FiniteGateTest(unittest.TestCase):
    """Every metric emitted into JSON must be a syntactically valid,
    finite, domain-valid JSON number. Overflow-oriented digit-only
    sources (hundreds of digits) convert to inf/nan in awk and must
    fail closed before the final render."""

    #: 400 nines exceed the IEEE double maximum (~1.8e308), so the
    #: available awk converts this token to a non-finite value.
    OVERFLOW_DIGITS = "9" * 400

    def test_awk_runtime_really_overflows_chosen_literal(self) -> None:
        """Reproducible runtime evidence that the chosen 400-digit
        token becomes non-finite in THIS awk — the overflow scenarios
        below are real, not hypothetical."""
        with _temp_fixture(self.OVERFLOW_DIGITS + "\n") as fixture:
            proc = _run_bash_code(
                "awk '{printf \"%s\\n\", $1 + 0}' "
                f"\"{_env_path(fixture)}\""
            )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        converted = proc.stdout.strip()
        self.assertFalse(
            re.fullmatch(r"[0-9]+(\.[0-9]+)?", converted),
            msg=f"awk kept the token finite: {converted!r}",
        )

    def test_overflow_uptime_fails(self) -> None:
        # QA case C.
        with _temp_fixture(self.OVERFLOW_DIGITS + ".5\n") as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_UPTIME=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_overflow_load_fails(self) -> None:
        # QA case D: the first of the three load values overflows.
        content = self.OVERFLOW_DIGITS + " 0.32 0.25 3/812 9123\n"
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_LOADAVG=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_overflow_memtotal_fails(self) -> None:
        # QA case E.
        content = (
            "MemTotal:        " + self.OVERFLOW_DIGITS + " kB\n"
            "MemAvailable:    4567890 kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_overflow_swaptotal_fails(self) -> None:
        # QA case F.
        content = (
            f"MemTotal:        {MEM_TOTAL_KIB} kB\n"
            f"MemAvailable:    {MEM_AVAILABLE_KIB} kB\n"
            "SwapTotal:       " + self.OVERFLOW_DIGITS + " kB\n"
            f"SwapFree:        {SWAP_FREE_KIB} kB\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_PROC_MEMINFO=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_overflow_df_total_fails(self) -> None:
        # QA case G.
        content = (
            "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
            "/dev/sda1      " + self.OVERFLOW_DIGITS
            + " 12345678901  8165657547  61% /\n"
        )
        with _temp_fixture(content) as fixture:
            proc = _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_final_decimal_gate_validator_direct(self) -> None:
        """The sourceable production gate function rejects every
        non-finite spelling and every non-canonical form, and accepts
        only the plain non-negative decimals the renderer emits."""
        code = (
            f'source "{_bash_path(SCRIPT)}"\n'
            "for token in 'inf' '-inf' 'nan' 'NaN' 'Infinity' ''"
            " '1e5' '1E5' '-1' '+1' '1,5' '.5' '5.' '0x10'; do\n"
            "  if require_metric_decimal \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
            "for token in '0' '1' '75.00' '758901.33' '0.00'; do\n"
            "  if require_metric_decimal \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
        )
        proc = _run_bash_code(code)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rejected = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("reject:")
        )
        accepted = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("accept:")
        )
        self.assertEqual(
            rejected,
            sorted(
                [
                    "inf",
                    "-inf",
                    "nan",
                    "NaN",
                    "Infinity",
                    "",
                    "1e5",
                    "1E5",
                    "-1",
                    "+1",
                    "1,5",
                    ".5",
                    "5.",
                    "0x10",
                ]
            ),
        )
        self.assertEqual(accepted, ["0", "0.00", "1", "75.00", "758901.33"])

    def test_final_count_gate_validator_direct(self) -> None:
        """The byte/count gate accepts plain integers only."""
        code = (
            f'source "{_bash_path(SCRIPT)}"\n'
            "for token in '1.5' 'inf' 'nan' '' '-1' '1e5' '5.'; do\n"
            "  if require_metric_count \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
            "for token in '0' '123' '999999999999'; do\n"
            "  if require_metric_count \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
        )
        proc = _run_bash_code(code)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rejected = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("reject:")
        )
        accepted = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("accept:")
        )
        self.assertEqual(
            rejected, sorted(["1.5", "inf", "nan", "", "-1", "1e5", "5."])
        )
        self.assertEqual(accepted, ["0", "123", "999999999999"])

    def test_final_timestamp_gate_validator_direct(self) -> None:
        """The reported_at gate accepts exactly the fixed UTC format
        the renderer produces and rejects naive/other spellings."""
        code = (
            f'source "{_bash_path(SCRIPT)}"\n'
            "for token in '2026-09-10T08:00:00+00:00'; do\n"
            "  if require_metric_timestamp \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
            "for token in '2026-09-10T08:00:00'"
            " '2026-09-10T08:00:00Z' '2026-09-10 08:00:00+00:00'"
            " 'garbage' ''; do\n"
            "  if require_metric_timestamp \"$token\"; then"
            " echo \"accept:$token\"; else echo \"reject:$token\"; fi\n"
            "done\n"
        )
        proc = _run_bash_code(code)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rejected = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("reject:")
        )
        accepted = sorted(
            line.split(":", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("accept:")
        )
        self.assertEqual(
            rejected,
            sorted(
                [
                    "2026-09-10T08:00:00",
                    "2026-09-10T08:00:00Z",
                    "2026-09-10 08:00:00+00:00",
                    "garbage",
                    "",
                ]
            ),
        )
        self.assertEqual(accepted, ["2026-09-10T08:00:00+00:00"])


# --- df structural validation (QA defect 3) ---------------------------------


class DfStructuralTest(unittest.TestCase):
    """Malformed df header/data fails closed: the parser validates the
    full C-locale structure per mode (bytes / inodes), including the
    columns not used directly for telemetry."""

    BYTES_HEADER = (
        "Filesystem     1-blocks      Used  Available Capacity Mounted on\n"
    )
    BYTES_ROW = "/dev/sda1      20511336448 12345678901  8165657547  61% /\n"
    INODES_HEADER = (
        "Filesystem        Inodes      IUsed       IFree IUse% Mounted on\n"
    )
    INODES_ROW = "/dev/sda1        1310720     456789     853931   35% /\n"

    def _run_bytes(self, content: str):
        with _temp_fixture(content) as fixture:
            return _run_collector(
                _fixture_env(SENTINEL_DF_BYTES_FILE=_env_path(fixture))
            )

    def _run_inodes(self, content: str):
        with _temp_fixture(content) as fixture:
            return _run_collector(
                _fixture_env(SENTINEL_DF_INODES_FILE=_env_path(fixture))
            )

    def _assert_failed(self, proc) -> None:
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_bytes_header_garbage_fails(self) -> None:
        # QA case H: "Filesystem garbage" + otherwise plausible row.
        self._assert_failed(
            self._run_bytes("Filesystem garbage\n" + self.BYTES_ROW)
        )

    def test_bytes_header_wrong_structural_column_fails(self) -> None:
        # QA case H variant: Used column replaced by garbage.
        header = (
            "Filesystem     1-blocks      Garbage  Available Capacity Mounted on\n"
        )
        self._assert_failed(self._run_bytes(header + self.BYTES_ROW))

    def test_inode_header_garbage_fails(self) -> None:
        # QA case I.
        self._assert_failed(
            self._run_inodes("Filesystem garbage\n" + self.INODES_ROW)
        )

    def test_bytes_malformed_available_fails(self) -> None:
        # QA case J.
        row = "/dev/sda1      20511336448 12345678901  8x65757547  61% /\n"
        self._assert_failed(self._run_bytes(self.BYTES_HEADER + row))

    def test_inode_malformed_ifree_fails(self) -> None:
        # QA case J.
        row = "/dev/sda1        1310720     456789     85x931   35% /\n"
        self._assert_failed(self._run_inodes(self.INODES_HEADER + row))

    def test_bytes_malformed_capacity_fails(self) -> None:
        # QA case K.
        row = "/dev/sda1      20511336448 12345678901  8165657547  sixty% /\n"
        self._assert_failed(self._run_bytes(self.BYTES_HEADER + row))

    def test_bytes_capacity_without_percent_sign_fails(self) -> None:
        # QA case K variant: structurally not a capacity token.
        row = "/dev/sda1      20511336448 12345678901  8165657547  61 /\n"
        self._assert_failed(self._run_bytes(self.BYTES_HEADER + row))

    def test_inode_iuse_without_percent_sign_fails(self) -> None:
        # QA case K variant.
        row = "/dev/sda1        1310720     456789     853931   35 /\n"
        self._assert_failed(self._run_inodes(self.INODES_HEADER + row))

    def test_bytes_capacity_out_of_range_fails(self) -> None:
        # QA case L.
        row = "/dev/sda1      20511336448 12345678901  8165657547  150% /\n"
        self._assert_failed(self._run_bytes(self.BYTES_HEADER + row))

    def test_inode_iuse_out_of_range_fails(self) -> None:
        # QA case L.
        row = "/dev/sda1        1310720     456789     853931  101% /\n"
        self._assert_failed(self._run_inodes(self.INODES_HEADER + row))

    def test_bytes_available_exceeds_total_fails(self) -> None:
        # QA case M.
        row = "/dev/sda1      1000 400 1600  40% /\n"
        self._assert_failed(self._run_bytes(self.BYTES_HEADER + row))

    def test_inode_available_exceeds_total_fails(self) -> None:
        # QA case M.
        row = "/dev/sda1        1000     400     1600   40% /\n"
        self._assert_failed(self._run_inodes(self.INODES_HEADER + row))


if __name__ == "__main__":
    unittest.main()
