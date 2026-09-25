"""Deterministic systemd packaging tests for the Stage H1B-2 external
dead-man.

Production packaging authority is the declarative content of
``packaging/systemd/hermes-sentinel-deadman.service`` plus
``hermes-sentinel-deadman.timer`` and ``deadman.env.example``. This
module is only a test harness: it inspects the ACTUAL packaging
files, the accepted H1B-2 config loader and pyproject — it is not a
second systemd manager, it executes no units, and it requires no
systemd (or Linux) on the developer machine.

The tests are semantic (a small purpose-built unit/env-file parser
plus contract-level assertions), so dangerous unit drift — a daemon
Restart policy, a shell wrapper in ExecStart, a network-blocking
directive, a leaked credential, a high-frequency timer, a
reporter-token reuse, or a mismatch between the env example, the
unit and the console entrypoint — fails even when a single
happy-string grep would still pass.
"""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.deadman_config import (  # noqa: E402
    DeadManSettingsError,
    load_deadman_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = REPO_ROOT / "packaging" / "systemd"
SERVICE_FILE = PACKAGING / "hermes-sentinel-deadman.service"
TIMER_FILE = PACKAGING / "hermes-sentinel-deadman.timer"
ENV_EXAMPLE = PACKAGING / "deadman.env.example"
PYPROJECT = REPO_ROOT / "pyproject.toml"
ARCHITECTURE = REPO_ROOT / "docs" / "ARCHITECTURE.md"

SERVICE_UNIT_NAME = "hermes-sentinel-deadman.service"
TIMER_UNIT_NAME = "hermes-sentinel-deadman.timer"
DEADMAN_IDENTITY = "hermes-sentinel-deadman"
EXEC_PATH = "/opt/hermes-sentinel/venv/bin/hermes-sentinel-deadman"
ENV_FILE_PATH = "/etc/hermes-sentinel/deadman.env"
STATE_DIR = "/var/lib/hermes-sentinel"
STATE_PATH = "/var/lib/hermes-sentinel/deadman-state.json"

FORBIDDEN_IDENTITIES = ("root", "hermes", "deploy", "www-data",
                        "nobody", "hermes-sentinel-reporter")

#: Required service hardening directives (directive -> exact value).
#: Network access is deliberately NOT restricted — the dead-man needs
#: outbound HTTPS and DNS.
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
    "UMask": "0077",
    "StandardOutput": "journal",
    "StandardError": "journal",
}

#: Directives whose PRESENCE would block dead-man networking or add
#: speculative filtering H1B-2 is forbidden to introduce.
FORBIDDEN_NETWORK_DIRECTIVES = (
    "PrivateNetwork",
    "IPAddressDeny",
    "IPAddressAllow",
    "RestrictAddressFamilies",
    "SystemCallFilter",
    "SystemCallArchitectures",
)

#: The exact H1B-2 configuration surface (deadman_config.py is
#: authoritative; these drive the env-example boundary checks).
REQUIRED_ENV_VARS = (
    "HERMES_SENTINEL_DEADMAN_PROBE_URL",
    "HERMES_SENTINEL_DEADMAN_STATE_PATH",
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN",
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID",
)
OPTIONAL_ENV_VARS = (
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID",
    "HERMES_SENTINEL_DEADMAN_PROBE_TIMEOUT_SECONDS",
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_TIMEOUT_SECONDS",
)

#: The frozen [Unit]/[Service]/[Timer]/[Install] directive sets — any
#: added or removed directive is drift.
EXPECTED_UNIT_KEYS = {"Description", "Wants", "After"}
EXPECTED_SERVICE_KEYS = {
    "Type", "User", "Group", "EnvironmentFile", "ExecStart",
    "TimeoutStartSec", "Restart", "UMask", "StandardOutput",
    "StandardError", "StateDirectory", "StateDirectoryMode",
    "NoNewPrivileges", "PrivateTmp", "PrivateDevices", "ProtectSystem",
    "ProtectHome", "ProtectKernelTunables", "ProtectKernelModules",
    "ProtectControlGroups", "RestrictSUIDSGID", "LockPersonality",
    "CapabilityBoundingSet", "AmbientCapabilities",
}
EXPECTED_TIMER_KEYS = {
    "OnBootSec", "OnUnitActiveSec", "AccuracySec", "RandomizedDelaySec",
    "Unit",
}


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
    """Parse active KEY=value pairs from the EnvironmentFile example.

    A duplicate active variable is a structural failure.
    """
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


