"""Deterministic configuration tests for the Stage H1B-2 dead-man
runtime settings loader.

These tests prove the strict, secret-safe ``HERMES_SENTINEL_DEADMAN_*``
boundary of ``load_deadman_settings`` (the E6 loader precedent):
complete valid environments, fail-closed missing/empty required
values, strict integer/float parsing, optional thread id / timeout
handling, token secrecy in repr and errors, exact handoff of the
parsed values to the accepted H1B-1 settings objects, and the
irrelevance of unrelated environment keys.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# src-layout bootstrap (same pattern as the other test modules).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.deadman_config import (  # noqa: E402
    DeadManRuntimeSettings,
    DeadManSettingsError,
    load_deadman_settings,
)
from hermes_sentinel.deadman_probe import DeadManProbeSettings  # noqa: E402
from hermes_sentinel.deadman_store import DeadManStateStore  # noqa: E402
from hermes_sentinel.deadman_telegram import (  # noqa: E402
    DeadManTelegramSettings,
)

VALID_ENV = {
    "HERMES_SENTINEL_DEADMAN_PROBE_URL": (
        "https://sentinel.example/v1/heartbeat"
    ),
    "HERMES_SENTINEL_DEADMAN_STATE_PATH": (
        "/var/lib/hermes-sentinel/deadman-state.json"
    ),
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN": "123456:ABC-DEF_example",
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID": "-1001234567890",
}

REQUIRED_VARIABLES = tuple(VALID_ENV)


def _env(**overrides: str) -> dict[str, str]:
    env = dict(VALID_ENV)
    env.update(overrides)
    return env


class ValidEnvironmentTest(unittest.TestCase):
    def test_valid_complete_env_loads(self) -> None:
        settings = load_deadman_settings(VALID_ENV)
        self.assertIsInstance(settings, DeadManRuntimeSettings)

    def test_exact_parsed_defaults(self) -> None:
        settings = load_deadman_settings(_env())
        self.assertEqual(
            settings.probe.url, "https://sentinel.example/v1/heartbeat"
        )
        self.assertEqual(settings.probe.timeout_seconds, 10.0)
        self.assertEqual(settings.telegram.chat_id, -1001234567890)
        self.assertIsNone(settings.telegram.message_thread_id)
        self.assertEqual(settings.telegram.timeout_seconds, 10.0)

    def test_accepted_settings_objects_constructed(self) -> None:
        settings = load_deadman_settings(_env())
        self.assertIsInstance(settings.probe, DeadManProbeSettings)
        self.assertIsInstance(settings.telegram, DeadManTelegramSettings)
        self.assertIsInstance(settings.store, DeadManStateStore)

    def test_state_path_kept_verbatim_no_filesystem_access(
        self,
    ) -> None:
        # A path whose parent does not exist loads fine: the loader
        # performs NO filesystem access and never creates parent
        # directories (deployment provisioning is H2 scope).
        missing = Path(self.id().replace("_", "-")) / "nested"
        env = _env(
            HERMES_SENTINEL_DEADMAN_STATE_PATH=str(missing / "state.json")
        )
        settings = load_deadman_settings(env)
        self.assertEqual(settings.state_path, missing / "state.json")
        self.assertEqual(settings.store.path, settings.state_path)
        self.assertFalse(missing.exists())

    def test_optional_values_parsed_exactly(self) -> None:
        settings = load_deadman_settings(
            _env(
                HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID="42",
                HERMES_SENTINEL_DEADMAN_PROBE_TIMEOUT_SECONDS="5.5",
                HERMES_SENTINEL_DEADMAN_TELEGRAM_TIMEOUT_SECONDS="3",
            )
        )
        self.assertEqual(settings.telegram.message_thread_id, 42)
        self.assertEqual(settings.probe.timeout_seconds, 5.5)
        self.assertEqual(settings.telegram.timeout_seconds, 3.0)

    def test_positive_chat_id_accepted(self) -> None:
        settings = load_deadman_settings(
            _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID="12345")
        )
        self.assertEqual(settings.telegram.chat_id, 12345)

    def test_unrelated_environment_is_irrelevant(self) -> None:
        settings = load_deadman_settings(
            _env(
                PATH="/usr/bin:/bin",
                HOME="/home/operator",
                TOTALLY_UNRELATED="whatever",
                SENTINEL_TOKEN="not-a-deadman-variable",
            )
        )
        self.assertIsInstance(settings, DeadManRuntimeSettings)


class RequiredValueTest(unittest.TestCase):
    def test_missing_required_variable_fails_closed(self) -> None:
        for variable in REQUIRED_VARIABLES:
            with self.subTest(variable=variable):
                env = dict(VALID_ENV)
                del env[variable]
                with self.assertRaises(DeadManSettingsError) as caught:
                    load_deadman_settings(env)
                self.assertIn(variable, str(caught.exception))

    def test_empty_required_variable_rejected(self) -> None:
        for variable in REQUIRED_VARIABLES:
            for empty in ("", "   ", "\t"):
                with self.subTest(variable=variable, value=empty):
                    with self.assertRaises(DeadManSettingsError):
                        load_deadman_settings(_env(**{variable: empty}))

    def test_non_string_value_rejected(self) -> None:
        with self.assertRaises(DeadManSettingsError):
            load_deadman_settings(
                _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID=42)
            )

    def test_none_mapping_read_fails_bounded(self) -> None:
        class HostileMapping(dict):
            def keys(self):
                raise RuntimeError("hostile mapping")

        with self.assertRaises(DeadManSettingsError):
            load_deadman_settings(HostileMapping())  # type: ignore[arg-type]


class StrictParsingTest(unittest.TestCase):
    def test_invalid_chat_id_rejected(self) -> None:
        for bad in (
            "abc", "12.5", "1e3", " 42", "42 ", "0x2A", "42_0",
            "true", "+-1", "１２３",
        ):
            with self.subTest(value=bad):
                with self.assertRaises(DeadManSettingsError):
                    load_deadman_settings(
                        _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID=bad)
                    )

    def test_zero_chat_id_rejected_by_accepted_settings(self) -> None:
        with self.assertRaises(DeadManSettingsError):
            load_deadman_settings(
                _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID="0")
            )

    def test_invalid_optional_thread_id_rejected(self) -> None:
        for bad in ("abc", "1.5", " 7", "0", "-1"):
            with self.subTest(value=bad):
                with self.assertRaises(DeadManSettingsError):
                    load_deadman_settings(
                        _env(
                            HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID=bad
                        )
                    )

    def test_empty_thread_id_is_present_not_unset(self) -> None:
        # A present empty string is a value, not an unset optional —
        # the strict integer parse rejects it (clean unset is the
        # variable being absent entirely).
        with self.assertRaises(DeadManSettingsError):
            load_deadman_settings(
                _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID="")
            )

    def test_invalid_timeout_rejected(self) -> None:
        for variable in (
            "HERMES_SENTINEL_DEADMAN_PROBE_TIMEOUT_SECONDS",
            "HERMES_SENTINEL_DEADMAN_TELEGRAM_TIMEOUT_SECONDS",
        ):
            for bad in (
                "abc", "0", "-5", "1e999", "nan", "inf", "", "10s",
                " 10", "10 ",
            ):
                with self.subTest(variable=variable, value=bad):
                    with self.assertRaises(DeadManSettingsError):
                        load_deadman_settings(_env(**{variable: bad}))

    def test_https_only_probe_url_enforced_by_accepted_settings(
        self,
    ) -> None:
        for bad in (
            "http://sentinel.example/v1/heartbeat",
            "ftp://sentinel.example/v1/heartbeat",
            "https://:443/v1/heartbeat",
        ):
            with self.subTest(value=bad):
                with self.assertRaises(DeadManSettingsError):
                    load_deadman_settings(
                        _env(HERMES_SENTINEL_DEADMAN_PROBE_URL=bad)
                    )


class SecretSafetyTest(unittest.TestCase):
    def test_token_absent_from_settings_repr(self) -> None:
        settings = load_deadman_settings(_env())
        self.assertNotIn(
            "123456:ABC-DEF_example", repr(settings)
        )
        self.assertNotIn(
            "123456:ABC-DEF_example", repr(settings.telegram)
        )

    def test_token_absent_from_error_text(self) -> None:
        # A token OUTSIDE the conservative alphabet is rejected by the
        # accepted constructor — and the secret must not travel back
        # inside its own rejection text.
        secret = "987654:Z Z Z secret leak probe!"
        with self.assertRaises(DeadManSettingsError) as caught:
            load_deadman_settings(
                _env(HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN=secret)
            )
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("987654", str(caught.exception))

    def test_error_never_echoes_environment_values(self) -> None:
        # Naming the missing VARIABLE is the contract; the raw VALUE
        # of any other present variable must never travel along.
        env = {
            "HERMES_SENTINEL_DEADMAN_PROBE_URL": (
                "https://sentinel.example/v1/heartbeat"
            ),
            "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN": (
                "123456:ABC-DEF_example"
            ),
        }
        with self.assertRaises(DeadManSettingsError) as caught:
            load_deadman_settings(env)
        message = str(caught.exception)
        self.assertIn("HERMES_SENTINEL_DEADMAN_STATE_PATH", message)
        self.assertNotIn("sentinel.example", message)
        self.assertNotIn("123456", message)

    def test_chat_id_value_not_echoed_in_parse_error(self) -> None:
        with self.assertRaises(DeadManSettingsError) as caught:
            load_deadman_settings(
                _env(
                    HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID=(
                        "not-a-number-xyz"
                    )
                )
            )
        message = str(caught.exception)
        self.assertNotIn("not-a-number-xyz", message)
        self.assertIn("HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID", message)


if __name__ == "__main__":
    unittest.main()
