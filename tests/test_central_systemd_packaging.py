"""Deterministic systemd packaging tests for the Stage E9 central unit.

Production packaging authority is the declarative content of
``packaging/systemd/hermes-sentinel.service`` plus
``packaging/systemd/sentinel.env.example`` and the operator runbook
``docs/SENTINEL_DEPLOYMENT.md``. This module is only a test harness:
it inspects the ACTUAL packaging files and the accepted E6 settings
loader — it is not a second systemd manager, it executes no units, and
it requires no systemd (or Linux) on the developer machine.

The tests are semantic (a small purpose-built unit/env-file parser
plus contract-level assertions), so dangerous unit drift — a shell
wrapper in ExecStart, Restart=always, a Stage F hardening policy
sneaking in, a leaked secret, a lost JSON quoting boundary — fails
even when a single happy-string grep would still pass.

The one runtime-dependent proof (the synthetic example is rejected by
the ACTUAL accepted E6 loader after systemd-style quote stripping) uses
the real ``load_central_settings`` — no second validation copy.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.settings import (  # noqa: E402
    CentralSettingsError,
    load_central_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = REPO_ROOT / "packaging" / "systemd"
SERVICE_FILE = PACKAGING / "hermes-sentinel.service"
ENV_EXAMPLE = PACKAGING / "sentinel.env.example"
DEPLOY_DOC = REPO_ROOT / "docs" / "SENTINEL_DEPLOYMENT.md"
README = REPO_ROOT / "README.md"

SERVICE_UNIT_NAME = "hermes-sentinel.service"
CENTRAL_IDENTITY = "hermes-sentinel"
EXEC_PATH = "/opt/hermes-sentinel/venv/bin/hermes-sentinel"
ENV_FILE_PATH = "/etc/hermes-sentinel/sentinel.env"
STATE_DIR = "/var/lib/hermes-sentinel"
DB_PATH = "/var/lib/hermes-sentinel/sentinel.sqlite3"
UNIT_INSTALL_PATH = "/etc/systemd/system/hermes-sentinel.service"

FORBIDDEN_IDENTITIES = ("root", "hermes", "deploy", "www-data",
                        "nobody")

#: The exact accepted E6 settings surface (settings.py is
#: authoritative; these drive the env-example boundary checks).
REQUIRED_ENV_VARS = (
    "SENTINEL_DATABASE_PATH",
    "SENTINEL_LISTEN_HOST",
    "SENTINEL_LISTEN_PORT",
    "SENTINEL_MONITOR_INTERVAL_SECONDS",
    "SENTINEL_POLL_INTERVAL_SECONDS",
    "SENTINEL_HOSTS_JSON",
    "SENTINEL_NODE_TOKENS_JSON",
    "SENTINEL_TELEGRAM_BOT_TOKEN",
    "SENTINEL_TELEGRAM_CHAT_ID",
)
OPTIONAL_ENV_VARS = (
    "SENTINEL_TELEGRAM_MESSAGE_THREAD_ID",
    "SENTINEL_TELEGRAM_TIMEOUT_SECONDS",
)
JSON_ENV_VARS = ("SENTINEL_HOSTS_JSON", "SENTINEL_NODE_TOKENS_JSON")

#: The frozen [Service]/[Unit]/[Install] directive sets — any added or
#: removed directive is drift (including a Stage F hardening policy
#: that E9 must not attempt).
EXPECTED_UNIT_SECTIONS = {"Unit", "Service", "Install"}
EXPECTED_UNIT_KEYS = {"Description", "Wants", "After"}
EXPECTED_SERVICE_KEYS = {
    "Type", "User", "Group", "EnvironmentFile", "WorkingDirectory",
    "ExecStart", "KillSignal", "Restart", "RestartSec", "UMask",
    "StandardOutput", "StandardError", "StateDirectory",
    "StateDirectoryMode", "NoNewPrivileges", "PrivateTmp",
}
EXPECTED_INSTALL_KEYS = {"WantedBy"}


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

    Values keep their raw (whitespace-stripped) text INCLUDING any
    whole-value single quotes, so the quoting boundary stays testable;
    a duplicate active variable is a structural failure.
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


def _systemd_value(raw: str) -> str:
    """Apply the ONE systemd EnvironmentFile rule these values need:
    a whole value wrapped in single quotes has that outer pair removed
    (inner double quotes and everything else stay literal)."""
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    return raw


# --- packaging files exist ----------------------------------------------------


class PackagingFilesTest(unittest.TestCase):
    def test_service_file_exists(self) -> None:
        self.assertTrue(SERVICE_FILE.is_file(),
                        f"missing {SERVICE_FILE}")

    def test_env_example_exists(self) -> None:
        self.assertTrue(ENV_EXAMPLE.is_file(),
                        f"missing {ENV_EXAMPLE}")

    def test_packaging_files_are_lf_only(self) -> None:
        """systemd units are Linux artifacts: CR bytes would corrupt
        the effective unit on a real host."""
        for path in (SERVICE_FILE, ENV_EXAMPLE):
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

    def test_sections_are_exactly_unit_service_install(self) -> None:
        self.assertEqual(set(self.unit), EXPECTED_UNIT_SECTIONS)

    def test_unit_section_is_exactly_the_frozen_directives(self) -> None:
        self.assertEqual(set(self.unit["Unit"]), EXPECTED_UNIT_KEYS)

    def test_service_section_is_exactly_the_frozen_directives(self) -> None:
        """Baseline non-invasive isolation only: NoNewPrivileges and
        PrivateTmp are allowed, a full Stage F sandbox/hardening
        policy is NOT part of E9 and is drift here."""
        self.assertEqual(set(self.unit["Service"]),
                         EXPECTED_SERVICE_KEYS)

    def test_install_section_is_exactly_wanted_by(self) -> None:
        self.assertEqual(set(self.unit["Install"]),
                         EXPECTED_INSTALL_KEYS)

    def test_type_is_simple(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Type"),
                         ["simple"])

    def test_dedicated_central_user_and_group(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "User"),
                         [CENTRAL_IDENTITY])
        self.assertEqual(_entries(self.unit, "Service", "Group"),
                         [CENTRAL_IDENTITY])
        for forbidden in FORBIDDEN_IDENTITIES:
            self.assertNotEqual(
                _entries(self.unit, "Service", "User"), [forbidden])

    def test_exact_environment_file(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "EnvironmentFile"),
                         [ENV_FILE_PATH])

    def test_exact_working_directory(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "WorkingDirectory"),
                         [STATE_DIR])

    def test_execstart_is_exact_e8_entrypoint(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "ExecStart"),
                         [EXEC_PATH])

    def test_execstart_has_no_shell_wrapper_or_sudo(self) -> None:
        execstart = _entries(self.unit, "Service", "ExecStart")
        self.assertEqual(len(execstart), 1)
        command = execstart[0]
        # A single bare path: no arguments, no separators, no wrappers,
        # no env/prefix assignments (no inline secrets either).
        self.assertNotRegex(command, r"\s")
        self.assertTrue(command.startswith("/"))
        for forbidden in ("/bin/sh", "/bin/bash", "sh -c", "bash -c",
                          "sudo", "su ", "ssh", "env "):
            self.assertNotIn(forbidden, command)

    def test_kill_signal_is_sigterm(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "KillSignal"),
                         ["SIGTERM"])

    def test_restart_on_failure_with_5s_delay(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "Restart"),
                         ["on-failure"])
        self.assertEqual(_entries(self.unit, "Service", "RestartSec"),
                         ["5s"])
        # A normal operator stop must not be restarted indefinitely
        # (comments may mention the forbidden value; directives may not).
        self.assertNotRegex(self.text, r"(?m)^\s*Restart=always")

    def test_state_directory_and_mode(self) -> None:
        self.assertEqual(_entries(self.unit, "Service", "StateDirectory"),
                         [CENTRAL_IDENTITY])
        self.assertEqual(
            _entries(self.unit, "Service", "StateDirectoryMode"),
            ["0750"])

    def test_wanted_by_multi_user_target(self) -> None:
        self.assertEqual(_entries(self.unit, "Install", "WantedBy"),
                         ["multi-user.target"])

    def test_wants_and_after_network_online(self) -> None:
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "Wants"))
        self.assertIn("network-online.target",
                      _entries(self.unit, "Unit", "After"))

    def test_baseline_isolation_and_journal(self) -> None:
        expected = {
            "NoNewPrivileges": "yes",
            "PrivateTmp": "yes",
            "UMask": "0077",
            "StandardOutput": "journal",
            "StandardError": "journal",
        }
        for directive, value in expected.items():
            self.assertEqual(
                _entries(self.unit, "Service", directive), [value],
                msg=f"missing/incorrect {directive}={value}")

    def test_no_secret_values(self) -> None:
        # No literal configuration values: no variable assignments, no
        # URL, no token-like mixed letter+digit runs anywhere.
        self.assertRegex(self.text, r"Description=.+")
        self.assertNotRegex(self.text, r"SENTINEL_[A-Z_]+\s*=")
        self.assertNotIn("https://", self.text)
        self.assertNotIn("http://", self.text)
        self.assertNotRegex(
            self.text,
            r"(?=[A-Za-z0-9._~-]*[A-Za-z])(?=[A-Za-z0-9._~-]*[0-9])"
            r"[A-Za-z0-9._~-]{32,}",
        )

    def test_no_reporter_references(self) -> None:
        """The central unit must not reference the accepted reporter
        executable, its env file or its timer."""
        self.assertNotRegex(self.text, r"(?i)reporter")
        self.assertNotIn("/usr/local/libexec", self.text)
        self.assertNotIn("sentinel-report", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*\[Timer\]")


# --- environment file example -------------------------------------------------


class EnvExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(ENV_EXAMPLE)
        cls.values = _parse_env_example(cls.text)

    def test_exactly_the_required_e6_variables_each_once(self) -> None:
        # The parser already fails on duplicate active variables, so
        # key presence IS the exactly-once proof; nothing beyond the
        # accepted required surface may be active.
        self.assertEqual(set(self.values), set(REQUIRED_ENV_VARS))

    def test_no_unknown_active_variable(self) -> None:
        self.assertLessEqual(
            set(self.values), set(REQUIRED_ENV_VARS) | set(OPTIONAL_ENV_VARS))

    def test_optional_variables_documented_but_not_active(self) -> None:
        for variable in OPTIONAL_ENV_VARS:
            self.assertIn(variable, self.text)          # documented
            self.assertNotIn(variable, self.values)     # not active

    def test_no_export(self) -> None:
        # An EnvironmentFile is not a shell profile: no `export` lines.
        self.assertNotRegex(self.text, r"(?m)^[ \t]*export\b")

    def test_json_values_keep_whole_value_single_quoting(self) -> None:
        """The systemd EnvironmentFile quoting boundary: the whole JSON
        value is wrapped in ONE pair of single quotes (removed by
        systemd), and the literal JSON double quotes inside must reach
        E6 intact — proven by parsing the unquoted value as JSON."""
        for variable in JSON_ENV_VARS:
            with self.subTest(variable=variable):
                raw = self.values[variable]
                self.assertTrue(
                    raw.startswith("'") and raw.endswith("'"),
                    f"{variable} must keep whole-value single quoting:"
                    f" {raw!r}")
                inner = _systemd_value(raw)
                self.assertIn('"', inner)
                document = json.loads(inner)
                if variable == "SENTINEL_HOSTS_JSON":
                    self.assertIsInstance(document, list)
                    self.assertTrue(
                        all(isinstance(host, dict) and "name" in host
                            for host in document))
                else:
                    self.assertIsInstance(document, dict)
                    self.assertTrue(
                        all(isinstance(token, str)
                            for token in document.values()))

    def test_example_binds_loopback(self) -> None:
        self.assertEqual(self.values["SENTINEL_LISTEN_HOST"], "127.0.0.1")
        for wildcard in ("0.0.0.0", "::"):
            self.assertNotIn(wildcard, self.values["SENTINEL_LISTEN_HOST"])

    def test_database_path_is_frozen_default(self) -> None:
        self.assertEqual(self.values["SENTINEL_DATABASE_PATH"], DB_PATH)

    def test_mandatory_placeholders_deliberately_invalid(self) -> None:
        # Pinned mechanism: at least these mandatory placeholders stay
        # invalid until the operator replaces them — they are NOT
        # production-valid values.
        self.assertEqual(self.values["SENTINEL_LISTEN_PORT"], "REPLACE_ME")
        self.assertEqual(self.values["SENTINEL_TELEGRAM_CHAT_ID"],
                         "REPLACE_ME")
        self.assertIn("REPLACE", self.values["SENTINEL_TELEGRAM_BOT_TOKEN"])

    def test_unedited_example_rejected_by_e6_loader(self) -> None:
        """The synthetic example must be INVALID under the ACTUAL
        accepted E6 loader (after systemd-style quote stripping), so an
        accidentally unedited example fails closed during startup
        BEFORE a usable service runs."""
        env = {key: _systemd_value(value)
               for key, value in self.values.items()}
        with self.assertRaises(CentralSettingsError):
            load_central_settings(env)

    def test_no_real_secrets_or_endpoints(self) -> None:
        # No secret-like long tokens: a run of token-alphabet
        # characters that mixes letters AND digits (base64/hex-ish);
        # plain identifiers without digits are not secrets.
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
        # No public wildcard or real-IP-looking listen host.
        self.assertNotRegex(self.values["SENTINEL_LISTEN_HOST"],
                            r"^(0\.0\.0\.0|::)$")


# --- deployment runbook -------------------------------------------------------


class DeploymentDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _read(DEPLOY_DOC)

    def test_frozen_installation_paths_and_ownership(self) -> None:
        for path in ("/opt/hermes-sentinel", "/opt/hermes-sentinel/venv",
                     EXEC_PATH, "/etc/hermes-sentinel", ENV_FILE_PATH,
                     STATE_DIR, DB_PATH, UNIT_INSTALL_PATH):
            self.assertIn(path, self.doc)
        for ownership in ("root:root",
                          "hermes-sentinel:hermes-sentinel"):
            self.assertIn(ownership, self.doc)
        for mode in ("0600", "0750", "0644"):
            self.assertIn(mode, self.doc)
        self.assertRegex(self.doc, r"(?i)not\s+writable by the service user")

    def test_tls_and_reverse_proxy_stay_stage_f(self) -> None:
        self.assertRegex(self.doc, r"(?i)plaintext\s+HTTP")
        self.assertRegex(self.doc, r"(?i)reverse\s+proxy")
        self.assertIn("Stage F", self.doc)
        self.assertIn("127.0.0.1", self.doc)
        self.assertRegex(self.doc, r"(?i)public Internet")
        # E9 packaging alone does not claim remote production reporters
        # can safely reach the plaintext backend ((?s): phrase wraps).
        self.assertRegex(
            self.doc, r"(?is)not.{0,80}claim.{0,80}reporters")

    def test_operator_local_no_remote_execution(self) -> None:
        self.assertRegex(self.doc, r"(?i)local operator actions only")
        self.assertRegex(self.doc, r"(?i)locally on the central host")
        for boundary in ("SSHes to hosts", "invokes remote",
                         "performs remediation", "Ansible",
                         "self-updater"):
            self.assertIn(boundary, self.doc)
        # No remote-execution command invocations in the runbook.
        self.assertNotRegex(
            self.doc, r"(?m)^\s*(sudo\s+)?(ssh|scp|ansible|fab)\b")

    def test_no_deployment_claims(self) -> None:
        self.assertRegex(self.doc, r"(?i)no deployment claim")
        self.assertRegex(self.doc, r"(?i)not.{0,20}LIVE acceptance")
        # No real production success is claimed from packaging or
        # documentation alone (regex \s bridges the wrapped lines).
        self.assertRegex(
            self.doc,
            r"(?i)no\s+real\s+heartbeat/Telegram\s+production\s+success"
            r"\s+is\s+claimed",
        )

    def test_configuration_variables_documented(self) -> None:
        for variable in REQUIRED_ENV_VARS + OPTIONAL_ENV_VARS:
            self.assertIn(variable, self.doc)
        # E6 stays the authoritative validation reference.
        self.assertIn("load_central_settings", self.doc)
        self.assertIn("src/hermes_sentinel/settings.py", self.doc)

    def test_json_quoting_boundary_documented(self) -> None:
        self.assertRegex(self.doc, r"(?i)single quotes?")
        self.assertRegex(self.doc, r"(?i)double quotes?")
        for variable in JSON_ENV_VARS:
            self.assertIn(variable, self.doc)

    def test_secret_handling_documented(self) -> None:
        self.assertIn("sudoedit", self.doc)
        self.assertRegex(
            self.doc, r"(?i)never\s+be\s+pasted\s+into\s+the\s+unit")
        self.assertRegex(self.doc, r"(?i)never\s+pass\s+secrets")

    def test_restart_semantics_documented(self) -> None:
        self.assertIn("Restart=on-failure", self.doc)
        self.assertIn("RestartSec=5s", self.doc)
        self.assertRegex(self.doc, r"(?i)process\s+supervision")
        self.assertRegex(self.doc, r"(?i)not application retry")

    def test_smoke_and_enable_commands(self) -> None:
        self.assertIn("systemctl daemon-reload", self.doc)
        self.assertIn(f"systemctl start {SERVICE_UNIT_NAME}", self.doc)
        self.assertIn(f"systemctl status {SERVICE_UNIT_NAME}", self.doc)
        self.assertIn(f"journalctl -u {SERVICE_UNIT_NAME}", self.doc)
        self.assertRegex(
            self.doc, rf"systemctl enable --now {SERVICE_UNIT_NAME}")
        self.assertIn(f"systemctl is-enabled {SERVICE_UNIT_NAME}", self.doc)
        self.assertIn(f"systemctl is-active {SERVICE_UNIT_NAME}", self.doc)
        self.assertIn(f"systemctl cat {SERVICE_UNIT_NAME}", self.doc)

    def test_stop_and_update_lifecycle_documented(self) -> None:
        self.assertIn(f"systemctl stop {SERVICE_UNIT_NAME}", self.doc)
        self.assertRegex(self.doc, r"(?i)cooperative\s+stop")
        self.assertIn("KillSignal=SIGTERM", self.doc)
        self.assertRegex(self.doc, r"(?i)no self-update")
        self.assertRegex(self.doc, r"(?i)no automatic rollback")

    def test_no_real_secrets_or_real_endpoints(self) -> None:
        # No secret-like long tokens (mixed letters+digits, 32+).
        self.assertNotRegex(
            self.doc,
            r"(?=[A-Za-z0-9._~-]*[A-Za-z])(?=[A-Za-z0-9._~-]*[0-9])"
            r"[A-Za-z0-9._~-]{32,}",
        )
        # Every URL in the runbook is a safe synthetic example.
        for url in re.findall(r"https?://\S+", self.doc):
            self.assertRegex(
                url, r"\.(example|invalid)(/|$)",
                msg=f"runbook must not contain real endpoints: {url}")


# --- README status ------------------------------------------------------------


class ReadmeStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(README)

    def test_e8_marked_closed_green(self) -> None:
        self.assertIn("Stage E8 is CLOSED_GREEN", self.text)

    def test_documents_e9_packaging(self) -> None:
        self.assertIn("Stage E9", self.text)
        self.assertRegex(self.text, r"(?i)packaging")
        self.assertIn("hermes-sentinel.service", self.text)

    def test_links_deployment_runbook(self) -> None:
        self.assertIn("docs/SENTINEL_DEPLOYMENT.md", self.text)

    def test_no_deployment_or_live_acceptance_claims(self) -> None:
        for claim in ("DEPLOYED", "LIVE_ACCEPTED", "PRODUCTION VERIFIED"):
            self.assertNotIn(claim, self.text,
                             msg=f"README must not claim {claim}")

    def test_stage_e_acceptance_and_stage_f_outstanding(self) -> None:
        self.assertRegex(self.text, r"(?i)final\s+runtime/MVP\s+acceptance")
        self.assertRegex(self.text,
                         r"(?i)Stage\s+F\s+hardening\s+remains\s+outstanding")
        self.assertRegex(self.text, r"(?i)Stage\s+E\s+is\s+not\s+complete")


if __name__ == "__main__":
    unittest.main()
