#!/usr/bin/env bash
#
# Hermes Sentinel — Stage C2 one-shot HTTPS host reporter.
#
# One-shot reporter for Ubuntu Linux / bash: reads local host metrics
# (the accepted Stage C1 collector) and performs exactly ONE outbound
# HTTPS POST of the heartbeat JSON document (the accepted Stage B3
# wire payload) to the configured Sentinel heartbeat endpoint. The
# script then exits — there is no daemon, no retry loop and no local
# spool/queue. It needs NO Python, jq or other extra packages on the
# monitored host — only ordinary /proc, df, date, sleep, awk, printf,
# standard bash builtins/coreutils and curl (the single C2 transport
# dependency).
#
# Usage:
#     SENTINEL_NODE=<node> \
#     SENTINEL_ENDPOINT=https://sentinel.example/v1/heartbeat \
#     SENTINEL_TOKEN=<reporter-token> \
#     scripts/sentinel-report.sh
#
# Contracts:
#   - success (HTTP 204 exactly): exit 0, stdout EMPTY — the payload,
#     the response body and the token are never printed;
#   - any configuration/collection/transport failure: non-zero exit,
#     EMPTY stdout, one short generic diagnostic on stderr (never
#     /proc contents, never the token, never the response body);
#   - fail closed: a measurement is never invented, clamped into
#     range or silently normalized. A final pre-render numeric gate
#     additionally rejects any non-finite (inf/nan), exponent or
#     signed token before the single JSON render;
#   - transport is HTTPS-only (curl --proto '=https', default
#     certificate/hostname verification, no --insecure), ambient
#     curl configuration is disabled (--disable, first option),
#     redirects are never followed and only HTTP 204 is success.
#
# Node identity is configuration, never hostname inference:
#   SENTINEL_NODE is required. Documented deterministic reporter
#   constraint on its text: one or more groups of ASCII letters,
#   digits, dot, underscore, hyphen, separated by single spaces; no
#   leading/trailing whitespace, no quotes, no backslashes, no
#   control characters, no other symbols. The accepted value is used
#   VERBATIM as the wire field "node" — no case folding, no trimming,
#   no other mutation ("Prod" and "prod" stay distinct).
#
# Transport configuration (Stage C2):
#   SENTINEL_ENDPOINT is required: the FULL heartbeat endpoint URL,
#   HTTPS only (http:// is rejected), e.g.
#   https://sentinel.example/v1/heartbeat. The value is used
#   VERBATIM — the reporter never derives a URL from the hostname,
#   never adds or normalizes a path, and performs no service
#   discovery. Empty values and values containing whitespace or
#   control characters are rejected before curl is ever invoked.
#   SENTINEL_TOKEN is required: the reporter token. Documented
#   deterministic transport constraint (a narrow subset of the B3
#   in-memory token contract): one or more characters from
#   A-Z a-z 0-9 . _ ~ -  — no whitespace, no CR/LF, no control
#   characters, no quotes. The accepted value is used VERBATIM (no
#   trimming, no case folding) and is transported to curl ONLY
#   through a protected stdin header path (--header @-), never in
#   argv, never in JSON, never on stdout/stderr. Before any child
#   process is spawned the exported SENTINEL_TOKEN variable is
#   captured into a non-exported local variable and removed from the
#   child environment, and shell xtrace is disabled for the whole
#   configuration/transport path so `bash -x` can never print it.
#
# Deterministic test seams (production defaults are the live Linux
# sources; fixtures use these to make the collector testable without
# depending on the developer machine's live /proc):
#   SENTINEL_PROC_UPTIME     path to a /proc/uptime-format file
#   SENTINEL_PROC_LOADAVG    path to a /proc/loadavg-format file
#   SENTINEL_PROC_MEMINFO    path to a /proc/meminfo-format file
#   SENTINEL_PROC_STAT_A     first /proc/stat-format CPU sample file
#   SENTINEL_PROC_STAT_B     second /proc/stat-format CPU sample file
#                            (A and B must be set together; when both
#                            are unset the live /proc/stat is sampled
#                            twice, 1 second apart)
#   SENTINEL_DF_BYTES_FILE   file with the exact output of
#                            `LC_ALL=C df -P -B1 /`
#   SENTINEL_DF_INODES_FILE  file with the exact output of
#                            `LC_ALL=C df -Pi /`
#
# All arithmetic lives in awk (IEEE doubles), never in shell integer
# arithmetic, so huge-but-finite kernel counters cannot overflow.

