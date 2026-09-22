"""Deterministic tests for the Stage E6 central settings loader."""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

# src-layout bootstrap: allows running the suite without installing the
# package (stdlib unittest has no pythonpath support; pytest gets the
# same path from pyproject [tool.pytest.ini_options]).
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_sentinel.config import (  # noqa: E402
    HeartbeatSettings,
    SentinelConfig,
    Thresholds,
)
from hermes_sentinel.settings import (  # noqa: E402
    CentralSettings,
    CentralSettingsError,
    load_central_settings,
)
from hermes_sentinel.telegram import TelegramSettings  # noqa: E402
from hermes_sentinel.wire import NodeCredentials  # noqa: E402

_TOKEN_ONE = "token-one-secret-value"
_TOKEN_TWO = "token-two-secret-value"
_BOT_TOKEN = "123456:ABC-DEF_example-bot-token"
_NODE_TOKENS_JSON_NAME = "SENTINEL_NODE_TOKENS_JSON"

_REQUIRED_VARIABLES = (
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

_HOSTS: list[dict[str, object]] = [
    {
        "name": "vds-01",
        "heartbeat": {
            "expected_interval_seconds": 30,
            "stale_after_seconds": 90,
        },
        "external": {
            "tcp_host": "203.0.113.10",
            "tcp_port": 22,
            "timeout_seconds": 4.0,
            "down_confirmations": 4,
            "recovery_confirmations": 3,
        },
        "thresholds": {
            "cpu_percent": 75,
            "ram_percent": 80,
            "swap_percent": 70,
            "disk_percent": 90,
            "inode_percent": 85,
            "load5_max": 2.5,
        },
        "services": ["nginx", "postgres"],
    },
    {
        "name": "vds-02",
        "heartbeat": {
            "expected_interval_seconds": 60.5,
            "stale_after_seconds": 180.0,
        },
        "external": {"tcp_host": "vds-02.example", "tcp_port": 2222},
    },
]


def _base_env() -> dict[str, str]:
    return {
        "SENTINEL_DATABASE_PATH": "/var/lib/hermes-sentinel/heartbeats.db",
        "SENTINEL_LISTEN_HOST": "127.0.0.1",
        "SENTINEL_LISTEN_PORT": "8080",
        "SENTINEL_MONITOR_INTERVAL_SECONDS": "30",
        "SENTINEL_POLL_INTERVAL_SECONDS": "0.5",
        "SENTINEL_HOSTS_JSON": json.dumps(_HOSTS),
        "SENTINEL_NODE_TOKENS_JSON": json.dumps(
            {"vds-01": _TOKEN_ONE, "vds-02": _TOKEN_TWO}
        ),
        "SENTINEL_TELEGRAM_BOT_TOKEN": _BOT_TOKEN,
        "SENTINEL_TELEGRAM_CHAT_ID": "-1001234567890",
    }


def _variant(env: Mapping[str, str], **changes: str) -> dict[str, str]:
    result = dict(env)
    result.update(changes)
    return result


def _with_hosts(env: Mapping[str, str], hosts: object) -> dict[str, str]:
    return _variant(env, SENTINEL_HOSTS_JSON=json.dumps(hosts))


def _hosts_copy() -> list[dict[str, object]]:
    return json.loads(json.dumps(_HOSTS))


class _SingleReadMapping(Mapping):  # type: ignore[type-arg]
    """An environment mapping whose keys are readable exactly once.

    A loader that snapshots the mapping reads each key one time; a
    loader that keeps reading the caller's mapping trips the second
    read and fails the test.
    """

    def __init__(self, data: Mapping[str, str]) -> None:
        self._data = dict(data)
        self._read: set[str] = set()

    def __getitem__(self, key: str) -> str:
        if key in self._read:
            raise AssertionError(f"environment key {key!r} read twice")
        self._read.add(key)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class ValidLoadTest(unittest.TestCase):
    def test_fully_valid_multi_host_load(self) -> None:
        settings = load_central_settings(_base_env())

        self.assertIsInstance(settings, CentralSettings)
        self.assertIsInstance(settings.config, SentinelConfig)
        self.assertIsInstance(settings.credentials, NodeCredentials)
        self.assertIsInstance(settings.telegram, TelegramSettings)
        self.assertEqual(
            settings.database_path, "/var/lib/hermes-sentinel/heartbeats.db"
        )
        self.assertEqual(settings.listen_host, "127.0.0.1")
        self.assertEqual(settings.listen_port, 8080)
        self.assertIsInstance(settings.listen_port, int)
        self.assertEqual(settings.monitor_interval_seconds, 30.0)
        self.assertIsInstance(settings.monitor_interval_seconds, float)
        self.assertEqual(settings.poll_interval_seconds, 0.5)

        self.assertEqual(
            [host.name for host in settings.config.hosts], ["vds-01", "vds-02"]
        )

        first, second = settings.config.hosts

        self.assertEqual(first.heartbeat.expected_interval_seconds, 30.0)
        self.assertEqual(first.heartbeat.stale_after_seconds, 90.0)
        self.assertEqual(first.external.tcp_host, "203.0.113.10")
        self.assertEqual(first.external.tcp_port, 22)
        self.assertEqual(first.external.timeout_seconds, 4.0)
        self.assertEqual(first.external.down_confirmations, 4)
        self.assertEqual(first.external.recovery_confirmations, 3)
        self.assertEqual(first.thresholds.cpu_percent, 75.0)
        self.assertEqual(first.thresholds.ram_percent, 80.0)
        self.assertEqual(first.thresholds.swap_percent, 70.0)
        self.assertEqual(first.thresholds.disk_percent, 90.0)
        self.assertEqual(first.thresholds.inode_percent, 85.0)
        self.assertEqual(first.thresholds.load5_max, 2.5)
        self.assertEqual(first.services, ("nginx", "postgres"))
        self.assertIsInstance(first.services, tuple)

        # Second host keeps every accepted default for absent optional
        # nested fields.
        self.assertEqual(second.external.timeout_seconds, 5.0)
        self.assertEqual(second.external.down_confirmations, 3)
        self.assertEqual(second.external.recovery_confirmations, 2)
        self.assertEqual(second.thresholds.cpu_percent, 90.0)
        self.assertEqual(second.thresholds.ram_percent, 90.0)
        self.assertEqual(second.thresholds.swap_percent, 80.0)
        self.assertEqual(second.thresholds.disk_percent, 85.0)
        self.assertEqual(second.thresholds.inode_percent, 90.0)
        self.assertIsNone(second.thresholds.load5_max)
        self.assertEqual(second.services, ())

        self.assertEqual(settings.credentials.token_for("vds-01"), _TOKEN_ONE)
        self.assertEqual(settings.credentials.token_for("vds-02"), _TOKEN_TWO)

        self.assertEqual(settings.telegram.chat_id, -1001234567890)
        self.assertIsNone(settings.telegram.message_thread_id)
        self.assertEqual(settings.telegram.timeout_seconds, 10.0)

    def test_result_is_immutable(self) -> None:
        settings = load_central_settings(_base_env())
        with self.assertRaises(FrozenInstanceError):
            settings.listen_port = 1  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            settings.database_path = "/elsewhere"  # type: ignore[misc]

    def test_unrelated_env_keys_are_ignored(self) -> None:
        env = _variant(
            _base_env(),
            PATH="/usr/bin",
            HOME="/root",
            UNRELATED_SECRET="nothing-to-do-with-sentinel",
        )
        settings = load_central_settings(env)
        self.assertEqual(settings.listen_port, 8080)
        self.assertEqual(len(settings.config.hosts), 2)

    def test_load_reads_each_environment_key_exactly_once(self) -> None:
        # The snapshot contract: one load uses one stable input view.
        mapping = _SingleReadMapping(_base_env())
        settings = load_central_settings(mapping)
        self.assertEqual(settings.listen_port, 8080)

    def test_loaded_settings_are_insulated_from_later_env_mutation(self) -> None:
        env = _base_env()
        settings = load_central_settings(env)
        env["SENTINEL_LISTEN_PORT"] = "99999"
        env["SENTINEL_TELEGRAM_CHAT_ID"] = "1"
        self.assertEqual(settings.listen_port, 8080)
        self.assertEqual(settings.telegram.chat_id, -1001234567890)

    def test_values_are_preserved_verbatim(self) -> None:
        env = _variant(_base_env(), SENTINEL_LISTEN_HOST="node1.internal")
        settings = load_central_settings(env)
        self.assertEqual(settings.listen_host, "node1.internal")


class RequiredVariablesTest(unittest.TestCase):
    def test_every_required_variable_is_mandatory(self) -> None:
        for name in _REQUIRED_VARIABLES:
            with self.subTest(variable=name):
                env = _base_env()
                del env[name]
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(env)
                self.assertIn(name, str(ctx.exception))

    def test_empty_required_scalars_are_rejected(self) -> None:
        for name in _REQUIRED_VARIABLES:
            for empty in ("", "   "):
                with self.subTest(variable=name, value=empty):
                    env = _base_env()
                    env[name] = empty
                    with self.assertRaises(CentralSettingsError):
                        load_central_settings(env)


class ListenPortTest(unittest.TestCase):
    def test_boundary_ports_are_accepted(self) -> None:
        for port in ("1", "65535"):
            with self.subTest(port=port):
                settings = load_central_settings(
                    _variant(_base_env(), SENTINEL_LISTEN_PORT=port)
                )
                self.assertEqual(settings.listen_port, int(port))

    def test_out_of_range_ports_are_rejected(self) -> None:
        for port in ("0", "-1", "65536", "99999"):
            with self.subTest(port=port):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_LISTEN_PORT=port)
                    )
                self.assertIn("SENTINEL_LISTEN_PORT", str(ctx.exception))

    def test_non_integer_port_strings_are_rejected(self) -> None:
        for port in (
            "8080.5",
            " 8080",
            "8080 ",
            "8_080",
            "0x1F90",
            "tcp",
            "+-8080",
        ):
            with self.subTest(port=port):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_LISTEN_PORT=port)
                    )
                self.assertIn("SENTINEL_LISTEN_PORT", str(ctx.exception))


