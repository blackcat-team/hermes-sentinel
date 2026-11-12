# Hermes Sentinel production hardening (Stage F)

## F1. HTTPS ingress

Use the complete [NGINX configuration](../packaging/nginx/hermes-sentinel.conf.example)
for a dedicated ingress instance on the central host. Install the distribution's
ABI-matched `libnginx-mod-http-lua` package and its NDK dependency. Load both
modules from the paths in the template. Record the NGINX build, Lua/NDK package
versions and effective configuration digest. Test that exact combination
before use and after any package or configuration change.

Render the public DNS name, certificate chain and private-key paths and the
loopback port. Keep the private key root-readable only. The template offers
TLS 1.2/1.3 with HTTP/1.x. It listens on TCP 443 for IPv4 and IPv6. Permit
ingress only on the approved HTTPS listener. Keep the B5 listener on
`127.0.0.1` and deny direct backend access at the IPv4 and IPv6 firewalls,
including through any port forwarding or container publishing. Verify the
listener locally and test backend reachability from outside the central host.

Check `sudo nginx -t` and review `sudo nginx -T` before activating the
configuration. Use the dedicated instance so there is no inherited rewrite,
custom error page, error interception, upstream retry, cache or request
logging from another virtual host. Keep external port 80 closed. Use the
client's normal CA and hostname verification for the exact
`https://<approved-host>/v1/heartbeat` URL.

The ingress preserves the origin-form path, token and buffered body on the
ordinary proxy path. The perimeter admits only the exact origin-form target
so URI normalization cannot turn an alias into a heartbeat. B4 owns backend
route and method semantics and B3 owns authentication. The Lua check runs
before proxy normalization on the heartbeat POST. It uses structured header presence to reject `Expect`
(including empty) and `Transfer-Encoding` and requires `Content-Length`.
It checks the bounded original header lines for malformed fields and rejects a
token with trailing whitespace before NGINX can trim it. The NGINX parser can
reject conflicting framing earlier. A rejected request must produce no
SQLite row. The unauthenticated H1B GET receives the real upstream 405 with
`Allow: POST` and an empty body. It must fail when B5 stops.

The profile buffers at most 16 KiB per body before proxying. Limits are
20 requests/s plus a 40-request burst and 16 active requests per source IP,
and 100 requests/s plus a 100-request burst and 64 active requests across the
vhost. Excess admitted headers get 429. Source IP is the immediate peer.
Check the largest expected simultaneous reporter batch behind a shared NAT
and stagger timers if necessary. In-flight connections with incomplete
headers are governed by the NGINX worker connection budget (512, including
upstream connections) and header deadline, before the per-IP active request
counter applies. The header deadline is 5 s, the upstream connect timeout
is 1 s, and body, send and upstream read/write idle timeouts are 5 s.
A client trickling bytes below the idle timeout can retain an ingress slot.
Full buffering keeps that request off the serial B5 listener.

Access logs contain only response status, elapsed seconds and upstream status.
NGINX error logs go to `/dev/null` because even parser and upstream errors
can include raw request data. Use the numeric access categories and service
status for diagnosis. Restrict access and retention for logs and review
effective logging before enabling the listener.

For ingress rollback, restore the previous approved TLS configuration and
validate it before reloading. Close public ingress if a verified previous
configuration is unavailable. Keep B5 bound to loopback and preserve SQLite.

## Local Linux acceptance

Run the canonical `python tools/verify.py` with Python 3.11 and the pinned
development dependencies. For the supplemental ingress tests, extend the
existing local Stage F image with the distribution Lua module. The build
fetches distribution packages. The test container uses only its loopback,
synthetic credentials and certificate, and disposable SQLite.

```bash
set -o pipefail
lab_dir=$(mktemp -d)
cat > "$lab_dir/Dockerfile" <<'DOCKERFILE'
FROM blackcat-hermes-stage-f:local
RUN apt-get update && apt-get install -y --no-install-recommends libnginx-mod-http-lua \
    && rm -rf /var/lib/apt/lists/*
DOCKERFILE
docker build --pull=false -t hermes-sentinel-stage-f-lab:local "$lab_dir"
rm -r "$lab_dir"

candidate=$(git rev-parse HEAD)
git archive "$candidate" | docker run --rm --network none -i \
    hermes-sentinel-stage-f-lab:local /bin/sh -ec '
        mkdir /candidate
        cd /candidate
        tar -xf -
        nginx -v
        dpkg-query -W nginx libnginx-mod-http-lua libnginx-mod-http-ndk
        HERMES_STAGE_F_LIVE=1 python3.11 -m pytest -q -s tests/test_stage_f_ingress.py
    '
```

Retain the candidate SHA, image ID, package versions, configuration digest
and status/SQLite row-delta output. The suite validates the rendered effective
configuration and uses the real reporter and H1B prober with verified TLS.
It covers refusal cases, incomplete bodies, rate and connection limits, and
the response to loss of B5. External reachability is checked during rollout.
