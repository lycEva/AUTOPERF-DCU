#!/usr/bin/python3
"""Sample DCU VRAM of a container's GPU processes into a CSV time series.

Usage: vram_series.py <container> <out.csv> <interval_seconds> [stop_file]
Long format: epoch,iso,pid,vram_mib,vram_pct,rss_gib,total_mib,avail_gpu0,avail_gpu1
"""
import csv
import datetime
import os
import subprocess
import sys
import time

container = sys.argv[1]
out = sys.argv[2]
interval = float(sys.argv[3])
stop_file = sys.argv[4] if len(sys.argv) > 4 else None


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def container_pids():
    out = sh("docker top %s -eo pid,ppid,rss 2>/dev/null" % container)
    res = {}
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[0].isdigit() and p[2].isdigit():
            res[int(p[0])] = int(p[2])
    return res


def gpu_state():
    """Return ({pid: (mib, pct)}, {gpu_index: avail_mib})."""
    text = sh("rocm-smi --showpids 2>/dev/null")
    procs, cur, avail = {}, None, {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("PID:") and s[4:].strip().isdigit():
            cur = int(s.split()[1])
            procs.setdefault(cur, [0, 0])
        elif "VRAM USED(MiB):" in s and cur is not None:
            try:
                procs[cur][0] = int(float(s.split(":")[1]))
            except Exception:
                pass
        elif "VRAM USED(%):" in s and cur is not None:
            try:
                procs[cur][1] = int(float(s.split(":")[1]))
            except Exception:
                pass
    for line in sh("hy-smi --showmemavailable 2>/dev/null").splitlines():
        if "Available memory size (MiB)" in line:
            try:
                idx = int(line.split("HCU[")[1].split("]")[0])
                avail[idx] = int(line.rsplit(":", 1)[1])
            except Exception:
                pass
    return procs, avail


fh = open(out, "w", newline="", encoding="utf-8")
w = csv.writer(fh)
w.writerow(["epoch", "iso", "pid", "vram_mib", "vram_pct", "rss_gib",
            "total_mib", "avail_gpu0", "avail_gpu1"])
while True:
    if stop_file and os.path.exists(stop_file):
        break
    now = time.time()
    iso = datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds")
    pids = container_pids()
    gpus, avail = gpu_state()
    total = 0
    rows = []
    for pid in pids:
        if pid in gpus:
            mib, pct = gpus[pid]
            total += mib
            rows.append([int(now), iso, pid, mib, pct,
                         round(pids[pid] / 1048576.0, 2), None,
                         avail.get(0, ""), avail.get(1, "")])
    if not rows:
        rows.append([int(now), iso, "", 0, 0, "", None,
                     avail.get(0, ""), avail.get(1, "")])
    for r in rows:
        r[6] = total
        w.writerow(r)
    fh.flush()
    time.sleep(interval)
fh.close()
print("sampler done")