class IntervalTest(unittest.TestCase):
    def test_valid_interval_strings_produce_positive_floats(self) -> None:
        cases = {
            "SENTINEL_MONITOR_INTERVAL_SECONDS": (
                ("30", 30.0),
                ("2.5", 2.5),
                ("1e2", 100.0),
                ("2.5E-1", 0.25),
                ("0.001", 0.001),
            ),
            "SENTINEL_POLL_INTERVAL_SECONDS": (
                ("0.5", 0.5),
                ("1", 1.0),
                ("0.25", 0.25),
            ),
        }
        for variable, pairs in cases.items():
            for raw, expected in pairs:
                with self.subTest(variable=variable, raw=raw):
                    settings = load_central_settings(
                        _variant(_base_env(), **{variable: raw})
                    )
                    value = getattr(settings, _attribute_name(variable))
                    self.assertEqual(value, expected)
                    self.assertIsInstance(value, float)

    def test_non_positive_or_malformed_intervals_are_rejected(self) -> None:
        variables = (
            "SENTINEL_MONITOR_INTERVAL_SECONDS",
            "SENTINEL_POLL_INTERVAL_SECONDS",
        )
        for variable in variables:
            for raw in ("0", "-1", "abc", "", "nan", "inf", "1e999", " 30"):
                with self.subTest(variable=variable, raw=raw):
                    with self.assertRaises(CentralSettingsError) as ctx:
                        load_central_settings(
                            _variant(_base_env(), **{variable: raw})
                        )
                    self.assertIn(variable, str(ctx.exception))


