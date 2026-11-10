# External Dead-Man Deployment & Live Acceptance Runbook (Stage H2)

This is an **operator runbook** for deploying the already-accepted H1B-2
dead-man package on the external dead-man host and executing the H2 live
acceptance. It documents local operator actions only — no installer, no
deployer and no remote-execution tooling exists in the project. Nothing
here changes the frozen H1A/H1B-1/H1B-2 semantics or the central Sentinel.

The runbook uses **synthetic placeholders** for every value the operator
must resolve at runtime:

| Placeholder            | Meaning                                            |
|------------------------|----------------------------------------------------|
| `<repository-remote>`  | operator's trusted source of the canonical repository |
| `<checkout-dir>`       | local checkout of the exact accepted commit         |
| `<DEADMAN_BOT_TOKEN>`  | independent Stage-H Telegram bot token             |
| `<DEADMAN_CHAT_ID>`    | Telegram chat id                                   |
| `<DEADMAN_THREAD_ID>`  | optional topic/thread id                           |

The Sentinel probe endpoint, hostname, port and resolved addresses are
NEVER typed by hand and NEVER printed into evidence: they are loaded by
the fail-closed blocks in section 6.1 into the shell variables
`SENTINEL_PROBE_URL`, `SENTINEL_HOST` and `SENTINEL_PORT`, which the
later recovery procedure reuses.

No real hostname, IP, domain, token, chat id, external-host identity or
account/provider identifier belongs in this file or in any returned
evidence. The one exception is the PUBLIC canonical commit SHA deployed
below, which is required evidence.

**Pasting convention**: every fenced block below is ONE command
sequence and must be pasted whole. Blocks marked fail-closed contain an
explicit `exit 1` guard — if such a block fails, it TERMINATES the
shell by design so no later mutation can run; re-login, run the
documented cleanup/retry procedure, and only then try again.

## Boundary

Hermes Sentinel is **observability only**. The dead-man never opens an
inbound listener, never SSHes anywhere, never invokes `systemctl` against
the central Sentinel or monitored hosts, and performs no remediation.
Every command below runs **locally on the external dead-man host**.

## 1. Pre-flight

Before installing anything, verify on the dead-man host:

- Ubuntu Linux, systemd as init (`ps -p 1 -o comm=`);
- Python >= 3.11 with venv/pip capability (`python3 --version`);
- nftables present (`nft --version`); the acceptance isolation below is
  nftables-only;
- DNS resolution works (`resolvectl status`);
- outbound HTTPS works to both `api.telegram.org` and the Sentinel
  endpoint host (any HTTP response code proves the path);
- the host is genuinely **outside the central Sentinel failure domain**:
  different physical/virtual machine, different provider or power/network
  domain than the central Sentinel VDS — this is the whole point of H2;
- no real secret value is printed during the session: never `cat` the
  env file after editing it, never put a token on a command line, never
  echo the probe target variables or resolved addresses into collected
  evidence.

## 2. Install (exact accepted runtime SHA)

The deployment installs EXACTLY this accepted runtime revision:

    37c2e1d6a4b484dcc29acc739ed16f44ed510b4e

1. Obtain the canonical repository and detach at that exact commit:

   ```bash
   git clone <repository-remote> <checkout-dir>
   git -C <checkout-dir> checkout --detach \
       37c2e1d6a4b484dcc29acc739ed16f44ed510b4e
   ```

2. Verify fail-closed that HEAD is exactly that SHA before installing:

   ```bash
   test "$(git -C <checkout-dir> rev-parse HEAD)" = \
       37c2e1d6a4b484dcc29acc739ed16f44ed510b4e \
       || { echo "HEAD is not the accepted SHA" >&2; exit 1; }
   ```

