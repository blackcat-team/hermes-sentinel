"""Deterministic transport tests for the Stage C2 HTTPS one-shot reporter.

Production transport authority is ``scripts/sentinel-report.sh``
(bash + curl). This module is only a test harness — there is no second
reporter implementation here. It proves, with runtime evidence:

- the executable reporter performs exactly ONE outbound HTTPS POST,
  prints NOTHING on success and emits only a short generic stderr
  diagnostic on failure;
- transport configuration is validated fail-closed BEFORE any request
  and BEFORE collection (missing/empty/HTTP/whitespace/control
  endpoint, missing/invalid token, missing curl);
- the token never appears in curl argv, in the curl child
  environment, in reporter stdout/stderr or in the JSON body, and
  reaches curl exclusively through the protected stdin header path
  (``--header @-``); shell xtrace can never print it;
- Expect is suppressed on the wire, the HTTPS-only protocol
  restriction is present, ambient curl configuration is disabled
  (``--disable`` as the FIRST curl option), and ``--insecure``/
  ``--location``/``--retry`` are never used;
- only HTTP 204 is success: 200/201/202/302/401/413/417/500 all
  fail, and a redirect is never followed (exactly one attempt);
- a collection failure or an oversized payload never invokes curl;
- a REAL verified local HTTPS integration: the production script,
  the real installed curl, a local Python stdlib TLS server with a
  synthetic TEST-ONLY certificate, and full server-side request
  capture. Because the production invocation disables ambient curl
  configuration, test trust for the synthetic certificate is
  established WITHOUT any production mechanism: an isolated TEST-ONLY
  pass-through ``curl`` shim on the harness PATH simply execs the
  REAL installed curl with the synthetic ``--cacert`` anchor added
  (the real curls on this Windows machine are Schannel builds that
  empirically ignore ``CURL_CA_BUNDLE`` and reject CA-only anchors
  through revocation checking, which is why the shim is used). The
  final network client remains the actual installed curl with full
  certificate and hostname verification — nothing about the
  production runtime contract is weakened;
- TLS fail-closed proof: the same endpoint WITHOUT the shim and
  WITHOUT any trust anchor fails, and no request is delivered;
- malicious-curlrc adversarial proof: a TEST-ONLY ``CURL_HOME``
  whose ``.curlrc`` contains ``--insecure`` is demonstrably honored
  by the raw real curl (control), yet the production reporter still
  fails TLS verification against the untrusted endpoint with zero
  delivered requests — proving ambient curl config cannot alter the
  reporter request.

No production bypass variable (TEST_MODE / SENTINEL_COLLECT_ONLY /
SENTINEL_DISABLE_NETWORK) exists or is introduced by these tests: the
fake-curl harness only controls PATH normally ahead of the real curl.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from ssl import PROTOCOL_TLS_SERVER, SSLContext

# test_reporter_collector already bootstraps the src layout into
# sys.path (same directory) and owns the real-bash discovery plus the
# C1 fixture constants reused here as the single source of truth.
import test_reporter_collector as c1
from hermes_sentinel.wire import decode_heartbeat_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sentinel-report.sh"

#: Synthetic TEST-ONLY reporter token (transport-safe alphabet).
TEST_TOKEN = "synthetic-test-token-ABC123._~-"

#: Synthetic endpoint used for fake-transport tests (RFC 2606 TLD).
FAKE_ENDPOINT = "https://sentinel.example/v1/heartbeat"

#: Exactly the accepted B4 server body limit.
MAX_BODY_BYTES = 16384

#: The fixed single-attempt transport limits the script must pass to
#: curl (tests never actually wait for these durations — every test
#: transport is local or synthetic).
CONNECT_TIMEOUT_SECONDS = "10"
REQUEST_TIMEOUT_SECONDS = "20"

#: TEST-ONLY synthetic curl stand-in. It is placed ahead of the real
#: curl on PATH by the test harness (normal PATH control — no
#: production "curl path" seam exists) and captures argv, the child
#: environment and stdin, then emulates ONE deterministic outcome
#: selected by $FAKE_CURL_MODE:
#:   status:<code>  print <code> as the --write-out result, exit 0;
#:   exit:<code>    exit <code> with no output (network failure).
#: Built with explicit "\n" joins so the generated script always has
#: LF endings regardless of this source file's own line endings.
FAKE_CURL_SCRIPT = "\n".join(
    [
        "#!/usr/bin/env bash",
        "# TEST-ONLY synthetic curl (never installed on any monitored host).",
        'mode=${FAKE_CURL_MODE-status:000}',
        "printf 'x\\n' >>\"$FAKE_CURL_COUNT\"",
        "printf '%s\\n' \"$@\" >>\"$FAKE_CURL_ARGV\"",
        "env >>\"$FAKE_CURL_ENV\"",
        "cat >>\"$FAKE_CURL_STDIN\"",
        'case "$mode" in',
        '    status:*) printf \'%s\' "${mode#status:}"; exit 0 ;;',
        '    exit:*) exit "${mode#exit:}" ;;',
        "esac",
        "exit 1",
    ]
) + "\n"


def setUpModule() -> None:
    if c1.BASH is None:
        raise RuntimeError(
            "C2 transport verification requires a real bash on this "
            "machine; none was found by the C1 harness"
        )


# --- environment helpers ----------------------------------------------------


def _wslenv_add(
    env: dict[str, str],
    plain: tuple[str, ...] = (),
    path_vars: tuple[str, ...] = (),
) -> None:
    """Register additional variables for WSL inheritance.

    WSL forwards only variables listed in WSLENV; the ``/p`` flag
    translates a Windows path value into its WSL form.
    """
    if c1.BASH_FLAVOR != "wsl":
        return
    parts = [*plain, *(f"{name}/p" for name in path_vars)]
    inherited = env.get("WSLENV", "")
    env["WSLENV"] = ":".join(parts + ([inherited] if inherited else []))


def _transport_env(
    endpoint: str | None = FAKE_ENDPOINT,
    token: str | None = TEST_TOKEN,
    curl_home: Path | None = None,
    node: str | None = "Prod",
    **fixture_overrides: str | None,
) -> dict[str, str]:
    """Environment for one reporter run against the nominal fixtures.

    ``None`` removes the variable entirely (missing-endpoint /
    missing-token failure paths). Ambient trust anchors, curl
    configuration and proxy settings are never inherited into the
    deterministic test environment: the real-HTTPS tests control
    curl's trust exclusively through ``curl_home`` (a ``CURL_HOME``
    directory whose ``.curlrc`` may carry the synthetic TEST-ONLY
    ``--cacert`` anchor).
    """
    env = c1._fixture_env(node=node, **fixture_overrides)
    for var in (
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "CURL_HOME",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        env.pop(var, None)
    env.pop("SENTINEL_ENDPOINT", None)
    env.pop("SENTINEL_TOKEN", None)
    if endpoint is not None:
        env["SENTINEL_ENDPOINT"] = endpoint
    if token is not None:
        env["SENTINEL_TOKEN"] = token
    _wslenv_add(env, plain=("SENTINEL_ENDPOINT", "SENTINEL_TOKEN"))
    if curl_home is not None:
        env["CURL_HOME"] = c1._env_path(curl_home)
        _wslenv_add(env, path_vars=("CURL_HOME",))
    return env


_DIR_CACHE: dict[str, str] = {}


def _bash_dir(path: Path) -> str:
    """Directory path in the form usable inside a bash PATH entry.

    MSYS PATH entries must be true POSIX paths (/c/...) — a
    drive-letter form would be split on its colon by PATH parsing.
    """
    resolved = path.resolve()
    key = str(resolved)
    if key in _DIR_CACHE:
        return _DIR_CACHE[key]
    if c1.BASH_FLAVOR == "msys":
        proc = c1._run_bash_code(f"cygpath -u '{resolved.as_posix()}'")
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(f"cannot convert directory for PATH: {key}")
        converted = proc.stdout.strip()
    else:
        converted = c1._bash_path(resolved)
    _DIR_CACHE[key] = converted
    return converted


def _run_reporter(
    env: dict[str, str],
    path_prepend: str | None = None,
    path_replace: str | None = None,
    trace: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute the production script as the real one-shot reporter.

    ``path_prepend``/``path_replace`` control PATH normally inside the
    bash wrapper (synthetic curl ahead of the real one, or an empty
    PATH for the missing-curl proof). ``trace`` runs the script under
    ``bash -x`` for the xtrace secret-safety proof.
    """
    script_bash = c1._bash_path(SCRIPT)
    if path_replace is not None:
        inner_bash = c1._bash_path(Path(c1._bash()).resolve())
        setup = [f'export PATH="{path_replace}"']
    else:
        inner_bash = "bash"
        setup = []
        if path_prepend is not None:
            setup.append(f'export PATH="{path_prepend}:$PATH"')
    trace_flag = " -x" if trace else ""
    setup.append(f'exec "{inner_bash}"{trace_flag} "{script_bash}"')
    return subprocess.run(
        [c1._bash(), "-c", "\n".join(setup) + "\n"],
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _curl_invocations(path: Path) -> int:
    return sum(1 for line in _read_lines(path) if line.strip())


def _has_pair(argv: list[str], name: str, value: str) -> bool:
    return any(
        argv[index] == name and argv[index + 1] == value
        for index in range(len(argv) - 1)
    )


# --- fake-curl harness ------------------------------------------------------


class FakeCurlTestCase(unittest.TestCase):
    """Base: temp capture dir + synthetic curl ahead of real curl."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "curl").write_text(
            FAKE_CURL_SCRIPT, encoding="utf-8", newline="\n"
        )
        proc = c1._run_bash_code(
            f'chmod +x "{_bash_dir(self.bin_dir)}/curl"'
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.count_file = self.root / "count.log"
        self.argv_file = self.root / "argv.log"
        self.env_file = self.root / "env.log"
        self.stdin_file = self.root / "stdin.log"

    def _reset_capture(self) -> None:
        for path in (
            self.count_file,
            self.argv_file,
            self.env_file,
            self.stdin_file,
        ):
            path.unlink(missing_ok=True)

    def fake_env(
        self, mode: str, **fixture_overrides: str | None
    ) -> dict[str, str]:
        env = _transport_env(**fixture_overrides)
        env["FAKE_CURL_MODE"] = mode
        env["FAKE_CURL_COUNT"] = c1._env_path(self.count_file)
        env["FAKE_CURL_ARGV"] = c1._env_path(self.argv_file)
        env["FAKE_CURL_ENV"] = c1._env_path(self.env_file)
        env["FAKE_CURL_STDIN"] = c1._env_path(self.stdin_file)
        _wslenv_add(
            env,
            plain=("FAKE_CURL_MODE",),
            path_vars=(
                "FAKE_CURL_COUNT",
                "FAKE_CURL_ARGV",
                "FAKE_CURL_ENV",
                "FAKE_CURL_STDIN",
            ),
        )
        return env

    def run_reporter(
        self, mode: str, **fixture_overrides: str | None
    ) -> subprocess.CompletedProcess[str]:
        return _run_reporter(
            self.fake_env(mode, **fixture_overrides),
            path_prepend=_bash_dir(self.bin_dir),
        )

    def assertCurlNotInvoked(self) -> None:
        self.assertFalse(
            self.count_file.exists(),
            "curl must not be invoked for this failure path",
        )

    def assertCurlInvokedOnce(self) -> None:
        self.assertEqual(_curl_invocations(self.count_file), 1)


# --- transport configuration (fail before curl, before collection) ----------


class TransportConfigTest(FakeCurlTestCase):
    def test_missing_endpoint_fails_before_curl(self) -> None:
        env = self.fake_env("status:204")
        env.pop("SENTINEL_ENDPOINT")
        proc = _run_reporter(env, path_prepend=_bash_dir(self.bin_dir))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")
        self.assertCurlNotInvoked()

    def test_empty_endpoint_fails_before_curl(self) -> None:
        proc = self.run_reporter("status:204", endpoint="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertCurlNotInvoked()

    def test_http_endpoint_rejected_before_curl(self) -> None:
        proc = self.run_reporter(
            "status:204", endpoint="http://127.0.0.1:9/v1/heartbeat"
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertCurlNotInvoked()

    def test_malformed_endpoints_rejected_before_curl(self) -> None:
        """Whitespace/CR/LF/control or non-https values never reach
        curl and are never silently normalized."""
        for bad in (
            " ",
            "https://",
            " https://sentinel.example/v1/heartbeat",
            "https://sentinel.example/v1/heartbeat ",
            "https://sentinel.example/v1/heartbeat\t",
            "https://sentinel.example/v1/heartbeat\r",
            "https://sentinel.example/v1/heartbeat\n",
            "https://senti nel.example/v1/heartbeat",
            "ftp://sentinel.example/v1/heartbeat",
            "sentinel.example/v1/heartbeat",
        ):
            with self.subTest(endpoint=repr(bad)):
                proc = self.run_reporter("status:204", endpoint=bad)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertCurlNotInvoked()

    def test_missing_token_fails_before_curl(self) -> None:
        env = self.fake_env("status:204")
        env.pop("SENTINEL_TOKEN")
        proc = _run_reporter(env, path_prepend=_bash_dir(self.bin_dir))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")
        self.assertCurlNotInvoked()

    def test_invalid_tokens_rejected_without_mutation(self) -> None:
        """Anything outside the documented transport-safe alphabet is
        rejected verbatim — never trimmed or normalized into a valid
        token that would then be sent."""
        for bad in (
            "",
            " ",
            "token with space",
            "token\twith-tab",
            "token\rwith-cr",
            "token\nwith-lf",
            "quote'token",
            'dq"token',
            "tok;en",
            "tok/en",
            "tok+en",
            "tok=EN",
            "пароль",
        ):
            with self.subTest(token=repr(bad)):
                proc = self.run_reporter("status:204", token=bad)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertCurlNotInvoked()

    def test_missing_curl_fails(self) -> None:
        empty = self.root / "no-curl-here"
        empty.mkdir()
        proc = _run_reporter(
            self.fake_env("status:204"), path_replace=_bash_dir(empty)
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertNotEqual(proc.stderr, "")
        self.assertCurlNotInvoked()


# --- fake-curl success / argv / secret proofs -------------------------------


class FakeCurlTransportTest(FakeCurlTestCase):
    def test_success_is_silent(self) -> None:
        """The executable reporter no longer prints the payload: on
        the only success status (204) stdout AND stderr are empty."""
        proc = self.run_reporter("status:204")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")
        self.assertCurlInvokedOnce()

    def test_curl_argv_contract(self) -> None:
        proc = self.run_reporter("status:204")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        argv = _read_lines(self.argv_file)
        self.assertCurlInvokedOnce()

        # The token never appears in argv (no process-listing leak).
        self.assertNotIn(TEST_TOKEN, argv)
        self.assertFalse(any(TEST_TOKEN in arg for arg in argv))

        # Ambient curl configuration is disabled: --disable must be
        # the FIRST option after the executable (curl only honors
        # the config-disabling option in first position).
        self.assertEqual(argv[0], "--disable")

        # Exactly one POST to the configured endpoint, used verbatim.
        self.assertTrue(_has_pair(argv, "--request", "POST"))
        self.assertEqual(argv[-1], FAKE_ENDPOINT)

        # Content-Type header and the protected stdin header path.
        self.assertTrue(
            _has_pair(argv, "--header", "Content-Type: application/json")
        )
        self.assertTrue(_has_pair(argv, "--header", "@-"))

        # Expect suppression semantics: `-H 'Expect:'` removes the
        # header from transmission; it must NOT be an empty-value
        # header on the wire.
        self.assertTrue(_has_pair(argv, "--header", "Expect:"))

        # HTTPS-only protocol restriction and fixed one-shot limits.
        self.assertTrue(_has_pair(argv, "--proto", "=https"))
        self.assertTrue(
            _has_pair(argv, "--connect-timeout", CONNECT_TIMEOUT_SECONDS)
        )
        self.assertTrue(
            _has_pair(argv, "--max-time", REQUEST_TIMEOUT_SECONDS)
        )

        # Output safety: body discarded, only the status captured.
        self.assertTrue(_has_pair(argv, "--output", "/dev/null"))
        self.assertTrue(_has_pair(argv, "--write-out", "%{http_code}"))
        self.assertIn("--silent", argv)

        # Forbidden options are never used.
        for forbidden in (
            "--insecure",
            "-k",
            "--location",
            "-L",
            "--retry",
            "--verbose",
            "-v",
            "--trace",
            "--trace-ascii",
        ):
            self.assertNotIn(forbidden, argv)

        # The body is the exact C1 JSON (single line, no trailing
        # newline after command substitution), still B3-valid.
        body = argv[argv.index("--data-binary") + 1]
        payload = json.loads(body)
        self.assertEqual(set(payload), c1.TOP_LEVEL_KEYS)
        decode_heartbeat_payload(payload)
        self.assertNotIn(TEST_TOKEN, body)

    def test_curl_child_environment_has_no_sentinel_token(self) -> None:
        proc = self.run_reporter("status:204")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        env_text = self.env_file.read_text(
            encoding="utf-8", errors="replace"
        )
        names = [
            line.split("=", 1)[0]
            for line in env_text.splitlines()
            if "=" in line
        ]
        self.assertNotIn("SENTINEL_TOKEN", names)
        self.assertNotIn(TEST_TOKEN, env_text)

    def test_token_reaches_curl_only_via_stdin_header(self) -> None:
        proc = self.run_reporter("status:204")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        stdin = self.stdin_file.read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertEqual(stdin, f"X-Sentinel-Token: {TEST_TOKEN}\n")

    def test_xtrace_never_prints_the_token(self) -> None:
        proc = _run_reporter(
            self.fake_env("status:204"),
            path_prepend=_bash_dir(self.bin_dir),
            trace=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn(TEST_TOKEN, proc.stderr)
        self.assertNotIn(TEST_TOKEN, proc.stdout)

    def test_only_http_204_is_success(self) -> None:
        """200/201/202, 3xx, 4xx and 5xx all fail — exactly one
        attempt each, no retry, stdout always empty."""
        for status in ("200", "201", "202", "302", "401", "413",
                       "417", "500"):
            with self.subTest(status=status):
                self._reset_capture()
                proc = self.run_reporter(f"status:{status}")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn(
                    TEST_TOKEN, proc.stdout + proc.stderr
                )
                self.assertCurlInvokedOnce()

    def test_redirect_is_failure_and_never_followed(self) -> None:
        proc = self.run_reporter("status:302")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        # Exactly one curl invocation: no redirect-follow request.
        self.assertCurlInvokedOnce()
        argv = _read_lines(self.argv_file)
        self.assertNotIn("--location", argv)
        self.assertNotIn("-L", argv)

    def test_curl_network_failure_maps_to_generic_failure(self) -> None:
        """A curl-level failure (here exit 7, connection refused)
        becomes the reporter's own short generic diagnostic — never
        raw curl output, never the token."""
        proc = self.run_reporter("exit:7")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertLess(len(proc.stderr), 300)
        self.assertNotIn(TEST_TOKEN, proc.stdout + proc.stderr)
        self.assertNotIn("Failed to connect", proc.stderr)
        self.assertNotIn("refused", proc.stderr)
        # One attempt only: no retry policy.
        self.assertCurlInvokedOnce()


# --- collection failure precedence / payload size ---------------------------


class CollectionPrecedenceTest(FakeCurlTestCase):
    def test_collection_failure_invokes_no_curl(self) -> None:
        missing = c1._env_path(c1.FIXTURES / "no-such-fixture.txt")
        proc = self.run_reporter(
            "status:204", SENTINEL_PROC_UPTIME=missing
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertCurlNotInvoked()

    def test_oversized_payload_never_invokes_curl(self) -> None:
        """A syntactically valid but > 16384-byte payload (long node
        name) fails the deterministic pre-transport size gate; the
        payload is never truncated and curl is never invoked."""
        proc = self.run_reporter("status:204", node="a" * 20000)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertCurlNotInvoked()


class SourceableGateTest(unittest.TestCase):
    """Direct proofs for the sourceable transport gate functions."""

    def test_validate_payload_size_boundaries(self) -> None:
        source = f'source "{c1._bash_path(SCRIPT)}"\n'
        # Pure-bash construction of exact-length ASCII strings.
        code = (
            source
            + 'printf -v ok "%16384s" ""\n'
            + 'ok="${ok// /a}"\n'
            + 'printf -v big "%16385s" ""\n'
            + 'big="${big// /a}"\n'
            + 'if validate_payload_size "$ok"; then echo "accept:16384";'
            " else echo \"reject:16384\"; fi\n"
            + 'if validate_payload_size "$big"; then echo "accept:16385";'
            " else echo \"reject:16385\"; fi\n"
            + 'if validate_payload_size ""; then echo "accept:empty";'
            " else echo \"reject:empty\"; fi\n"
        )
        proc = c1._run_bash_code(code)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(
            proc.stdout.split(),
            ["accept:16384", "reject:16385", "reject:empty"],
        )

    def _validator_outcomes(
        self, function: str, values: tuple[str, ...]
    ) -> list[bool]:
        """Source the production script and call ``function`` on each
        single-quoted value; return per-value accept/reject booleans."""
        lines = [f'source "{c1._bash_path(SCRIPT)}"']
        for index, value in enumerate(values):
            lines.append(
                f"if {function} '{value}'; then"
                f' echo "yes:{index}"; else echo "no:{index}"; fi'
            )
        proc = c1._run_bash_code("\n".join(lines) + "\n")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        outcomes = proc.stdout.split()
        self.assertEqual(len(outcomes), len(values))
        return [outcome.startswith("yes:") for outcome in outcomes]

    def test_validate_endpoint_url_direct(self) -> None:
        accepts = (
            "https://sentinel.example/v1/heartbeat",
            "https://127.0.0.1:8443/v1/heartbeat",
            "https://example.com/",
        )
        rejects = (
            "",
            "http://sentinel.example/v1/heartbeat",
            "https://",
            " https://example.com",
            "https://example.com/v1 ",
            "https://exa mple.com",
            "ftp://example.com",
            "example.com",
        )
        outcomes = self._validator_outcomes(
            "validate_endpoint_url", accepts + rejects
        )
        for index, value in enumerate(accepts):
            self.assertTrue(outcomes[index], msg=f"expected accept: {value!r}")
        for index, value in enumerate(rejects):
            self.assertFalse(
                outcomes[len(accepts) + index],
                msg=f"expected reject: {value!r}",
            )

    def test_validate_reporter_token_direct(self) -> None:
        accepts = (TEST_TOKEN, "a", "0123456789", "a._~-Z9")
        # Quote-bearing shapes are covered by the env-based
        # TransportConfigTest (arbitrary bytes through subprocess env).
        rejects = (
            "",
            " ",
            "token with space",
            "tok/en",
            "tok=EN",
        )
        outcomes = self._validator_outcomes(
            "validate_reporter_token", accepts + rejects
        )
        for index, value in enumerate(accepts):
            self.assertTrue(outcomes[index], msg=f"expected accept: {value!r}")
        for index, value in enumerate(rejects):
            self.assertFalse(
                outcomes[len(accepts) + index],
                msg=f"expected reject: {value!r}",
            )

    def test_bash_syntax_check(self) -> None:
        proc = c1._run_bash_code(f'bash -n "{c1._bash_path(SCRIPT)}"')
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "")


# --- real HTTPS integration -------------------------------------------------


class _RequestCapture:
    """Server-side record of every received request (thread-safe by
    test serialization: the reporter runs to completion between
    captures)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.status: int = 204


class _CapturingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    capture: _RequestCapture

    def do_POST(self) -> None:
        capture = self.capture
        length_header = self.headers.get("Content-Length")
        body = (
            self.rfile.read(int(length_header))
            if length_header is not None
            else b""
        )
        capture.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": tuple(self.headers.items()),
                "content_length_header": length_header,
                "body": body,
            }
        )
        self.send_response_only(capture.status)
        if capture.status == 302:
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _QuietHTTPServer(HTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        # The TLS fail-closed proof intentionally produces handshake
        # failures on the server side; they are expected test noise,
        # never diagnostics to print.
        return


def _bash_utility(name: str) -> str | None:
    proc = c1._run_bash_code(f"command -v {name}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _generate_tls_material(root: Path) -> tuple[Path, Path, Path]:
    """Synthetic TEST-ONLY TLS material via openssl.

    Generates a throwaway CA (valid two days) and a server
    certificate for DNS:localhost / IP:127.0.0.1, then returns
    ``(server_cert, server_key, trust_bundle)``. Nothing here is a
    production credential: everything is synthetic, test-only,
    generated fresh in a temp directory per test run and deleted
    afterwards. No real hostname, account or deployment secret is
    involved.

    The trust bundle is the concatenation of the synthetic server
    certificate and the synthetic CA certificate; the real-HTTPS
    tests hand it to the real curl through an isolated TEST-ONLY
    pass-through curl shim (see ``RealHttpsIntegrationTest``) that
    execs the REAL installed curl with ``--cacert`` added. Why not
    ``CURL_CA_BUNDLE`` or ``$CURL_HOME/.curlrc``: the production
    invocation disables ambient curl configuration (``--disable``,
    first option), so config files must never influence it; and the
    real curls available on a Windows test machine are Schannel
    builds, which (verified empirically on the builds installed
    here) ignore the ``CURL_CA_BUNDLE`` environment variable, and
    whose revocation checking additionally rejects a CA-only anchor
    with curl exit 60 ("the revocation status is unknown") even when
    the CA file is loaded — well-known Schannel test-environment
    quirks. The combined leaf+CA bundle via ``--cacert`` makes
    deterministic local verification work with Schannel (Windows)
    and OpenSSL (Linux) curls alike. This is purely test-environment
    trust configuration: the production script never reads
    ``CURL_HOME``, ``CURL_CA_BUNDLE`` or any other curl variable,
    never passes any CA option, and always uses default
    certificate/hostname verification.
    """
    if _bash_utility("openssl") is None:
        raise RuntimeError(
            "real HTTPS integration requires a working openssl in the "
            "bash environment (synthetic TEST-ONLY certificate "
            "generation); none was found"
        )
    b_root = c1._bash_path(root)
    # MSYS bash transparently converts leading-slash arguments (like
    # -subj '/CN=...') into Windows paths, which would corrupt the
    # openssl subjects; both standard disable switches are applied
    # (no-ops outside MSYS).
    no_pathconv = "MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "
    commands = (
        no_pathconv
        + "openssl req -x509 -newkey rsa:2048 -nodes -days 2 "
        "-subj '/CN=Sentinel Synthetic Test CA' "
        f"-keyout '{b_root}/ca.key' -out '{b_root}/ca.crt'",
        no_pathconv
        + "openssl req -newkey rsa:2048 -nodes -days 2 "
        "-subj '/CN=localhost' "
        f"-keyout '{b_root}/server.key' -out '{b_root}/server.csr'",
        no_pathconv
        + "openssl x509 -req -days 2 -in "
        f"'{b_root}/server.csr' -CA '{b_root}/ca.crt' "
        f"-CAkey '{b_root}/ca.key' -CAcreateserial "
        f"-out '{b_root}/server.crt' -extfile '{b_root}/san.cnf'",
    )
    (root / "san.cnf").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n",
        encoding="utf-8",
        newline="\n",
    )
    for command in commands:
        proc = subprocess.run(
            [c1._bash(), "-c", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "synthetic TEST-ONLY certificate generation failed "
                f"(openssl exit {proc.returncode}): {proc.stderr.strip()}"
            )
    ca_cert = root / "ca.crt"
    server_cert = root / "server.crt"
    server_key = root / "server.key"
    for path in (ca_cert, server_cert, server_key):
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"openssl produced no {path.name}")
    trust_bundle = root / "test-only-trust-bundle.pem"
    trust_bundle.write_bytes(
        server_cert.read_bytes() + ca_cert.read_bytes()
    )
    return server_cert, server_key, trust_bundle


def _shell_quote(value: str) -> str:
    """Single-quote a value for safe embedding in bash code."""
    return "'" + value.replace("'", "'\\''") + "'"


class RealHttpsIntegrationTest(unittest.TestCase):
    """Production script + real curl + verified local TLS server."""

    _tmp: tempfile.TemporaryDirectory[str]
    trust_bundle: Path
    server_cert: Path
    server_key: Path
    malicious_curl_home: Path
    shim_bin: Path
    shim_path: str
    capture: _RequestCapture
    server: _QuietHTTPServer
    port: int
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        tmp = tempfile.TemporaryDirectory()
        cls._tmp = tmp
        root = Path(tmp.name)
        cls.server_cert, cls.server_key, cls.trust_bundle = (
            _generate_tls_material(root)
        )
        real_curl = _bash_utility("curl")
        if real_curl is None:
            raise RuntimeError(
                "real HTTPS integration requires a real installed "
                "curl in the bash environment; none was found"
            )
        version = c1._run_bash_code("curl --version")
        if version.returncode != 0 or not version.stdout.strip():
            raise RuntimeError("the discovered curl is not executable")
        # TEST-ONLY trust mechanism for the real-HTTPS integration.
        # The production invocation disables ambient curl
        # configuration (--disable as the first curl option), so no
        # config-file facility (CURL_HOME/.curlrc) may be used to
        # establish test trust. Instead an isolated TEST-ONLY
        # pass-through curl shim is placed ahead of the real curl on
        # the harness PATH: it execs the REAL installed curl with the
        # synthetic TEST-ONLY --cacert anchor added (and --disable in
        # first position, matching the frozen production invariant —
        # curl only honors the config-disabling option there). The
        # final network client remains the actual installed curl with
        # full certificate and hostname verification; no production
        # mechanism, CA option or executable seam is introduced.
        cls.shim_bin = root / "shim-bin"
        cls.shim_bin.mkdir()
        if c1.BASH_FLAVOR == "wsl":
            bundle_arg = c1._bash_path(cls.trust_bundle)
        else:
            bundle_arg = c1._env_path(cls.trust_bundle)
        (cls.shim_bin / "curl").write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "# TEST-ONLY curl pass-through (never installed on",
                    "# any monitored host). Executes the REAL installed",
                    "# curl, adding only the synthetic TEST-ONLY trust",
                    "# anchor for the local HTTPS integration server.",
                    f"exec {_shell_quote(real_curl)} --disable "
                    f"--cacert {_shell_quote(bundle_arg)} \"$@\"",
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        proc = c1._run_bash_code(
            f"chmod +x {_shell_quote(c1._bash_path(cls.shim_bin) + '/curl')}"
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cannot make the test shim executable: {proc.stderr}")
        cls.shim_path = _bash_dir(cls.shim_bin)
        # TEST-ONLY adversarial curl configuration: a CURL_HOME whose
        # .curlrc would disable TLS verification if curl read it.
        cls.malicious_curl_home = root / "curl-home-malicious"
        cls.malicious_curl_home.mkdir()
        (cls.malicious_curl_home / ".curlrc").write_text(
            "--insecure\n", encoding="utf-8", newline="\n"
        )
        cls.capture = _RequestCapture()
        _CapturingHandler.capture = cls.capture
        cls.server = _QuietHTTPServer(("127.0.0.1", 0), _CapturingHandler)
        context = SSLContext(PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(cls.server_cert), keyfile=str(cls.server_key)
        )
        cls.server.socket = context.wrap_socket(
            cls.server.socket, server_side=True
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        cls._tmp.cleanup()

    @property
    def endpoint(self) -> str:
        return f"https://127.0.0.1:{self.port}/v1/heartbeat"

    def _run(
        self, trusted: bool
    ) -> subprocess.CompletedProcess[str]:
        """Run the production reporter against the local TLS server.

        ``trusted=True`` routes ``curl`` through the TEST-ONLY
        pass-through shim (synthetic anchor, real curl). ``trusted=
        False`` resolves the real curl directly from the default
        PATH with NO trust mechanism of any kind — the fail-closed
        baseline. Neither path sets CURL_HOME/CURL_CA_BUNDLE.
        """
        env = _transport_env(endpoint=self.endpoint, token=TEST_TOKEN)
        if trusted:
            return _run_reporter(env, path_prepend=self.shim_path)
        return _run_reporter(env)

    def test_real_verified_https_success(self) -> None:
        """The server independently observes the exact request: POST,
        /v1/heartbeat, application/json, the exact token header, NO
        Expect, NO Transfer-Encoding, a correct Content-Length and the
        exact B3-valid C1 JSON body without the token."""
        self.capture.status = 204
        self.capture.requests.clear()
        proc = self._run(trusted=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")
        # Exactly one request attempt.
        self.assertEqual(len(self.capture.requests), 1)
        request = self.capture.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/v1/heartbeat")

        headers = request["headers"]
        assert isinstance(headers, tuple)
        values: dict[str, list[str]] = {}
        for name, value in headers:
            assert isinstance(name, str) and isinstance(value, str)
            values.setdefault(name.lower(), []).append(value)
        self.assertEqual(values.get("content-type"), ["application/json"])
        self.assertEqual(values.get("x-sentinel-token"), [TEST_TOKEN])
        self.assertNotIn("expect", values)
        self.assertNotIn("transfer-encoding", values)

        body = request["body"]
        assert isinstance(body, bytes)
        self.assertEqual(request["content_length_header"], str(len(body)))

        payload = json.loads(body.decode("utf-8"))
        telemetry = decode_heartbeat_payload(payload)
        self.assertEqual(telemetry.host, "Prod")
        self.assertEqual(telemetry.uptime_seconds, c1.UPTIME_EXPECTED)
        self.assertEqual(telemetry.load.one, c1.LOAD_ONE)
        self.assertEqual(telemetry.cpu_percent, c1.CPU_EXPECTED)
        self.assertEqual(telemetry.ram.used, c1.RAM_USED_BYTES)
        self.assertEqual(telemetry.ram.total, c1.RAM_TOTAL_BYTES)
        self.assertEqual(telemetry.swap.used, c1.SWAP_USED_BYTES)
        self.assertEqual(telemetry.swap.total, c1.SWAP_TOTAL_BYTES)
        self.assertEqual(
            telemetry.root_filesystem.used, c1.DF_BYTES_USED
        )
        self.assertEqual(
            telemetry.root_filesystem.total, c1.DF_BYTES_TOTAL
        )
        self.assertEqual(telemetry.root_inodes.used, c1.DF_INODES_USED)
        self.assertEqual(
            telemetry.root_inodes.total, c1.DF_INODES_TOTAL
        )
        self.assertNotIn(TEST_TOKEN.encode(), body)

    def test_real_https_untrusted_tls_fails_closed(self) -> None:
        """Same endpoint WITHOUT any trust mechanism (no shim, no
        CURL_HOME, no CURL_CA_BUNDLE; the real curl resolved directly
        from the default PATH): TLS verification fails, the reporter
        exits non-zero with empty stdout, and the server observes NO
        request — success is not falsely claimed. --insecure is never
        used to make anything pass."""
        self.capture.status = 204
        self.capture.requests.clear()
        proc = self._run(trusted=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertLess(len(proc.stderr), 300)
        self.assertNotIn(TEST_TOKEN, proc.stdout + proc.stderr)
        self.assertEqual(self.capture.requests, [])

    def test_real_https_malicious_curlrc_is_ignored(self) -> None:
        """MANDATORY ambient-config adversarial proof: a TEST-ONLY
        CURL_HOME whose .curlrc contains `--insecure` is demonstrably
        honored by the raw real curl (control: the request is
        delivered), yet the production reporter — which invokes curl
        with `--disable` as the FIRST option — still fails TLS
        verification against this untrusted endpoint, exits non-zero
        with empty stdout and delivers ZERO requests. If the .curlrc
        were read, the reporter would succeed; its failure therefore
        proves ambient curl configuration cannot alter the reporter
        request."""
        self.capture.status = 204
        self.capture.requests.clear()
        # Control: same malicious CURL_HOME, raw real curl WITHOUT
        # --disable — the .curlrc takes effect and TLS verification
        # is bypassed, so the request IS delivered to the server.
        malicious = c1._env_path(self.malicious_curl_home)
        control = subprocess.run(
            [
                c1._bash(),
                "-c",
                "unset CURL_CA_BUNDLE; "
                f"CURL_HOME={_shell_quote(malicious)} curl "
                "--request POST --silent --output /dev/null "
                f"--write-out '%{{http_code}}' "
                f"{_shell_quote(self.endpoint)}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(
            control.returncode, 0, msg=control.stderr
        )
        self.assertEqual(control.stdout, "204")
        self.assertEqual(len(self.capture.requests), 1)
        # Production proof: same malicious CURL_HOME around the real
        # reporter (no shim, no other trust mechanism) — the ambient
        # config is isolated by the reporter's own --disable.
        self.capture.requests.clear()
        env = _transport_env(
            endpoint=self.endpoint,
            token=TEST_TOKEN,
            curl_home=self.malicious_curl_home,
        )
        proc = _run_reporter(env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertLess(len(proc.stderr), 300)
        self.assertNotIn(TEST_TOKEN, proc.stdout + proc.stderr)
        self.assertEqual(self.capture.requests, [])

    def test_real_https_only_204_is_success(self) -> None:
        """On the real verified TLS transport every non-204 status —
        including a 302 redirect — fails, and each run performs
        exactly one request (the redirect is never followed)."""
        for status in (200, 302, 401, 413, 417, 500):
            with self.subTest(status=status):
                self.capture.status = status
                before = len(self.capture.requests)
                proc = self._run(trusted=True)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertNotIn(TEST_TOKEN, proc.stdout + proc.stderr)
                self.assertEqual(
                    len(self.capture.requests), before + 1
                )
        self.capture.status = 204


if __name__ == "__main__":
    unittest.main()