set -euo pipefail

# Deterministic byte semantics for df/awk/date regardless of the
# ambient environment (decimal separator, column padding, messages).
export LC_ALL=C

PROG_NAME="sentinel-report"

fail() {
    # Short generic diagnostic on stderr; never metric values.
    printf '%s: %s\n' "$PROG_NAME" "$*" >&2
    exit 1
}

# --- node identity -------------------------------------------------------

validate_node_identity() {
    # Non-empty, and matching the documented allowlist above. The
    # allowlist guarantees the value can never break JSON rendering
    # (no quote, backslash or control characters) while keeping the
    # configured identity verbatim.
    local value=${1-}
    [[ -n $value ]] || return 1
    [[ $value =~ ^[A-Za-z0-9_.-]+( [A-Za-z0-9_.-]+)*$ ]]
}

# --- transport configuration (Stage C2) ------------------------------------

# Accepted heartbeat body limit in bytes — exactly the accepted B4
# server limit (MAX_HEARTBEAT_BODY_BYTES). Enforced deterministically
# BEFORE curl is invoked: the C1 payload is ASCII-only under the
# accepted C1 contracts, so the bash string length IS the exact wire
# byte length. An empty or oversized payload fails closed and is
# never truncated.
SENTINEL_MAX_BODY_BYTES=16384

# Fixed single-attempt transport limits (seconds) for the one-shot
# timer-driven reporter. Deliberately NO retry policy of any kind —
# advanced timeout tuning belongs to Stage F.
SENTINEL_CONNECT_TIMEOUT_SECONDS=10
SENTINEL_REQUEST_TIMEOUT_SECONDS=20

