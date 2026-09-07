"""Authenticated heartbeat wire contract (Stage B3).

Minimal transport-neutral wire/auth boundary in front of the Stage B2
ingestion core:

    future HTTP POST (B4)
                |
    AuthenticatedHeartbeatAdapter (this module)
                |
    HostTelemetry (Stage A domain contract)
                |
    HeartbeatIngestor (Stage B2)
                |
    HeartbeatRepository (Stage B1) -> SQLite

B3 is deliberately not an HTTP/API transport: the adapter accepts an
already decoded ``Mapping[str, object]`` plus a separately presented
node token. JSON bytes/string parsing, Content-Type semantics, header
parsing and the server lifecycle belong to Stage B4.

Normative wire/auth semantics (see docs/ARCHITECTURE.md):

- the wire payload carries exactly the mandatory telemetry MVP fields
  (``node``, ``reported_at``, ``uptime_seconds``, ``load``,
  ``cpu_percent``, ``ram``, ``swap``, ``root_fs``, ``root_inodes``)
  and nothing else: no ``received_at``, no health state, no token.
  The central ``received_at`` stays the exclusive responsibility of
  the B2 ingestion clock;
- decoding is strict and fail-closed: non-mapping payloads, missing
  or unknown (extra) fields anywhere, wrong scalar types, numeric
  strings, bools-as-numbers, malformed or naive ``reported_at`` and
  invalid/whitespace-only ``node`` are rejected before ingestion.
  Values are never silently coerced; Stage A domain invariants are
  reused (not duplicated) by decoding into the typed domain models;
- the external payload mapping is read exactly once through a
  defensive plain-dict snapshot: claimed node, authentication and
  the full strict decode all operate on that same stable snapshot,
  so a hostile mutable mapping cannot change the authenticated node
  identity between the token check and the decode (no node/token
  TOCTOU). A mapping that cannot be snapshotted deterministically is
  a malformed payload — no clock call, no write;
- every monitored node has its own secret token. Authentication
  verifies the presented token against the credential of the *claimed
  node* only — never against "any known token" — using the stdlib
  constant-time comparison ``hmac.compare_digest`` over a total
  ``str`` encoding (UTF-8 with ``surrogatepass``): arbitrary ``str``
  secrets, including lone surrogates, compare exactly and
  deterministically, and ``UnicodeEncodeError`` can never escape the
  auth boundary. Node A can never authenticate as Node B;
- authentication is fail-closed: unknown nodes, configured nodes
  without a credential, wrong tokens and empty/whitespace-only
  presented tokens all fail with the same generic external
  ``HeartbeatAuthenticationError`` (no identity enumeration, no
  secret disclosure). The B2 ``UnknownNodeError`` remains an
  internal contract;
- secret tokens are never logged, persisted, included in receipts,
  or embedded in exception messages;
- a credential set rejects a duplicate token assignment to two
  different nodes at construction time (frozen "own identity/token
  per node" invariant);
- repository/B2 failures after a successful authentication propagate
  unchanged — never masked as a successful receipt.

Out of scope for B3: HTTP server/listener, headers, JSON bytes
parsing, TLS, credential file/env loaders, token rotation, rate
limiting, replay protection, clock-skew policy, deduplication,
heartbeat freshness and any HEALTHY/DEGRADED/DOWN computation.
"""

from __future__ import annotations

import hmac
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from hermes_sentinel.config import SentinelConfig
from hermes_sentinel.domain import (
    HostTelemetry,
    LoadAverage,
    ResourceUsage,
)
from hermes_sentinel.ingestion import HeartbeatIngestor, HeartbeatReceipt

__all__ = [
    "HeartbeatWireError",
    "MalformedHeartbeatPayloadError",
    "HeartbeatAuthenticationError",
    "DuplicateNodeTokenError",
    "NodeCredentials",
    "decode_heartbeat_payload",
    "AuthenticatedHeartbeatAdapter",
]


