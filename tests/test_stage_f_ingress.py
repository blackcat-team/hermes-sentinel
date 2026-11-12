"""Stage F1 packaging and opt-in real NGINX/TLS/B5/SQLite acceptance."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time

import pytest
import test_http_server as b5

from hermes_sentinel.deadman import DeadManProbeOutcome
from hermes_sentinel.deadman_probe import DeadManProbeSettings, DeadManProber

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "packaging/nginx/hermes-sentinel.conf.example"
LIVE = pytest.mark.skipif(
    os.environ.get("HERMES_STAGE_F_LIVE") != "1",
    reason="real NGINX acceptance runs in the Stage F Linux lab",
)


def test_ingress_template_keeps_proxy_boundaries() -> None:
    config = TEMPLATE.read_text()
    for directive in (
        "listen 443 ssl;",
        "listen [::]:443 ssl;",
        "ssl_protocols TLSv1.2 TLSv1.3;",
        "error_log /dev/null crit;",
        """log_format sentinel '$status $request_time $upstream_status';""",
        "client_max_body_size 16k;",
        "proxy_request_buffering on;",
        "proxy_pass_request_headers on;",
        "proxy_next_upstream off;",
        "proxy_cache off;",
        "proxy_intercept_errors off;",
        "proxy_pass http://127.0.0.1:8080;",
    ):
        assert directive in config
    for directive in ("include", "error_page", "proxy_method", "proxy_pass_header"):
        assert not any(line.strip().startswith(f"{directive} ") for line in config.splitlines())


@dataclass
class Ingress:
    backend: b5.HttpServerTestCase
    port: int
    certificate: Path
    access_log: Path
    startup_log: Path

    def connect(
        self, hostname: str = "localhost", *, trusted: bool = True
    ) -> ssl.SSLSocket:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if trusted:
            context.load_verify_locations(cafile=str(self.certificate))
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=7)
        try:
            return context.wrap_socket(connection, server_hostname=hostname)
        except BaseException:
            connection.close()
            raise

    def exchange(self, request: bytes) -> bytes:
        with self.connect() as connection:
            connection.sendall(request)
            return b5._read_all(connection)

    def request(self, *, extra_lines: tuple[bytes, ...] = (), **kwargs: object) -> bytes:
        return self.backend._request_bytes(
            extra_lines=(b"Host: localhost", *extra_lines), **kwargs,
        )

    def rows(self) -> int:
        return self.backend._row_count()

    def probe(self) -> DeadManProber:
        return DeadManProber(
            DeadManProbeSettings(f"https://localhost:{self.port}/v1/heartbeat", 3)
        )


@pytest.fixture
def ingress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = b5.HttpServerTestCase()
    with ExitStack() as resources:
        resources.callback(backend.doCleanups)
        backend.setUp()
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-nodes", "-newkey", "ec",
                "-pkeyopt", "ec_paramgen_curve:prime256v1", "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                "-keyout", str(key), "-out", str(cert),
            ],
            check=True, capture_output=True, timeout=10,
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(cert))
        monkeypatch.setenv("NO_PROXY", "*")
        with socket.socket() as free_port:
            free_port.bind(("127.0.0.1", 0))
            port = free_port.getsockname()[1]
        access_log = tmp_path / "access.log"
        startup_log = tmp_path / "startup.log"
        rendered = TEMPLATE.read_text()
        for source, target in (
            ("listen 443 ssl;", f"listen 127.0.0.1:{port} ssl;"),
            ("listen [::]:443 ssl;", f"listen [::1]:{port} ssl;"),
            ("sentinel.example.invalid", "localhost"),
            ("/etc/nginx/hermes-sentinel/fullchain.pem", str(cert)),
            ("/etc/nginx/hermes-sentinel/privkey.pem", str(key)),
            ("/run/nginx.pid", str(tmp_path / "nginx.pid")),
            ("/var/log/nginx/hermes-sentinel-access.log", str(access_log)),
            ("127.0.0.1:8080", f"127.0.0.1:{backend.server_address[1]}"),
        ):
            assert rendered.count(source) == 1
            rendered = rendered.replace(source, target)
        conf = tmp_path / "nginx.conf"
        conf.write_text(rendered)
        command = ["nginx", "-c", str(conf)]
        tested = subprocess.run([*command, "-t"], check=True, capture_output=True, text=True, timeout=10)
        print(tested.stderr.strip())
        effective = subprocess.check_output([*command, "-T"], stderr=subprocess.PIPE, text=True, timeout=10)
        assert effective.split("\n", 1)[1].rstrip() == rendered.rstrip()
        with startup_log.open("wb") as output:
            process = subprocess.Popen(
                [*command, "-g", "daemon off;"], stdout=output, stderr=output,
            )
            try:
                endpoint = Ingress(backend, port, cert, access_log, startup_log)
                for _ in range(40):
                    assert process.poll() is None, startup_log.read_text()
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                            break
                    except ConnectionRefusedError:
                        time.sleep(0.05)
                else:
                    pytest.fail("NGINX did not start")
                yield endpoint
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@LIVE
def test_real_tls_reporter_and_deadman_probe(ingress: Ingress) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        ingress.connect(trusted=False)
    with pytest.raises(ssl.SSLCertVerificationError):
        ingress.connect("wrong.example.invalid")
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.2", ingress.backend.server_address[1]), timeout=1)
    with socket.create_connection(("127.0.0.1", ingress.port), timeout=2) as plaintext:
        plaintext.sendall(ingress.request())
        assert b5._parse_response(b5._read_all(plaintext))[0] == 400
    assert ingress.rows() == 0

    probe_request = ingress.request(method="GET", body=b"", include_content_length=False, headers=())
    status, headers, body = b5._parse_response(ingress.exchange(probe_request))
    assert status == 405 and ("Allow", "POST") in headers and body == b""
    assert ingress.probe().probe() is DeadManProbeOutcome.HEALTHY
    assert ingress.rows() == 0
    print("verified GET 405, Allow POST, empty body, H1B healthy, SQLite +0")

    reporter = subprocess.run(
        ["bash", str(ROOT / "scripts/sentinel-report.sh")],
        env={
            "PATH": os.environ["PATH"],
            "SENTINEL_NODE": "vds-01",
            "SENTINEL_TOKEN": b5._TOKEN_ONE,
            "SENTINEL_ENDPOINT": f"https://localhost:{ingress.port}/v1/heartbeat",
            "CURL_CA_BUNDLE": str(ingress.certificate),
            "NO_PROXY": "*",
        },
        capture_output=True, timeout=25,
    )
    assert (reporter.returncode, reporter.stdout, reporter.stderr) == (0, b"", b"")
    assert ingress.rows() == 1
    assert any(
        line.startswith("204 ") and line.endswith(" 204")
        for line in ingress.access_log.read_text().splitlines()
    )
    print("production reporter: verified TLS POST 204, SQLite +1")

    ingress.backend.server.shutdown()
    ingress.backend.server.server_close()
    assert ingress.probe().probe() is DeadManProbeOutcome.FAILED
    assert b5._parse_response(ingress.exchange(probe_request))[0] == 502
    assert ingress.rows() == 1
    print("upstream stopped: GET 502, H1B failed, SQLite +0")


@LIVE
def test_malformed_requests_leave_sqlite_unchanged(ingress: Ingress) -> None:
    secret = b5._TOKEN_ONE.encode()
    query = b"synthetic-query-marker"
    body_marker = b"synthetic-body-marker"
    payload = b5._payload_bytes()
    cases = (
        ("empty Expect", ingress.request(extra_lines=(b"Expect:",)), {417}),
        ("OWS Expect", ingress.request(extra_lines=(b"Expect: \t ",)), {417}),
        ("mixed-case Expect", ingress.request(extra_lines=(b"eXpEcT:",)), {417}),
        ("duplicate Expect", ingress.request(extra_lines=(b"Expect:", b"eXpEcT:")), {400, 417}),
        ("arbitrary Expect", ingress.request(extra_lines=(b"Expect: arbitrary",)), {417}),
        ("100-continue headers only", ingress.request(extra_lines=(b"Expect: 100-continue",))[:-len(payload)], {417}),
        ("Expect and TE+CL", ingress.request(extra_lines=(b"Expect:", b"Transfer-Encoding: chunked")), {400, 417}),
        ("TE and CL", ingress.request(extra_lines=(b"Transfer-Encoding: chunked",)), {400}),
        ("chunked valid payload", ingress.request(
            extra_lines=(b"Transfer-Encoding: chunked",), include_content_length=False,
            body=b"%x\r\n" % len(payload) + payload + b"\r\n0\r\n\r\n",
        ), {400}),
        ("missing CL", ingress.request(include_content_length=False), {411}),
        ("duplicate CL", ingress.request(extra_lines=(f"Content-Length: {len(payload)}".encode(),)), {400}),
        ("invalid CL", ingress.request(content_length="+123"), {400}),
        ("oversize", ingress.request(content_length=16385, body=b""), {413}),
        ("colonless header", ingress.request(extra_lines=(b"NoColon",)), {400}),
        ("invalid header name", ingress.request(extra_lines=(b"Invalid/Header: value",)), {400}),
        ("malformed JSON", ingress.request(body=body_marker), {400}),
        ("wrong token", ingress.request(headers=(("Content-Type", "application/json"), ("X-Sentinel-Token", "synthetic-wrong-token"))), {401}),
        ("trailing token space", ingress.request().replace(secret + b"\r\n", secret + b" \r\n"), {400, 401}),
        ("trailing token tab", ingress.request().replace(secret + b"\r\n", secret + b"\t\r\n"), {400, 401}),
        ("duplicate token", ingress.request(extra_lines=(b"x-sentinel-token: " + secret,)), {401}),
        ("duplicate media type", ingress.request(extra_lines=(b"content-type: application/json",)), {400}),
        ("query", ingress.request(target="/v1/heartbeat?" + query.decode()), {404}),
        ("empty query", ingress.request(target="/v1/heartbeat?"), {404}),
        ("encoded path", ingress.request(target="/v1/%68eartbeat"), {404}),
        ("alias", ingress.request(target="/v1/extra/../heartbeat"), {404}),
        ("repeated leading slash", ingress.request(target="//v1/heartbeat"), {404}),
        ("repeated slash and Expect", ingress.request(
            target="//v1/heartbeat", extra_lines=(b"Expect:",),
        ), {404}),
        ("target whitespace", ingress.request(target="/v1/heartbeat").replace(
            b"heartbeat HTTP", b"heartbeat\xa0 HTTP",
        ), {400, 404}),
        ("absolute target", ingress.request(target="https://localhost/v1/heartbeat"), {404}),
    )
    direct = ingress.backend._exchange(cases[0][1])
    assert b5._parse_response(direct)[0] == 417 and ingress.rows() == 0
    for label, request, expected in cases:
        response = ingress.exchange(request)
        status, _, _ = b5._parse_response(response)
        assert status in expected, label
        assert ingress.rows() == 0, label
        assert all(marker not in response for marker in (secret, query, body_marker)), label
        print(f"{label}: HTTP {status}, SQLite +0")

    assert b5._parse_response(ingress.exchange(ingress.request()))[0] == 204
    assert ingress.rows() == 1
    logs = ingress.access_log.read_bytes() + ingress.startup_log.read_bytes()
    assert all(marker not in logs for marker in (secret, query, body_marker))
    assert all(
        re.fullmatch(r"\d{3} \d+\.\d{3} (?:\d{3}|-)", line)
        for line in ingress.access_log.read_text().splitlines()
    )


@LIVE
def test_incomplete_body_is_buffered_and_connections_are_bounded(ingress: Ingress) -> None:
    incomplete = ingress.request(content_length=1000, body=b"{")
    start = time.monotonic()
    with ingress.connect() as slow:
        slow.sendall(incomplete)
        time.sleep(0.15)
        status, _, _ = b5._parse_response(ingress.backend._exchange(ingress.request(), timeout=2))
        assert status == 204 and time.monotonic() - start < 2
        response = b5._read_all(slow)
        if response:
            assert b5._parse_response(response)[0] == 408
    assert 4 <= time.monotonic() - start < 7
    assert ingress.rows() == 1
    print("incomplete body timed out at ingress, direct B5 control 204, SQLite +0 for incomplete")

    with ExitStack() as held:
        for _ in range(16):
            connection = held.enter_context(ingress.connect())
            connection.sendall(incomplete)
        time.sleep(0.15)
        assert b5._parse_response(ingress.exchange(ingress.request()))[0] == 429
    assert ingress.rows() == 1
    print("17th active request from the same address: HTTP 429, SQLite +0")


@LIVE
def test_shared_nat_burst_and_rate_limit(ingress: Ingress) -> None:
    request = ingress.request()

    def status(_: int) -> int:
        return b5._parse_response(ingress.exchange(request))[0]

    with ThreadPoolExecutor(max_workers=4) as clients:
        normal = list(clients.map(status, range(10)))
        excess = list(clients.map(status, range(90)))
    assert normal == [204] * 10
    assert set(excess) == {204, 429}
    assert ingress.rows() == (normal + excess).count(204)
    print(
        f"shared NAT: {len(normal)} accepted in normal burst, "
        f"{excess.count(429)} rate-limited with SQLite +0"
    )
