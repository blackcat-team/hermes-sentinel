"""Dead-man oneshot process / CLI entrypoint (Stage H1B-2).

The minimal process boundary that connects the accepted layers into
ONE launchable external dead-man oneshot:

    main() -> run_deadman_process(os.environ)
                    |
    H1B-2 load_deadman_settings(env)  (exactly once, live mapping)
                    |
    build DeadManProber / DeadManTelegramSender / utc_now
                    |
    H1B-2 run_deadman_cycle(...)      (exactly ONE cycle)
                    |
    minimal secret-safe stdout line, exit 0
    (bounded failure category on stderr, non-zero exit)

This module owns ONLY the process boundary: the single implicit
environment read in ``main``, adapter construction and the bounded
exit-code mapping. It is a ONESHOT: no daemon loop, no sleep, no
inbound listener, no retry, no signal-management framework (a short
oneshot needs none) and no second cycle. Run CADENCE is owned by the
systemd timer packaging, never by this process.

Contract points (see docs/ARCHITECTURE.md section 33):

- **Minimal public surface**: ``run_deadman_process(env)`` is the
  testable process boundary — it receives the environment mapping
  EXPLICITLY and never reads ``os.environ`` itself; ``main()`` is the
  one production place that passes the live ``os.environ`` through
  (which hands it to the settings loader unmodified). The prober,
  sender and clock seams are injectable for deterministic tests; the
  production values are the accepted H1B-1 adapters and the
  ``utc_now`` boundary;
- **Exit semantics**: a normal completed cycle returns 0 — including
  a cycle whose observation was a target DOWN with successful state
  processing (an unreachable target is dead-man input, not a process
  failure). Bounded operational failures return 1 with one concise
  secret-safe diagnostic line on stderr: invalid configuration
  (``DeadManSettingsError``), state-store read/decode/write failure
  (``DeadManStateStoreError``), Telegram delivery failure
  (``DeadManTelegramDeliveryError``), a defective runtime boundary
  (``DeadManRuntimeError``), an unexpected PRODUCTION ADAPTER
  construction failure (a dedicated bounded setup category, raised
  before the cycle begins) and any unexpected failure during the
  cycle — the two unexpected categories carry a generic category plus
  the exception TYPE name only, never raw exception text, which could
  carry an authenticated URL or another secret-bearing message; no
  raw traceback ever reaches the CLI;
- **Secret safety**: stdout/stderr never contain the Telegram bot
  token, an authenticated Telegram request URL, a response body, the
  raw environment mapping or the persisted document contents. The
  success line carries exactly the cycle facts: completion, the
  dead-man state and the pending count. No structured logging
  framework exists here;
- **Unchanged neighbors**: the central Sentinel process
  (``hermes_sentinel.process``) is untouched — this is a separate
  entrypoint for the external dead-man host.

Out of scope for H1B-2: the runtime cycle itself (deadman_runtime),
settings semantics (deadman_config), systemd packaging, deployment
and H2 live acceptance.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime

from hermes_sentinel.deadman_config import (
    DeadManSettingsError,
    load_deadman_settings,
)
from hermes_sentinel.deadman_probe import DeadManProber
from hermes_sentinel.deadman_runtime import (
    DeadManRuntimeError,
    run_deadman_cycle,
    utc_now,
)
from hermes_sentinel.deadman_store import DeadManStateStoreError
from hermes_sentinel.deadman_telegram import (
    DeadManTelegramDeliveryError,
    DeadManTelegramSender,
)

__all__ = [
    "main",
    "run_deadman_process",
]


def run_deadman_process(
    env: Mapping[str, str],
    *,
    prober: DeadManProber | None = None,
    sender: DeadManTelegramSender | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Run exactly ONE dead-man cycle against ``env`` and return the
    process exit code.

    Loads the settings through the H1B-2 loader exactly once (the
    exact mapping object supplied — never copied or re-read here),
    builds the production adapters from the accepted settings unless
    deterministic doubles are injected (construction itself inside the
    bounded secret-safe setup boundary — an unexpected constructor
    failure is a bounded non-zero exit, never a raw traceback), runs
    exactly one ``run_deadman_cycle`` and maps its outcome to the
    process exit code: 0 for a normal completed cycle (target DOWN
    included), 1 with one bounded secret-safe stderr line for every
    bounded operational failure category.
    """
    try:
        settings = load_deadman_settings(env)
    except DeadManSettingsError as error:
        print(
            f"hermes-sentinel-deadman: configuration error: {error}",
            file=sys.stderr,
        )
        return 1
    store = settings.store
    # Production adapter construction stays INSIDE the bounded
    # secret-safe process boundary: an unexpected constructor failure
    # (the injected deterministic doubles are constructed by the
    # caller and never take this path) is a bounded SETUP failure —
    # a generic category plus the exception TYPE name only, never the
    # raw exception text or traceback, which could carry a secret-
    # bearing message out through the CLI. Chaining is moot: the
    # original exception is swallowed here and never re-raised.
    try:
        effective_prober = (
            prober
            if prober is not None
            else DeadManProber(settings.probe)
        )
        effective_sender = (
            sender
            if sender is not None
            else DeadManTelegramSender(settings.telegram)
        )
    except Exception as error:
        print(
            "hermes-sentinel-deadman: dead-man runtime setup failed:"
            f" {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    effective_clock = clock if clock is not None else utc_now
    try:
        result = run_deadman_cycle(
            store=store,
            prober=effective_prober,
            sender=effective_sender,
            clock=effective_clock,
        )
    except DeadManStateStoreError as error:
        print(
            f"hermes-sentinel-deadman: state store failure: {error}",
            file=sys.stderr,
        )
        return 1
    except DeadManTelegramDeliveryError as error:
        print(
            f"hermes-sentinel-deadman: {error}",
            file=sys.stderr,
        )
        return 1
    except DeadManRuntimeError as error:
        print(
            f"hermes-sentinel-deadman: runtime failure: {error}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        # Unexpected adapter/runtime setup failure: a bounded category
        # plus the exception TYPE name only — raw exception text can
        # carry an authenticated URL and is never echoed.
        print(
            "hermes-sentinel-deadman: unexpected runtime failure:"
            f" {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(
        "dead-man cycle completed:"
        f" state={result.state.value}"
        f" pending={result.pending_count}"
    )
    return 0


def main() -> int:
    """The console entrypoint: pass the live process environment
    through.

    The ONLY place in dead-man production code where the process
    environment is implicitly read: the live ``os.environ`` object
    itself is handed to ``run_deadman_process`` (and through it to
    the settings loader, which snapshots and validates it) — never
    copied, parsed or filtered here. The returned int is the process
    exit code.
    """
    return run_deadman_process(os.environ)
