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

## F2. Central service confinement

Install the [drop-in](../packaging/systemd/hermes-sentinel.service.d/10-hardening.conf)
alongside the E9 service on Ubuntu. Use the dedicated service user, the
root-managed application and environment file, the loopback bind and a
database path inside the service's StateDirectory.

```bash
sudo install -d -o root -g root -m 0755 \
    /etc/systemd/system/hermes-sentinel.service.d
sudo install -o root -g root -m 0644 \
    packaging/systemd/hermes-sentinel.service.d/10-hardening.conf \
    /etc/systemd/system/hermes-sentinel.service.d/10-hardening.conf
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/hermes-sentinel.service
sudo systemctl cat hermes-sentinel.service
```

The base unit supplies `StateDirectory=hermes-sentinel`, which systemd leaves
writable under `ProtectSystem=strict`. The service's private temporary
directories and shared-memory filesystem are read-only when present.
Home directories and physical devices are hidden. The service has empty
bounding and ambient capability sets, cannot create namespaces, and may
open UNIX, IPv4 and IPv6 sockets. System CA and resolver files are readable.
Check the actual application path, identity, state directory and egress
before accepting the effective unit.

In the target service context, prove that SQLite remains writable and
survives restart. Check that a write to an operator-prepared, otherwise
writable path outside the state directory and a read of an otherwise
readable home sentinel file both fail. Verify the process capability masks
in `/proc/<MainPID>/status`. Confirm actual DNS, configured TCP probes and
outbound Telegram TLS. Exercise normal SIGTERM and abnormal exit using the
supervision checks in the [central runbook](SENTINEL_DEPLOYMENT.md).

For rollback, remove only
`/etc/systemd/system/hermes-sentinel.service.d/10-hardening.conf`, restore the
previous approved configuration, reload systemd and verify the effective
unit before restart. Preserve SQLite and keep the public backend closed.

## Local Linux acceptance

Run the canonical `python tools/verify.py` with Python 3.11 and the pinned
development dependencies. For the supplemental ingress tests, extend the
existing local Stage F image with the distribution Lua module. The build
fetches distribution packages. The test container uses only its loopback,
synthetic credentials and certificate, and disposable SQLite.

```bash
set -euo pipefail
lab_dir=$(mktemp -d)
cat > "$lab_dir/Dockerfile" <<'DOCKERFILE'
FROM blackcat-hermes-stage-f:local
RUN apt-get update && apt-get install -y --no-install-recommends libnginx-mod-http-lua \
    && rm -rf /var/lib/apt/lists/*
DOCKERFILE
docker build --pull=false -t hermes-sentinel-stage-f-lab:local "$lab_dir"
rm -r "$lab_dir"
docker image inspect --format '{{.Id}}' hermes-sentinel-stage-f-lab:local

candidate=$(git rev-parse HEAD)
git archive "$candidate" | docker run --rm --network none -i \
    hermes-sentinel-stage-f-lab:local /bin/sh -ec '
        mkdir /candidate
        cd /candidate
        tar -xf -
        nginx -v
        dpkg-query -W nginx libnginx-mod-http-lua libnginx-mod-http-ndk
        systemd-analyze --version
        python3.11 -m venv --system-site-packages /opt/hermes-sentinel/venv
        /opt/hermes-sentinel/venv/bin/pip install --no-build-isolation --no-deps .
        HERMES_STAGE_F_LIVE=1 python3.11 -m pytest -q -s \
            tests/test_stage_f_ingress.py tests/test_stage_f_systemd.py
    '
```

Retain the candidate SHA, image ID, package versions, configuration digest
and status/SQLite row-delta output. The suite validates the rendered effective
configuration and uses the real reporter and H1B prober with verified TLS.
It covers refusal cases, incomplete bodies, rate and connection limits, and
the response to loss of B5. External reachability is checked during rollout.

The systemd suite inspects the packaged unit and drop-in and uses an invalid
ExecStart as a negative control. Exercise the effective sandbox and lifecycle
under the target Ubuntu service manager during rollout.

## F3. Operator acceptance