3. Create the dedicated system identity (never `root` or any shared
   account):

   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin \
       hermes-sentinel-deadman
   ```

4. Create the application root and virtualenv at the path the accepted
   service unit expects
   (`ExecStart=/opt/hermes-sentinel/venv/bin/hermes-sentinel-deadman`),
   and install the Python project FROM THAT EXACT CHECKOUT:

   ```bash
   sudo install -d -o root -g root -m 0755 /opt/hermes-sentinel
   sudo python3 -m venv /opt/hermes-sentinel/venv
   sudo /opt/hermes-sentinel/venv/bin/pip install <checkout-dir>
   ```

   No release/wheel publishing infrastructure is introduced — the
   checkout itself is the install source.

5. Verify fail-closed that the console script exists:

   ```bash
   test -x /opt/hermes-sentinel/venv/bin/hermes-sentinel-deadman \
       || { echo "console script MISSING" >&2; exit 1; } \
       && echo "console script present"
   ```

6. Install the systemd units and the env example FROM THE SAME checkout
   (the packaging in the deployed runtime revision is authoritative):

   ```bash
   sudo install -o root -g root -m 0644 \
       <checkout-dir>/packaging/systemd/hermes-sentinel-deadman.service \
       /etc/systemd/system/hermes-sentinel-deadman.service
   sudo install -o root -g root -m 0644 \
       <checkout-dir>/packaging/systemd/hermes-sentinel-deadman.timer \
       /etc/systemd/system/hermes-sentinel-deadman.timer
   sudo systemctl daemon-reload
   ```

7. Provision the state directory required by the packaging
   (`StateDirectory=hermes-sentinel`, `StateDirectoryMode=0750`;
   systemd creates `/var/lib/hermes-sentinel` on first service start,
   owned by `hermes-sentinel-deadman:hermes-sentinel-deadman`). If it
   already exists, fix it explicitly:

   ```bash
   sudo install -d -o hermes-sentinel-deadman \
       -g hermes-sentinel-deadman -m 0750 /var/lib/hermes-sentinel
   ```

## 3. Config

Create the environment file from the safe synthetic example in the same
checkout:

```bash
sudo install -d -o root -g root -m 0750 /etc/hermes-sentinel
sudo install -o root -g root -m 0600 \
    <checkout-dir>/packaging/systemd/deadman.env.example \
    /etc/hermes-sentinel/deadman.env
sudoedit /etc/hermes-sentinel/deadman.env
```

Set exactly these values:

```ini
HERMES_SENTINEL_DEADMAN_PROBE_URL=https://<sentinel-host>/v1/heartbeat
HERMES_SENTINEL_DEADMAN_STATE_PATH=/var/lib/hermes-sentinel/deadman-state.json
HERMES_SENTINEL_DEADMAN_TELEGRAM_BOT_TOKEN=<DEADMAN_BOT_TOKEN>
HERMES_SENTINEL_DEADMAN_TELEGRAM_CHAT_ID=<DEADMAN_CHAT_ID>
# optional topic delivery; omit the line entirely when not used:
# HERMES_SENTINEL_DEADMAN_TELEGRAM_THREAD_ID=<DEADMAN_THREAD_ID>
```

Rules:

- the file stays `root:root 0600`; the service user never reads it
  (the systemd manager supplies the values);
- the bot token is the **independent Stage-H dead-man token** — never
  the reporter token and never the central Sentinel's Stage-E bot token;
  double-check the credential manager entry before saving;
- use `sudoedit`, never `echo`/`cat` redirection with real values;
- no real value is ever committed to the repository.

## 4. Secret-safe state summary (strict accepted decoder, read-only)

The process journal alone is insufficient acceptance evidence: normal
process output does not expose the probe result or the debounce
counters. The authoritative proof is the persisted H1A state at
`/var/lib/hermes-sentinel/deadman-state.json`. This summary is
READ-ONLY and STRICT:

- it requires the state file to EXIST first (a missing file would be
  silently canonicalized to UNKNOWN by the accepted store — valid for
  the runtime, but NOT valid deployment evidence);
- it decodes through the ACCEPTED H1A/H1B-1 `DeadManStateStore`
  implementation, so a corrupt, malformed or unsupported-schema state
  FAILS non-zero — no permissive `.get()` fallbacks, no fabricated
  zeros, no fake UNKNOWN snapshot;
- it prints only typed, non-secret operational fields — never a JSON
  dump, never env values, tokens, endpoints or Telegram ids.

```bash
sudo /opt/hermes-sentinel/venv/bin/python - <<'PY'
import sys
from pathlib import Path

