"""Strict dead-man runtime environment configuration (Stage H1B-2).

The bounded typed loader that turns ONE caller-supplied environment
mapping into exactly the already-accepted H1A/H1B-1 configuration
objects the external dead-man oneshot runtime needs — and nothing
else:

    load_deadman_settings(env) -> DeadManRuntimeSettings
                    |
    one stable plain-dict snapshot of the supplied mapping
                    |
    strict, fail-closed boundary parsing
                    |
    construction of the accepted H1B-1 settings objects
                    |
    DeadManRuntimeSettings(
        probe (DeadManProbeSettings), state_path, store
        (DeadManStateStore), telegram (DeadManTelegramSettings))

H1B-2 owns ONLY typed loading/validation of the dead-man runtime
configuration. It constructs no prober, no Telegram sender, runs no
cycle and starts nothing. It never reads ``os.environ`` implicitly:
the caller supplies the environment mapping explicitly (the process
entrypoint may pass ``os.environ``), and the mapping is snapshotted
into a plain ``dict`` before parsing so one load uses one stable
input view.

The accepted H1B-1 constructors remain the sole semantic authorities
for their own invariants: ``DeadManProbeSettings``
(deadman_probe.py), ``DeadManStateStore`` (deadman_store.py) and
``DeadManTelegramSettings`` (deadman_telegram.py). This loader
performs only the strict boundary parse and then hands construction
to them; their business semantics are never duplicated here.

Contract points (see docs/ARCHITECTURE.md section 33):

- required variables: ``HERMES_SENTINEL_DEADMAN_PROBE_URL``,
  ``HERMES_SENTINEL_DEADMAN_STATE_PATH``,
  ``HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN``,
  ``HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID``; optional:
  ``HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID``,
  ``HERMES_SENTINEL_DEADMAN_PROBE_TIMEOUT_SECONDS`` and
  ``HERMES_SENTINEL_DEADMAN_TELEGRAM_TIMEOUT_SECONDS`` (absent
  preserves the accepted H1B-1 defaults). Unrelated environment keys
  are ignored; no aliases or legacy variable names exist;
- missing required values fail closed and empty required values
  (empty or whitespace-only) are rejected;
- the chat id and the optional thread id are strict decimal integer
  strings — no float forms, no underscores, no surrounding
  whitespace, no bool-style spellings — and the timeouts are strict
  decimal number strings parsed to finite, strictly positive floats;
  the accepted constructors stay authoritative for their own
  semantic ranges (non-zero chat id, positive thread id, token
  alphabet, HTTPS URL shape);
- the state-file path is a required non-empty string kept verbatim;
  the loader performs NO filesystem access — no directory or file is
  ever created (parent-directory provisioning belongs to the
  operator deployment, never to the runtime);
- the bot token is a secret: it never appears in the
  :class:`DeadManRuntimeSettings` repr (the accepted
  ``DeadManTelegramSettings`` repr contract), never in any error
  text, and raw environment mappings are never echoed in errors;
- every external malformed/missing-settings failure raises the
  bounded :class:`DeadManSettingsError`; raw int/float and Path
  exceptions never leak, and no raw constructor exception stays
  chained onto a boundary error (flag-based control flow, the E6
  precedent).

Out of scope for H1B-2 configuration: building the prober/sender
adapters, running a cycle, the process entrypoint, systemd
packaging, deployment and H2 live acceptance.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from hermes_sentinel.deadman_probe import DeadManProbeSettings
from hermes_sentinel.deadman_store import DeadManStateStore
from hermes_sentinel.deadman_telegram import DeadManTelegramSettings

__all__ = [
    "DeadManRuntimeSettings",
    "DeadManSettingsError",
    "load_deadman_settings",
]

_PROBE_URL_VAR = "HERMES_SENTINEL_DEADMAN_PROBE_URL"
_STATE_PATH_VAR = "HERMES_SENTINEL_DEADMAN_STATE_PATH"
_BOT_TOKEN_VAR = "HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN"
_CHAT_ID_VAR = "HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID"
_THREAD_ID_VAR = "HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID"
_PROBE_TIMEOUT_VAR = "HERMES_SENTINEL_DEADMAN_PROBE_TIMEOUT_SECONDS"
_TELEGRAM_TIMEOUT_VAR = (
    "HERMES_SENTINEL_DEADMAN_TELEGRAM_TIMEOUT_SECONDS"
)

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

#: Label used when an accepted H1B-1 constructor rejects a value: its
#: own messages name the offending field (never the token).
_PROBE_LABEL = "DeadManProbeSettings"
_TELEGRAM_LABEL = "DeadManTelegramSettings"
_STORE_LABEL = "DeadManStateStore"


class DeadManSettingsError(Exception):
    """A bounded dead-man settings loading/validation failure.

    Concise and secret-safe by construction: the message identifies at
    most the environment variable name and the category of validation
    failure — never the Telegram bot token, a raw parsed value that
    could be a secret, or the environment mapping. Raw int/float/Path
    exceptions never travel through it.
    """


@dataclass(frozen=True, slots=True)
class DeadManRuntimeSettings:
    """Immutable dead-man runtime settings aggregate (Stage H1B-2).

    Exactly the already-accepted H1A/H1B-1 configuration objects the
    oneshot runtime needs, and nothing runtime-shaped: the HTTPS probe
    settings (``DeadManProbeSettings``), the state-file path, the
    constructed state store (``DeadManStateStore``) and the delivery
    settings (``DeadManTelegramSettings``). The repr stays secret-safe
    through the accepted ``DeadManTelegramSettings`` repr contract
    (the bot token is excluded there); the store carries no repr facts
    beyond its absence.
    """

    probe: DeadManProbeSettings
    state_path: Path
    store: DeadManStateStore = field(repr=False)
    telegram: DeadManTelegramSettings


_T = TypeVar("_T")


def _accepted(build: Callable[[], _T], label: str) -> _T:
    """Hand construction to an accepted H1B-1 constructor, bounded.

    The accepted constructors are themselves secret-safe — their
    messages name fields and reasons, never token values — so their
    text is surfaced. Their expected external-input validation
    failures are bounded here; the boundary error is raised OUTSIDE
    the active handler (flag-based control flow, the E6 precedent):
    the domain exception is never chained onto it. Programmer-defect
    exception types beyond these propagate unchanged.
    """
    failure: str | None = None
    try:
        return build()
    except (TypeError, ValueError, OverflowError) as error:
        failure = str(error)
    raise DeadManSettingsError(f"{label}: {failure}")


def _required_non_empty_string(
    env: Mapping[str, object], variable: str
) -> str:
    """Required string scalar: present, a string, and non-empty after
    a whitespace check. The value is returned verbatim — never
    stripped or normalized."""
    if variable not in env:
        raise DeadManSettingsError(f"{variable} is required but missing")
    value = env[variable]
    if not isinstance(value, str):
        raise DeadManSettingsError(f"{variable} must be a string")
    if not value.strip():
        raise DeadManSettingsError(
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
        raise DeadManSettingsError(f"{variable} must be a string")
    return value


def _parse_decimal_int(variable: str, raw: str) -> int:
    """Strict decimal integer parse; no liberal ``int()`` acceptance
    (no underscores, whitespace, float forms or unicode digits), and
    never the raw (possibly secret-bearing) input echoed or the
    parser exception chained (raised outside the handler)."""
    if not _SIGNED_DECIMAL_INTEGER.match(raw):
        raise DeadManSettingsError(
            f"{variable} must be a strict decimal integer string"
        )
    converted: int | None = None
    try:
        converted = int(raw)
    except ValueError:
        pass
    if converted is None:
        raise DeadManSettingsError(
            f"{variable} must be a decimal integer within the supported"
            " conversion length"
        )
    return converted


def _parse_positive_float(variable: str, raw: str) -> float:
    """Strict decimal number parse for a bounded timeout; the regex
    already excludes the ``nan``/``inf`` spellings, overflowed
    literals (``1e999``) are rejected by the finiteness check, and
    only a strictly positive finite value is accepted."""
    if not _DECIMAL_NUMBER.match(raw):
        raise DeadManSettingsError(
            f"{variable} must be a strict decimal number string"
        )
    value = float(raw)
    if not math.isfinite(value):
        raise DeadManSettingsError(f"{variable} must be a finite number")
    if value <= 0:
        raise DeadManSettingsError(
            f"{variable} must be a finite positive number"
        )
    return value


def _load_probe_settings(env: Mapping[str, object]) -> DeadManProbeSettings:
    url = _required_non_empty_string(env, _PROBE_URL_VAR)
    timeout_raw = _optional_string(env, _PROBE_TIMEOUT_VAR)
    if timeout_raw is None:
        return _accepted(
            lambda: DeadManProbeSettings(url=url), _PROBE_LABEL
        )
    timeout = _parse_positive_float(_PROBE_TIMEOUT_VAR, timeout_raw)
    return _accepted(
        lambda: DeadManProbeSettings(url=url, timeout_seconds=timeout),
        _PROBE_LABEL,
    )


def _load_store(env: Mapping[str, object]) -> tuple[Path, DeadManStateStore]:
    raw = _required_non_empty_string(env, _STATE_PATH_VAR)
    path: Path = _accepted(lambda: Path(raw), _STORE_LABEL)
    store = _accepted(lambda: DeadManStateStore(path), _STORE_LABEL)
    return path, store


def _load_telegram_settings(
    env: Mapping[str, object],
) -> DeadManTelegramSettings:
    bot_token = _required_non_empty_string(env, _BOT_TOKEN_VAR)
    chat_id = _parse_decimal_int(
        _CHAT_ID_VAR, _required_non_empty_string(env, _CHAT_ID_VAR)
    )

    thread_id: int | None = None
    thread_raw = _optional_string(env, _THREAD_ID_VAR)
    if thread_raw is not None:
        thread_id = _parse_decimal_int(_THREAD_ID_VAR, thread_raw)

    fields: dict[str, Any] = {
        "bot_token": bot_token,
        "chat_id": chat_id,
        "message_thread_id": thread_id,
    }
    timeout_raw = _optional_string(env, _TELEGRAM_TIMEOUT_VAR)
    if timeout_raw is not None:
        fields["timeout_seconds"] = _parse_positive_float(
            _TELEGRAM_TIMEOUT_VAR, timeout_raw
        )
    return _accepted(
        lambda: DeadManTelegramSettings(**fields), _TELEGRAM_LABEL
    )


def load_deadman_settings(
    env: Mapping[str, str]
) -> DeadManRuntimeSettings:
    """Load and validate the dead-man runtime settings from ``env``.

    The caller supplies the environment mapping explicitly — this
    function never reads ``os.environ`` implicitly. The mapping is
    snapshotted into a plain ``dict`` exactly once before parsing, so
    one load uses one stable input view (a mapping that cannot be read
    deterministically is itself a bounded settings failure). Unrelated
    keys are ignored. Every malformed or missing setting fails through
    the bounded :class:`DeadManSettingsError` with a secret-safe
    message; nothing is constructed beyond the accepted configuration
    objects returned inside the immutable
    :class:`DeadManRuntimeSettings`.
    """
    snapshot: dict[str, Any] = {}
    snapshot_failed = False
    try:
        snapshot = dict(env)
    except Exception:
        # Flag-based control flow (the E6 snapshot precedent): the
        # sanitized boundary error must be raised outside the active
        # handler, and the hostile exception object is never retained.
        snapshot_failed = True
    if snapshot_failed:
        raise DeadManSettingsError(
            "environment mapping must be deterministically readable"
        )

    probe = _load_probe_settings(snapshot)
    state_path, store = _load_store(snapshot)
    telegram = _load_telegram_settings(snapshot)
    return DeadManRuntimeSettings(
        probe=probe,
        state_path=state_path,
        store=store,
        telegram=telegram,
    )
