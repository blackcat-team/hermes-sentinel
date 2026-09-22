"""Central application settings loader (Stage E6).

The bounded typed loader that turns ONE caller-supplied environment
mapping into exactly the already-accepted configuration objects the
future application composition root will need — and nothing else:

    load_central_settings(env) -> CentralSettings
                |
    one stable plain-dict snapshot of the supplied mapping
                |
    strict, fail-closed boundary parsing
                |
    construction through the accepted domain constructors
                |
    CentralSettings(
        config, credentials, telegram, database_path, listen_host,
        listen_port, monitor_interval_seconds, poll_interval_seconds)

E6 owns ONLY typed loading/validation of central settings. It
constructs no application parts — no SQLite connection or repository,
no HeartbeatIngestor, no B3/B4/B5 adapters or server, no HealthEngine,
no TelegramSender, no NotificationCoordinator, no MonitoringCycle, no
SentinelRuntime — and starts nothing. It never reads ``os.environ``
implicitly: the caller supplies the environment mapping explicitly
(the future composition root may pass ``os.environ``), and the mapping
is snapshotted into a plain ``dict`` before parsing so one load uses
one stable input view.

The accepted domain constructors remain the sole semantic authorities
for their own invariants: ``HeartbeatSettings`` /
``ExternalCheckSettings`` / ``Thresholds`` / ``HostConfig`` /
``SentinelConfig`` (config.py), ``NodeCredentials`` (wire.py) and
``TelegramSettings`` (telegram.py). This loader performs only the
strict boundary parse and then hands construction to them; their
business semantics are never duplicated here.

Contract points (see docs/ARCHITECTURE.md section 27):

- required variables: ``SENTINEL_DATABASE_PATH``,
  ``SENTINEL_LISTEN_HOST``, ``SENTINEL_LISTEN_PORT``,
  ``SENTINEL_MONITOR_INTERVAL_SECONDS``,
  ``SENTINEL_POLL_INTERVAL_SECONDS``, ``SENTINEL_HOSTS_JSON``,
  ``SENTINEL_NODE_TOKENS_JSON``, ``SENTINEL_TELEGRAM_BOT_TOKEN``,
  ``SENTINEL_TELEGRAM_CHAT_ID``; optional:
  ``SENTINEL_TELEGRAM_MESSAGE_THREAD_ID`` and
  ``SENTINEL_TELEGRAM_TIMEOUT_SECONDS`` (absent preserves the accepted
  ``TelegramSettings`` 10.0-second default). Unrelated environment
  keys are ignored; no aliases or legacy variable names exist;
- string scalars are kept verbatim (never stripped or normalized) but
  must be non-empty after a whitespace check. The database path gets
  no filesystem access (no directories or files are created) and the
  listen host no DNS/network action;
- ``SENTINEL_LISTEN_PORT`` and the Telegram integer values are strict
  decimal integer strings — no float forms, no underscores, no
  surrounding whitespace, no silent coercion; the listen port is
  additionally restricted to the production range [1, 65535]. The
  monitor/poll interval values are strict decimal number strings
  parsed to finite, strictly positive floats — the same fail-fast
  interval semantics the accepted E5 ``SentinelRuntime`` enforces,
  with no silent fallback for explicitly malformed values; decimal
  integer strings beyond the CPython integer-string conversion
  limit fail through the same bounded error, so no raw ``int()``
  ValueError ever leaks;
- ``SENTINEL_HOSTS_JSON`` is a JSON array of host objects (required:
  ``name``, ``heartbeat``, ``external``; optional: ``thresholds``,
  ``services``). Missing optional nested fields keep the accepted
  dataclass defaults (``Thresholds`` defaults, empty services tuple,
  external timeout/confirmation defaults). At every supported object
  level the boundary parse is strict: no unknown keys, no duplicate
  JSON object keys, no bool-as-number, no numeric strings where a
  JSON number is required, no JSON integers given as floats, and no
  non-finite constants or overflowed number literals (``NaN``,
  ``Infinity``, ``1e999``). Exact JSON integers are preserved
  verbatim as integers — never rounded through a premature float
  conversion (``9007199254740993`` stays exact) — and handed to the
  accepted constructors, which stay authoritative for their own
  numeric validity: E6 establishes no independent numeric range. A
  constructor validation failure caused by external configuration —
  including the numeric ``OverflowError`` an accepted constructor
  itself raises for an integer it cannot safely evaluate — is
  bounded as :class:`CentralSettingsError`, as is excessively nested
  JSON: no raw overflow or recursion failure leaks. Malformed values
  are never silently coerced;
  duplicate host names are rejected through the accepted
  ``SentinelConfig`` constructor;
- ``SENTINEL_NODE_TOKENS_JSON`` is a JSON object of node name ->
  secret token (every value a string). For production settings
  loading the credential node set must match the configured
  ``SentinelConfig`` host-name set exactly — every configured host
  has exactly one credential, and no credential exists for an
  unknown/unconfigured host. Construction then goes through the
  accepted ``NodeCredentials``, whose secret validation and
  duplicate-token rejection stay authoritative;
- every external malformed/missing-settings failure raises the
  bounded :class:`CentralSettingsError`; raw json/int/float,
  overflow and recursion exceptions never leak. Messages identify at
  most the variable name,
  non-secret field paths and the category of validation failure —
  never heartbeat tokens, the Telegram bot token, the secret JSON
  documents or the environment mapping. Unsafe exception chaining is
  suppressed: boundary errors are raised OUTSIDE the active parser
  handlers (flag-based control flow), so no raw secret-bearing
  parser/context exception stays attached. Nothing is logged. The
  immutable ``CentralSettings`` repr stays secret-safe through the
  accepted ``NodeCredentials`` and ``TelegramSettings`` repr
  contracts.

Out of scope for E6: everything application-shaped — the composition
root, server/runtime construction and lifecycle, CLI/entrypoints,
signal handling, the central systemd service, TLS termination,
reverse proxy, retries/backoff, logging frameworks, persistence or
schema changes, Hermes integration, remote remediation, deployment
and Stage F production hardening.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from hermes_sentinel.config import (
    ExternalCheckSettings,
    HeartbeatSettings,
    HostConfig,
    SentinelConfig,
    Thresholds,
)
from hermes_sentinel.telegram import TelegramSettings
from hermes_sentinel.wire import NodeCredentials

__all__ = [
    "CentralSettings",
    "CentralSettingsError",
    "load_central_settings",
]

_DATABASE_PATH_VAR = "SENTINEL_DATABASE_PATH"
_LISTEN_HOST_VAR = "SENTINEL_LISTEN_HOST"
_LISTEN_PORT_VAR = "SENTINEL_LISTEN_PORT"
_MONITOR_INTERVAL_VAR = "SENTINEL_MONITOR_INTERVAL_SECONDS"
_POLL_INTERVAL_VAR = "SENTINEL_POLL_INTERVAL_SECONDS"
_HOSTS_JSON_VAR = "SENTINEL_HOSTS_JSON"
_NODE_TOKENS_JSON_VAR = "SENTINEL_NODE_TOKENS_JSON"
_BOT_TOKEN_VAR = "SENTINEL_TELEGRAM_BOT_TOKEN"
_CHAT_ID_VAR = "SENTINEL_TELEGRAM_CHAT_ID"
_THREAD_ID_VAR = "SENTINEL_TELEGRAM_MESSAGE_THREAD_ID"
_TIMEOUT_VAR = "SENTINEL_TELEGRAM_TIMEOUT_SECONDS"

#: Strict signed decimal integer strings: ASCII digits with an
#: optional leading sign — nothing else (no float forms, no
#: underscores, no whitespace, no ``int()``-style liberal parsing).
_SIGNED_DECIMAL_INTEGER = re.compile(r"[+-]?[0-9]+\Z")

#: Strict decimal number strings: optional sign, digits with an
#: optional fractional part (or a bare fractional part) and an
#: optional decimal exponent. JSON-number-like strictness (a bare
#: trailing dot is not a number), and ``nan``/``inf`` spellings do
#: not match.
_DECIMAL_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z"
)

#: Label used when an accepted TelegramSettings construction rejects a
#: value: its own messages name the offending field (never the token).
_TELEGRAM_LABEL = "TelegramSettings"

_HOST_REQUIRED_FIELDS = frozenset({"name", "heartbeat", "external"})
_HOST_OPTIONAL_FIELDS = frozenset({"thresholds", "services"})
_HEARTBEAT_FIELDS = frozenset(
    {"expected_interval_seconds", "stale_after_seconds"}
)
_EXTERNAL_REQUIRED_FIELDS = frozenset({"tcp_host", "tcp_port"})
_EXTERNAL_OPTIONAL_FIELDS = frozenset(
    {"timeout_seconds", "down_confirmations", "recovery_confirmations"}
)
_THRESHOLDS_FIELDS = frozenset(
    {
        "cpu_percent",
        "ram_percent",
        "swap_percent",
        "disk_percent",
        "inode_percent",
        "load5_max",
    }
)


class CentralSettingsError(Exception):
    """A bounded central-settings loading/validation failure.

    Concise and secret-safe by construction: the message identifies at
    most the environment variable name, a non-secret field path and
    the category of validation failure — never a heartbeat token, the
    Telegram bot token, a secret JSON document or the environment
    mapping. Raw json/int/float exceptions never travel through it.
    """


@dataclass(frozen=True, slots=True)
class CentralSettings:
    """Immutable central-application settings aggregate (Stage E6).

    Exactly the already-accepted configuration objects the future
    composition root needs, and nothing application-shaped: the
    monitored-host inventory (``SentinelConfig``), the per-node
    reporter credentials (``NodeCredentials``), the Telegram delivery
    settings (``TelegramSettings``), the SQLite database path, the
    heartbeat listen endpoint and the E5 runtime intervals. The repr
    stays secret-safe through the accepted ``NodeCredentials`` and
    ``TelegramSettings`` repr contracts.
    """

    config: SentinelConfig
    credentials: NodeCredentials
    telegram: TelegramSettings
    database_path: str
    listen_host: str
    listen_port: int
    monitor_interval_seconds: float
    poll_interval_seconds: float


_T = TypeVar("_T")


def _accepted(build: Callable[[], _T], label: str) -> _T:
    """Hand construction to an accepted domain constructor, bounded.

    The accepted constructors (config.py / wire.py / telegram.py) are
    themselves secret-safe — their messages name fields and reasons,
    never token values — so their text is surfaced. Their expected
    external-input validation failures are bounded here: TypeError /
    ValueError, plus the numeric ``OverflowError`` a constructor's own
    float-domain validation raises for an external integer it cannot
    safely evaluate (for example ``math.isfinite(10**400)``) — the
    constructor stays the authority and E6 predicts nothing with a
    parallel numeric policy or cutoff. The boundary error is raised
    OUTSIDE the active handler (flag-based control flow): the domain
    exception is never chained onto it, exactly like the wire.py
    snapshot boundary. Programmer-defect exception types beyond
    these propagate unchanged.
    """
    failure: str | None = None
    try:
        return build()
    except (TypeError, ValueError, OverflowError) as error:
        failure = str(error)
    raise CentralSettingsError(f"{label}: {failure}")


class _JSONRejected(Exception):
    """Internal strict-JSON rejection raised by the decoder hooks.

    The ``category`` is static boundary text only — it never carries
    document content, so it is always safe to surface.
    """

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook: a JSON object with a duplicate key is a
    malformed document at EVERY object level (fail-closed)."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _JSONRejected("duplicate object key")
        result[key] = value
    return result


def _reject_non_finite_constant(constant: str) -> Any:
    """parse_constant hook: NaN/Infinity/-Infinity are not values."""
    raise _JSONRejected("non-finite constant")


def _load_json_document(variable: str, raw: str) -> Any:
    """Strict ``json.loads`` behind the bounded settings error.

    Rejects malformed JSON, duplicate object keys, non-finite
    constants and excessively nested documents, and never lets a raw
    parser exception (whose message can quote the secret-bearing raw
    input) escape or chain: the boundary error is raised outside the
    active handler, so ``__context__`` stays ``None``, and the
    message never echoes any part of the document.
    """
    document: Any = None
    category: str | None = None
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except _JSONRejected as rejection:
        category = rejection.category
    except ValueError:
        category = "malformed JSON"
    except RecursionError:
        category = "nesting too deep"
    if category is not None:
        raise CentralSettingsError(
            f"{variable} is not acceptable strict JSON: {category}"
        )
    return document


def _required_non_empty_string(
    env: Mapping[str, object], variable: str
) -> str:
    """Required string scalar: present, a string, and non-empty after
    a whitespace check. The value is returned verbatim — never
    stripped or normalized."""
    if variable not in env:
        raise CentralSettingsError(f"{variable} is required but missing")
    value = env[variable]
    if not isinstance(value, str):
        raise CentralSettingsError(f"{variable} must be a string")
    if not value.strip():
        raise CentralSettingsError(
            f"{variable} must be a non-empty string (not empty or"
            " whitespace-only)"
        )
    return value


def _optional_string(
    env: Mapping[str, object], variable: str
) -> str | None:
    """Optional string scalar: absent means ``None``. A present value
    is returned verbatim (an empty string is a present value, not an
    absent one — callers reject it in their own strict parse)."""
    if variable not in env:
        return None
    value = env[variable]
    if not isinstance(value, str):
        raise CentralSettingsError(f"{variable} must be a string")
    return value


def _parse_decimal_int(variable: str, raw: str) -> int:
    """Strict decimal integer parse; no liberal ``int()`` acceptance
    (no underscores, whitespace, float forms or unicode digits). A
    syntactically decimal string can still exceed the CPython
    integer-string conversion limit; that conversion failure is
    bounded here — never a raw ``int()`` ValueError, never the raw
    (possibly secret-bearing) input echoed, never the parser
    exception chained (raised outside the handler)."""
    if not _SIGNED_DECIMAL_INTEGER.match(raw):
        raise CentralSettingsError(
            f"{variable} must be a strict decimal integer string"
        )
    converted: int | None = None
    try:
        converted = int(raw)
    except ValueError:
        pass
    if converted is None:
        raise CentralSettingsError(
            f"{variable} must be a decimal integer within the supported"
            " conversion length"
        )
    return converted


def _parse_decimal_float(variable: str, raw: str) -> float:
    """Strict decimal number parse; the regex already excludes the
    ``nan``/``inf`` spellings, and overflowed literals (``1e999``)
    are rejected by the finiteness check."""
    if not _DECIMAL_NUMBER.match(raw):
        raise CentralSettingsError(
            f"{variable} must be a strict decimal number string"
        )
    value = float(raw)
    if not math.isfinite(value):
        raise CentralSettingsError(f"{variable} must be a finite number")
    return value


def _json_object(path: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CentralSettingsError(f"{path} must be a JSON object")
    return value


def _json_array(path: str, value: object) -> list[Any]:
    if not isinstance(value, list):
        raise CentralSettingsError(f"{path} must be a JSON array")
    return value


def _json_string(path: str, value: object) -> str:
    if not isinstance(value, str):
        raise CentralSettingsError(f"{path} must be a JSON string")
    return value


def _json_number(path: str, value: object) -> int | float:
    """Strict JSON number: int or float, never bool, never a numeric
    string. Exact JSON integers are preserved verbatim as integers —
    the accepted constructors consume numeric values directly, and no
    premature ``float()`` conversion may overflow or silently round
    (``9007199254740993`` and ``int(sys.float_info.max) + 1`` stay
    exact). Numeric validity beyond type and finiteness stays with
    the accepted constructors: E6 establishes no independent numeric
    range. JSON floats must be finite (the decoder hooks reject the
    NaN/Infinity constants, this rejects overflowed literals like
    ``1e999``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CentralSettingsError(f"{path} must be a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise CentralSettingsError(f"{path} must be a finite JSON number")
    return value


def _json_int(path: str, value: object) -> int:
    """Strict JSON integer: an int, never bool and never a float
    (``443.0`` is not silently coerced to ``443``)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CentralSettingsError(f"{path} must be a JSON integer")
    return value


def _require_known_keys(
    path: str,
    mapping: Mapping[str, Any],
    required: frozenset[str],
    optional: frozenset[str],
) -> None:
    """Fail-closed key check: mandatory fields present, and no
    unknown keys at this object level."""
    present = set(mapping)
    missing = sorted(required - present)
    unknown = sorted(present - required - optional)
    if missing:
        raise CentralSettingsError(
            f"{path} is missing mandatory field(s): {', '.join(missing)}"
        )
    if unknown:
        raise CentralSettingsError(
            f"{path} contains unknown field(s): {', '.join(unknown)}"
        )


def _parse_heartbeat(path: str, value: object) -> HeartbeatSettings:
    mapping = _json_object(path, value)
    _require_known_keys(path, mapping, _HEARTBEAT_FIELDS, frozenset())
    expected = _json_number(
        f"{path}.expected_interval_seconds",
        mapping["expected_interval_seconds"],
    )
    stale_after = _json_number(
        f"{path}.stale_after_seconds", mapping["stale_after_seconds"]
    )
    return _accepted(
        lambda: HeartbeatSettings(
            expected_interval_seconds=expected,
            stale_after_seconds=stale_after,
        ),
        path,
    )


def _parse_external(path: str, value: object) -> ExternalCheckSettings:
    mapping = _json_object(path, value)
    _require_known_keys(
        path, mapping, _EXTERNAL_REQUIRED_FIELDS, _EXTERNAL_OPTIONAL_FIELDS
    )
    fields: dict[str, Any] = {
        "tcp_host": _json_string(f"{path}.tcp_host", mapping["tcp_host"]),
        "tcp_port": _json_int(f"{path}.tcp_port", mapping["tcp_port"]),
    }
    if "timeout_seconds" in mapping:
        fields["timeout_seconds"] = _json_number(
            f"{path}.timeout_seconds", mapping["timeout_seconds"]
        )
    if "down_confirmations" in mapping:
        fields["down_confirmations"] = _json_int(
            f"{path}.down_confirmations", mapping["down_confirmations"]
        )
    if "recovery_confirmations" in mapping:
        fields["recovery_confirmations"] = _json_int(
            f"{path}.recovery_confirmations",
            mapping["recovery_confirmations"],
        )
    return _accepted(lambda: ExternalCheckSettings(**fields), path)


def _parse_thresholds(path: str, value: object) -> Thresholds:
    mapping = _json_object(path, value)
    # Every threshold field is optional (missing fields keep the
    # accepted Thresholds defaults); only unknown keys are rejected.
    _require_known_keys(path, mapping, frozenset(), _THRESHOLDS_FIELDS)
    fields: dict[str, Any] = {}
    for name in (
        "cpu_percent",
        "ram_percent",
        "swap_percent",
        "disk_percent",
        "inode_percent",
    ):
        if name in mapping:
            fields[name] = _json_number(f"{path}.{name}", mapping[name])
    if "load5_max" in mapping:
        raw_load = mapping["load5_max"]
        fields["load5_max"] = (
            None
            if raw_load is None
            else _json_number(f"{path}.load5_max", raw_load)
        )
    return _accepted(lambda: Thresholds(**fields), path)


def _parse_services(path: str, value: object) -> tuple[str, ...]:
    items = _json_array(path, value)
    return tuple(
        _json_string(f"{path}[{index}]", item)
        for index, item in enumerate(items)
    )


def _parse_host(path: str, value: object) -> HostConfig:
    mapping = _json_object(path, value)
    _require_known_keys(
        path, mapping, _HOST_REQUIRED_FIELDS, _HOST_OPTIONAL_FIELDS
    )
    name = _json_string(f"{path}.name", mapping["name"])
    heartbeat = _parse_heartbeat(f"{path}.heartbeat", mapping["heartbeat"])
    external = _parse_external(f"{path}.external", mapping["external"])
    thresholds = (
        _parse_thresholds(f"{path}.thresholds", mapping["thresholds"])
        if "thresholds" in mapping
        else Thresholds()
    )
    services = (
        _parse_services(f"{path}.services", mapping["services"])
        if "services" in mapping
        else ()
    )
    return _accepted(
        lambda: HostConfig(
            name=name,
            heartbeat=heartbeat,
            external=external,
            thresholds=thresholds,
            services=services,
        ),
        path,
    )


def _load_config(env: Mapping[str, object]) -> SentinelConfig:
    raw = _required_non_empty_string(env, _HOSTS_JSON_VAR)
    document = _load_json_document(_HOSTS_JSON_VAR, raw)
    entries = _json_array(_HOSTS_JSON_VAR, document)
    hosts = tuple(
        _parse_host(f"{_HOSTS_JSON_VAR}[{index}]", entry)
        for index, entry in enumerate(entries)
    )
    return _accepted(lambda: SentinelConfig(hosts=hosts), _HOSTS_JSON_VAR)


def _load_credentials(
    env: Mapping[str, object], config: SentinelConfig
) -> NodeCredentials:
    raw = _required_non_empty_string(env, _NODE_TOKENS_JSON_VAR)
    document = _load_json_document(_NODE_TOKENS_JSON_VAR, raw)
    mapping = _json_object(_NODE_TOKENS_JSON_VAR, document)
    for token in mapping.values():
        if not isinstance(token, str):
            raise CentralSettingsError(
                f"{_NODE_TOKENS_JSON_VAR} values must all be strings"
                " (node name -> secret token)"
            )
    configured = {host.name for host in config.hosts}
    credential_nodes = set(mapping)
    missing = sorted(configured - credential_nodes)
    unknown = sorted(credential_nodes - configured)
    if missing or unknown:
        details = []
        if missing:
            details.append(
                "no credential for configured host(s): " + ", ".join(missing)
            )
        if unknown:
            details.append(
                "credential for unknown host(s): " + ", ".join(unknown)
            )
        raise CentralSettingsError(
            f"{_NODE_TOKENS_JSON_VAR} credential node names must match"
            f" the configured host names exactly ({'; '.join(details)})"
        )
    return _accepted(lambda: NodeCredentials(mapping), _NODE_TOKENS_JSON_VAR)


def _load_listen_port(env: Mapping[str, object]) -> int:
    raw = _required_non_empty_string(env, _LISTEN_PORT_VAR)
    port = _parse_decimal_int(_LISTEN_PORT_VAR, raw)
    if not 1 <= port <= 65535:
        raise CentralSettingsError(
            f"{_LISTEN_PORT_VAR} must be within [1, 65535], got {port}"
        )
    return port


def _load_positive_interval(env: Mapping[str, object], variable: str) -> float:
    """E5's fail-fast interval semantics: finite and strictly > 0."""
    raw = _required_non_empty_string(env, variable)
    value = _parse_decimal_float(variable, raw)
    if value <= 0:
        raise CentralSettingsError(
            f"{variable} must be a finite positive number"
        )
    return value


def _load_telegram(env: Mapping[str, object]) -> TelegramSettings:
    bot_token = _required_non_empty_string(env, _BOT_TOKEN_VAR)
    chat_raw = _required_non_empty_string(env, _CHAT_ID_VAR)
    chat_id = _parse_decimal_int(_CHAT_ID_VAR, chat_raw)

    thread_raw = _optional_string(env, _THREAD_ID_VAR)
    thread_id: int | None = None
    if thread_raw is not None:
        thread_id = _parse_decimal_int(_THREAD_ID_VAR, thread_raw)

    timeout: float | None = None
    timeout_raw = _optional_string(env, _TIMEOUT_VAR)
    if timeout_raw is not None:
        timeout = _parse_decimal_float(_TIMEOUT_VAR, timeout_raw)
        if timeout <= 0:
            raise CentralSettingsError(
                f"{_TIMEOUT_VAR} must be a finite positive number"
            )

    if timeout is None:
        return _accepted(
            lambda: TelegramSettings(
                bot_token=bot_token,
                chat_id=chat_id,
                message_thread_id=thread_id,
            ),
            _TELEGRAM_LABEL,
        )
    return _accepted(
        lambda: TelegramSettings(
            bot_token=bot_token,
            chat_id=chat_id,
            message_thread_id=thread_id,
            timeout_seconds=timeout,
        ),
        _TELEGRAM_LABEL,
    )


def load_central_settings(env: Mapping[str, str]) -> CentralSettings:
    """Load and validate the central Sentinel settings from ``env``.

    The caller supplies the environment mapping explicitly — this
    function never reads ``os.environ`` implicitly. The mapping is
    snapshotted into a plain ``dict`` exactly once before parsing, so
    one load uses one stable input view (a mapping that cannot be read
    deterministically is itself a bounded settings failure). Unrelated
    keys are ignored. Every malformed or missing setting fails through
    the bounded :class:`CentralSettingsError` with a secret-safe
    message; nothing is constructed beyond the accepted configuration
    objects returned inside the immutable :class:`CentralSettings`.
    """
    snapshot: dict[str, Any] = {}
    snapshot_failed = False
    try:
        snapshot = dict(env)
    except Exception:
        # Flag-based control flow (wire.py snapshot precedent): the
        # sanitized boundary error must be raised outside the active
        # handler, and the hostile exception object is never retained.
        snapshot_failed = True
    if snapshot_failed:
        raise CentralSettingsError(
            "environment mapping must be deterministically readable"
        )

    database_path = _required_non_empty_string(snapshot, _DATABASE_PATH_VAR)
    listen_host = _required_non_empty_string(snapshot, _LISTEN_HOST_VAR)
    listen_port = _load_listen_port(snapshot)
    monitor_interval_seconds = _load_positive_interval(
        snapshot, _MONITOR_INTERVAL_VAR
    )
    poll_interval_seconds = _load_positive_interval(
        snapshot, _POLL_INTERVAL_VAR
    )
    config = _load_config(snapshot)
    telegram = _load_telegram(snapshot)
    credentials = _load_credentials(snapshot, config)
    return CentralSettings(
        config=config,
        credentials=credentials,
        telegram=telegram,
        database_path=database_path,
        listen_host=listen_host,
        listen_port=listen_port,
        monitor_interval_seconds=monitor_interval_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