from hermes_sentinel.deadman_store import (
    DeadManStateStore,
    DeadManStateStoreError,
)

path = Path("/var/lib/hermes-sentinel/deadman-state.json")
if not path.is_file():
    sys.exit("state file missing - not valid acceptance evidence")
try:
    status = DeadManStateStore(path).load()
except DeadManStateStoreError:
    sys.exit("strict dead-man state decode failed - not valid evidence")

print(f"state={status.state.name}")
print(f"consecutive_failures={status.consecutive_failures}")
print(f"consecutive_successes={status.consecutive_successes}")
print(f"pending_count={len(status.pending_notifications)}")
current = status.current_down
if current is None:
    print("current_down_id=<none>")
    print("current_down_acknowledged=<none>")
else:
    print(f"current_down_id={current.notification_id}")
    print(f"current_down_acknowledged={str(current.acknowledged).lower()}")
anchor = status.state_changed_at
print(f"state_changed_at={anchor.isoformat() if anchor else '<none>'}")
print(f"next_notification_id={status.next_notification_id}")
PY
```

This block is referenced as **"state summary"** throughout.

## 5. Initial runtime acceptance

After installation, verify — by observation, not by trusting unit-file
comments:

1. Static unit verification:

   ```bash
   systemd-analyze verify \
       /etc/systemd/system/hermes-sentinel-deadman.service \
       /etc/systemd/system/hermes-sentinel-deadman.timer
   ```

   (no output and exit 0 is a pass).

2. ONE manual smoke invocation (this is the only manual start of the
   whole acceptance — all threshold sequences later are timer-driven):

   ```bash
   sudo systemctl start hermes-sentinel-deadman.service
   systemctl status hermes-sentinel-deadman.service
   ```

   Expect a successful oneshot result (`ExecMainStatus=0`). The first
   successful observation of an absent state file establishes UP
   silently (no startup notification). Run the state summary AFTER
   this smoke: expect `state=UP` — a missing state file here FAILS
   the summary command (section 4), and that is a defect to report,
   not an UNKNOWN to accept.

3. State file exists at the configured path with correct ownership:

   ```bash
   sudo stat -c '%U:%G %a %n' \
       /var/lib/hermes-sentinel/deadman-state.json
   ```

   Expect `hermes-sentinel-deadman:hermes-sentinel-deadman 600`
   (created under the service `UMask=0077`).

4. Timer enabled and active, 60-second cadence, oneshot service:

   ```bash
   sudo systemctl enable --now hermes-sentinel-deadman.timer
   systemctl is-active hermes-sentinel-deadman.timer
   systemctl list-timers hermes-sentinel-deadman.timer
   systemctl show hermes-sentinel-deadman.service \
       -p Type -p ActiveState -p User -p Group
   ```

   Expect `Type=oneshot`, the timer ACTIVE with
   `OnUnitActiveSec=60s`, the service `(dead)`/inactive between
   activations — never continuously active — and
   `User=hermes-sentinel-deadman`.

5. Establish the healthy UP baseline with timer cycles only: after 2–3
   timer activations, the state summary shows `state=UP`,
   `pending_count=0`, and NO Telegram message has arrived.

6. Journal is secret-safe:

   ```bash
   sudo journalctl -u hermes-sentinel-deadman.service --since -10min
   ```

   The output must contain no token, chat id or authenticated Telegram
   URL.

7. No inbound listener — honest evidence phrasing: the accepted runtime
   has no listener code, no socket unit and no listening directive in
   the service unit; that unit/process design is the primary evidence.
   An `ss` snapshot is supporting evidence only (a oneshot that is
   inactive between runs cannot be caught listening by a random
   snapshot):

   ```bash
   sudo ss -tlnp
   ```

   No socket owned by `hermes-sentinel-deadman` may ever appear in any
   snapshot taken during or between cycles.

## 6. Live DOWN acceptance (safe, reversible)

Goal: the external dead-man observes **3 consecutive failed probes**
through the real H1B-1 HTTPS prober and the real 3-failure debounce,
with exactly one DOWN notification — no code edits, no manual state
edits, no debounce/timer changes, no fake Telegram calls.

The NORMAL flow of this section keeps the isolation active through the
DOWN no-spam check (6.7); the isolation is removed only in section 7
(recovery). The cleanup primitive appears in the normal flow THERE,
and in the abort/rollback context of section 10 — nowhere between 6.1
and 6.7.

### 6.1 Define the probe target (accepted URL semantics, fail-closed)

This block loads ONLY the probe URL line from `deadman.env` (never
sources the file, never touches the Telegram variables) and parses it
with Python stdlib `urllib.parse.urlsplit` — the accepted URL
semantics. An explicit `:443` works, the implicit HTTPS default
becomes 443, IPv6 literals come back bracket-free, and the hostname
never carries `:443`. Paste as ONE block; do not echo the resulting
variables into evidence:

```bash
read -r SENTINEL_PROBE_URL SENTINEL_HOST SENTINEL_PORT < <(
    sudo /opt/hermes-sentinel/venv/bin/python - <<'PY'
import sys
from urllib.parse import urlsplit

raw = None
with open("/etc/hermes-sentinel/deadman.env", encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("HERMES_SENTINEL_DEADMAN_PROBE_URL="):
            raw = line.split("=", 1)[1].strip()
            break
if not raw:
    sys.exit("probe URL missing from deadman.env")
parsed = urlsplit(raw)
if parsed.scheme != "https":
    sys.exit("probe URL is not HTTPS")
host = parsed.hostname
if not host:
    sys.exit("probe URL has no hostname")
try:
    port = parsed.port
except ValueError:
    sys.exit("probe URL has an invalid port")
if port is None:
    port = 443
print(raw, host, port)
PY
) || { echo "probe target extraction failed" >&2; exit 1; }
test -n "${SENTINEL_PROBE_URL:-}" && test -n "${SENTINEL_HOST:-}" \
    && test -n "${SENTINEL_PORT:-}" \
    || { echo "probe target variables undefined" >&2; exit 1; }
echo "probe target defined (values not printed)"
```

These three variables stay in this shell for the whole session; the
recovery procedure (section 7) reuses `SENTINEL_PROBE_URL` and re-checks
it is still set.

### 6.2 Create the dedicated isolation table (fail-closed on stale and port)

ONE dedicated, clearly H2-temporary nftables table — never a
pre-existing filter chain and never iptables:

    table inet hermes_sentinel_h2

Paste as ONE block. The FIRST statement validates the CURRENT shell
value of `SENTINEL_PORT` with PYTHON integer semantics — non-empty,
ASCII decimal digits only, `int()` parse, `1 <= port <= 65535` — which
is overflow-safe by construction: a redefined garbage value such as
`abc`, `+443`, `0`, `65536`, whitespace, or an arbitrarily large
digit-only string like `999999999999999999999999999999` terminates
non-zero CLEANLY, with no Bash integer-expression error and no silent
normalization or 443 fallback. Deliberately NO Bash `-lt`/`-gt`/`(( ))`
arithmetic touches this untrusted shell text. The stale-table check
follows; either failure TERMINATES the shell before ANY nft mutation
(a malformed explicit URL port already failed closed in 6.1 — this
re-validation is defense in depth for a lost/redefined variable). The
exact variable validated here is the exact one interpolated into BOTH
rules below. After such a stop, run the documented cleanup/retry
procedure (section 10, cleanup primitive) and start this section
again:

```bash
if ! sudo /opt/hermes-sentinel/venv/bin/python - "$SENTINEL_PORT" <<'PY'
import sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
if not raw or not raw.isascii() or not raw.isdigit():
    sys.exit(1)
port = int(raw)
if not 1 <= port <= 65535:
    sys.exit(1)
PY
then
    echo "SENTINEL_PORT failed validation (ASCII integer 1..65535 required)" >&2
    exit 1
fi
if sudo nft list table inet hermes_sentinel_h2 >/dev/null 2>&1; then
    echo "stale H2 table already present - run the documented cleanup first" >&2
    exit 1
fi
sudo nft add table inet hermes_sentinel_h2
sudo nft add chain inet hermes_sentinel_h2 output \
    '{ type filter hook output priority 0; }'
sudo nft add set inet hermes_sentinel_h2 sentinel_v4 '{ type ipv4_addr; }'
sudo nft add set inet hermes_sentinel_h2 sentinel_v6 '{ type ipv6_addr; }'
sudo nft add rule inet hermes_sentinel_h2 output \
    ip daddr @sentinel_v4 tcp dport "$SENTINEL_PORT" reject
sudo nft add rule inet hermes_sentinel_h2 output \
    ip6 daddr @sentinel_v6 tcp dport "$SENTINEL_PORT" reject
echo "H2 isolation table created (both address sets present, both empty,"
echo "probe port $SENTINEL_PORT blocked for both families)"
```

The rules block the ACTUAL configured probe path — the parsed
`SENTINEL_PORT`, never a hard-coded 443: an implicit HTTPS port
resolves to 443 in 6.1, while an endpoint such as
`https://<sentinel-host>:8443/v1/heartbeat` produces `tcp dport 8443`.
BOTH address sets and BOTH destination rules are created
UNCONDITIONALLY: an empty set is the correct representation of a
family with no currently resolved addresses, and a family appearing
later (DNS change) needs no structural change — the refresh helper
(6.3) replaces only address-set CONTENTS; the port is stable session
configuration living in these rules. The table contains ONLY these
Sentinel probe isolation rules; the base chain has no default-drop
policy, so ALL unrelated outbound traffic — SSH/operator connectivity,
DNS, and Telegram HTTPS to `api.telegram.org:443` — stays untouched
and independently verified in 6.4.

### 6.3 Dual-stack refresh helper (fail-closed, atomic)

Define this repeatable helper ONCE in the same shell (it uses
`SENTINEL_HOST` from 6.1). It resolves the CURRENT complete address
sets via Python stdlib `socket.getaddrinfo` (both `AF_INET` and
`AF_INET6`, de-duplicated), generates ONE nft batch that flushes and
repopulates BOTH sets, and submits it through ONE `nft -f -` — the
refresh is all-or-nothing from nft's point of view, no family can go
stale because the other changed, and NO error is ever hidden:

```bash
h2_refresh() {
    local batch
    test -n "${SENTINEL_HOST:-}" \
        || { echo "SENTINEL_HOST undefined (run 6.1 first)" >&2; return 1; }
    batch="$(
        sudo /opt/hermes-sentinel/venv/bin/python - "$SENTINEL_HOST" <<'PY'
import socket
import sys

host = sys.argv[1]
v4, v6 = set(), set()
try:
    for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        addr = info[4][0]
        (v4 if info[0] == socket.AF_INET else v6).add(addr)
except socket.gaierror:
    sys.exit("resolution failed - isolation sets left untouched")
if not v4 and not v6:
    sys.exit("no addresses resolved - isolation sets left untouched")

lines = ["flush set inet hermes_sentinel_h2 sentinel_v4"]
if v4:
    lines.append(
        "add element inet hermes_sentinel_h2 sentinel_v4 { "
        + ", ".join(sorted(v4)) + " }"
    )
lines.append("flush set inet hermes_sentinel_h2 sentinel_v6")
if v6:
    lines.append(
        "add element inet hermes_sentinel_h2 sentinel_v6 { "
        + ", ".join(sorted(v6)) + " }"
    )
print("\n".join(lines))
PY
    )" || { echo "H2 refresh: resolution failed" >&2; return 1; }
    printf '%s\n' "$batch" | sudo nft -f - \
        || { echo "H2 refresh: nft batch rejected" >&2; return 1; }
    sudo nft list table inet hermes_sentinel_h2 >/dev/null \
        || { echo "H2 refresh: table missing" >&2; return 1; }
    sudo nft list set inet hermes_sentinel_h2 sentinel_v4 >/dev/null \
        || { echo "H2 refresh: sentinel_v4 missing" >&2; return 1; }
    sudo nft list set inet hermes_sentinel_h2 sentinel_v6 >/dev/null \
        || { echo "H2 refresh: sentinel_v6 missing" >&2; return 1; }
    echo "H2 refresh OK (table and both sets verified present)"
}
```

Run it IMMEDIATELY after creating the table, then record the blocked
counts (counts only — never the address values) as evidence:

```bash
h2_refresh || { echo "refresh failed - do not proceed" >&2; exit 1; }
blocked_ipv4_count="$(
    sudo nft list set inet hermes_sentinel_h2 sentinel_v4 \
        | sed -n 's/.*elements = { \(.*\) }/\1/p' | wc -w)"
blocked_ipv6_count="$(
    sudo nft list set inet hermes_sentinel_h2 sentinel_v6 \
        | sed -n 's/.*elements = { \(.*\) }/\1/p' | wc -w)"
echo "blocked_ipv4_count=$blocked_ipv4_count"
echo "blocked_ipv6_count=$blocked_ipv6_count"
sudo nft list table inet hermes_sentinel_h2
```

Inspect the full listing locally and prove the complete resolved set
is represented (the set element counts must match the resolver's view —
the values stay off the record).

### 6.4 Prove Telegram stays reachable while isolated

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org/
```

Any HTTP status code proves DNS + TCP + TLS + HTTP reachability. This
check uses NO bot token and calls NO authenticated Bot API method.

### 6.5 Count real timer cycles (debounce evidence)

Record the isolation start timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`).
Every counted observation pairs TWO pieces of evidence — do not count
timestamps alone, and run NO manual `systemctl start` during the
threshold sequence:

