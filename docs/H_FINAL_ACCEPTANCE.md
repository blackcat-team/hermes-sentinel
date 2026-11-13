# Hermes Sentinel v1.0 deployment and H-FINAL acceptance

Use this procedure for the single live acceptance window after the
Architect approves the candidate SHA and authorizes the window. The linked
runbooks supply the command details for the steps below. Record the results
in one release record. The Operator executes each step locally on the
designated host.

## 1. Prepare the release and acceptance window

- Approve one full Git SHA, the candidate fingerprint against the accepted
  Stage F base, the canonical verification, the independent QA evidence and
  the successful GitHub Actions `CI / verify` run for that exact head SHA.
- Assign the central, reporter and independent dead-man hosts and the
  designated test node. Record private inventory, maintenance windows,
  operator roles, out-of-band access, expected reporter burst, thresholds,
  alert destinations and stop deadlines. Use separate Stage E and Stage H
  bots and per-node reporter tokens. Confirm synchronized clocks and verified
  TLS using the approved trust stores. The dead-man must have independent
  compute and network/power failure domains.
- Keep the dead-man timer stopped throughout central installation and
  acceptance. Agree who can approve the central gate and who can execute
  cleanup. Open the release record with all live gates pending.
- Record the previous approved release SHA and artifact digests, or the
  pre-install state for a first deployment. In a private archive keep the
  prior application, systemd units/drop-ins, reporter scripts, NGINX
  configuration, TLS files and root-only environment files. Back up existing
  SQLite via its backup API and verify the backup integrity. Back up any
  existing dead-man ledger while its timer and oneshot are stopped. Record
  backups and rollback decisions by private reference.

### Build once from the reviewed SHA