def _attribute_name(variable: str) -> str:
    return variable.removeprefix("SENTINEL_").lower()


class HostsJsonTest(unittest.TestCase):
    def test_malformed_hosts_json_is_rejected(self) -> None:
        for raw in ('[{"name": "vds-01"', "{", "", "not json at all"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_HOSTS_JSON=raw)
                    )
                self.assertIn("SENTINEL_HOSTS_JSON", str(ctx.exception))

    def test_duplicate_json_keys_are_rejected_at_every_level(self) -> None:
        cases = [
            # duplicate host-level key
            '[{"name": "vds-01", "name": "vds-02", '
            '"heartbeat": {"expected_interval_seconds": 30,'
            ' "stale_after_seconds": 90}, '
            '"external": {"tcp_host": "h", "tcp_port": 22}}]',
            # duplicate nested heartbeat key
            '[{"name": "vds-01", '
            '"heartbeat": {"expected_interval_seconds": 30,'
            ' "expected_interval_seconds": 60,'
            ' "stale_after_seconds": 90}, '
            '"external": {"tcp_host": "h", "tcp_port": 22}}]',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_HOSTS_JSON=raw)
                    )
                self.assertIn("duplicate object key", str(ctx.exception))

    def test_non_finite_json_constants_are_rejected(self) -> None:
        cases = [
            '[{"name": "vds-01", "heartbeat": {"expected_interval_seconds"'
            ": NaN, \"stale_after_seconds\": 90}, "
            '"external": {"tcp_host": "h", "tcp_port": 22}}]',
            '[{"name": "vds-01", "heartbeat": {"expected_interval_seconds"'
            ": Infinity, \"stale_after_seconds\": 90}, "
            '"external": {"tcp_host": "h", "tcp_port": 22}}]',
            '[{"name": "vds-01", "heartbeat": {"expected_interval_seconds"'
            ": 1e999, \"stale_after_seconds\": 90}, "
            '"external": {"tcp_host": "h", "tcp_port": 22}}]',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_HOSTS_JSON=raw)
                    )
                self.assertIn("SENTINEL_HOSTS_JSON", str(ctx.exception))

    def test_non_array_hosts_root_is_rejected(self) -> None:
        for raw in ('{"hosts": []}', "42", '"vds-01"', "null", "true"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_HOSTS_JSON=raw)
                    )
                self.assertIn("must be a JSON array", str(ctx.exception))

    def test_non_object_host_entry_is_rejected(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), ["vds-01"]))
        self.assertIn("must be a JSON object", str(ctx.exception))

    def test_unknown_keys_are_rejected_at_every_level(self) -> None:
        def check(hosts: list[dict[str, object]], key: str) -> None:
            with self.subTest(key=key):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(_with_hosts(_base_env(), hosts))
                self.assertIn("unknown field", str(ctx.exception))
                self.assertIn(key, str(ctx.exception))

        hosts = _hosts_copy()
        hosts[0]["extra"] = 1
        check(hosts, "extra")

        hosts = _hosts_copy()
        heartbeat = dict(hosts[0]["heartbeat"])  # type: ignore[arg-type]
        heartbeat["extra"] = 1
        hosts[0]["heartbeat"] = heartbeat
        check(hosts, "extra")

        hosts = _hosts_copy()
        external = dict(hosts[0]["external"])  # type: ignore[arg-type]
        external["extra"] = 1
        hosts[0]["external"] = external
        check(hosts, "extra")

        hosts = _hosts_copy()
        thresholds = dict(hosts[0]["thresholds"])  # type: ignore[arg-type]
        thresholds["extra"] = 1
        hosts[0]["thresholds"] = thresholds
        check(hosts, "extra")

    def test_missing_host_required_fields_are_rejected(self) -> None:
        for field in ("name", "heartbeat", "external"):
            with self.subTest(field=field):
                hosts = _hosts_copy()
                del hosts[0][field]
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(_with_hosts(_base_env(), hosts))
                self.assertIn("missing mandatory field", str(ctx.exception))
                self.assertIn(field, str(ctx.exception))

    def test_missing_nested_required_fields_are_rejected(self) -> None:
        hosts = _hosts_copy()
        del hosts[0]["heartbeat"]["stale_after_seconds"]  # type: ignore[union-attr]
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertIn("stale_after_seconds", str(ctx.exception))

        hosts = _hosts_copy()
        del hosts[0]["external"]["tcp_host"]  # type: ignore[union-attr]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        del hosts[1]["external"]["tcp_port"]  # type: ignore[union-attr]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

    def test_bool_and_numeric_strings_are_rejected_at_numeric_fields(self) -> None:
        def reject(hosts: list[dict[str, object]]) -> None:
            with self.assertRaises(CentralSettingsError) as ctx:
                load_central_settings(_with_hosts(_base_env(), hosts))
            self.assertIn("SENTINEL_HOSTS_JSON", str(ctx.exception))

        hosts = _hosts_copy()
        hosts[0]["external"]["tcp_port"] = True  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["external"]["tcp_port"] = "22"  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["external"]["tcp_port"] = 22.0  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["heartbeat"]["expected_interval_seconds"] = "30"  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["heartbeat"]["stale_after_seconds"] = False  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["thresholds"]["cpu_percent"] = True  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["thresholds"]["load5_max"] = "2.5"  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["external"]["down_confirmations"] = True  # type: ignore[index]
        reject(hosts)

        hosts = _hosts_copy()
        hosts[0]["external"]["timeout_seconds"] = "4"  # type: ignore[index]
        reject(hosts)

    def test_non_string_name_and_heartbeat_objects_are_rejected(self) -> None:
        hosts = _hosts_copy()
        hosts[0]["name"] = 42
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["heartbeat"] = None
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["external"] = [22]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

    def test_partial_thresholds_keep_remaining_defaults(self) -> None:
        hosts = _hosts_copy()
        hosts[0]["thresholds"] = {"cpu_percent": 50}
        settings = load_central_settings(_with_hosts(_base_env(), hosts))
        thresholds = settings.config.hosts[0].thresholds
        self.assertEqual(thresholds.cpu_percent, 50.0)
        self.assertEqual(thresholds.ram_percent, 90.0)
        self.assertEqual(thresholds.swap_percent, 80.0)
        self.assertEqual(thresholds.disk_percent, 85.0)
        self.assertEqual(thresholds.inode_percent, 90.0)
        self.assertIsNone(thresholds.load5_max)

    def test_explicit_null_load5_max_is_none(self) -> None:
        hosts = _hosts_copy()
        hosts[0]["thresholds"] = {"load5_max": None}
        settings = load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertIsNone(settings.config.hosts[0].thresholds.load5_max)

    def test_invalid_domain_values_are_wrapped(self) -> None:
        hosts = _hosts_copy()
        hosts[0]["heartbeat"]["expected_interval_seconds"] = 0
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertIn("expected_interval_seconds", str(ctx.exception))

        hosts = _hosts_copy()
        hosts[0]["external"]["tcp_port"] = 70000
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["thresholds"]["cpu_percent"] = 0
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

    def test_services_default_and_tuple_conversion(self) -> None:
        hosts = _hosts_copy()
        del hosts[0]["services"]
        hosts[0]["services"] = []
        settings = load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertEqual(settings.config.hosts[0].services, ())
        self.assertIsInstance(settings.config.hosts[0].services, tuple)

        hosts = _hosts_copy()
        hosts[0]["services"] = ["nginx", 42]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["services"] = ["nginx", ""]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["services"] = ["nginx", "nginx"]
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        hosts = _hosts_copy()
        hosts[0]["services"] = "nginx"
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

    def test_duplicate_host_names_rejected_via_sentinel_config(self) -> None:
        hosts = _hosts_copy()
        hosts[1]["name"] = "vds-01"
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertIn("unique", str(ctx.exception))


