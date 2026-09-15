# Reporter Deployment Runbook (Stage C3)

This is an **operator runbook** for deploying the Hermes Sentinel host
reporter on a monitored Ubuntu Linux host. It documents local operator
actions only — it is not product automation, and the project contains
no installer, no deployer and no remote-execution tooling of any kind.

## Boundary

Hermes Sentinel is **observability only**. Sentinel itself never
deploys anything: it never SSHes to monitored hosts, never installs or
modifies files remotely, never invokes remote `systemctl`, never
executes arbitrary remote commands, never restarts host services and
never reboots machines. There is no central deployer, no SSH
automation, no Ansible/Fabric and no deployment daemon. Every command
in this runbook is performed **locally on the monitored host** by the
host's operator, from a local checkout of the packaging files.

## Prerequisites

- Ubuntu Linux with systemd as init system;
- bash, awk/coreutils, `date`, `sleep`, `df`, `printf` (standard
  base system);
- `curl` (the single reporter transport dependency);
- root privileges via `sudo` for the local installation steps.

No Python, no jq, no Windows/PowerShell/WSL involvement — the
production target is Ubuntu Linux with systemd.

## Dedicated reporter identity

The reporter must run under its own dedicated system account — never
`root`, `hermes`, `www-data` or `nobody`. Create it once, locally:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
    hermes-sentinel-reporter
```

Required properties of `hermes-sentinel-reporter`:

- a system account;
- no interactive login shell (`/usr/sbin/nologin`);
- no usable home directory;
- no sudo privileges;
- no extra groups;
- no Linux capabilities.

## Frozen installation paths

| Artifact              | Installed path                                              | Owner    | Mode  |
|-----------------------|-------------------------------------------------------------|----------|-------|
| Reporter executable   | `/usr/local/libexec/hermes-sentinel/sentinel-report.sh`     | root:root | 0755 |
| libexec directory     | `/usr/local/libexec/hermes-sentinel`                        | root:root | 0755 |
| Reporter environment  | `/etc/hermes-sentinel/reporter.env`                         | root:root | 0600 |
| Configuration dir     | `/etc/hermes-sentinel`                                      | root:root | 0750 |
| Service unit          | `/etc/systemd/system/hermes-sentinel-reporter.service`      | root:root | 0644 |
| Timer unit            | `/etc/systemd/system/hermes-sentinel-reporter.timer`        | root:root | 0644 |

The reporter runtime user (`hermes-sentinel-reporter`) never owns the
executable and must not be able to modify it. No production copy of
the reporter belongs under `/home`, `/root` or `/tmp`.

## Installation

From a local checkout of this repository on the monitored host:

```bash
# Reporter executable (root-owned, world-executable, not writable).
sudo install -d -o root -g root -m 0755 /usr/local/libexec/hermes-sentinel
sudo install -o root -g root -m 0755 \
    scripts/sentinel-report.sh \
    /usr/local/libexec/hermes-sentinel/sentinel-report.sh

# Configuration directory and the environment file, from the safe
# synthetic example. 0600 root:root: only root and the systemd
# manager reading it at service start ever see the contents.
sudo install -d -o root -g root -m 0750 /etc/hermes-sentinel
sudo install -o root -g root -m 0600 \
    packaging/systemd/reporter.env.example \
    /etc/hermes-sentinel/reporter.env

# systemd units.
sudo install -o root -g root -m 0644 \
    packaging/systemd/hermes-sentinel-reporter.service \
    /etc/systemd/system/hermes-sentinel-reporter.service
sudo install -o root -g root -m 0644 \
    packaging/systemd/hermes-sentinel-reporter.timer \
    /etc/systemd/system/hermes-sentinel-reporter.timer
```

## Configuring `reporter.env`

Edit the installed copy in a trusted editor — never pass the token on
a command line, where it would be recorded in shell history and
process listings:

```bash
sudoedit /etc/hermes-sentinel/reporter.env
```

The file contains exactly three variables:

- `SENTINEL_NODE` — the explicit node identity for this host. Node
  names such as `Prod`, `Hermes`, `VPN-1`, `VPN-2` are configured
  explicitly here (never inferred from the hostname) and are
  case-sensitive and verbatim.
- `SENTINEL_ENDPOINT` — the **full** HTTPS heartbeat endpoint URL,
  e.g. `https://sentinel.example/v1/heartbeat` (your real Sentinel
  HTTPS endpoint; the reporter uses the value verbatim, HTTPS only).
- `SENTINEL_TOKEN` — the per-node reporter token issued for THIS node.
  Tokens are per-node: never share one token between two hosts.

Keep the file root-owned with mode 0600. The reporter process itself
never needs filesystem read permission on it: the systemd manager
reads `EnvironmentFile` and supplies the variables to the service
process itself. The token therefore never appears in the unit files,
in unit commands, in command-line arguments or in the journal.

## Reloading and verifying units

```bash
sudo systemctl daemon-reload
systemctl cat hermes-sentinel-reporter.service
systemctl cat hermes-sentinel-reporter.timer
```

## Manual smoke test (once, before enabling the timer)

Run the oneshot service exactly once:

```bash
sudo systemctl start hermes-sentinel-reporter.service
systemctl status hermes-sentinel-reporter.service
```

Expected success: the service exits successfully (`status=0/SUCCESS`)
and the reporter produces **no stdout output** — the payload, the
response body and the token are never printed.

On failure the service is marked failed and a short generic reporter
diagnostic may be visible in the journal:

```bash
journalctl -u hermes-sentinel-reporter.service
```

Do **not** repeatedly run the service to overcome a network failure —
the reporter has no retry policy by design. Fix the cause (endpoint,
token, DNS, connectivity), then repeat the single smoke test.

## Enabling the heartbeat (timer only)

Enable and start the TIMER. The oneshot service itself is not an
enable target — it is activated by the timer or manually for the
smoke test:

```bash
sudo systemctl enable --now hermes-sentinel-reporter.timer
systemctl list-timers --all
systemctl status hermes-sentinel-reporter.timer
```

The timer fires approximately one report per minute (30 s after boot,
then every 60 s). Because the service is `Type=oneshot` and systemd
never starts a unit that is already active, a still-running reporter
can never overlap with a second one — there is exactly one reporter
process at a time.

## Failure semantics

A failed oneshot heartbeat does **not** stop the timer permanently.
The next scheduled timer activation is simply the next **normal
measurement attempt** — this is sampling cadence, not an immediate
retry policy and not a transport retry. The service deliberately has
`Restart=no`; do not add `Restart=on-failure` or timer retry
workarounds. A heartbeat represents the current host state: missed
heartbeats after downtime are deliberately not replayed.

## Disabling / stopping

```bash
sudo systemctl disable --now hermes-sentinel-reporter.timer
```

## Update procedure

To replace the reporter script or unit files safely, locally on the
host:

1. Stop the timer: `sudo systemctl disable --now hermes-sentinel-reporter.timer`
   (or `sudo systemctl stop hermes-sentinel-reporter.timer`).
2. Replace the files with the exact ownership and modes from the
   table above (`install -o root -g root -m 0755` for the script,
   `-m 0644` for the units).
3. If unit files changed: `sudo systemctl daemon-reload`.
4. Perform the single manual smoke test (start the service once,
   check status/journal as above).
5. Start the timer again:
   `sudo systemctl enable --now hermes-sentinel-reporter.timer`.

There is no self-update mechanism: the reporter never fetches code
from GitHub, contains no curl/wget updater, and the systemd service
never performs deployment.
