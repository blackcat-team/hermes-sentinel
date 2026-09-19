"""Deterministic systemd packaging tests for the Stage C3 reporter.

Production packaging authority is the declarative content of
``packaging/systemd/**`` plus the operator runbook
``docs/REPORTER_DEPLOYMENT.md``. This module is only a test harness:
it inspects the ACTUAL packaging files and the accepted reporter
source — it is not a second systemd manager, it executes no units,
and it requires no systemd (or Linux) on the developer machine.

The tests are semantic (a real unit-file parser plus contract-level
assertions), so dangerous unit drift — a shell wrapper in ExecStart,
a broadened SuccessExitStatus, a network-blocking directive, a
Persistent/retry timer, a leaked token value — fails even when a
single happy-string grep would still pass.

The one runtime-dependent proof (the example token placeholder is
rejected by the ACTUAL accepted C2 token validator) reuses the real
bash harness from ``test_reporter_collector`` — the same real-bash
requirement the accepted C1/C2 suite already has.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
# tests-directory bootstrap so `import test_reporter_collector` also
# works when this module is executed directly instead of discovered.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_reporter_collector as c1  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = REPO_ROOT / "packaging" / "systemd"
SERVICE_FILE = PACKAGING / "hermes-sentinel-reporter.service"
TIMER_FILE = PACKAGING / "hermes-sentinel-reporter.timer"
ENV_EXAMPLE = PACKAGING / "reporter.env.example"
DEPLOY_DOC = REPO_ROOT / "docs" / "REPORTER_DEPLOYMENT.md"
README = REPO_ROOT / "README.md"
SCRIPT = REPO_ROOT / "scripts" / "sentinel-report.sh"

SERVICE_UNIT_NAME = "hermes-sentinel-reporter.service"
TIMER_UNIT_NAME = "hermes-sentinel-reporter.timer"
REPORTER_IDENTITY = "hermes-sentinel-reporter"
EXEC_PATH = "/usr/local/libexec/hermes-sentinel/sentinel-report.sh"
ENV_FILE_PATH = "/etc/hermes-sentinel/reporter.env"

#: Required service hardening directives (directive -> exact value).
#: Network access is deliberately NOT restricted here — the reporter
#: needs outbound HTTPS and DNS.
SERVICE_HARDENING = {
    "NoNewPrivileges": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectControlGroups": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
}

#: Directives whose PRESENCE would block reporter networking or add
#: speculative filtering C3 is forbidden to introduce.
FORBIDDEN_NETWORK_DIRECTIVES = (
    "PrivateNetwork",
    "IPAddressDeny",
    "IPAddressAllow",
    "RestrictAddressFamilies",
    "SystemCallFilter",
    "SystemCallArchitectures",
)

ENV_VARIABLES = ("SENTINEL_NODE", "SENTINEL_ENDPOINT", "SENTINEL_TOKEN")


def _read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"required packaging file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """Parse a unit file into ``{section: {key: [values...]}}``.

    Comments/blank lines are skipped; every directive must live in a
    section and have the ``Key=Value`` shape, so a structurally broken
    unit fails loudly instead of silently passing greps.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if current is None:
            raise AssertionError(f"directive outside any section: {raw!r}")
        if "=" not in line:
            raise AssertionError(f"directive without '=': {raw!r}")
        key, value = line.split("=", 1)
        sections[current].setdefault(key.strip(), []).append(value.strip())
    return sections


def _entries(sections: dict[str, dict[str, list[str]]], section: str,
             key: str) -> list[str]:
    return sections.get(section, {}).get(key, [])