class CredentialsTest(unittest.TestCase):
    def test_malformed_token_json_is_rejected_without_chaining(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON='{"vds-01": "tok"',
                )
            )
        self.assertIn("SENTINEL_NODE_TOKENS_JSON", str(ctx.exception))
        # The raw parser exception can quote the secret-bearing raw
        # input; the boundary error must not carry it as context.
        self.assertIsNone(ctx.exception.__context__)

    def test_token_json_non_object_is_rejected(self) -> None:
        for raw in ('["vds-01"]', '"x"', "42", "null"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_NODE_TOKENS_JSON=raw)
                    )
                self.assertIn("must be a JSON object", str(ctx.exception))

    def test_non_string_token_values_are_rejected(self) -> None:
        for value in ("123", "true", "null", "1.5"):
            with self.subTest(value=value):
                raw = '{"vds-01": %s, "vds-02": "t"}' % value
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_NODE_TOKENS_JSON=raw)
                    )
                self.assertIn("strings", str(ctx.exception))

    def test_credential_set_missing_a_configured_host_is_rejected(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps({"vds-01": _TOKEN_ONE}),
                )
            )
        message = str(ctx.exception)
        self.assertIn("vds-02", message)
        self.assertIn(_NODE_TOKENS_JSON_NAME, message)

    def test_credential_for_unknown_host_is_rejected(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {
                            "vds-01": _TOKEN_ONE,
                            "vds-02": _TOKEN_TWO,
                            "ghost": "ghost-token",
                        }
                    ),
                )
            )
        message = str(ctx.exception)
        self.assertIn("ghost", message)
        self.assertNotIn("ghost-token", message)

    def test_duplicate_tokens_rejected_through_node_credentials(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"vds-01": "same-secret", "vds-02": "same-secret"}
                    ),
                )
            )
        message = str(ctx.exception)
        self.assertIn("duplicate", message)
        # Node names are non-secret; the shared token value is.
        self.assertNotIn("same-secret", message)

    def test_whitespace_only_token_rejected_through_node_credentials(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"vds-01": "   ", "vds-02": _TOKEN_TWO}
                    ),
                )
            )
        self.assertIn("vds-01", str(ctx.exception))

    def test_empty_credential_node_name_is_rejected(self) -> None:
        with self.assertRaises(CentralSettingsError):
            load_central_settings(
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"": "token", "vds-01": _TOKEN_ONE, "vds-02": _TOKEN_TWO}
                    ),
                )
            )


