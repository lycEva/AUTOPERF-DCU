#!/bin/bash
set -u

CONTAINER_NAME=${1:-}
[ -n "$CONTAINER_NAME" ] || { echo 0; exit 0; }

# Use the same GPU PID set as get_vram_mib.sh. docker top reports host PIDs.
TOP=$(docker top "$CONTAINER_NAME" -eo pid,ppid,pcpu 2>/dev/null) || TOP=""
GPU_PIDS=$(rocm-smi --showpids 2>/dev/null | awk '/^PID:/ && $2 ~ /^[0-9]+$/ { print $2 }')

# Each EXEC sample is a new process, so retain per-PID CPU tick baselines.
# Locking avoids races if a sample takes longer than the polling interval.
SAFE_NAME=$(printf '%s' "$CONTAINER_NAME" | tr -c 'A-Za-z0-9_.-' '_')
# Generated per-run wrappers override this to avoid sharing CPU baselines.
STATE_DIR=${XNCS_CPU_STATE_DIR:-${TMPDIR:-/tmp}/xncs-cpu-monitor}
mkdir -p "$STATE_DIR" 2>/dev/null || { echo 0; exit 0; }
STATE_FILE="$STATE_DIR/${SAFE_NAME}.state"
LOCK_FILE="$STATE_DIR/${SAFE_NAME}.lock"
exec 9>"$LOCK_FILE" || { echo 0; exit 0; }
flock -x 9 || { echo 0; exit 0; }

NOW_NS=$(date +%s%N)
CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
[ "$CLK_TCK" -gt 0 ] 2>/dev/null || CLK_TCK=100
PROC_ROOT=${PROC_ROOT:-/proc}
PREV=$(cat "$STATE_FILE" 2>/dev/null || true)
TMP_FILE="$STATE_FILE.$$"
: > "$TMP_FILE" || { echo 0; exit 0; }

CPU_TOTAL=$(printf '%s\n' "$TOP" | awk \
    -v gpu="$GPU_PIDS" -v prev="$PREV" -v now="$NOW_NS" \
    -v hz="$CLK_TCK" -v procroot="$PROC_ROOT" -v statefile="$TMP_FILE" '
    BEGIN {
        n = split(gpu, g, /[[:space:]]+/)
        for (i = 1; i <= n; i++) if (g[i] ~ /^[0-9]+$/) uses_gpu[g[i]] = 1
        n = split(prev, lines, /\n/)
        for (i = 1; i <= n; i++) {
            split(lines[i], p, /[[:space:]]+/)
            if (p[1] ~ /^[0-9]+$/) {
                old_cpu[p[1]] = p[2]
                old_start[p[1]] = p[3]
                old_time[p[1]] = p[4]
            }
        }
    }
    NR > 1 && $1 ~ /^[0-9]+$/ && ($1 in uses_gpu) {
        pid = $1
        proc = procroot "/" pid "/stat"
        if ((getline stat < proc) > 0) {
            close(proc)
            # Strip pid and comm; remaining field 12/13 are utime/stime,
            # and field 20 is starttime (protects against PID reuse).
            sub(/^.*\) /, "", stat)
            split(stat, s, /[[:space:]]+/)
            ticks = s[12] + s[13]
            start = s[20]
            if (start != "") {
                if ((pid in old_cpu) && old_start[pid] == start && now > old_time[pid]) {
                    delta = ticks - old_cpu[pid]
                    if (delta > 0) total += delta * 100000000000 / (hz * (now - old_time[pid]))
                }
                print pid, ticks, start, now >> statefile
            }
        }
    }
    END { printf "%.0f\n", total + 0 }
')

if mv -f "$TMP_FILE" "$STATE_FILE" 2>/dev/null; then
    :
else
    echo 0
    exit 0
fi

[ -n "$CPU_TOTAL" ] || CPU_TOTAL=0
echo "$CPU_TOTAL"