A. proof of a distinct completed timer activation of the oneshot:

   ```bash
   systemctl show hermes-sentinel-deadman.service \
       -p InvocationID -p ExecMainExitTimestamp \
       -p ExecMainStatus -p ActiveEnterTimestamp
   ```

   (a NEW `InvocationID` per activation; `ExecMainStatus=0` — a failed
   probe is a normal observation, not a crash);

B. a state summary (section 4) taken AFTER that exact invocation.

BEFORE each counted observation — including the no-spam cycle in 6.7 —
re-run the SAME dual-stack refresh and let it succeed; DNS can change
mid-test, and a cycle must not be counted as a failed observation
merely because the firewall was *intended* to block it (the persisted
H1A counters are the authoritative proof). If the refresh fails, DO
NOT count the next timer cycle — resolve the failure first:

```bash
h2_refresh || { echo "refresh failed - cycle NOT countable" >&2; exit 1; }
```

Expected sequence, proven by the state summaries:

- **Observation 1** — refresh OK; distinct timer cycle; `state=UP`;
  `consecutive_failures=1`.
- **Observation 2** — refresh OK; a distinct later timer cycle;
  `state=UP`; `consecutive_failures=2`.
- **Observation 3** — refresh OK; a distinct later timer cycle;
  `state=DOWN` per the accepted H1A semantics (the confirmed-DOWN
  snapshot canonicalizes the counters — assert the state, not a
  specific post-transition counter value).

