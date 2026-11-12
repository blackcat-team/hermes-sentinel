# Central Sentinel Deployment Runbook (Stage E9)

This is an **operator runbook** for installing the already-accepted
central Hermes Sentinel process as a local systemd service on the
central Ubuntu host. It documents local operator actions only — it is
not deployment automation, and the project contains no installer
script, no deployer and no remote-execution tooling of any kind.

## Boundary

Hermes Sentinel is **observability only** and never deploys anything.
Sentinel itself never:

- SSHes to hosts;
- pulls or deploys itself;
- invokes remote `systemctl`;
- installs packages remotely;
- modifies monitored hosts;
- performs remediation.

There is no Ansible, no Fabric, no deployment daemon and no
self-updater in this stage. Every command in this runbook is performed
**locally on the central host** by its operator, from a local trusted
copy/checkout of the accepted repository state.

The runbook covers the CENTRAL service only. The already-accepted
reporter deployment (monitored hosts) is documented separately in
[REPORTER_DEPLOYMENT.md](REPORTER_DEPLOYMENT.md) and is not changed
here.

## Prerequisites

- Ubuntu Linux with systemd as init system;
- Python >= 3.11 with `python3-venv` / pip capability;
- root privileges via `sudo` for the installation and systemd
  operations;
- a local trusted copy/checkout of the accepted repository state (or
  an artifact built from it) already present on the host, transferred
  there through the operator's own trusted channel.

The Sentinel runtime itself never requires root: root is used only to
install files and manage systemd, and the service runs as the
dedicated unprivileged identity below.

## Dedicated service identity

The central service must run under its own dedicated system account —
never `root`, `hermes`, `deploy`, `www-data` or `nobody`. Create it
once, locally:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
    hermes-sentinel