validate_endpoint_url() {
    # SENTINEL_ENDPOINT: mandatory, HTTPS-only, used VERBATIM.
    # Rejected before curl is ever invoked: the empty value, any
    # value not starting with the literal `https://` (this includes
    # every `http://` value), and any value containing whitespace or
    # control characters (space, tab, CR, LF, DEL — nothing that
    # could mutate the request line or smuggle a second header).
    # No path is added, nothing is normalized: the configured value
    # is handed to curl exactly as configured.
    local value=${1-}
    [[ -n $value ]] || return 1
    [[ $value =~ ^https://[^[:space:][:cntrl:]]+$ ]]
}

validate_reporter_token() {
    # SENTINEL_TOKEN: mandatory reporter token in the documented
    # transport-safe form — one or more characters from
    # A-Z a-z 0-9 . _ ~ -  (the RFC 3986 unreserved set). No
    # whitespace, no CR/LF, no control characters, no quotes: the
    # value can never break header framing. This is a deliberately
    # narrow transport subset of the broader B3 in-memory token
    # contract; no entropy or minimum-length policy is imposed here
    # (token strength/provisioning belongs to deployment hardening).
    # The accepted value is used VERBATIM — never trimmed,
    # case-folded or otherwise normalized.
    local value=${1-}
    [[ -n $value ]] || return 1
    [[ $value =~ ^[A-Za-z0-9._~-]+$ ]]
}

validate_payload_size() {
    # Deterministic pre-transport size gate. The payload is ASCII
    # (accepted C1 contracts), so ${#1} is the exact byte length the
    # server will receive. Valid: 0 < length <= 16384. The payload
    # is never truncated — oversized input is a hard failure.
    local payload=$1
    local size=${#payload}
    (( size > 0 && size <= SENTINEL_MAX_BODY_BYTES ))
}

# --- /proc parsers (fail closed, print validated values) -----------------

parse_uptime() {
    # First field of a /proc/uptime-format file, in seconds.
    awk '
        NR > 1 { bad = 1; exit }
        {
            if (NF < 1 || $1 !~ /^[0-9]+(\.[0-9]+)?$/) { bad = 1; exit }
            value = $1 + 0
            ok = 1
        }
        END {
            if (bad || !ok) exit 1
            # Finite gate: a digit-only token long enough to overflow
            # the awk IEEE double converts to inf; NaN fails the
            # self-inequality. Neither may become a metric.
            if (value != value || !(value < 1e308)) exit 1
            printf "%.2f\n", value
        }
    ' "$1"
}

parse_loadavg() {
    # First three fields of a /proc/loadavg-format file (1/5/15 min).
    awk '
        NR > 1 { bad = 1; exit }
        {
            if (NF < 3) { bad = 1; exit }
            for (i = 1; i <= 3; i++) {
                if ($i !~ /^[0-9]+(\.[0-9]+)?$/) { bad = 1; exit }
            }
            one = $1 + 0
            five = $2 + 0
            fifteen = $3 + 0
            ok = 1
        }
        END {
            if (bad || !ok) exit 1
            # Finite gate (see parse_uptime).
            if (one != one || five != five || fifteen != fifteen) exit 1
            if (!(one < 1e308) || !(five < 1e308) || !(fifteen < 1e308)) exit 1
            printf "%.2f %.2f %.2f\n", one, five, fifteen
        }
    ' "$1"
}

read_cpu_sample() {
    # First aggregate "cpu" line of a /proc/stat-format file
    # (lines of individual cores like "cpu0" do not match "cpu").
    awk '
        $1 == "cpu" { print; found = 1; exit }
        END { if (!found) exit 1 }
    ' "$1"
}

compute_cpu_percent() {
    # Interval CPU percentage from two aggregate cpu lines:
    #   cpu user nice system idle iowait irq softirq steal
    #     [guest guest_nice]
    # guest/guest_nice are deliberately NOT counted again (user
    # already includes guest on Linux).
    #   idle_all = idle + iowait
    #   non_idle = user + nice + system + irq + softirq + steal
    #   cpu_percent = 100 * (total_delta - idle_delta) / total_delta
    # Fails closed on: wrong shape, non-integer counters, any counter
    # decreasing between samples, total_delta <= 0, or a result that
    # is non-finite or outside [0, 100].
    printf '%s\n%s\n' "$1" "$2" | awk '
        NR > 2 { bad = 1; exit }
        {
            if ($1 != "cpu" || NF < 9) { bad = 1; exit }
            for (i = 2; i <= 9; i++) {
                if ($i !~ /^[0-9]+$/) { bad = 1; exit }
                if (NR == 1) a[i] = $i + 0
                else b[i] = $i + 0
            }
        }
        END {
            if (bad || NR < 2) exit 1
            # Per-component monotonicity: kernel jiffy counters never
            # decrease; a decrease means corrupt input.
            for (i = 2; i <= 9; i++) {
                if (b[i] < a[i]) exit 1
            }
            idle_a = a[5] + a[6]
            idle_b = b[5] + b[6]
            non_a = a[2] + a[3] + a[4] + a[7] + a[8] + a[9]
            non_b = b[2] + b[3] + b[4] + b[7] + b[8] + b[9]
            total_a = idle_a + non_a
            total_b = idle_b + non_b
            total_delta = total_b - total_a
            idle_delta = idle_b - idle_a
            if (!(total_delta > 0)) exit 1
            pct = 100.0 * (total_delta - idle_delta) / total_delta
            if (pct != pct || pct < 0 || pct > 100) exit 1
            printf "%.2f\n", pct
        }
    '
}

parse_ram_usage() {
    # MemTotal/MemAvailable from a /proc/meminfo-format file (KiB).
    # used = (MemTotal - MemAvailable) * 1024 — MemAvailable is used
    # deliberately, NOT MemFree. Physical RAM is never an absent
    # resource (unlike swap): MemTotal == 0 is a corrupt measurement
    # and fails closed. Duplicate or malformed required keys, wrong
    # units, MemAvailable outside [0, MemTotal] and non-finite
    # (overflowed) values fail closed as well.
    awk '
        $1 == "MemTotal:" || $1 == "MemAvailable:" {
            if (seen[$1]) { bad = 1; exit }
            if (NF != 3 || $2 !~ /^[0-9]+$/ || $3 != "kB") { bad = 1; exit }
            seen[$1] = 1
            val[$1] = $2 + 0
        }
        END {
            if (bad) exit 1
            if (!seen["MemTotal:"] || !seen["MemAvailable:"]) exit 1
            # Zero physical RAM is not a valid host state.
            if (!(val["MemTotal:"] > 0)) exit 1
            total = val["MemTotal:"] * 1024
            used = (val["MemTotal:"] - val["MemAvailable:"]) * 1024
            if (used < 0 || used > total) exit 1
            # Finite gate: an overflowed token converts to inf (or
            # inf - inf = nan); neither may become a metric.
            if (!(total < 1e308) || used != used || !(used < 1e308)) exit 1
            pct = used / total * 100.0
            if (pct != pct || pct < 0 || pct > 100) exit 1
            printf "%.0f %.0f %.2f\n", used, total, pct
        }
    ' "$1"
}

parse_swap_usage() {
    # SwapTotal/SwapFree from a /proc/meminfo-format file (KiB).
    # SwapTotal == 0 (with SwapFree == 0) means swap is absent:
    # used=0, total=0, percent=0. Inconsistent input (SwapFree >
    # SwapTotal, or free swap without total) fails closed.
    awk '
        $1 == "SwapTotal:" || $1 == "SwapFree:" {
            if (seen[$1]) { bad = 1; exit }
            if (NF != 3 || $2 !~ /^[0-9]+$/ || $3 != "kB") { bad = 1; exit }
            seen[$1] = 1
            val[$1] = $2 + 0
        }
        END {
            if (bad) exit 1
            if (!seen["SwapTotal:"] || !seen["SwapFree:"]) exit 1
            st = val["SwapTotal:"]
            sf = val["SwapFree:"]
            if (sf > st) exit 1
            if (st == 0) {
                if (sf != 0) exit 1
                used = 0
                total = 0
                pct = 0
            } else {
                total = st * 1024
                used = (st - sf) * 1024
                # Finite gate (see parse_ram_usage).
                if (!(total < 1e308) || used != used || !(used < 1e308)) exit 1
                pct = used / total * 100.0
                if (pct != pct || pct < 0 || pct > 100) exit 1
            }
            printf "%.0f %.0f %.2f\n", used, total, pct
        }
    ' "$1"
}

parse_root_usage() {
    # "used total percent" from POSIX `df -P` output for exactly the
    # root filesystem, validated against the expected C-locale
    # structure of the specific invocation (mode "bytes" for
    # `df -P -B1 /`, mode "inodes" for `df -Pi /`).
    #
    # HEADER (semantic whitespace-separated fields, spacing never
    # assumed): bytes mode must be
    #   Filesystem <size-label> Used Available Capacity Mounted on
    # (the second field — the block-size label — legitimately varies
    # across coreutils versions and is deliberately not overfit);
    # inode mode must be
    #   Filesystem Inodes IUsed IFree IUse% Mounted on.
    # Anything else ("Filesystem garbage", wrong column positions,
    # missing columns) fails closed.
    #
    # DATA ROW: exactly the six semantic `-P` columns — filesystem,
    # total, used, available, percent, mount. Even the fields not
    # used directly for telemetry are validated: total/used/available
    # must be non-negative integer tokens, the capacity column must
    # be an integer 0..100 with a literal "%", and mount must be "/".
    # used <= total and available <= total are enforced. The df
    # capacity value itself is never trusted as telemetry: the
    # percentage is recomputed from used/total.
    local mode=$1
    awk -v mode="$mode" '
        function fail() { bad = 1; exit }
        NR == 1 {
            if (NF != 7) fail()
            if ($1 != "Filesystem") fail()
            if ($6 != "Mounted" || $7 != "on") fail()
            if (mode == "bytes") {
                if ($3 != "Used" || $4 != "Available" || $5 != "Capacity") fail()
            } else {
                if ($2 != "Inodes" || $3 != "IUsed" || $4 != "IFree" \
                    || $5 != "IUse%") fail()
            }
            next
        }
        {
            if (n > 0) fail()
            n = 1
            if (NF != 6) fail()
            if ($6 != "/") fail()
            if ($2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $4 !~ /^[0-9]+$/) fail()
            if ($5 !~ /^[0-9]+%$/) fail()
            cap = substr($5, 1, length($5) - 1) + 0
            if (cap < 0 || cap > 100) fail()
            total = $2 + 0
            used = $3 + 0
            avail = $4 + 0
            # Finite gate: an overflowed digit-only token converts to
            # inf; a real df total can never approach 1e308.
            if (!(total >= 0 && total < 1e308)) fail()
            if (!(used >= 0 && used < 1e308)) fail()
            if (!(avail >= 0 && avail < 1e308)) fail()
            if (used > total) fail()
            if (avail > total) fail()
            pct = 0
            if (total > 0) {
                pct = used / total * 100.0
                if (pct != pct || pct < 0 || pct > 100) fail()
            }
            printf "%.0f %.0f %.2f\n", used, total, pct
        }
        END {
            if (bad || n == 0) exit 1
        }
    ' "$2"
}

collect_root_fs() {
    # Root filesystem usage in bytes: fixture file or live
    # `LC_ALL=C df -P -B1 /` (captured to a temp file so a df failure
    # is detected explicitly, never silently accepted).
    local source=${SENTINEL_DF_BYTES_FILE-}
    if [[ -n $source ]]; then
        parse_root_usage bytes "$source"
        return
    fi
    local out
    out=$(mktemp 2>/dev/null) || return 1
    if ! LC_ALL=C df -P -B1 / >"$out" 2>/dev/null; then
        rm -f -- "$out"
        return 1
    fi
    if ! parse_root_usage bytes "$out"; then
        rm -f -- "$out"
        return 1
    fi
    rm -f -- "$out"
}

collect_root_inodes() {
    # Root filesystem inode usage (counts): fixture file or live
    # `LC_ALL=C df -Pi /`.
    local source=${SENTINEL_DF_INODES_FILE-}
    if [[ -n $source ]]; then
        parse_root_usage inodes "$source"
        return
    fi
    local out
    out=$(mktemp 2>/dev/null) || return 1
    if ! LC_ALL=C df -Pi / >"$out" 2>/dev/null; then
        rm -f -- "$out"
        return 1
    fi
    if ! parse_root_usage inodes "$out"; then
        rm -f -- "$out"
        return 1
    fi
    rm -f -- "$out"
}

# --- final pre-render numeric gate ----------------------------------------

require_metric_decimal() {
    # Last fail-closed defense before the single JSON render: a
    # fractional metric token must be a plain non-negative decimal —
    # exactly the canonical grammar the C1 renderer produces. An
    # empty token, a sign, an exponent form, a locale comma or the
    # inf/nan spelling of a non-finite awk result can never pass.
    local token=${1-}
    [[ -n $token ]] || return 1
    [[ $token =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
}

require_metric_count() {
    # Same gate for byte/count tokens, which the renderer produces
    # as plain non-negative integers.
    local token=${1-}
    [[ -n $token ]] || return 1
    [[ $token =~ ^[0-9]+$ ]] || return 1
}

require_metric_timestamp() {
    # reported_at comes from `date -u` with a fixed format string;
    # anything else never reaches the document.
    local token=${1-}
    local pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$'
    [[ $token =~ $pattern ]] || return 1
}

# --- collection authority (Stage C1, sourceable) ---------------------------
#
# collect_payload: the complete accepted C1 collector — the ONLY
# collection logic in the reporter. Prints exactly one heartbeat JSON
# document (the accepted B3 wire payload) on stdout and nothing else;
# any failure is a non-zero exit with NO partial JSON on stdout and a
# short generic stderr diagnostic. Kept as a sourceable function (the
# script is a no-op when sourced) so C1 fixture tests exercise the
# ACTUAL production collection logic; the executable path (main
# below) validates transport configuration, invokes this function and
# performs the single HTTPS delivery.

collect_payload() {
    local node=${SENTINEL_NODE-}
    if ! validate_node_identity "$node"; then
        fail "SENTINEL_NODE is required and must match the documented node identity constraint"
    fi

    local uptime
    uptime=$(parse_uptime "${SENTINEL_PROC_UPTIME:-/proc/uptime}") \
        || fail "cannot collect uptime"

    local load_line
    load_line=$(parse_loadavg "${SENTINEL_PROC_LOADAVG:-/proc/loadavg}") \
        || fail "cannot collect load average"
    local load_one load_five load_fifteen
    read -r load_one load_five load_fifteen <<<"$load_line"

    # CPU is an interval measurement over two samples. Fixture mode
    # (both sample files set) skips the sleep; production samples the
    # live /proc/stat 1 second apart.
    local stat_a=${SENTINEL_PROC_STAT_A-}
    local stat_b=${SENTINEL_PROC_STAT_B-}
    local cpu_a cpu_b
    if [[ -n $stat_a || -n $stat_b ]]; then
        if [[ -z $stat_a || -z $stat_b ]]; then
            fail "SENTINEL_PROC_STAT_A and SENTINEL_PROC_STAT_B must be set together"
        fi
        cpu_a=$(read_cpu_sample "$stat_a") || fail "cannot collect CPU sample"
        cpu_b=$(read_cpu_sample "$stat_b") || fail "cannot collect CPU sample"
    else
        cpu_a=$(read_cpu_sample /proc/stat) || fail "cannot collect CPU sample"
        sleep 1
        cpu_b=$(read_cpu_sample /proc/stat) || fail "cannot collect CPU sample"
    fi
    local cpu_percent
    cpu_percent=$(compute_cpu_percent "$cpu_a" "$cpu_b") \
        || fail "cannot compute CPU usage"

    local meminfo=${SENTINEL_PROC_MEMINFO:-/proc/meminfo}
    local ram_line swap_line
    ram_line=$(parse_ram_usage "$meminfo") || fail "cannot collect RAM usage"
    swap_line=$(parse_swap_usage "$meminfo") || fail "cannot collect swap usage"
    local ram_used ram_total ram_percent
    read -r ram_used ram_total ram_percent <<<"$ram_line"
    local swap_used swap_total swap_percent
    read -r swap_used swap_total swap_percent <<<"$swap_line"

    local root_fs_line root_inodes_line
    root_fs_line=$(collect_root_fs) || fail "cannot collect root filesystem usage"
    root_inodes_line=$(collect_root_inodes) || fail "cannot collect root inode usage"
    local fs_used fs_total fs_percent
    read -r fs_used fs_total fs_percent <<<"$root_fs_line"
    local ino_used ino_total ino_percent
    read -r ino_used ino_total ino_percent <<<"$root_inodes_line"

    # Host UTC time, timezone-aware ISO 8601 (e.g.
    # 2026-09-10T08:00:00+00:00). The reporter owns only reported_at;
    # the central received_at is B2 responsibility.
    local reported_at
    reported_at=$(date -u +%Y-%m-%dT%H:%M:%S+00:00) \
        || fail "cannot read host clock"

    # FINAL NUMERIC GATE: the mandatory last fail-closed defense.
    # Every token interpolated into the JSON document must match the
    # exact canonical grammar the renderer produces — a non-finite
    # awk result (inf/nan), an exponent form, a sign or an empty
    # token fails non-zero BEFORE the single render below, so no
    # invalid JSON can ever be emitted.
    local token
    for token in \
        "$uptime" \
        "$load_one" "$load_five" "$load_fifteen" \
        "$cpu_percent" \
        "$ram_percent" "$swap_percent" \
        "$fs_percent" "$ino_percent"
    do
        require_metric_decimal "$token" \
            || fail "metric validation failed before rendering"
    done
    for token in \
        "$ram_used" "$ram_total" \
        "$swap_used" "$swap_total" \
        "$fs_used" "$fs_total" \
        "$ino_used" "$ino_total"
    do
        require_metric_count "$token" \
            || fail "metric validation failed before rendering"
    done
    require_metric_timestamp "$reported_at" \
        || fail "timestamp validation failed before rendering"

    # Single deterministic render. Every numeric argument below has
    # just passed the final gate, so each is a plain JSON number,
    # never a numeric string. The node value is allowlist-validated,
    # so it cannot break JSON structure. No partial document can ever
    # appear: this printf runs only after every collection and
    # validation succeeded.
    printf '{"node":"%s","reported_at":"%s","uptime_seconds":%s,"load":{"one":%s,"five":%s,"fifteen":%s},"cpu_percent":%s,"ram":{"used":%s,"total":%s,"percent":%s},"swap":{"used":%s,"total":%s,"percent":%s},"root_fs":{"used":%s,"total":%s,"percent":%s},"root_inodes":{"used":%s,"total":%s,"percent":%s}}\n' \
        "$node" \
        "$reported_at" \
        "$uptime" \
        "$load_one" "$load_five" "$load_fifteen" \
        "$cpu_percent" \
        "$ram_used" "$ram_total" "$ram_percent" \
        "$swap_used" "$swap_total" "$swap_percent" \
        "$fs_used" "$fs_total" "$fs_percent" \
        "$ino_used" "$ino_total" "$ino_percent"
}

# --- transport (Stage C2) ---------------------------------------------------

send_heartbeat() {
    # Exactly ONE outbound HTTPS POST attempt — no retry, no
    # retry-after, no backoff, no loop. Arguments:
    #   $1 endpoint — validated https:// URL, used verbatim;
    #   $2 payload  — the exact C1 JSON document (size-gated);
    #   $3 token    — validated reporter token, a NON-EXPORTED copy.
    #
    # curl invocation policy:
    #   - `--disable` is the FIRST curl option: loading of ambient
    #     curl configuration (CURL_HOME/.curlrc, XDG config,
    #     ~/.curlrc) is prevented entirely, so a host- or user-level
    #     curl config can never inject --insecure, --location,
    #     --retry, extra headers or tracing options into the reporter
    #     request. The frozen C2 runtime invariants hold for the
    #     EFFECTIVE invocation, not merely for this script's explicit
    #     argv (curl only honors the config-disabling option in first
    #     position, hence the placement);
    #   - POST with Content-Type: application/json;
    #   - `-H 'Expect:'` is curl's documented header-suppression
    #     form: the Expect: 100-continue header curl would otherwise
    #     emit for larger bodies is NOT transmitted at all (the
    #     accepted B5 rejects ANY Expect header with 417). It does
    #     NOT send an empty Expect header on the wire;
    #   - the token reaches curl ONLY through stdin (`--header @-`
    #     reads additional header lines from stdin): it is never an
    #     argv element, so it can never appear in a process listing;
    #   - `--proto '=https'`: HTTPS only — non-HTTPS protocols can
    #     never be used even by a misconfigured endpoint value;
    #   - no --location: a redirect (3xx) is a failure, never a
    #     second request;
    #   - `--data-binary`: known body length, ordinary Content-Length
    #     framing, never Transfer-Encoding: chunked, no compression;
    #   - `--silent` + `--output /dev/null`: response body and
    #     progress meter never reach reporter stdout; only the HTTP
    #     status code is captured via `--write-out '%{http_code}'`;
    #   - raw curl stderr is suppressed: on failure the reporter
    #     emits its own short generic diagnostic (never token,
    #     header, payload or response-body values);
    #   - fixed single-attempt limits: connect timeout 10 s, overall
    #     request timeout 20 s.
    local endpoint=$1
    local payload=$2
    local token=$3
    local status

    status=$(
        printf 'X-Sentinel-Token: %s\n' "$token" | curl \
            --disable \
            --request POST \
            --header 'Content-Type: application/json' \
            --header 'Expect:' \
            --header @- \
            --data-binary "$payload" \
            --proto '=https' \
            --connect-timeout "$SENTINEL_CONNECT_TIMEOUT_SECONDS" \
            --max-time "$SENTINEL_REQUEST_TIMEOUT_SECONDS" \
            --silent \
            --output /dev/null \
            --write-out '%{http_code}' \
            "$endpoint" 2>/dev/null
    ) || return 1

    # Success requires the final HTTP status to be EXACTLY 204 — not
    # merely any 2xx. 200/201/202, 3xx, 4xx and 5xx all fail. A
    # redirect surfaces here as its own status code (never followed)
    # and fails; a curl-level failure (DNS, TCP, TLS validation,
    # timeout, non-zero exit) already returned 1 above.
    [[ $status == 204 ]]
}

# --- executable path (Stage C2): validate → collect → size-gate → send ----

main() {
    # Secret safety: shell xtrace (bash -x) would print SENTINEL_TOKEN
    # the moment it is read into a variable or passed as a function
    # argument. Disable xtrace for the ENTIRE configuration/transport
    # path; it is restored only after the one-shot heartbeat attempt
    # has completed. (Failure paths exit the process — there is
    # nothing left to restore.)
    local restore_xtrace=0
    if [[ $- == *x* ]]; then
        restore_xtrace=1
        set +x
    fi

    # Transport configuration is validated BEFORE the expensive
    # collection: an invalid endpoint/token or a missing curl fails
    # immediately, and no request can ever be emitted with incomplete
    # or invalid configuration.
    local endpoint=${SENTINEL_ENDPOINT-}
    if ! validate_endpoint_url "$endpoint"; then
        fail "SENTINEL_ENDPOINT is required and must be an https:// URL"
    fi

    local token=${SENTINEL_TOKEN-}
    if ! validate_reporter_token "$token"; then
        fail "SENTINEL_TOKEN is required and must match the documented reporter token constraint"
    fi

    command -v curl >/dev/null 2>&1 \
        || fail "curl is required for heartbeat delivery"

    # The token is captured into a NON-EXPORTED local variable and
    # removed from the environment BEFORE any child process is
    # spawned: neither curl nor any collection helper can inherit it.
    # It reaches curl exclusively through the protected stdin header
    # path inside send_heartbeat — never through argv.
    unset SENTINEL_TOKEN

    # Collection: the exact accepted C1 payload from the single
    # collection authority. A collection failure fails the whole
    # report BEFORE curl is invoked. The C1-internal diagnostic is
    # suppressed here so the executable reporter emits exactly one
    # short generic message of its own.
    local payload
    payload=$(collect_payload 2>/dev/null) \
        || fail "cannot collect telemetry"

    # Deterministic pre-transport size gate: empty or > 16384 bytes
    # never invokes curl, and the payload is never truncated.
    if ! validate_payload_size "$payload"; then
        fail "heartbeat payload size is outside the accepted transport limit"
    fi

    send_heartbeat "$endpoint" "$payload" "$token" \
        || fail "heartbeat delivery failed"

    # Success: exit 0 with EMPTY stdout — the telemetry payload, the
    # response body and the token are never printed.
    if (( restore_xtrace )); then
        set -x
    fi
    return 0
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