# --- packaging files exist ----------------------------------------------------


class PackagingFilesTest(unittest.TestCase):
    def test_service_file_exists(self) -> None:
        self.assertTrue(SERVICE_FILE.is_file(), f"missing {SERVICE_FILE}")

    def test_timer_file_exists(self) -> None:
        self.assertTrue(TIMER_FILE.is_file(), f"missing {TIMER_FILE}")

    def test_env_example_exists(self) -> None:
        self.assertTrue(ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}")

    def test_packaging_files_are_lf_only(self) -> None:
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

    def test_sections_are_exactly_unit_service(self) -> None:
        """A oneshot service is never enabled directly — only its
        timer is, so no [Install] section may exist."""
        self.assertEqual(set(self.unit), {"Unit", "Service"})

    def test_unit_section_is_exactly_the_frozen_directives(self) -> None:
        self.assertEqual(set(self.unit["Unit"]), EXPECTED_UNIT_KEYS)

    def test_service_section_is_exactly_the_frozen_directives(self) -> None:
        self.assertEqual(set(self.unit["Service"]), EXPECTED_SERVICE_KEYS)

    def test_type_is_oneshot(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Type"),
                         ["oneshot"])

    def test_dedicated_user_and_group(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "User"),
                         [DEADMAN_IDENTITY])
        self.assertEqual(_entries(self.unit, "Service", "Group"),
                         [DEADMAN_IDENTITY])
        for forbidden in FORBIDDEN_IDENTITIES:
            self.assertNotEqual(
                _entries(self.unit, "Service", "User"), [forbidden])

    def test_exact_environment_file(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "EnvironmentFile"),
                         [ENV_FILE_PATH])

    def test_execstart_is_exact_direct_entrypoint(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "ExecStart"),
                         [EXEC_PATH])

    def test_execstart_has_no_shell_wrapper(self) -> None:
        execstart = _entries(self.unit, "Service", "ExecStart")
        self.assertEqual(len(execstart), 1)
        command = execstart[0]
        # A single bare path: no arguments, no separators, no wrappers.
        self.assertNotRegex(command, r"\s")
        self.assertTrue(command.startswith("/"))
        for forbidden in ("/bin/sh", "/bin/bash", "sh -c", "bash -c",
                          "sudo", "su ", "ssh", "curl", "env "):
            self.assertNotIn(forbidden, command)

    def test_restart_is_no_never_always(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Restart"),
                         ["no"])
        # The timer owns cadence; no directive may ever daemonize the
        # oneshot (comments may mention the term; directives may not).
        self.assertNotRegex(self.text, r"(?m)^\s*Restart=always")
        self.assertNotRegex(self.text, r"(?m)^\s*Restart=on-(failure|success)")

    def test_timeout_start_is_bounded(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "TimeoutStartSec"),
                         ["30s"])

    def test_wants_and_after_network_online(self) -> None:
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "Wants"))
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "After"))

    def test_state_directory_contract(self) -> None:
        """The packaged state directory matches the env example's
        state-file path (the generic public product configuration)."""
        self.assertEqual(_entries(self.unit, "Service", "StateDirectory"),
                         ["hermes-sentinel"])
        self.assertEqual(
            _entries(self.unit, "Service", "StateDirectoryMode"), ["0750"])

    def test_hardening_directives_present(self) -> None:
        for directive, value in SERVICE_HARDENING.items():
            self.assertEqual(
                _entries(self.unit, "Service", directive), [value],
                msg=f"missing/incorrect hardening {directive}={value}")

    def test_capability_sets_are_empty(self) -> None:
        """Outbound HTTPS and one state-file write need no
        capabilities; no privileged remote-control surface exists."""
        self.assertEqual(_entries(self.unit, "Service",
                                  "CapabilityBoundingSet"), [""])
        self.assertEqual(_entries(self.unit, "Service",
                                  "AmbientCapabilities"), [""])

    def test_no_network_blocking_directives(self) -> None:
        for directive in FORBIDDEN_NETWORK_DIRECTIVES:
            self.assertNotRegex(
                self.text, rf"(?m)^\s*{directive}\s*=",
                msg=f"{directive} would break the dead-man's outbound "
                    "HTTPS/DNS or add forbidden speculative filtering")

    def test_no_success_exit_status_broadening(self) -> None:
        self.assertNotIn("SuccessExitStatus", self.unit.get("Service", {}))

    def test_no_configuration_values_in_unit(self) -> None:
        # No literal configuration values: no variable assignments, no
        # URLs, no header names carrying a token.
        self.assertRegex(self.text, r"Description=.+")
        self.assertNotRegex(
            self.text, r"(?m)^\s*HERMES_SENTINEL_DEADMAN_[A-Z_]+\s*=")
        self.assertNotIn("https://", self.text)
        self.assertNotIn("http://", self.text)

    def test_no_real_infrastructure_facts(self) -> None:
        # No IP literals, no secret-like runs, no real domains.
        self.assertNotRegex(self.text, r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
        self.assertNotRegex(
            self.text,
            r"(?=[A-Za-z0-9._~-]*[A-Za-z])(?=[A-Za-z0-9._~-]*[0-9])"
            r"[A-Za-z0-9._~-]{32,}",
        )

    def test_no_reporter_or_central_references(self) -> None:
        """The dead-man unit must not reference the accepted reporter
        or central executables, env files or identities."""
        self.assertNotRegex(self.text, r"(?i)reporter")
        self.assertNotIn("/usr/local/libexec", self.text)
        self.assertNotIn("sentinel-report", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*\[Timer\]")


# --- timer unit semantics -----------------------------------------------------


class TimerUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(TIMER_FILE)
        cls.unit = _parse_unit(cls.text)

    def test_sections_are_exactly_unit_timer_install(self) -> None:
        self.assertEqual(set(self.unit), {"Unit", "Timer", "Install"})

    def test_timer_targets_exact_service(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "Unit"),
                         [SERVICE_UNIT_NAME])

    def test_timer_section_is_exactly_the_frozen_directives(self) -> None:
        """Any added retry/keep-alive/replay directive is drift."""
        self.assertEqual(set(self.unit["Timer"]), EXPECTED_TIMER_KEYS)

    def test_frozen_60_second_cadence(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "OnUnitActiveSec"),
                         ["60s"])
        self.assertEqual(_entries(self.unit, "Timer", "OnBootSec"),
                         ["30s"])
        self.assertEqual(_entries(self.unit, "Timer", "AccuracySec"),
                         ["1s"])

    def test_no_randomized_cadence_drift(self) -> None:
        self.assertEqual(_entries(self.unit, "Timer", "RandomizedDelaySec"),
                         ["0"])

    def test_no_high_frequency_or_replay_machinery(self) -> None:
        self.assertNotRegex(self.text, r"(?m)^\s*OnCalendar\s*=")
        self.assertNotIn("Persistent", self.unit.get("Timer", {}))
        self.assertNotRegex(self.text, r"(?m)^\s*Persistent\s*=")
        self.assertNotRegex(self.text, r"(?m)^\s*Restart\s*=")

    def test_wanted_by_timers_target_only(self) -> None:
        self.assertEqual(_entries(self.unit, "Install", "WantedBy"),
                         ["timers.target"])
        self.assertEqual(len(_entries(self.unit, "Install", "WantedBy")), 1)

    def test_no_shell_loop_or_second_daemon(self) -> None:
        for forbidden in ("/bin/sh", "bash", "sleep", "while"):
            self.assertNotIn(forbidden, self.text.lower())