### 6.6 DOWN receipt and same-cycle ACK

The DOWN-confirming cycle performs, within that SAME successful cycle:
persist transition -> deliver Telegram -> ACK the exact notification id
-> persist the ACK. Therefore, after that cycle and the actually
RECEIVED Stage-H DOWN message (a pending/unsent notification is NOT
acceptance):

- exactly one DOWN Telegram message observed (three-line
  "Hermes Sentinel Dead-Man" format);
- state summary: `state=DOWN`, `current_down_acknowledged=true`,
  `pending_count=0` (same-cycle ACK).

### 6.7 DOWN steady-state / no-spam (KEEP the isolation ACTIVE)

Do NOT remove the isolation yet. Refresh, then allow ONE LATER timer
cycle under continued isolation and verify with the state summary and
the chat:

```bash
h2_refresh || { echo "refresh failed - cycle NOT countable" >&2; exit 1; }
```

- `state` remains `DOWN`; no second DOWN notification appears;
- pending/ack state stays coherent (`current_down_acknowledged=true`,
  `pending_count=0`).

This proves transition-only delivery — no DOWN notification spam. The
literal normal-flow sequence so far is: isolation active -> failure #1
-> failure #2 -> failure #3 (DOWN) -> real DOWN Telegram received ->
same-cycle ACK/state checked -> KEEP ISOLATION -> one later isolated
cycle -> no-spam verified. ONLY NOW proceed to section 7, where the
isolation is removed.