class TelegramTest(unittest.TestCase):
    def test_valid_chat_thread_and_timeout_parsing(self) -> None:
        settings = load_central_settings(
            _variant(
                _base_env(),
                SENTINEL_TELEGRAM_CHAT_ID="123456789",
                SENTINEL_TELEGRAM_MESSAGE_THREAD_ID="42",
                SENTINEL_TELEGRAM_TIMEOUT_SECONDS="7.5",
            )
        )
        self.assertEqual(settings.telegram.chat_id, 123456789)
        self.assertEqual(settings.telegram.message_thread_id, 42)
        self.assertEqual(settings.telegram.timeout_seconds, 7.5)

    def test_negative_group_chat_id_is_valid(self) -> None:
        settings = load_central_settings(_base_env())
        self.assertEqual(settings.telegram.chat_id, -1001234567890)

    def test_absent_optional_values_keep_defaults(self) -> None:
        settings = load_central_settings(_base_env())
        self.assertIsNone(settings.telegram.message_thread_id)
        self.assertEqual(settings.telegram.timeout_seconds, 10.0)

    def test_empty_optional_values_are_invalid_not_absent(self) -> None:
        for variable in (
            "SENTINEL_TELEGRAM_MESSAGE_THREAD_ID",
            "SENTINEL_TELEGRAM_TIMEOUT_SECONDS",
        ):
            with self.subTest(variable=variable):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(_variant(_base_env(), **{variable: ""}))
                self.assertIn(variable, str(ctx.exception))

    def test_malformed_thread_ids_are_rejected(self) -> None:
        for raw in ("abc", "4.2", " 42", "1_2"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError):
                    load_central_settings(
                        _variant(
                            _base_env(),
                            SENTINEL_TELEGRAM_MESSAGE_THREAD_ID=raw,
                        )
                    )
        # Strict integers that TelegramSettings itself rejects.
        for raw in ("-3", "0"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(
                            _base_env(),
                            SENTINEL_TELEGRAM_MESSAGE_THREAD_ID=raw,
                        )
                    )
                self.assertIn("message_thread_id", str(ctx.exception))

    def test_malformed_timeouts_are_rejected(self) -> None:
        for raw in ("abc", "0", "-1", "inf", "nan", "1e999", "7.", "."):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(
                            _base_env(), SENTINEL_TELEGRAM_TIMEOUT_SECONDS=raw
                        )
                    )
                self.assertIn(
                    "SENTINEL_TELEGRAM_TIMEOUT_SECONDS", str(ctx.exception)
                )

    def test_malformed_chat_ids_are_rejected(self) -> None:
        for raw in ("abc", "12.5", " 1", "1e3", "0x10"):
            with self.subTest(raw=raw):
                with self.assertRaises(CentralSettingsError):
                    load_central_settings(
                        _variant(_base_env(), SENTINEL_TELEGRAM_CHAT_ID=raw)
                    )
        # Strict integer, but zero is rejected by TelegramSettings.
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(_base_env(), SENTINEL_TELEGRAM_CHAT_ID="0")
            )
        self.assertIn("chat_id", str(ctx.exception))

    def test_invalid_bot_token_rejected_through_telegram_settings(self) -> None:
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(
                    _base_env(), SENTINEL_TELEGRAM_BOT_TOKEN="bad token!"
                )
            )
        message = str(ctx.exception)
        self.assertIn("bot_token", message)
        self.assertNotIn("bad token!", message)
        self.assertIsNone(ctx.exception.__context__)