# --- environment file example -------------------------------------------------


class EnvExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(ENV_EXAMPLE)
        cls.values = _parse_env_example(cls.text)

    def test_exactly_the_required_variables_each_once(self) -> None:
        self.assertEqual(set(self.values), set(REQUIRED_ENV_VARS))

    def test_no_unknown_active_variable(self) -> None:
        self.assertLessEqual(
            set(self.values),
            set(REQUIRED_ENV_VARS) | set(OPTIONAL_ENV_VARS),
        )

    def test_optional_variables_documented_but_not_active(self) -> None:
        for variable in OPTIONAL_ENV_VARS:
            self.assertIn(variable, self.text)       # documented
            self.assertNotIn(variable, self.values)  # not active

    def test_no_export(self) -> None:
        # An EnvironmentFile is not a shell profile: no `export` lines.
        self.assertNotRegex(self.text, r"(?m)^[ \t]*export\b")

    def test_probe_url_is_https_and_invalid_tld(self) -> None:
        endpoint = self.values["HERMES_SENTINEL_DEADMAN_PROBE_URL"]
        self.assertTrue(endpoint.startswith("https://"), endpoint)
        self.assertRegex(endpoint, r"\.invalid(/|$)")

    def test_state_path_matches_packaged_contract(self) -> None:
        self.assertEqual(
            self.values["HERMES_SENTINEL_DEADMAN_STATE_PATH"], STATE_PATH
        )

    def test_placeholders_deliberately_invalid(self) -> None:
        self.assertIn(
            "REPLACE", self.values[
                "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN"]
        )
        self.assertEqual(
            self.values["HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID"],
            "REPLACE_ME",
        )

    def test_unedited_example_rejected_by_actual_loader(self) -> None:
        """The synthetic example must be INVALID under the ACTUAL
        accepted H1B-2 loader, so an accidentally unedited example
        fails closed BEFORE any network delivery."""
        with self.assertRaises(DeadManSettingsError):
            load_deadman_settings(self.values)

    def test_no_reporter_token_or_central_credential_reuse(self) -> None:
        """The dead-man holds its own Stage-H credential: no reporter
        variable and no central E6 Telegram variable may appear."""
        for foreign in ("SENTINEL_TOKEN", "SENTINEL_ENDPOINT",
                        "SENTINEL_NODE", "SENTINEL_TELEGRAM_BOT_TOKEN",
                        "SENTINEL_TELEGRAM_CHAT_ID"):
            self.assertNotIn(foreign, self.text)
        for variable in self.values:
            self.assertTrue(variable.startswith(
                "HERMES_SENTINEL_DEADMAN_"))

    def test_no_real_secrets_or_endpoints(self) -> None:
        # No secret-like long tokens: a run of token-alphabet
        # characters that mixes letters AND digits (base64/hex-ish).
        self.assertNotRegex(
            self.text,
            r"(?=[A-Za-z0-9._~-]*[A-Za-z])(?=[A-Za-z0-9._~-]*[0-9])"
            r"[A-Za-z0-9._~-]{32,}",
        )
        # Every URL in the example is a safe synthetic value.
        for url in re.findall(r"https?://\S+", self.text):
            self.assertRegex(
                url, r"\.(example|invalid)(/|$)",
                msg=f"example must not contain real endpoints: {url}")
        # No IP literals anywhere.
        self.assertNotRegex(self.text, r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


# --- console entrypoint / packaging agreement ----------------------------------


class EntrypointContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with PYPROJECT.open("rb") as handle:
            cls.pyproject = tomllib.load(handle)
        cls.scripts = cls.pyproject["project"]["scripts"]

    def test_console_entrypoint_registered(self) -> None:
        self.assertEqual(
            self.scripts.get("hermes-sentinel-deadman"),
            "hermes_sentinel.deadman_process:main",
        )

    def test_central_entrypoint_unchanged(self) -> None:
        self.assertEqual(
            self.scripts.get("hermes-sentinel"),
            "hermes_sentinel.process:main",
        )

    def test_execstart_matches_console_entrypoint(self) -> None:
        """The packaged service command and the pyproject console
        entrypoint must agree exactly."""
        unit = _parse_unit(_read(SERVICE_FILE))
        execstart = _entries(unit, "Service", "ExecStart")
        self.assertEqual(execstart, [EXEC_PATH])
        self.assertEqual(EXEC_PATH.rsplit("/", 1)[-1],
                         "hermes-sentinel-deadman")
        self.assertIn("hermes-sentinel-deadman", self.scripts)

    def test_environment_file_path_matches_env_example_contract(
        self,
    ) -> None:
        """The unit's EnvironmentFile is exactly the documented
        /etc/hermes-sentinel/deadman.env deployment path."""
        unit = _parse_unit(_read(SERVICE_FILE))
        self.assertEqual(
            _entries(unit, "Service", "EnvironmentFile"), [ENV_FILE_PATH]
        )
        self.assertIn("/etc/hermes-sentinel/deadman.env", _read(ENV_EXAMPLE))


# --- architecture documentation ------------------------------------------------


class ArchitectureDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _read(ARCHITECTURE)

    def test_section_33_documents_h1b2(self) -> None:
        self.assertIn(
            "## 33. External dead-man oneshot runtime (Stage H1B-2)",
            self.doc,
        )

    def test_documented_semantics_present(self) -> None:
        for phrase in (
            "60-second cadence",
            "At most one delivery attempt per cycle",
            "Persist-before-deliver / ACK-after-success",
            "AT-LEAST-ONCE",
            "no internal daemon",
        ):
            self.assertIn(phrase, self.doc,
                          msg=f"architecture must state: {phrase}")

    def test_stage_h_not_claimed_closed(self) -> None:
        self.assertIn("Stage H is NOT closed by H1B-2", self.doc)
        self.assertIn("remain NOT completed", self.doc)


if __name__ == "__main__":
    unittest.main()