def _parse_env_example(text: str) -> dict[str, str]:
    """Parse KEY=value pairs from the EnvironmentFile example."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue        # comments are allowed in an EnvironmentFile
        if "=" not in line:
            raise AssertionError(f"env line without '=': {raw!r}")
        key, value = line.split("=", 1)
        if key.strip() in values:
            raise AssertionError(f"duplicate variable: {key}")
        values[key.strip()] = value.strip()
    return values


# --- packaging files exist ---------------------------------------------------


class PackagingFilesTest(unittest.TestCase):
    def test_service_file_exists(self) -> None:
        self.assertTrue(SERVICE_FILE.is_file(),
                        f"missing {SERVICE_FILE}")

    def test_timer_file_exists(self) -> None:
        self.assertTrue(TIMER_FILE.is_file(),
                        f"missing {TIMER_FILE}")

    def test_env_example_exists(self) -> None:
        self.assertTrue(ENV_EXAMPLE.is_file(),
                        f"missing {ENV_EXAMPLE}")

    def test_unit_files_are_lf_only(self) -> None:
        """systemd units are Linux artifacts: CR bytes would corrupt
        the effective unit on a real host."""
        for path in (SERVICE_FILE, TIMER_FILE, ENV_EXAMPLE):
            with self.subTest(path=path.name):
                data = path.read_bytes()
                self.assertNotIn(b"\r", data,
                                 f"{path.name} must use LF line endings")


# --- service unit semantics ---------------------------------------------------


class ServiceUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(SERVICE_FILE)
        cls.unit = _parse_unit(cls.text)

    def test_type_is_oneshot(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Type"),
                         ["oneshot"])

    def test_dedicated_user_and_group(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "User"),
                         [REPORTER_IDENTITY])
        self.assertEqual(_entries(self.unit, "Service", "Group"),
                         [REPORTER_IDENTITY])
        for forbidden in ("root", "hermes", "www-data", "nobody"):
            self.assertNotEqual(
                _entries(self.unit, "Service", "User"), [forbidden])

    def test_exact_environment_file(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "EnvironmentFile"),
                         [ENV_FILE_PATH])

    def test_execstart_is_exact_direct_path(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "ExecStart"),
                         [EXEC_PATH])

    def test_execstart_has_no_shell_wrapper(self) -> None:
        execstart = _entries(self.unit, "Service", "ExecStart")
        self.assertEqual(len(execstart), 1)
        command = execstart[0]
        # A single bare path: no arguments, no separators, no wrappers.
        self.assertNotRegex(command, r"\s")
        for forbidden in ("/bin/sh", "/bin/bash", "sh -c", "bash -c",
                          "sudo", "su ", "ssh", "curl"):
            self.assertNotIn(forbidden, command)
        self.assertTrue(command.startswith("/"))

    def test_restart_is_no(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Restart"),
                         ["no"])

    def test_timeout_start_is_30s(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "TimeoutStartSec"),
                         ["30s"])

    def test_wants_and_after_network_online(self) -> None:
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "Wants"))
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "After"))

    def test_service_has_no_install_target(self) -> None:
        """The oneshot service is never enabled — only the timer is."""
        self.assertNotIn("Install", self.unit)

    def test_no_token_endpoint_or_node_values(self) -> None:
        # No literal configuration values: no variable assignments, no
        # URL, no header name carrying the token.
        self.assertRegex(self.text, r"Description=.+")
        self.assertNotRegex(self.text,
                            r"SENTINEL_(TOKEN|ENDPOINT|NODE)\s*=")
        self.assertNotIn("https://", self.text)
        self.assertNotIn("X-Sentinel-Token", self.text)

    def test_no_curl_command(self) -> None:
        self.assertNotRegex(self.text, r"(?i)\bcurl\b")

    def test_no_network_blocking_directives(self) -> None:
        for directive in FORBIDDEN_NETWORK_DIRECTIVES:
            self.assertNotRegex(
                self.text, rf"(?m)^\s*{directive}\s*=",
                msg=f"{directive} would break the reporter's outbound "
                    "HTTPS/DNS or add forbidden speculative filtering")

    def test_hardening_directives_present(self) -> None:
        for directive, value in SERVICE_HARDENING.items():
            self.assertEqual(
                _entries(self.unit, "Service", directive), [value],
                msg=f"missing/incorrect hardening {directive}={value}")
        self.assertEqual(_entries(self.unit, "Service", "UMask"), ["0077"])
        self.assertEqual(
            _entries(self.unit, "Service", "StandardOutput"), ["journal"])
        self.assertEqual(
            _entries(self.unit, "Service", "StandardError"), ["journal"])

    def test_capability_sets_are_empty(self) -> None:
        self.assertEqual(_entries(self.unit, "Service",
                                  "CapabilityBoundingSet"), [""])
        self.assertEqual(_entries(self.unit, "Service",
                                  "AmbientCapabilities"), [""])

    def test_no_success_exit_status_broadening(self) -> None:
        self.assertNotIn("SuccessExitStatus", self.unit.get("Service", {}))


# --- timer unit semantics -----------------------------------------------------


class TimerUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(TIMER_FILE)
        cls.unit = _parse_unit(cls.text)

    def test_timer_targets_exact_service(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "Unit"),
                         [SERVICE_UNIT_NAME])

    def test_on_boot_sec(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "OnBootSec"),
                         ["30s"])

    def test_on_unit_active_sec(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "OnUnitActiveSec"),
                         ["60s"])

    def test_accuracy_sec(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "AccuracySec"),
                         ["1s"])

    def test_randomized_delay_sec(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "RandomizedDelaySec"),
                         ["0"])

    def test_wanted_by_timers_target(self) -> None:
        self.assertEqual(_entries(self.unit, "Install", "WantedBy"),
                         ["timers.target"])
        # exactly one enable target — no multiple timer targets
        self.assertEqual(len(_entries(self.unit, "Install", "WantedBy")), 1)

    def test_no_persistent(self) -> None:
        self.assertNotIn("Persistent", self.unit.get("Timer", {}))
        self.assertNotRegex(self.text, r"(?m)^\s*Persistent\s*=")

    def test_no_on_calendar(self) -> None:
        self.assertNotRegex(self.text, r"(?m)^\s*OnCalendar\s*=")

    def test_no_retry_or_restart_machinery(self) -> None:
        # The [Timer] section is exactly the five accepted directives —
        # any added retry/keep-alive directive is drift.
        self.assertEqual(
            set(self.unit.get("Timer", {})),
            {"OnBootSec", "OnUnitActiveSec", "AccuracySec",
             "RandomizedDelaySec", "Unit"},
        )
        self.assertNotRegex(self.text, r"(?m)^\s*Restart\s*=")


# --- environment file example -------------------------------------------------


class EnvExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(ENV_EXAMPLE)
        cls.values = _parse_env_example(cls.text)

    def test_exactly_the_three_configuration_variables(self) -> None:
        self.assertEqual(set(self.values), set(ENV_VARIABLES))
        self.assertEqual(len(self.values), 3)

    def test_no_export(self) -> None:
        # An EnvironmentFile is not a shell profile: no `export` lines.
        self.assertNotRegex(self.text, r"(?m)^[ \t]*export\b")

    def test_endpoint_example_is_https_and_invalid(self) -> None:
        endpoint = self.values["SENTINEL_ENDPOINT"]
        self.assertTrue(endpoint.startswith("https://"), endpoint)
        self.assertIn(".invalid", endpoint)

    def test_token_placeholder_rejected_by_c2_validator(self) -> None:
        """The synthetic placeholder must be INVALID under the ACTUAL
        accepted C2 token validation, so an accidentally unedited
        example fails closed BEFORE network delivery."""
        if c1.BASH is None:
            self.fail(
                "C3 packaging verification requires a real bash (as the "
                "accepted C1/C2 suite does); none was found"
            )
        placeholder = self.values["SENTINEL_TOKEN"]
        # A placeholder-looking marker only (never a real secret).
        self.assertRegex(placeholder, r"REPLACE")
        code = (
            f'source "{c1._bash_path(SCRIPT)}"\n'
            f"if validate_reporter_token '{placeholder}'; then "
            'echo accept; else echo reject; fi\n'
        )
        proc = c1._run_bash_code(code)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout.split(), ["reject"])


# --- deployment runbook -------------------------------------------------------


class DeploymentDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _read(DEPLOY_DOC)

    def test_env_file_permissions_0600_root(self) -> None:
        self.assertIn("0600", self.doc)
        self.assertIn("root:root", self.doc)
        self.assertIn("/etc/hermes-sentinel/reporter.env", self.doc)
        self.assertIn("0750", self.doc)  # the /etc/hermes-sentinel dir

    def test_reporter_executable_0755_root(self) -> None:
        self.assertIn("0755", self.doc)
        self.assertIn(EXEC_PATH, self.doc)
        self.assertIn("root:root", self.doc)

    def test_unit_files_0644_root(self) -> None:
        self.assertIn("0644", self.doc)
        self.assertIn(
            "/etc/systemd/system/hermes-sentinel-reporter.service",
            self.doc,
        )
        self.assertIn(
            "/etc/systemd/system/hermes-sentinel-reporter.timer",
            self.doc,
        )
        self.assertIn("root:root", self.doc)

    def test_enables_timer_not_service(self) -> None:
        lines = [line for line in self.doc.splitlines()
                 if "systemctl" in line]
        self.assertTrue(
            any("enable" in line and TIMER_UNIT_NAME in line
                for line in lines),
            "runbook must enable the timer",
        )
        offenders = [line for line in lines
                     if "enable" in line and ".service" in line]
        self.assertEqual(
            offenders, [],
            "runbook must never instruct enabling the oneshot service",
        )

    def test_local_operator_boundary_no_remote_execution(self) -> None:
        self.assertRegex(self.doc, r"(?i)local")
        self.assertRegex(self.doc, r"(?i)never\s+SSH")
        # No remote-execution command invocations in the runbook.
        self.assertNotRegex(
            self.doc, r"(?m)^\s*(sudo\s+)?(ssh|scp|ansible|fab)\b")
        # The observability-only boundary is stated explicitly.
        self.assertRegex(self.doc, r"(?i)observability only")

    def test_no_real_secrets_or_real_endpoints(self) -> None:
        # No token assignment with a live-looking value anywhere.
        self.assertNotRegex(self.doc, r"SENTINEL_TOKEN\s*=\s*\S+")
        # No secret-like long tokens: a run of token-alphabet
        # characters that mixes letters AND digits (base64/hex-ish).
        # Pure punctuation runs (markdown table dividers) and plain
        # identifiers without digits are not secrets.
        self.assertNotRegex(
            self.doc,
            r"(?=[A-Za-z0-9._~-]*[A-Za-z])(?=[A-Za-z0-9._~-]*[0-9])"
            r"[A-Za-z0-9._~-]{32,}",
        )
        # Every URL in the runbook is a safe synthetic example.
        for url in re.findall(r"https?://\S+", self.doc):
            self.assertRegex(
                url, r"\.(example|invalid)(/|$)",
                msg=f"runbook must not contain real endpoints: {url}",
            )
        # No command that would place a token in argv/history.
        self.assertNotRegex(
            self.doc,
            r"(?im)^\s*(sudo\s+)?echo\s+[^\n]*SENTINEL_TOKEN")

    def test_stop_update_smoke_start_lifecycle(self) -> None:
        self.assertRegex(
            self.doc,
            r"systemctl (disable --now|stop) hermes-sentinel-reporter"
            r"\.timer",
        )
        self.assertIn("systemctl daemon-reload", self.doc)
        self.assertIn(
            "systemctl start hermes-sentinel-reporter.service", self.doc)
        self.assertRegex(
            self.doc,
            r"systemctl enable --now hermes-sentinel-reporter\.timer",
        )

    def test_dedicated_user_documented(self) -> None:
        self.assertIn("useradd", self.doc)
        self.assertIn(REPORTER_IDENTITY, self.doc)
        self.assertIn("/usr/sbin/nologin", self.doc)
        self.assertNotRegex(self.doc,
                            r"(?m)^User=(root|hermes|www-data|nobody)\s*$")

    def test_failure_and_smoke_semantics_documented(self) -> None:
        # no immediate retry: sampling cadence, timer keeps running
        self.assertIn("Restart=no", self.doc)
        self.assertRegex(self.doc, r"(?i)sampling cadence")
        self.assertRegex(self.doc, r"(?i)not.{0,40}retry")
        # manual smoke is a single run, not a retry loop
        self.assertRegex(self.doc, r"(?i)exactly once")
        self.assertRegex(self.doc, r"(?i)no stdout output")

    def test_secret_edited_not_passed_on_command_line(self) -> None:
        self.assertIn("sudoedit", self.doc)
        self.assertRegex(self.doc, r"(?i)never pass the token")
        self.assertRegex(self.doc, r"(?i)shell history")


# --- README status ------------------------------------------------------------


class ReadmeStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(README)

    def test_no_deployed_live_claims(self) -> None:
        for claim in ("DEPLOYED", "LIVE", "PRODUCTION VERIFIED"):
            self.assertNotRegex(
                self.text, rf"\b{claim}\b",
                msg=f"README must not claim {claim}",
            )

    def test_documents_stage_c3(self) -> None:
        self.assertIn("Stage C3", self.text)
        self.assertRegex(self.text, r"(?i)systemd")
        self.assertRegex(self.text, r"(?i)timer")
        for stage in ("C1", "C2", "C3"):
            self.assertIn(stage, self.text)


# --- accepted C1/C2 immutability -----------------------------------------------


class ReporterImmutabilityTest(unittest.TestCase):
    """The accepted reporter RUNTIME surface — the production reporter
    script and its fixtures — must stay byte-identical to the committed
    baseline, verified via read-only git queries (the working tree must
    match HEAD exactly for that surface).  The reporter test-harness
    modules themselves are governed by the repository workflow (exact
    candidate scope, candidate fingerprint, independent QA and Architect
    acceptance), not frozen by this guard."""

    _RUNTIME_PATHS = (
        "scripts/sentinel-report.sh",
        "tests/fixtures/reporter",
    )

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_accepted_reporter_surface_unmodified(self) -> None:
        diff = self._git("diff", "HEAD", "--", *self._RUNTIME_PATHS)
        self.assertEqual(
            diff.returncode, 0,
            msg=f"git failed: {diff.stderr.strip()}",
        )
        self.assertEqual(
            diff.stdout, "",
            msg="the accepted reporter runtime source and fixtures must "
                "stay unmodified",
        )
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard",
            "scripts", "tests/fixtures/reporter",
        )
        self.assertEqual(
            untracked.returncode, 0,
            msg=f"git failed: {untracked.stderr.strip()}",
        )
        self.assertEqual(
            untracked.stdout.strip(), "",
            msg="no new untracked files belong in the accepted reporter "
                "runtime surface",
        )


if __name__ == "__main__":
    unittest.main()