#: One generic message for every external authentication failure:
#: it must not reveal whether the claimed node exists, which token
#: was expected, or any other node identity/secret.
_AUTHENTICATION_FAILED = "heartbeat authentication failed"


class HeartbeatWireError(Exception):
    """Base class for deterministic external wire/auth failures."""


class MalformedHeartbeatPayloadError(HeartbeatWireError):
    """The external payload violates the strict wire contract.

    Raised before authentication-side effects on the ingestion path
    (only the claimed node is inspected first) and always before any
    write: no observation row is created. Messages name fields and
    reasons, never secret values (the payload contains none).
    """


class HeartbeatAuthenticationError(HeartbeatWireError):
    """The presented token does not authenticate the claimed node.

    One generic fail-closed error for every cause (unknown node,
    missing credential, wrong/empty/whitespace-only token): the
    message never discloses node existence or token material.
    """


class DuplicateNodeTokenError(ValueError):
    """Two different nodes were assigned the same secret token.

    Raised at credential-set construction: each configured node must
    have its own token (frozen per-node identity invariant). The
    message names the conflicting nodes, never the token value.
    """


class NodeCredentials:
    """Runtime per-node secret token mapping (node -> its own token).

    A minimal in-memory credential configuration. Deliberately not
    tied to SQLite heartbeat persistence, and deliberately without
    any file/env loading machinery (deployment loaders are out of
    scope for B3).

    Construction is fail-closed:

    - node names must be non-empty strings after ``strip()`` — the
      same identity rule the persistence boundary enforces. Names are
      kept verbatim (no normalization, no case folding);
    - tokens must be non-empty, non-whitespace-only strings. Tokens
      are compared verbatim later — never stripped or normalized;
    - the same token assigned to two different nodes is rejected
      (:class:`DuplicateNodeTokenError`).

    Secret safety: ``repr()`` exposes node names only, never token
    values.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not isinstance(tokens, Mapping):
            raise TypeError(
                "tokens must be a mapping of node name to secret token"
            )
        validated: dict[str, str] = {}
        owner: dict[str, str] = {}
        for node, token in tokens.items():
            if not isinstance(node, str) or not node.strip():
                raise ValueError(
                    "credential node name must be a non-empty string"
                    " after strip()"
                )
            if not isinstance(token, str) or not token.strip():
                raise ValueError(
                    f"credential token for node {node!r} must be a"
                    " non-empty, non-whitespace-only string"
                )
            if token in owner:
                raise DuplicateNodeTokenError(
                    "duplicate secret token assigned to nodes"
                    f" {owner[token]!r} and {node!r}: each configured"
                    " node must have its own token"
                )
            owner[token] = node
            validated[node] = token
        self._tokens: dict[str, str] = validated

    def token_for(self, node: str) -> str | None:
        """Return the expected secret token of ``node``, if any."""
        return self._tokens.get(node)

    def __repr__(self) -> str:
        # Secret safety: node names only, never token values.
        return f"{type(self).__name__}(nodes={sorted(self._tokens)!r})"


# --- strict wire decoding ------------------------------------------------

#: Exactly the mandatory telemetry MVP fields — nothing else.
#: ``received_at`` (central B2 clock), health state, token and
#: incident data are deliberately absent from the wire contract.
_TOP_LEVEL_FIELDS = frozenset(
    {
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
)
_LOAD_FIELDS = frozenset({"one", "five", "fifteen"})
_RESOURCE_FIELDS = frozenset({"used", "total", "percent"})


def _require_mapping(name: str, value: object) -> Mapping[Any, Any]:
    if not isinstance(value, Mapping):
        raise MalformedHeartbeatPayloadError(f"{name} must be a JSON object")
    return value


def _require_exact_keys(
    name: str, mapping: Mapping[Any, Any], required: frozenset[str]
) -> None:
    """Unknown/extra fields are rejected fail-closed (B3 policy).

    The wire contract is small and versioning is not introduced yet,
    so both missing mandatory fields and any extra field — top-level
    or nested — are hard failures.
    """
    present = set(mapping.keys())
    missing = required - present
    unexpected = present - required
    if missing:
        raise MalformedHeartbeatPayloadError(
            f"{name} is missing mandatory field(s):"
            f" {', '.join(sorted(map(str, missing)))}"
        )
    if unexpected:
        raise MalformedHeartbeatPayloadError(
            f"{name} contains unknown field(s):"
            f" {', '.join(sorted(map(str, unexpected)))}"
        )


def _require_node(name: str, value: object) -> str:
    """Node identity: non-empty string after strip(), kept verbatim.

    Same identity semantics as Stage A/B1/B2: no normalization, no
    case folding ("Prod" and "prod" stay distinct).
    """
    if not isinstance(value, str):
        raise MalformedHeartbeatPayloadError(f"{name} must be a string")
    if not value.strip():
        raise MalformedHeartbeatPayloadError(
            f"{name} must be a non-empty string after strip()"
        )
    return value


def _require_number(name: str, value: object) -> float:
    """Strict JSON number: int or float, never bool, never a string.

    ``bool`` is a subclass of ``int`` in Python but is not a JSON
    number, so it is explicitly rejected. No silent coercions like
    ``"12.5" -> 12.5``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedHeartbeatPayloadError(f"{name} must be a JSON number")
    try:
        result = float(value)
    except OverflowError:
        raise MalformedHeartbeatPayloadError(
            f"{name} must be a finite JSON number"
        ) from None
    if not math.isfinite(result):
        raise MalformedHeartbeatPayloadError(
            f"{name} must be a finite JSON number"
        )
    return result