class SecretSafetyTest(unittest.TestCase):
    def test_repr_never_exposes_secrets(self) -> None:
        settings = load_central_settings(_base_env())
        rendered = repr(settings)
        # Non-secret structure remains visible and useful...
        self.assertIn("vds-01", rendered)
        self.assertIn("chat_id", rendered)
        # ...while no secret material ever appears.
        self.assertNotIn(_TOKEN_ONE, rendered)
        self.assertNotIn(_TOKEN_TWO, rendered)
        self.assertNotIn(_BOT_TOKEN, rendered)

    def test_error_messages_never_expose_secrets(self) -> None:
        secret_bearing_envs: list[tuple[str, dict[str, str]]] = [
            (
                "malformed tokens JSON containing the secret",
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON='{"vds-01": "%s"' % _TOKEN_ONE,
                ),
            ),
            (
                "non-string token value",
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"vds-01": 123, "vds-02": _TOKEN_TWO}
                    ),
                ),
            ),
            (
                "duplicate tokens",
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"vds-01": _TOKEN_ONE, "vds-02": _TOKEN_ONE}
                    ),
                ),
            ),
            (
                "credential host mismatch",
                _variant(
                    _base_env(),
                    SENTINEL_NODE_TOKENS_JSON=json.dumps(
                        {"vds-01": _TOKEN_ONE}
                    ),
                ),
            ),
            (
                "invalid bot token",
                _variant(
                    _base_env(),
                    SENTINEL_TELEGRAM_BOT_TOKEN="123456:bad secret token",
                ),
            ),
        ]
        for label, env in secret_bearing_envs:
            with self.subTest(case=label):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(env)
                text = str(ctx.exception) + repr(ctx.exception)
                self.assertNotIn(_TOKEN_ONE, text)
                self.assertNotIn(_TOKEN_TWO, text)
                self.assertNotIn(_BOT_TOKEN, text)
                self.assertNotIn("bad secret token", text)

    def test_secret_bearing_failures_have_no_exception_context(self) -> None:
        cases = [
            _variant(
                _base_env(), SENTINEL_NODE_TOKENS_JSON='{"vds-01": "t"'
            ),
            _variant(_base_env(), SENTINEL_TELEGRAM_BOT_TOKEN="has space"),
            # Excessive nesting inside the credential document: the
            # bounded RecursionError boundary must stay secret-safe.
            _variant(
                _base_env(),
                SENTINEL_NODE_TOKENS_JSON='{"a": ' * 5000
                + '"%s"' % _TOKEN_ONE
                + "}" * 5000,
            ),
        ]
        for env in cases:
            with self.assertRaises(CentralSettingsError) as ctx:
                load_central_settings(env)
            self.assertIsNone(ctx.exception.__context__)


_HUGE_INT = 10**400
_EXACT_INT = 9007199254740993  # 2**53 + 1: not exactly a float
_HOSTILE_MARKER = "hostile-mapping-secret-marker"