Use one approved candidate SHA throughout. Keep MVP runtime and Stage F
acceptance open until each has its own complete observations and independent
review. Schedule a bounded window and designate a controlled reporter node,
its TCP target, the central host and the Stage E Telegram destination. Agree
on the thresholds, largest shared-NAT burst, outage and recovery deadlines,
and the point at which to restore the previous approved release.

### Preflight and installation

Confirm successful GitHub Actions `CI` job `verify` for the exact candidate
and retain the run ID and URL. Use a clean local checkout from the trusted
source on each installation host. Set `APPROVED_SHA` to the full reviewed
commit ID supplied by the operator. Run this preflight before building or
copying installation files.

<!-- stage-f-sha-preflight -->
```bash
test -n "${APPROVED_SHA:-}" &&
candidate_head=$(git rev-parse --verify HEAD) &&
checkout_status=$(git status --porcelain) &&
test "$candidate_head" = "$APPROVED_SHA" &&
test -z "$checkout_status" ||
    { printf '%s\n' 'Stage F candidate or checkout mismatch' >&2; exit 1; }
```

Record the source SHA and artifact SHA-256 at build and installation.
Check the installed reporter against `scripts/sentinel-report.sh` with
`cmp -s`. For central, install the verified wheel from that checkout into
the root-managed venv and compare its installed `hermes_sentinel` package
with `src/hermes_sentinel`, ignoring `__pycache__`. A source SHA alone
does not identify a previously installed executable.

1. Run the local Linux acceptance above on that SHA. Record NGINX/module
   and systemd versions, the image ID and checksums.
2. Inventory and preserve the last approved wheel, reporter and packaging,
   root-only configuration, and a verified SQLite backup made with the
   SQLite backup API. Keep this material in the private operator archive.
3. Follow the [central runbook](SENTINEL_DEPLOYMENT.md) to install E9,
   then install the F2 drop-in. Verify identity, permissions and effective
   unit. Start central and confirm its loopback listener and writable state
   before exposing ingress.
4. Render F1 into a root-controlled staging file. Run
   `sudo nginx -t -c /absolute/path/to/staged-nginx.conf`, install the
   reviewed config as `/etc/nginx/nginx.conf` for the dedicated instance,
   and validate the effective configuration again with `sudo nginx -t`.
   Start or reload `nginx.service` only after both checks pass. Test the
   public TLS name and chain from a separate client with ordinary CA
   verification, and confirm the B5 port is unreachable over each
   advertised address family. Run a valid GET and inspect the effective
   limits and safe logging.
5. Use the [reporter runbook](REPORTER_DEPLOYMENT.md) for the controlled
   oneshot and timer. Compare the node's SQLite count immediately before
   and after the oneshot with its timer stopped. A successful oneshot,
   HTTP 204 at ingress and exactly one additional row are required.
   Enable the timer and confirm another fresh observation through it.

### MVP runtime observations

Exercise the following on the controlled node with the same central
process and its configured monitoring cadence. Record UTC windows, central
receive timestamps and real TCP observations from the central side. Use
the deployed D1 functions (`evaluate_heartbeat_freshness` and
`evaluate_resource_thresholds` in `hermes_sentinel.health`) with the central
clock, the actual latest stored record and the configured settings. Feed
those results and the observed TCP reachability to `resolve_host_state`
in `hermes_sentinel.state_resolver`. Preserve the previous resolution
between observations within the sequence. This supplies evidence for the
quiet states. Correlate actual Stage E Telegram messages with the DOWN
and RECOVERED windows.

| Controlled observations | Required result |
| --- | --- |
| Fresh heartbeat, reachable TCP and resources within thresholds | HEALTHY evidence and no transition notification |
| Fresh heartbeat with a resource breach, or stale heartbeat with reachable TCP | DEGRADED evidence and no DOWN notification |
| Pause the controlled reporter until stale, then make its approved TCP target unreachable | DEGRADED until the configured consecutive DOWN confirmations, then one DOWN in Stage E Telegram |
| Restore TCP and reporting | One RECOVERED in Stage E Telegram on the configured consecutive TCP successes. With fresh, clear telemetry the result is HEALTHY, otherwise DEGRADED |