def _require_reported_at(name: str, value: object) -> datetime:
    """Strict ISO 8601 ``reported_at``: valid and truly aware.

    Naive values are rejected; effectively-naive values (``tzinfo``
    set but ``utcoffset()`` None) are rejected too, matching the B1
    full-awareness rule (``datetime.fromisoformat`` cannot produce
    them today, but the check is fail-closed regardless).
    """
    if not isinstance(value, str):
        raise MalformedHeartbeatPayloadError(
            f"{name} must be an ISO 8601 datetime string"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MalformedHeartbeatPayloadError(
            f"{name} must be a valid ISO 8601 datetime"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MalformedHeartbeatPayloadError(
            f"{name} must be timezone-aware"
        )
    return parsed


def _decode_load(name: str, value: object) -> LoadAverage:
    mapping = _require_mapping(name, value)
    _require_exact_keys(name, mapping, _LOAD_FIELDS)
    return LoadAverage(
        one=_require_number(f"{name}.one", mapping["one"]),
        five=_require_number(f"{name}.five", mapping["five"]),
        fifteen=_require_number(f"{name}.fifteen", mapping["fifteen"]),
    )


def _decode_resource(name: str, value: object) -> ResourceUsage:
    mapping = _require_mapping(name, value)
    _require_exact_keys(name, mapping, _RESOURCE_FIELDS)
    return ResourceUsage(
        used=_require_number(f"{name}.used", mapping["used"]),
        total=_require_number(f"{name}.total", mapping["total"]),
        percent=_require_number(f"{name}.percent", mapping["percent"]),
    )


def decode_heartbeat_payload(payload: Mapping[str, object]) -> HostTelemetry:
    """Strictly decode an external heartbeat payload into HostTelemetry.

    Fail-closed: any structural or scalar violation raises
    :class:`MalformedHeartbeatPayloadError` and nothing is ingested.
    Stage A domain invariants are reused by decoding into the typed
    domain models — their ``ValueError`` failures are wrapped into
    the wire error (payload fields carry no secrets, so domain
    messages with offending values are safe to surface).
    """
    mapping = _require_mapping("payload", payload)
    _require_exact_keys("payload", mapping, _TOP_LEVEL_FIELDS)
    node = _require_node("payload.node", mapping["node"])
    reported_at = _require_reported_at("payload.reported_at",
                                       mapping["reported_at"])
    uptime_seconds = _require_number(
        "payload.uptime_seconds", mapping["uptime_seconds"]
    )
    cpu_percent = _require_number("payload.cpu_percent",
                                  mapping["cpu_percent"])
    try:
        load = _decode_load("payload.load", mapping["load"])
        ram = _decode_resource("payload.ram", mapping["ram"])
        swap = _decode_resource("payload.swap", mapping["swap"])
        root_filesystem = _decode_resource("payload.root_fs",
                                           mapping["root_fs"])
        root_inodes = _decode_resource("payload.root_inodes",
                                       mapping["root_inodes"])
        return HostTelemetry(
            host=node,
            timestamp=reported_at,
            uptime_seconds=uptime_seconds,
            load=load,
            cpu_percent=cpu_percent,
            ram=ram,
            swap=swap,
            root_filesystem=root_filesystem,
            root_inodes=root_inodes,
        )
    except ValueError as error:
        # Domain invariant violations (LoadAverage / ResourceUsage /
        # HostTelemetry __post_init__) become wire decode failures.
        raise MalformedHeartbeatPayloadError(
            f"invalid telemetry value: {error}"
        ) from error


def _snapshot_payload(payload: object) -> dict[str, object]:
    """Defensive stable snapshot of an external mapping (one read).

    The caller-controlled mapping is materialized exactly once into a
    plain ``dict`` (nested mappings recursively), so the claimed node,
    authentication and the full strict decode all see the same stable
    data. A hostile mapping that raises, changes values between reads
    or otherwise cannot be read deterministically is a malformed
    payload: :class:`MalformedHeartbeatPayloadError`, raised here —
    before any authentication, clock call or write.

    Secret-safe exception chain: the sanitized boundary error is
    raised OUTSIDE the ``except`` handler (flag-based control flow).
    ``raise ... from None`` inside an active handler would only set
    ``__suppress_context__`` while the hostile exception object stays
    attached as ``__context__``; raising after the handler exits
    leaves the boundary error with ``__cause__ is None`` AND
    ``__context__ is None``. The hostile exception object is never
    retained in a local/member/container and its type/message/repr
    never enter the boundary error.
    """
    if not isinstance(payload, Mapping):
        raise MalformedHeartbeatPayloadError("payload must be a JSON object")
    snapshot: dict[str, object] = {}
    snapshot_failed = False
    try:
        snapshot = {
            key: _snapshot_value(value) for key, value in payload.items()
        }
    except Exception:
        # Deliberately NO re-raise here: the sanitized boundary error
        # must be raised below, outside the active exception handler,
        # so the hostile exception is not chained onto it. Only a
        # boolean flag is retained — never the exception object.
        snapshot_failed = True
    if snapshot_failed:
        raise MalformedHeartbeatPayloadError(
            "payload must be a deterministically readable JSON object"
        )
    return snapshot


def _snapshot_value(value: object) -> object:
    """Snapshot one payload value: mappings become plain dicts.

    Scalars are immutable and kept by reference; non-mapping
    structured values (lists, etc.) are never read field-wise by the
    decoder, so they pass through unchanged and are rejected by the
    strict decode as wrong types.
    """
    if isinstance(value, Mapping):
        return _snapshot_payload(value)
    return value


def _claimed_node(payload: object) -> str:
    """Extract just enough of the claimed node to choose a credential.

    Only the node identity is inspected before authentication; the
    full payload is decoded strictly only after the token has been
    verified for that node.
    """
    mapping = _require_mapping("payload", payload)
    if "node" not in mapping:
        raise MalformedHeartbeatPayloadError(
            "payload is missing mandatory field(s): node"
        )
    return _require_node("payload.node", mapping["node"])


class AuthenticatedHeartbeatAdapter:
    """Transport-neutral authenticated heartbeat wire adapter (B3).

    Sits in front of the accepted B2 ``HeartbeatIngestor``::

        adapter.handle(payload_mapping, presented_token) -> HeartbeatReceipt

    Fail-closed order of operations:

    0. one defensive plain-dict snapshot of the external payload —
       the caller-owned mapping is never read again after this point;
    1. extract the claimed node from the snapshot (malformed node
       identity => wire decode failure, before any auth lookup);
    2. the claimed node must be a configured Sentinel node —
       ``SentinelConfig.host()`` verbatim, case-sensitive semantics;
    3. a credential (expected token) must exist for THAT node;
    4. the presented token must have a valid shape (string,
       non-empty, non-whitespace-only) and match the expected token
       of that node via stdlib constant-time comparison
       (``hmac.compare_digest`` over UTF-8/``surrogatepass`` bytes,
       verbatim — total for arbitrary ``str``, so lone surrogates
       can never raise ``UnicodeEncodeError``);
    5. only then the same snapshot is strictly decoded into
       ``HostTelemetry`` (the decoded identity is confirmed to equal
       the authenticated one — defence in depth) and handed to B2
       ingestion.

    Steps 2-4 all raise the same generic
    :class:`HeartbeatAuthenticationError` — unknown nodes are
    indistinguishable from wrong tokens (no identity enumeration).
    Authentication failures never touch the B2 ingestion clock or
    SQLite; snapshot and wire decode failures also never write (a
    snapshot failure happens before authentication, a decode failure
    only after a successful one).
    """

    def __init__(
        self,
        config: SentinelConfig,
        credentials: NodeCredentials,
        ingestor: HeartbeatIngestor,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._ingestor = ingestor

    def handle(
        self, payload: Mapping[str, object], token: object
    ) -> HeartbeatReceipt:
        """Authenticate, strictly decode and ingest one heartbeat.

        The external mapping is read exactly once (defensive stable
        snapshot); claimed node, authentication and decoding all
        operate on that same snapshot, so the authenticated node
        identity can never differ from the decoded/persisted one.
        """
        snapshot = _snapshot_payload(payload)

        node = _claimed_node(snapshot)

        # Unknown node and missing credential are the same generic
        # external failure: no identity enumeration, no secrets.
        if self._config.host(node) is None:
            raise HeartbeatAuthenticationError(_AUTHENTICATION_FAILED)
        expected = self._credentials.token_for(node)
        if expected is None:
            raise HeartbeatAuthenticationError(_AUTHENTICATION_FAILED)

        # Presented token shape: string, non-empty, non-whitespace-only.
        # A valid exact secret like " abc " passes verbatim — the token
        # is never stripped or normalized before comparison.
        if not isinstance(token, str) or not token or not token.strip():
            raise HeartbeatAuthenticationError(_AUTHENTICATION_FAILED)

        # Total exact comparison for arbitrary str: UTF-8 with
        # surrogatepass is defined for every Python str (including
        # lone surrogates) and is injective, so distinct secrets stay
        # distinct and no UnicodeEncodeError can escape this boundary.
        if not hmac.compare_digest(
            token.encode("utf-8", errors="surrogatepass"),
            expected.encode("utf-8", errors="surrogatepass"),
        ):
            raise HeartbeatAuthenticationError(_AUTHENTICATION_FAILED)

        telemetry = decode_heartbeat_payload(snapshot)
        # Defence in depth on top of the stable snapshot: the decoded
        # identity must equal the authenticated one. Guaranteed today
        # (both read the same snapshot); re-checked so a future decode
        # change cannot silently break the frozen
        # one-node-one-own-token invariant.
        if telemetry.host != node:
            raise MalformedHeartbeatPayloadError(
                "authenticated node identity does not match the decoded"
                " payload"
            )
        return self._ingestor.ingest(telemetry)