class NumericBoundaryTest(unittest.TestCase):
    """QA remediation: JSON numeric exactness and overflow bounding.

    E6 owns only the external representation; the accepted config
    constructors stay authoritative for numeric validity, so the
    loader must not impose any independent numeric range.
    """

    def test_boundary_adjacent_integer_is_accepted_and_preserved(
        self,
    ) -> None:
        # int(sys.float_info.max) + 1 exceeds every float, yet the
        # accepted constructors consume it safely and retain the exact
        # int — so E6 must accept the same value with no float
        # rounding and no loader-only cutoff.
        value = int(sys.float_info.max) + 1

        # Constructor parity first: the accepted constructors accept
        # the boundary-adjacent value on their own authority.
        thresholds = Thresholds(load5_max=value)
        self.assertEqual(thresholds.load5_max, value)
        heartbeat = HeartbeatSettings(
            expected_interval_seconds=value, stale_after_seconds=90.0
        )
        self.assertEqual(heartbeat.expected_interval_seconds, value)

        # Then the loader: same value accepted, preserved exactly.
        hosts = _hosts_copy()
        hosts[0]["thresholds"]["load5_max"] = value
        hosts[0]["heartbeat"]["expected_interval_seconds"] = value
        settings = load_central_settings(_with_hosts(_base_env(), hosts))
        host = settings.config.hosts[0]
        self.assertEqual(host.thresholds.load5_max, value)
        self.assertIsInstance(host.thresholds.load5_max, int)
        self.assertEqual(host.heartbeat.expected_interval_seconds, value)
        # Exactness proof: float(value) is the rounded-down float max,
        # a DIFFERENT number, so equality with it would mean rounding.
        self.assertNotEqual(host.thresholds.load5_max, float(value))
        self.assertNotEqual(
            host.heartbeat.expected_interval_seconds, float(value)
        )

    def test_excessive_integer_fails_bounded_through_constructor(self) -> None:
        # 10**400 cannot be evaluated by the constructors' own
        # validation (math.isfinite raises OverflowError); E6 bounds
        # that downstream failure instead of predicting it with a
        # parallel numeric policy.
        hosts = _hosts_copy()
        hosts[0]["thresholds"]["load5_max"] = _HUGE_INT
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), hosts))
        error = ctx.exception
        message = str(error)
        self.assertIn("SENTINEL_HOSTS_JSON", message)
        self.assertNotIn(str(_HUGE_INT), message)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

        hosts = _hosts_copy()
        hosts[0]["heartbeat"]["expected_interval_seconds"] = _HUGE_INT
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(_with_hosts(_base_env(), hosts))
        self.assertIn("heartbeat", str(ctx.exception))
        self.assertNotIn(str(_HUGE_INT), str(ctx.exception))

        hosts = _hosts_copy()
        hosts[0]["external"]["timeout_seconds"] = _HUGE_INT
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

        # A huge integer at a JSON-integer field fails through the
        # accepted constructor's range validation, still bounded.
        hosts = _hosts_copy()
        hosts[0]["external"]["tcp_port"] = _HUGE_INT
        with self.assertRaises(CentralSettingsError):
            load_central_settings(_with_hosts(_base_env(), hosts))

    def test_exact_large_integer_is_preserved_verbatim(self) -> None:
        # 2**53 + 1 must reach the authoritative constructor as the
        # exact integer, never silently rounded to 2**53 by a
        # premature float conversion.
        hosts = _hosts_copy()
        hosts[0]["thresholds"]["load5_max"] = _EXACT_INT
        hosts[0]["heartbeat"]["expected_interval_seconds"] = _EXACT_INT
        settings = load_central_settings(_with_hosts(_base_env(), hosts))
        host = settings.config.hosts[0]
        self.assertEqual(host.thresholds.load5_max, _EXACT_INT)
        self.assertNotEqual(host.thresholds.load5_max, _EXACT_INT - 1)
        self.assertIsInstance(host.thresholds.load5_max, int)
        self.assertEqual(
            host.heartbeat.expected_interval_seconds, _EXACT_INT
        )

    def test_normal_json_numbers_still_behave_as_intended(self) -> None:
        settings = load_central_settings(_base_env())
        first = settings.config.hosts[0]
        # JSON integers arrive exactly (comparing equal to both the
        # int and float spelling of the same value); JSON floats stay
        # floats.
        self.assertEqual(first.heartbeat.expected_interval_seconds, 30)
        self.assertEqual(first.heartbeat.expected_interval_seconds, 30.0)
        self.assertEqual(first.external.timeout_seconds, 4.0)
        self.assertIsInstance(first.external.timeout_seconds, float)
        self.assertEqual(first.thresholds.cpu_percent, 75.0)
        self.assertEqual(first.thresholds.load5_max, 2.5)

    def test_bool_remains_rejected_as_numeric_input(self) -> None:
        for field, section in (
            ("expected_interval_seconds", "heartbeat"),
            ("cpu_percent", "thresholds"),
            ("tcp_port", "external"),
            ("timeout_seconds", "external"),
        ):
            with self.subTest(field=field):
                hosts = _hosts_copy()
                hosts[0][section][field] = True  # type: ignore[index]
                with self.assertRaises(CentralSettingsError):
                    load_central_settings(_with_hosts(_base_env(), hosts))


class IntegerStringLengthLimitTest(unittest.TestCase):
    """QA remediation: over-limit decimal integer strings."""

    def test_over_limit_integer_strings_fail_bounded_on_every_path(self) -> None:
        raw = "9" * (sys.get_int_max_str_digits() + 100)
        cases = (
            "SENTINEL_LISTEN_PORT",
            "SENTINEL_TELEGRAM_CHAT_ID",
            "SENTINEL_TELEGRAM_MESSAGE_THREAD_ID",
        )
        for variable in cases:
            with self.subTest(variable=variable):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(
                        _variant(_base_env(), **{variable: raw})
                    )
                error = ctx.exception
                message = str(error)
                self.assertIn(variable, message)
                self.assertNotIn(raw, message)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)