```

Required properties of `hermes-sentinel`:

- a system account;
- no interactive login shell (`/usr/sbin/nologin`);
- no sudo privileges;
- no unrelated supplementary groups.

The application and its virtualenv under `/opt/hermes-sentinel` are
root-managed and not writable by the service user; only the runtime
state under `/var/lib/hermes-sentinel` is owned by the dedicated
service identity.

## Frozen installation model

| Artifact                | Installed path                                   | Owner                         | Mode  |
|-------------------------|--------------------------------------------------|-------------------------------|-------|
| Application root        | `/opt/hermes-sentinel`                           | root:root                     | 0755  |
| Virtualenv              | `/opt/hermes-sentinel/venv`                      | root-managed                  | —     |
| Console entrypoint      | `/opt/hermes-sentinel/venv/bin/hermes-sentinel`  | root-managed                  | 0755  |
| Configuration directory | `/etc/hermes-sentinel`                           | root:root                     | 0750  |
| Central environment     | `/etc/hermes-sentinel/sentinel.env`              | root:root                     | 0600  |
| State directory         | `/var/lib/hermes-sentinel`                       | hermes-sentinel:hermes-sentinel | 0750 |
| Example database        | `/var/lib/hermes-sentinel/sentinel.sqlite3`      | hermes-sentinel:hermes-sentinel | —   |
| Service unit            | `/etc/systemd/system/hermes-sentinel.service`    | root:root                     | 0644  |

The `/opt/hermes-sentinel` application tree is root-owned and NOT
writable by the service user; the service user needs only
read/execute access to run the entrypoint. The state directory is
created and managed through the service `StateDirectory` boundary
(`StateDirectory=hermes-sentinel`, `StateDirectoryMode=0750`), so
systemd owns its creation with the dedicated identity. This model is
frozen for the central service and does not reinterpret the
already-accepted reporter paths.

## Installation

In this safe order, from the local trusted copy of the accepted
repository state:

1. Create/verify the dedicated service identity (above).
2. Create the application root with root ownership:

   ```bash
   sudo install -d -o root -g root -m 0755 /opt/hermes-sentinel
   ```

3. Create the Python virtualenv:

   ```bash
   sudo python3 -m venv /opt/hermes-sentinel/venv
   ```

4. Install the accepted project from the operator-supplied local
   repository/artifact into that venv, so the canonical entrypoint
   `/opt/hermes-sentinel/venv/bin/hermes-sentinel` exists:

   ```bash
   sudo /opt/hermes-sentinel/venv/bin/pip install \
       <local-path-to-accepted-artifact>
   ```

   `<local-path-to-accepted-artifact>` is the operator's local sdist,
   wheel or repository checkout already on the host. This runbook
   performs no git pull and no download from the Internet — which exact
   accepted state is installed is the operator's decision.

   Verify the entrypoint exists:

   ```bash
   ls -l /opt/hermes-sentinel/venv/bin/hermes-sentinel
   ```

5. Create the configuration directory:

   ```bash
   sudo install -d -o root -g root -m 0750 /etc/hermes-sentinel
   ```

6. Install the safe synthetic example as the central environment file,
   root:root 0600:

   ```bash
   sudo install -o root -g root -m 0600 \
       packaging/systemd/sentinel.env.example \
       /etc/hermes-sentinel/sentinel.env
   ```

7. Edit the installed copy locally (next section) — never pass secrets
   on a command line:

   ```bash
   sudoedit /etc/hermes-sentinel/sentinel.env
   ```

8. Install the service unit, root:root 0644:

   ```bash
   sudo install -o root -g root -m 0644 \
       packaging/systemd/hermes-sentinel.service \
       /etc/systemd/system/hermes-sentinel.service
   ```

9. Reload the systemd manager:

   ```bash
   sudo systemctl daemon-reload
   ```

10. Inspect the installed unit before starting anything:

    ```bash
    systemctl cat hermes-sentinel.service
    ```

## Configuring `sentinel.env`

The file contains exactly the accepted E6 settings variables — nothing
else (no `export`, no aliases, no additional application settings):

Required:

- `SENTINEL_DATABASE_PATH` — SQLite database file; the example default
  is `/var/lib/hermes-sentinel/sentinel.sqlite3` and must be writable
  by `hermes-sentinel`.
- `SENTINEL_LISTEN_HOST` / `SENTINEL_LISTEN_PORT` — the accepted B5
  backend endpoint the reporters post heartbeats to.
- `SENTINEL_MONITOR_INTERVAL_SECONDS` / `SENTINEL_POLL_INTERVAL_SECONDS`
  — the E5 runtime intervals.
- `SENTINEL_HOSTS_JSON` — the monitored-host inventory (strict JSON
  array of host objects).
- `SENTINEL_NODE_TOKENS_JSON` — per-node reporter credentials (strict
  JSON object).
- `SENTINEL_TELEGRAM_BOT_TOKEN` / `SENTINEL_TELEGRAM_CHAT_ID` — the
  Telegram delivery target.

Optional:

- `SENTINEL_TELEGRAM_MESSAGE_THREAD_ID` — omit it entirely when
  sending to a chat without a topic; for the Hermes Telegram topic
  deployment set it to the actual positive topic/thread id locally in
  the installed `sentinel.env`. Never commit the real bot token or
  production secret configuration anywhere.
- `SENTINEL_TELEGRAM_TIMEOUT_SECONDS` — omit to keep the accepted
  default (10.0 s).

Checks before first start:

- **JSON quoting**: systemd EnvironmentFile parsing removes ONE pair
  of outer single quotes around a whole value. Keep
  `SENTINEL_HOSTS_JSON='[...]'` and
  `SENTINEL_NODE_TOKENS_JSON='{...}'` wrapped in single quotes so the
  literal JSON double quotes inside reach the application (E6) intact.
- **Names must match**: the host `name` values in `SENTINEL_HOSTS_JSON`
  and the credential node names in `SENTINEL_NODE_TOKENS_JSON` must
  match exactly (case-sensitive).
- **One token per host**: each host uses its own token; tokens must
  not be shared between hosts.
- **B5 endpoint**: the listen host/port are the accepted B5 backend
  endpoint — see the plaintext safety section below before choosing a
  non-loopback listen host.
- **Secrets stay in the file**: the Telegram bot token and node tokens
  live only in `sentinel.env` (root:root, 0600). The service process
  never needs filesystem read permission on it — the systemd manager
  reads it and injects the variables. Real secrets must never be
  pasted into the unit file or any command line.

The settings loader (E6, `src/hermes_sentinel/settings.py`,
`load_central_settings(env)`) is the authoritative validation
reference: an unedited synthetic example fails during startup exactly
as designed.

## B5 plaintext safety

The accepted B5 listener is **plaintext HTTP**. Stage E9 implements no
TLS termination and no reverse proxy configuration. The safe checked-in
example binds `127.0.0.1`; do NOT expose the plaintext B5 listener
directly to the public Internet. Production HTTPS/reverse-proxy
termination belongs to [Stage F](PRODUCTION_HARDENING.md). E9 alone does
not claim that remote production reporters can safely reach the plaintext
backend from the Internet. For Stage F use `127.0.0.1` and put the SQLite
path inside `/var/lib/hermes-sentinel`.

## Smoke test and enablement

After installation and configuration, verify once:

```bash
sudo systemctl daemon-reload
sudo systemctl start hermes-sentinel.service
systemctl status hermes-sentinel.service
journalctl -u hermes-sentinel.service
```

Verify the service remains active with the valid configuration. If
this was only the smoke test, stop it again:

```bash
sudo systemctl stop hermes-sentinel.service
```

For persistent operation:

```bash
sudo systemctl enable --now hermes-sentinel.service
systemctl is-enabled hermes-sentinel.service
systemctl is-active hermes-sentinel.service
```

Restart semantics: the unit uses `Restart=on-failure` with
`RestartSec=5s` — systemd **process supervision** after an abnormal
exit only. It is not application retry, Telegram retry, heartbeat
retry, monitoring-cycle retry or backoff logic inside Sentinel, and a
normal operator stop (SIGTERM, handled cooperatively by the E8
process) is deliberately not restarted. No shell restart loops exist.

This runbook and these packaging files make no deployment claim: a
green local smoke test is not LIVE acceptance, and no real
heartbeat/Telegram production success is claimed from packaging or
documentation alone.

## Stop and update

```bash
sudo systemctl stop hermes-sentinel.service
```

The E8 process receives SIGTERM through `KillSignal=SIGTERM` and
performs its cooperative stop plus the E7 application cleanup
(listener and database connection).

For an update to a newer accepted repository state:

1. stop the service;
2. replace/reinstall the accepted project content into the frozen
   venv/application installation (step 4 above) so the canonical
   entrypoint is the new accepted build;
3. replace packaging/configuration files only when that is intended
   (re-check `sentinel.env` — it is configuration, not code);
4. `sudo systemctl daemon-reload` when the unit file changed;
5. start the service and check status/journal as in the smoke test.

There is no self-update and no automatic rollback framework in this
stage.

## Stage F acceptance on the central host

Apply the [production hardening procedure](PRODUCTION_HARDENING.md) from
the same approved SHA. Before opening HTTPS, check that the service
process uses the dedicated identity and expected executable, that the
application and unit files are root-owned, that the central environment
file is root:root 0600, and that SQLite and its sidecars are accessible
only to the intended service and operator. Check numeric modes with
`stat`, and the backend binding with `sudo ss -ltnp` locally. Keep these
raw outputs private and record only the pass/fail observations.

### Read-only heartbeat evidence

With the selected reporter timer stopped, collect this before and after
one attempt. Type the private configured node name at the hidden prompt.
Use the installed, approved SQLite path as the argument. The command
prints a count and the latest central receive timestamp for that node.

<!-- stage-f-sqlite-observation -->
```bash
(
set +x
set -o pipefail
read -rsp 'Acceptance node: ' sentinel_acceptance_node || exit 1
printf '\n'
printf '%s\n' "$sentinel_acceptance_node" |
    sudo /opt/hermes-sentinel/venv/bin/python -c '
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from hermes_sentinel.persistence.repository import parse_timestamp

node = sys.stdin.readline().rstrip("\n")
if not node:
    sys.exit("acceptance node is required")
uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
try:
    with closing(sqlite3.connect(uri, uri=True)) as db:
        count, received = db.execute(
            "SELECT COUNT(*), MAX(received_at) FROM heartbeat_observation WHERE node = ?",
            (node,),
        ).fetchone()
        latest = parse_timestamp(received).isoformat() if received else "none"
except (sqlite3.Error, ValueError):
    sys.exit("could not read heartbeat evidence")
print(f"rows={count} latest_received_at={latest}")
' /var/lib/hermes-sentinel/sentinel.sqlite3
)
```

An absent database or unreadable schema fails this check. A missing node
observation is expected only before the first controlled heartbeat.
For a status check later in the window, compare the timestamp with the
configured freshness threshold using the central clock.

### Confinement and supervision

Capture selected systemd fields locally, keeping the PID private.

```bash
systemctl show hermes-sentinel.service \
    -p ActiveState -p SubState -p Result -p ExecMainStatus -p NRestarts -p MainPID