Restore the timer and TCP target at the end of the window. Check that the
latest SQLite receive time advances again. Use a fresh controlled incident
for subsequent notification checks. Stage D state memory is process-local.

### Stage F fault, rotation and rollback observations

- Verify certificate/hostname failure, closed external B5, malformed
  framing and `Expect`, wrong or duplicate token, exact URI, size/rate
  limits, an incomplete body and upstream outage. Use the Docker matrix
  for the destructive load and parser cases. On the target use the approved
  bounded cases and record response status, row delta, and release of any
  deliberately held connections.
- Run the F2 service-context checks and central supervision procedure.
  Record actual DNS/TCP/TLS egress, failed write and home read, empty
  capabilities, normal SIGTERM, abnormal restart, invalid environment
  failure and recovery with the same SQLite state.
- Rotate one reporter token with both files coordinated in one window
  as described in the reporter runbook. Confirm 204/+1 with the new
  value and 401/+0 with the old value. The final state uses only the
  accepted replacement and has a working timer.
- Rotate Stage E Telegram credentials in the central root-only environment
  file and verify delivery with a new controlled transition after restart.
  Stage H credentials belong only on the independent dead-man host under
  its [runbook](DEADMAN_DEPLOYMENT.md). Track both credential IDs privately
  and revoke superseded credentials after the corresponding verification.
- For TLS rotation, stage the new chain/key with the same protected
  ownership. Validate with `nginx -t`, reload and independently verify
  the served chain, hostname and validity window. Repeat the reporter POST
  and the genuine unauthenticated GET. Retain the prior approved config and
  usable certificate/key for rollback until the new pair is verified.

For rollback, stop the test reporter timer and close public ingress if
needed. Restore the previous approved wheel, reporter and affected
configuration by recorded digest. Reinstall the matching units, remove
only the F2 drop-in if rolling it back, then reload and inspect systemd.
Keep the live SQLite database in place. Restore a backup only for actual
data recovery after the operator checks schema compatibility and the
consequences for newer observations. Revalidate the previous TLS profile,
loopback bind, HTTPS POST and row count before re-enabling the timer.
Confirm cleanup of the controlled failure and capture the rollback SHA,
checks and restored state. If the previous profile cannot pass, keep
public ingress closed.

### Acceptance record

Use one entry per check with UTC time, a safe observation source ID,
expected and actual result, and a private artifact reference and SHA-256.
Store raw private evidence with the operator. Export numeric HTTP
categories, SQLite row deltas and freshness, systemd state/exit status
and restart counts, and the presence/time of Telegram delivery. Review
every exported value for redaction. Exclude tokens, environment contents,
request bodies, raw queries, journal messages, addresses, and real
node/chat/topic identifiers. Do not record a successful gate when an
observation is missing, contradictory, or tied to a different SHA.

<!-- stage-f-evidence-template -->
```json
{
  "candidate_sha": "<full reviewed Git SHA>",
  "deployment_profile_id": "<redacted profile ID>",
  "ci": {"workflow": "CI", "job": "verify", "run_id": null, "url": null, "head_sha": null, "result": "open"},
  "installed_artifacts": [],
  "observations": [],
  "observation_fields": ["check", "utc", "source_id", "expected", "actual", "artifact_id", "sha256"],
  "mvp_runtime": {"status": "open", "reviewer_id": null},
  "stage_f": {"status": "open", "reviewer_id": null},
  "rollback": {"previous_sha": null, "procedure_evidence_id": null, "outcome": "open"},
  "operator_id": null,
  "h2": {"status": "open", "evidence_id": null}
}
```

MVP evidence covers the live heartbeat and Stage E transition path. Stage F
evidence covers ingress, confinement, supervision, rotation, redaction and
rollback at the same SHA. H2 follows accepted Stage F and requires its own
live evidence and independent review. Follow the
[dead-man deployment runbook](DEADMAN_DEPLOYMENT.md) for that separate gate.
