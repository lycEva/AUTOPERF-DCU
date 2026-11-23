#!/usr/bin/python3
"""Plot the VRAM time series CSV produced by vram_series.py into a PNG.

Usage: plot_vram.py <vram.csv> <out.png> <title>
Pure stdlib (zlib/struct) so it runs on the offline test host.
"""
import csv
import struct
import sys
import zlib
from collections import OrderedDict

src, dst = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else "VRAM over time"

series = OrderedDict()  # iso -> {pid: mib}
rows = list(csv.DictReader(open(src, encoding="utf-8")))
for r in rows:
    if not r["pid"]:
        continue
    series.setdefault(r["epoch"], {})[r["pid"]] = int(r["vram_mib"])
epochs = sorted(series, key=int)
if not epochs:
    sys.exit("no data")
t0 = int(epochs[0])
xs = [(int(e) - t0) / 60.0 for e in epochs]
pids = sorted({p for v in series.values() for p in v})
totals = [sum(series[e].values()) / 1024.0 for e in epochs]
perc = {p: [series[e].get(p, 0) / 1024.0 for e in epochs] for p in pids}

W, H, L, R, T, B = 1100, 620, 80, 30, 60, 70
pw, ph = W - L - R, H - T - B
ymax = max(max(totals), max((max(v) for v in perc.values()), default=1))
ymax = (int(ymax / 4) + 1) * 4
xmax = max(xs) if xs else 1.0

img = bytearray(b"\xff" * (W * H * 3))


def px(x, y, color):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 3
        img[i:i + 3] = bytes(color)


def line(x0, y0, x1, y1, color, w=2):
    dx, dy = x1 - x0, y1 - y0
    n = int(max(abs(dx), abs(dy))) or 1
    for k in range(n + 1):
        x = int(round(x0 + dx * k / n))
        y = int(round(y0 + dy * k / n))
        for ox in range(w):
            for oy in range(w):
                px(x + ox, y + oy, color)


def sx(x):
    return int(L + (x / xmax) * pw)


def sy(y):
    return int(T + ph - (y / ymax) * ph)


grid = (225, 228, 232)
axis = (120, 125, 132)
for g in range(0, ymax + 1, max(4, ymax // 8)):
    y = sy(g)
    line(L, y, L + pw, y, grid, 1)
for g in range(0, int(xmax) + 1, max(1, int(xmax) // 10 or 1)):
    x = sx(g)
    line(x, T, x, T + ph, grid, 1)
line(L, T, L, T + ph, axis, 2)
line(L, T + ph, L + pw, T + ph, axis, 2)

colors = [(220, 50, 47), (38, 139, 210), (133, 153, 0), (211, 54, 130)]
for idx, p in enumerate(pids):
    ys = perc[p]
    for i in range(len(xs) - 1):
        line(sx(xs[i]), sy(ys[i]), sx(xs[i + 1]), sy(ys[i + 1]), colors[idx % len(colors)], 2)
for i in range(len(xs) - 1):
    line(sx(xs[i]), sy(totals[i]), sx(xs[i + 1]), sy(totals[i + 1]), (60, 60, 60), 3)


def glyph(ch):
    table = {
        "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
        "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
        "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
        "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
        "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
        ".": ["000", "000", "000", "000", "010"], "-": ["000", "000", "111", "000", "000"],
        " ": ["000", "000", "000", "000", "000"],
    }
    return table.get(ch, table[" "])


def text(x, y, s, color=(40, 40, 40), scale=2):
    for ch in s:
        g = glyph(ch)
        for r, bits in enumerate(g):
            for c, b in enumerate(bits):
                if b == "1":
                    for ox in range(scale):
                        for oy in range(scale):
                            px(x + c * scale + ox, y + r * scale + oy, color)
        x += 4 * scale
    return x


for g in range(0, ymax + 1, max(4, ymax // 8)):
    text(10, sy(g) - 5, str(g))
text(10, T - 34, "GiB")
text(L + pw - 150, T + ph + 34, "minutes")
text(L, 18, title, (20, 20, 20), 2)
lx = L + 20
for idx, p in enumerate(pids):
    px(lx, 42, colors[idx % len(colors)])
    px(lx + 1, 41, colors[idx % len(colors)])
    lx = text(lx + 14, 38, "worker " + p, colors[idx % len(colors)], 2) + 20
px(lx, 42, (60, 60, 60))
lx = text(lx + 14, 38, "total", (60, 60, 60), 2) + 20

raw = b"".join(b"\x00" + bytes(img[r * W * 3:(r + 1) * W * 3]) for r in range(H))


def chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


png = (b"\x89PNG\r\n\x1a\n" +
       chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) +
       chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
open(dst, "wb").write(png)
print("wrote", dst)
print("workers=%s max_total=%.1f GiB duration=%.1f min" % (pids, max(totals), xmax))