```

For the filesystem check, create disposable markers on the test central
host. Use a readable regular file under a test home directory and a
writable target under `/var/tmp`. Verify the dedicated user can read or
write them outside confinement. Enter the active unit's mount namespace
with `nsenter --target <MainPID> --mount` and drop to the service user with
`setpriv --reuid=hermes-sentinel --regid=hermes-sentinel --clear-groups`
before attempting those same operations. Require both operations to fail
inside the namespace and remove the markers. Check that `CapInh`,
`CapPrm`, `CapEff`, `CapBnd` and `CapAmb` are all zero in the actual
process's `/proc/<MainPID>/status`. Use the real reporter, TCP probes and
Stage E delivery to verify database writes and egress in the service.

In the approved test window, send `sudo systemctl kill --signal=SIGTERM
hermes-sentinel.service`. Require successful exit and no new main process
after the restart interval. Start it again and verify the same SQLite
rows are present. For the E4/E5 fault exercise, make only the dedicated
test notification destination fail, then trigger a fresh controlled
DOWN. A delivery exception aborts the cycle and exits the process.
Observe a nonzero exit and a systemd restart after `RestartSec=5s`.
Restore egress within the agreed window, check that the service is
stable, and prove successful notification with a new controlled
transition. No missed notification is replayed by E4/E5.

Test invalid E6 configuration in a separate controlled run by replacing
one required setting with an invalid synthetic value using the root-only
environment file. Require startup failure with no value disclosed.
Stop the failing unit, restore the approved file, start and verify
the retained database. Review journal diagnostics in the private console
and export only the error category, time and exit status.