In a clean trusted checkout detached at `APPROVED_SHA`, run the
[Stage F SHA preflight](PRODUCTION_HARDENING.md#preflight-and-installation).
Use Python 3.11 with the project build tools available, including setuptools
as declared in `pyproject.toml` and wheel. Record their versions. Set
`RELEASE_DIR` to a new absolute directory outside the checkout. Run this
block in Bash from the repository root.

<!-- release-build -->
```bash
(
    set -euo pipefail
    test "$(git rev-parse --verify HEAD)" = "${APPROVED_SHA:?}"
    test -z "$(git status --porcelain)"
    case "${RELEASE_DIR:?}" in /*) ;; *) exit 1 ;; esac
    mkdir -- "$RELEASE_DIR"
    build_dir=$(mktemp -d)
    trap 'rm -rf -- "$build_dir"' EXIT
    git archive --format=tar --output="$RELEASE_DIR/source.tar" "$APPROVED_SHA"
    tar -xf "$RELEASE_DIR/source.tar" -C "$build_dir"
    SOURCE_DATE_EPOCH=$(git show -s --format=%ct "$APPROVED_SHA")
    export SOURCE_DATE_EPOCH
    python3.11 -m pip wheel --no-build-isolation --no-deps --no-index \
        --wheel-dir "$RELEASE_DIR" "$build_dir"
    printf '%s\n' "$APPROVED_SHA" > "$RELEASE_DIR/SOURCE_SHA"
    cd "$RELEASE_DIR"
    sha256sum SOURCE_SHA source.tar hermes_sentinel-1.0.0-py3-none-any.whl > SHA256SUMS
    sha256sum SHA256SUMS
)
```

Record the printed manifest SHA-256 and the wheel/source digests in the
reviewed release record. Deliver the four files to each host's operator
using the trusted release channel. The manifest digest and approved Git
SHA must come from the reviewed record. Reuse the same wheel bytes on
central and dead-man, and use the verified source archive for all scripts,
unit files and the NGINX template.

### Verify on each installation host

Set `RELEASE_DIR`, `APPROVED_SHA`, `APPROVED_MANIFEST_SHA256` and
`SOURCE_DIR` (a new absolute directory for extraction). Use an absolute path for
`RELEASE_DIR`. Keep the release
and extraction directories under operator control. Run this block in Bash
and continue only on exit zero. For rollback, use the same artifact and SHA
checks for a previous release prepared this way, or verify the archived
legacy artifacts against their recorded digests.

<!-- release-verify -->
```bash
(
    set -euo pipefail
    case "${SOURCE_DIR:?}" in /*) ;; *) exit 1 ;; esac
    cd "${RELEASE_DIR:?}"
    printf '%s  SHA256SUMS\n' "${APPROVED_MANIFEST_SHA256:?}" | sha256sum --check --strict
    sha256sum --check --strict SHA256SUMS
    test "$(cat SOURCE_SHA)" = "${APPROVED_SHA:?}"
    test "$(git get-tar-commit-id < source.tar)" = "$APPROVED_SHA"
    mkdir -- "$SOURCE_DIR"
    tar -xf source.tar -C "$SOURCE_DIR"
)
```

This verification needs Git, tar and sha256sum on each operator host.
After it passes, set `RELEASE_WHEEL` for the central and dead-man
installation steps to `$RELEASE_DIR/hermes_sentinel-1.0.0-py3-none-any.whl`.
Record per-role manifest verification and installed-file matches. Use
`cmp -s` for each installed script, service, timer and drop-in against
`SOURCE_DIR`. For each wheel installation, use the exact artifact path
with `pip install --no-index --no-deps --force-reinstall`. Compare installed
`hermes_sentinel` with `SOURCE_DIR/src/hermes_sentinel`, ignoring
`__pycache__`. Record the rendered and effective NGINX digests separately.

## 2. Install central, then HTTPS ingress and reporters

1. Keep the public listener closed to traffic until the local central
   checks pass. For updates stop the affected units and wait for active
   oneshots before replacing artifacts. Apply the
   [central installation](SENTINEL_DEPLOYMENT.md#installation) from
   `SOURCE_DIR` with the verified wheel. Preserve an existing environment
   file and edit it locally for reviewed configuration changes. Install the
   [Stage F drop-in](PRODUCTION_HARDENING.md#f2-central-service-confinement).
   Verify the effective unit and identity, enable central, verify the local
   listener on `127.0.0.1`, and verify that SQLite is accessible only to the
   intended identity and operator.
2. Render and validate the [dedicated HTTPS ingress](PRODUCTION_HARDENING.md#f1-https-ingress)
   from the verified template using the approved NGINX/Lua/NDK build and TLS
   material. Run staged and effective `nginx -t`, then activate. From a
   separate client, verify public TLS with normal CA/hostname checks and the
   genuine backend `GET /v1/heartbeat` signature (405, Allow includes POST,
   empty body). Confirm the B5 port and port 80 are closed externally over
   each advertised address family. Retain numeric ingress status and the
   effective configuration digest.
3. Install the [reporter](REPORTER_DEPLOYMENT.md#installation) from the
   verified source on each reporter host. Preserve existing protected
   configuration on update. With the controlled timer stopped, use the
   [per-node SQLite observation](SENTINEL_DEPLOYMENT.md#read-only-heartbeat-evidence)
   before and after one successful reporter oneshot. Require HTTPS 204 and
   exactly one new row for that node. Enable the timers, observe fresh
   reports through each and exercise the expected shared-NAT burst while
   monitoring is active.

## 3. Accept MVP runtime and Stage F before enabling the dead-man

1. Execute the [MVP observations](PRODUCTION_HARDENING.md#mvp-runtime-observations)
   on the controlled reporter node. Establish HEALTHY. Pause reporting long
   enough to prove stale + reachable gives DEGRADED without an alert. While
   still stale, make the approved TCP target unreachable. Observe exactly
   one real Stage E DOWN at the configured confirmation threshold. Hold the
   condition for a later evaluation and check for duplicate delivery. Restore
   reachability and reporting, observe exactly one real RECOVERED at the
   recovery threshold, and verify freshness resumes. Record the TCP and
   SQLite observations and the Telegram receipt times.
2. Execute [Stage F faults, rotation and rollback](PRODUCTION_HARDENING.md#stage-f-fault-rotation-and-rollback-observations)
   and [confinement and supervision](SENTINEL_DEPLOYMENT.md#confinement-and-supervision).
   Capture bounded fault/status and row-delta evidence, effective service
   privileges and egress, SQLite durability, credential and TLS rotation,
   and the validated rollback path. If exercising a prior profile as part of
   the rollback drill, return to the candidate and reverify its installed
   artifacts and health before completing the gate. Close every injected
   fault and remove all temporary confinement fixtures.
3. Obtain independent review of the live MVP and F results and the
   Architect's recorded central acceptance for the same SHA. Stop here if
   either gate remains pending or failed. Central and the reporters must
   be healthy before beginning H2.

## 4. Install and accept H2 on the independent host

Verify the same release manifest on the independent host and use the
[H2 runbook](DEADMAN_DEPLOYMENT.md) in order. Install the shared wheel and
matching units under the dedicated dead-man identity. Verify healthy GET,
the one-time oneshot and the timer-driven UP baseline. Confirm the state
file mode, secret-safe diagnostics, no inbound listener and separation
from Stage E credentials. Confirm the live Sentinel path is direct and
its DNS addresses can be isolated without affecting Telegram.

Run the bounded dual-stack isolation against the configured HTTPS port
from the dead-man host. Central stays up during H2. Count three distinct
consecutive failed timer invocations. Require one real Stage H DOWN with
same-cycle acknowledgement and an empty pending queue, then one later
isolated cycle without duplicate delivery. Remove the dedicated table and
verify the public GET signature is healthy again. Count two distinct
timer-driven successes, one real Stage H RECOVERED with an empty pending
queue, and a later healthy cycle without duplicate delivery. Complete the
durability check across a timer restart and retain the redacted evidence.
Confirm that central reporters continue writing fresh heartbeats.

## 5. Abort and roll back

- On any H2 fault or timeout, remove the temporary isolation first and
  prove absence. Follow the [H2 cleanup](DEADMAN_DEPLOYMENT.md#10-rollback--abort),
  stop the timer and preserve the ledger. If central needs rollback after
  H2 starts, finish this cleanup and quiesce dead-man before touching central.
- On a central failure, restore the controlled TCP target, stop the affected
  reporter timers and close public ingress as needed. Restore the verified
  prior wheel, unit/drop-in, reporter, configuration and certificate
  artifacts as applicable. On first installation, stop/disable new units and
  close the listener. Keep live SQLite in place and use its backup only for
  authorized data recovery after checking compatibility and loss of newer
  observations. Preserve the dead-man ledger during a binary rollback too.
- Before returning traffic, verify effective units, loopback bind, TLS,
  reporter HTTP 204/+1 and timer freshness for the restored profile. Keep
  ingress closed if verification fails. Record the rollback SHA (or
  pre-install state), cleanup and remaining impact. Restart dead-man only
  once central is accepted for the restored profile. A candidate that
  required an abort stays open until it passes a new authorized window.

## 6. H-FINAL evidence and completion criteria

Use the [Stage F observation format](PRODUCTION_HARDENING.md#acceptance-record)
and the [H2 evidence checklist](DEADMAN_DEPLOYMENT.md#9-evidence-contract-operator---architect).
Export UTC observations, redacted source aliases, expected and actual
results, artifact references and SHA-256 digests. Keep raw logs, config,
addresses, identities and credential material in the private operator
archive. Supply only sanitized results and receipt timestamps. Record
failed or missing checks explicitly.

<!-- h-final-evidence-template -->
```json
{
  "candidate_sha": null,
  "stage_f_base_sha": "1d2e92ccf3aa6e95e212218a7a2ccdc50b42d8f0",
  "candidate_diff_hash": null,
  "manifest_sha256": null,
  "wheel_sha256": null,
  "source_archive_sha256": null,
  "ci": {"workflow": "CI", "job": "verify", "head_sha": null, "run_url": null, "result": "open"},
  "qa_evidence_id": null,
  "installed_artifacts": [],
  "observations": [],
  "mvp_runtime": {"status": "open", "evidence_id": null, "reviewer_id": null},
  "stage_f": {"status": "open", "evidence_id": null, "reviewer_id": null},
  "central_acceptance": {"status": "open", "accepted_at_utc": null, "architect_id": null},
  "h2": {"status": "open", "evidence_id": null, "reviewer_id": null},
  "rollback": {"previous_sha": null, "evidence_id": null, "outcome": "open"},
  "final_health_and_cleanup": {"status": "open", "evidence_id": null},
  "h_final": {"status": "open", "decision_evidence_id": null, "accepted_at_utc": null, "architect_id": null},
  "project_complete": false
}
```

H-FINAL can close only when the Architect accepts the independent live
review of MVP, F and H2 and signs the record for the same CI-green SHA.
Require installed artifact matches for central, ingress, every reporter and
dead-man, the recorded central approval before dead-man activation, both
real DOWN/RECOVERED pairs, a verified rollback path, removal of all test
isolation, restored reporter freshness, active timers and a healthy public
GET. Any missing evidence, SHA mismatch, unresolved failure or unapproved
rollback leaves H-FINAL open. Set `project_complete` only after recording
the Architect's final acceptance.