## 7. Live RECOVERY acceptance

1. Remove the ENTIRE isolation and prove absence — this is the ONE
   authoritative cleanup primitive (deletion of the whole dedicated
   table; never handle-based):

   ```bash
   sudo nft delete table inet hermes_sentinel_h2
   sudo nft list table inet hermes_sentinel_h2 >/dev/null 2>&1 \
       && { echo "H2 table STILL PRESENT" >&2; exit 1; } \
       || echo "H2 isolation removed"
   ```

2. Prove the real Sentinel endpoint is reachable again AND healthy
   under the EXACT accepted public GET contract — HTTP status 405, an
   `Allow` method list semantically containing `POST`, and an empty
   body — by reusing the ACCEPTED H1B-1 probe adapter installed in the
   venv (the same classifier production uses; no second shell
   approximation, no weaker fallback). It performs exactly ONE
   unauthenticated HTTPS GET against the already-defined
   `SENTINEL_PROBE_URL` (fail-closed if the session lost it; never
   re-source the secret env file), with certificate and hostname
   verification and a bounded timeout; every deviation — 405 with a
   wrong/missing `Allow`, a non-empty body, any other status, a TLS or
   connection failure — is the adapter's FAILED outcome and exits
   non-zero. Only a generic PASS/FAIL line is printed — never the
   endpoint, resolved IPs, response body or any Telegram secret:

```bash
test -n "${SENTINEL_PROBE_URL:-}" \
    || { echo "SENTINEL_PROBE_URL undefined" >&2; exit 1; }
sudo /opt/hermes-sentinel/venv/bin/python - "$SENTINEL_PROBE_URL" <<'PY'
import sys

from hermes_sentinel.deadman import DeadManProbeOutcome
from hermes_sentinel.deadman_probe import (
    DeadManProber,
    DeadManProbeSettings,
)

try:
    settings = DeadManProbeSettings(url=sys.argv[1], timeout_seconds=10.0)
except ValueError:
    sys.exit("SENTINEL_RECOVERY_PROBE=FAIL (invalid probe URL)")
outcome = DeadManProber(settings).probe()
if outcome is not DeadManProbeOutcome.HEALTHY:
    sys.exit("SENTINEL_RECOVERY_PROBE=FAIL")
print("SENTINEL_RECOVERY_PROBE=PASS")
PY
```

   Required evidence: `SENTINEL_RECOVERY_PROBE=PASS` and exit 0.
   This verification probe is NOT a dead-man observation — only the
   runtime's own timer-driven probes feed the state machine (7.3).

3. Count distinct timer cycles with the same paired evidence as 6.5
   (new `InvocationID` + state summary after it; no manual starts; no
   refresh is needed — the isolation table is gone):

   - **Recovery observation 1** — distinct timer cycle;
     `state=DOWN` persists; `consecutive_successes=1`.
   - **Recovery observation 2** — a distinct later timer cycle; the
     accepted 2-success threshold transitions `state=UP`; exactly
     one RECOVERED Telegram message is RECEIVED; same-cycle ACK
     completed; final `pending_count=0`.

4. Allow at least one additional healthy timer cycle and verify:

   ```bash
   systemctl is-active hermes-sentinel-deadman.timer
   ```

   plus state summary: `state=UP`, `pending_count=0`, no extra
   RECOVERED message — no notification spam, timer still
   enabled/active.

## 8. Restart / durability check

Prove the persisted state survives process boundaries (no reboot
required — each timer activation is already a fresh process):

1. Capture the current state summary.
2. Restart the oneshot/timer boundary and force one later cycle:

   ```bash
   sudo systemctl restart hermes-sentinel-deadman.timer
   sudo systemctl start hermes-sentinel-deadman.service
   ```

