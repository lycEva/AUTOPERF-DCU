#!/bin/bash

CONTAINER_NAME=${1:-}
[ -n "$CONTAINER_NAME" ] || { echo 0; exit 0; }

# Restrict VRAM to the same in-container GPU PID set used by CPU monitoring.
PIDS=$(docker top "$CONTAINER_NAME" -eo pid,ppid,pcpu 2>/dev/null | awk '
    NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }
')
[ -n "$PIDS" ] || { echo 0; exit 0; }
command -v rocm-smi >/dev/null 2>&1 || { echo 0; exit 0; }

# Sum per-process VRAM (MiB), rather than reporting only the largest process.
rocm-smi --showpids 2>/dev/null | awk -v allowed="$PIDS" '
    BEGIN {
        n = split(allowed, pids, /[[:space:]]+/)
        for (i = 1; i <= n; i++) if (pids[i] ~ /^[0-9]+$/) valid[pids[i]] = 1
    }
    /^PID:/ {
        matched = ($2 in valid)
        next
    }
    matched && /VRAM USED\(MiB\):/ && $3 ~ /^[0-9]+([.][0-9]+)?$/ {
        total += $3
    }
    END { printf "%.0f\n", total + 0 }
'
