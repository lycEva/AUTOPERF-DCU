#!/usr/bin/env bash

CONTAINER_NAME=${1:-}
[ -n "$CONTAINER_NAME" ] || { echo 0; exit 0; }

PIDS=$(docker top "$CONTAINER_NAME" -eo pid,ppid,pcpu 2>/dev/null |
    awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }')
[ -n "$PIDS" ] || { echo 0; exit 0; }

# This metric is per-process VRAM-used percentage, not GPU compute/DCU utilization.
# Sum the percentages for the in-container GPU processes, matching the MiB series.
rocm-smi --showpids 2>/dev/null | awk -v allowed="$PIDS" '
    BEGIN {
        n = split(allowed, pids, /[[:space:]]+/)
        for (i = 1; i <= n; i++) if (pids[i] ~ /^[0-9]+$/) valid[pids[i]] = 1
    }
    /^[[:space:]]*PID:[[:space:]]*[0-9]+/ {
        matched = ($2 in valid)
        next
    }
    matched && $1 == "VRAM" && $2 == "USED(%):" && $3 ~ /^[0-9]+([.][0-9]+)?$/ {
        total += $3
    }
    END { printf "%.0f\n", total + 0 }
'