3. Capture the state summary again: the state must remain UP with its
   established anchor (`state_changed_at` unchanged in substance),
   never reset to UNKNOWN merely because the process exited. A reset
   to UNKNOWN here is a defect — report it, do not patch the state
   file.

## 9. Evidence contract (Operator -> Architect)

Return human-verifiable, secret-safe evidence. Never paste the bot
token, chat id, probe URL with private hostname, resolved address
values, or raw env files — counts and statements replace values.

**DEPLOYMENT**

- deployed SHA: the verified `git rev-parse HEAD` output, which must
  literally equal `37c2e1d6a4b484dcc29acc739ed16f44ed510b4e`;
- `python3 --version` output;
- console-script path verification (the `test -x` result for
  `/opt/hermes-sentinel/venv/bin/hermes-sentinel-deadman`);
- `systemd-analyze verify` result for both units;
- `systemctl is-enabled` / `is-active` for the timer plus
  `systemctl list-timers` showing the 60-second cadence;
- service identity (`User`/`Group` from `systemctl show`);
- `stat` output for the state file (owner/mode).

**ISOLATION**

- proof the `inet hermes_sentinel_h2` table existed (e.g. the
  inspected table listing with addresses masked);
- `blocked_ipv4_count=N` and `blocked_ipv6_count=N` (counts only,
  never the values);
- Telegram public HTTPS reachability PASS (the status code from 6.4).

**DOWN**

- the three distinct timer invocation ids/timestamps (InvocationID +
  ExecMainExitTimestamp) — one per counted observation;
- the state summary after each of the three observations;
- real DOWN Telegram receipt confirmation (message timestamp or
  screenshot with chat identity redacted);
- post-delivery state summary: `state=DOWN`,
  `current_down_acknowledged=true`, `pending_count=0`;
- one later isolated no-spam cycle (state summary + statement).

**RECOVERY**

- proof the H2 nft table is removed (the verification output);
- endpoint-reachable-again evidence: the
  `SENTINEL_RECOVERY_PROBE=PASS` output from the accepted-adapter
  check in 7.2 (the generic line only — the URL is never pasted);
- the two distinct timer invocation ids/timestamps with the state
  summary after each;
- real RECOVERED Telegram receipt confirmation;
- final state summary: `state=UP`, `pending_count=0`;
- one later healthy no-spam cycle and timer still active.

**SECURITY**

- statement that no token, chat id, private endpoint or resolved
  address value appears anywhere in the returned evidence;
- no reporter token and no central Stage-E Telegram credential
  configured on the dead-man host;
- no inbound feature introduced: unit/process design evidence plus
  (supporting) the `ss` snapshots.

## 10. Rollback / abort

Deterministic rollback, leaving the central Sentinel untouched.
Network isolation is removed FIRST so no stale isolation survives even
if a later systemctl command fails. The SAME cleanup primitive applies
to: an ABORTED DOWN test (immediately), operator retry preparation
(before recreating a clean table via 6.2), and the generic rollback:

1. Remove the H2 isolation table if present, then prove absence:

   ```bash
   if sudo nft list table inet hermes_sentinel_h2 >/dev/null 2>&1; then
       sudo nft delete table inet hermes_sentinel_h2
   fi
   sudo nft list table inet hermes_sentinel_h2 >/dev/null 2>&1 \
       && { echo "H2 table STILL PRESENT" >&2; exit 1; } \
       || echo "H2 isolation removed"
   ```

2. Stop and disable the timer:

   ```bash
   sudo systemctl disable --now hermes-sentinel-deadman.timer
   ```

3. Stop the service if active (a oneshot is normally already
   inactive):

   ```bash
   sudo systemctl stop hermes-sentinel-deadman.service
   ```

4. **Preserve the state file**
   `/var/lib/hermes-sentinel/deadman-state.json` unless a destructive
   reset is explicitly requested — the liveness ledger is operational
   history.

5. The central Sentinel and all reporters are not touched by any step
   of this runbook.

Before retrying, remove any isolation remnant (step 1) and inspect the
journal to diagnose the failure:

```bash
sudo journalctl -u hermes-sentinel-deadman.service --since -1h
```

No automatic remote remediation exists or is added.
