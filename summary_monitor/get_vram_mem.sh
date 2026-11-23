#!/bin/bash

CONTAINER_NAME=${1:-}
[ -n "$CONTAINER_NAME" ] || { echo 0; exit 0; }

# Limit RSS to the same container processes that have GPU allocations.
TOP=$(docker top "$CONTAINER_NAME" -eo pid,ppid,rss 2>/dev/null) || TOP=""
PIDS=$(printf '%s\n' "$TOP" | awk 'NR > 1 && $1 ~ /^[0-9]+$/ { print $1 }')
[ -n "$PIDS" ] || { echo 0; exit 0; }
GPU_PIDS=$(rocm-smi --showpids 2>/dev/null | awk '/^PID:/ && $2 ~ /^[0-9]+$/ { print $2 }')
[ -n "$GPU_PIDS" ] || { echo 0; exit 0; }

# docker top RSS is KiB. Sum matched processes, report GiB with precision.
printf '%s\n' "$TOP" | awk -v pids="$PIDS" -v gpu="$GPU_PIDS" '
    BEGIN {
        n = split(pids, a, /[[:space:]]+/)
        for (i = 1; i <= n; i++) if (a[i] ~ /^[0-9]+$/) in_container[a[i]] = 1
        n = split(gpu, b, /[[:space:]]+/)
        for (i = 1; i <= n; i++) if (b[i] ~ /^[0-9]+$/) uses_gpu[b[i]] = 1
    }
    NR > 1 && ($1 in in_container) && ($1 in uses_gpu) && $3 ~ /^[0-9]+$/ {
        total_kib += $3
    }
    END { printf "%.2f\n", total_kib / 1048576 }
'