class JsonNestingBoundaryTest(unittest.TestCase):
    """QA remediation: raw RecursionError from nested JSON."""

    def test_excessively_nested_hosts_json_fails_bounded(self) -> None:
        raw = "[" * 5000 + "]" * 5000
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(_base_env(), SENTINEL_HOSTS_JSON=raw)
            )
        error = ctx.exception
        self.assertIn("SENTINEL_HOSTS_JSON", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_excessively_nested_tokens_json_fails_bounded_and_secret_safe(
        self,
    ) -> None:
        raw = '{"a": ' * 5000 + '"%s"' % _TOKEN_ONE + "}" * 5000
        with self.assertRaises(CentralSettingsError) as ctx:
            load_central_settings(
                _variant(_base_env(), SENTINEL_NODE_TOKENS_JSON=raw)
            )
        error = ctx.exception
        self.assertIn("SENTINEL_NODE_TOKENS_JSON", str(error))
        text = str(error) + repr(error)
        self.assertNotIn(_TOKEN_ONE, text)
        self.assertNotIn(_BOT_TOKEN, text)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class _HostileIterationMapping(Mapping):  # type: ignore[type-arg]
    """A mapping whose iteration fails while the snapshot is built."""

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(_HOSTILE_MARKER)

    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __len__(self) -> int:
        return 1


class _HostileGetItemMapping(Mapping):  # type: ignore[type-arg]
    """A mapping whose item reads fail while the snapshot is built."""

    def __init__(self) -> None:
        self._keys = ("SENTINEL_DATABASE_PATH",)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __getitem__(self, key: str) -> str:
        raise RuntimeError(_HOSTILE_MARKER)


class SnapshotFailureTest(unittest.TestCase):
    """QA remediation: adversarial snapshot failures stay bounded."""

    def test_adversarial_mapping_snapshot_failure_is_bounded(self) -> None:
        for mapping in (
            _HostileIterationMapping(),
            _HostileGetItemMapping(),
        ):
            with self.subTest(mapping=type(mapping).__name__):
                with self.assertRaises(CentralSettingsError) as ctx:
                    load_central_settings(mapping)
                error = ctx.exception
                # The bounded failure names no variable and copies no
                # mapping content; parsing never starts.
                self.assertIn("environment mapping", str(error))
                text = str(error) + repr(error)
                self.assertNotIn(_HOSTILE_MARKER, text)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)


class ScalarSyntaxContractTest(unittest.TestCase):
    """QA-emphasized strict scalar syntax forms, asserted as the
    documented parser contract."""

    def test_signed_and_leading_zero_integer_forms_are_accepted(self) -> None:
        settings = load_central_settings(
            _variant(
                _base_env(),
                SENTINEL_LISTEN_PORT="+08080",
                SENTINEL_TELEGRAM_CHAT_ID="+123456789",
                SENTINEL_TELEGRAM_MESSAGE_THREAD_ID="+42",
            )
        )
        self.assertEqual(settings.listen_port, 8080)
        self.assertEqual(settings.telegram.chat_id, 123456789)
        self.assertEqual(settings.telegram.message_thread_id, 42)

        settings = load_central_settings(
            _variant(
                _base_env(),
                SENTINEL_LISTEN_PORT="01",
                SENTINEL_TELEGRAM_CHAT_ID="0123456789",
            )
        )
        self.assertEqual(settings.listen_port, 1)
        self.assertEqual(settings.telegram.chat_id, 123456789)

    def test_float_forms_are_not_integer_strings(self) -> None:
        variables = (
            "SENTINEL_LISTEN_PORT",
            "SENTINEL_TELEGRAM_CHAT_ID",
            "SENTINEL_TELEGRAM_MESSAGE_THREAD_ID",
        )
        for variable in variables:
            for raw in (".5", "1e-3", "0.5", "1e3", "5."):
                with self.subTest(variable=variable, raw=raw):
                    with self.assertRaises(CentralSettingsError):
                        load_central_settings(
                            _variant(_base_env(), **{variable: raw})
                        )

    def test_decimal_number_forms_for_intervals(self) -> None:
        cases = {
            "+30": 30.0,
            "01": 1.0,
            ".5": 0.5,
            "1e-3": 0.001,
            "2e+3": 2000.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                settings = load_central_settings(
                    _variant(
                        _base_env(), SENTINEL_POLL_INTERVAL_SECONDS=raw
                    )
                )
                self.assertEqual(settings.poll_interval_seconds, expected)
                self.assertIsInstance(
                    settings.poll_interval_seconds, float
                )

    def test_decimal_number_forms_for_telegram_timeout(self) -> None:
        for raw, expected in ((".5", 0.5), ("1e-3", 0.001), ("+10", 10.0)):
            with self.subTest(raw=raw):
                settings = load_central_settings(
                    _variant(
                        _base_env(), SENTINEL_TELEGRAM_TIMEOUT_SECONDS=raw
                    )
                )
                self.assertEqual(settings.telegram.timeout_seconds, expected)


if __name__ == "__main__":
    unittest.main()
