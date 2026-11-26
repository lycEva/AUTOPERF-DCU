"""Tests for the dependency-free SVG metric reports."""

import csv
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


MODULE_PATH = Path(__file__).resolve().parents[1] / "summary_monitor" / "plot_metrics.py"
SPEC = importlib.util.spec_from_file_location("plot_metrics_under_test", MODULE_PATH)
plot_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plot_metrics)
NS = {"s": "http://www.w3.org/2000/svg"}
COLUMNS = ["timestamp", "elapsed_s", "container", "cpu_pct", "vram_mib", "vram_pct",
           "memory_gib", "status", "error", "sample_duration_s"]


class PlotMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "samples.csv"
        self.output = self.root / "report"

    def render_rows(self, rows):
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return plot_metrics.render(self.source, self.output, svg=True)

    def row(self, elapsed, cpu="", vram="", utilization="", memory="", **extra):
        result = dict(timestamp=f"2026-11-10T13:40:{int(elapsed) % 60:02d}+08:00",
                      elapsed_s=elapsed, container="demo", cpu_pct=cpu, vram_mib=vram,
                      vram_pct=utilization, memory_gib=memory, status="ok")
        result.update(extra)
        return result

    def xml(self, name="cpu"):
        return ET.parse(self.output / f"{name}.svg").getroot()

    def texts(self, name="cpu"):
        return " ".join(self.xml(name).itertext())

    def test_full_data_peaks_units_and_outputs(self):
        rows = [self.row(0, 12, 80, 20, 1), self.row(3, 95, 70, 80, 3),
                self.row(9, 20, 200, 35, 2)]
        summary = self.render_rows(rows)
        for column, maximum, index in [("cpu_pct", 95, 1), ("vram_mib", 200, 2),
                                        ("vram_pct", 80, 1), ("memory_gib", 3, 1)]:
            metric = summary["metrics"][column]
            self.assertEqual(metric["max"], maximum)
            self.assertEqual(metric["valid_samples"], 3)
            self.assertEqual(metric["peak_timestamp"], rows[index]["timestamp"])
            self.assertEqual(metric["peak_elapsed_s"], rows[index]["elapsed_s"])
        self.assertEqual(json.loads((self.output / "summary.json").read_text()), summary)
        self.assertEqual({p.name for p in self.output.iterdir()},
                         {"cpu.svg", "vram.svg", "vram_utilization.svg", "memory.svg", "overview.svg", "summary.json",
                          "cpu.png", "vram.png", "vram_utilization.png", "memory.png"})
        for name, label in [("cpu", "CPU (%)"), ("vram", "VRAM (MiB)"),
                            ("vram_utilization", "VRAM utilization (%)"), ("memory", "Memory (GiB)")]:
            self.assertIn(label, self.texts(name))
            self.assertIn("demo", self.texts(name))
        self.assertEqual(len(self.xml("overview").findall("s:g", NS)), 4)

    def test_four_png_outputs_have_valid_signature_and_dimensions(self):
        import struct
        import zlib
        self.render_rows([self.row(0, 20, 100, 10, 1), self.row(1, 80, 200, 20, 2)])
        for name in ('cpu', 'vram', 'vram_utilization', 'memory'):
            data = (self.output / (name + '.png')).read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', data[16:24]), (2000, 680))
            offset = 8
            compressed = b''
            while offset < len(data):
                size = struct.unpack('>I', data[offset:offset + 4])[0]
                kind = data[offset + 4:offset + 8]
                payload = data[offset + 8:offset + 8 + size]
                checksum = struct.unpack('>I', data[offset + 8 + size:offset + 12 + size])[0]
                self.assertEqual(zlib.crc32(kind + payload) & 0xffffffff, checksum)
                if kind == b'IDAT':
                    compressed += payload
                offset += size + 12
            self.assertGreater(len(zlib.decompress(compressed)), 2000 * 680)

    def test_cli_default_writes_png_only_and_keep_svg_is_opt_in(self):
        self.render_rows([self.row(0, 20, 100, 10, 1)])
        default = self.root / 'png-only'
        result = subprocess.run([sys.executable, str(MODULE_PATH), str(self.source),
                                 '--output-dir', str(default)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({item.name for item in default.iterdir()},
                         {"cpu.png", "vram.png", "vram_utilization.png", "memory.png", "summary.json"})
        debug = self.root / 'with-svg'
        result = subprocess.run([sys.executable, str(MODULE_PATH), str(self.source),
                                 '--output-dir', str(debug), '--keep-svg'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(debug.glob('*.svg'))), 5)
        self.assertEqual(len(list(debug.glob('*.png'))), 4)

    def test_render_default_does_not_write_svg_files(self):
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows([self.row(0, 20, 100, 10, 1)])
        output = self.root / "flat"
        plot_metrics.render(self.source, output)
        self.assertEqual({item.name for item in output.iterdir()},
                         {"cpu.png", "vram.png", "vram_utilization.png", "memory.png", "summary.json"})

    def test_percentage_y_ticks_in_single_charts_and_overview(self):
        self.render_rows([self.row(0, 0, 100, 0, 1), self.row(1, 200, 200, 80, 2)])
        def y_ticks(root):
            return [node.text for node in root.findall(
                ".//s:text[@x='{}']".format(plot_metrics.LEFT - 9), NS)]
        self.assertEqual(y_ticks(self.xml('cpu')), ['0%', '50%', '100%', '150%', '200%'])
        self.assertEqual(y_ticks(self.xml('vram_utilization')), ['0%', '20%', '40%', '60%', '80%'])
        for name in ('vram', 'memory'):
            ticks = y_ticks(self.xml(name))
            self.assertEqual(len(ticks), 5)
            self.assertTrue(all('%' not in text for text in ticks))
        panels = self.xml('overview').findall('s:g', NS)
        self.assertEqual([all(text.endswith('%') for text in y_ticks(panel))
                          for panel in panels], [True, False, True, False])

    def test_x_axis_ticks_and_label_are_minutes(self):
        self.render_rows([self.row(0, 1), self.row(600, 2), self.row(1200, 3)])
        tick_y = str(plot_metrics.TOP + (plot_metrics.HEIGHT - plot_metrics.TOP
                                         - plot_metrics.BOTTOM) + 20)
        x_ticks = [node.text for node in self.xml().findall(
            ".//s:text[@y='{}']".format(tick_y), NS)]
        self.assertEqual(x_ticks, ['0', '5', '10', '15', '20'])
        self.assertIn("Elapsed time (min)", self.texts())
        self.assertNotIn("Elapsed time (s)", self.texts())

    def test_tiny_nonzero_origin_does_not_become_scientific_notation(self):
        # The first sample lands 0.16 ms after the run start; the origin tick
        # used to read 2.68e-06 min.
        self.render_rows([self.row(0.0001608, 1), self.row(1000, 2), self.row(2069.7, 3)])
        tick_y = str(plot_metrics.TOP + (plot_metrics.HEIGHT - plot_metrics.TOP
                                         - plot_metrics.BOTTOM) + 20)
        x_ticks = [node.text for node in self.xml().findall(
            ".//s:text[@y='{}']".format(tick_y), NS)]
        self.assertEqual(x_ticks[0], '0')
        self.assertFalse([text for text in x_ticks if 'e' in text.lower()])
        points = self.xml().find(".//s:polyline", NS).attrib["points"].split()
        x = [float(p.split(",")[0]) for p in points]
        self.assertAlmostEqual(x[0], plot_metrics.LEFT, places=1)

    def test_y_ticks_use_two_significant_digits(self):
        self.render_rows([self.row(0, 0, 100, 20, 1),
                          self.row(60, 200, 100, 25, 7.61076)])
        def y_ticks(root):
            return [node.text for node in root.findall(
                ".//s:text[@x='{}']".format(plot_metrics.LEFT - 9), NS)]
        self.assertEqual(y_ticks(self.xml('memory')), ['0', '1.9', '3.8', '5.7', '7.6'])
        # The peak readout uses the same two significant digits as the axis.
        self.assertIn("Peak: 7.611 GiB", self.texts('memory'))
        # Two significant digits must not become 1.5e+02 on the percentage axis.
        self.assertEqual(y_ticks(self.xml('cpu')), ['0%', '50%', '100%', '150%', '200%'])
        self.assertEqual(y_ticks(self.xml('vram')), ['0', '25', '50', '75', '100'])
        for name in ('cpu', 'vram', 'vram_utilization', 'memory'):
            self.assertFalse([t for t in y_ticks(self.xml(name)) if 'e' in t.lower()])

    def test_partial_samples_and_missing_breaks(self):
        summary = self.render_rows([self.row(0, 1), self.row(1, 2), self.row(2, "", 99),
                                    self.row(3, 4), self.row(4, 5, status="error", error="partial")])
        lines = self.xml().findall(".//s:polyline", NS)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line.attrib["points"].split()) == 2 for line in lines))
        self.assertEqual(summary["metrics"]["cpu_pct"]["valid_samples"], 4)
        self.assertEqual(summary["metrics"]["cpu_pct"]["max"], 5)
        self.assertEqual(summary["metrics"]["vram_mib"]["max"], 99)

    def test_no_data_and_nonfinite_values(self):
        summary = self.render_rows([self.row(0, "NaN", "Inf", "-inf"), self.row(1)])
        for metric in summary["metrics"].values():
            self.assertEqual(metric["valid_samples"], 0)
            self.assertIsNone(metric["max"])
            self.assertIsNone(metric["peak_timestamp"])
        self.assertIn("No valid samples", self.texts())
        self.assertIn("No valid data", self.texts())
        self.assertIn("Valid samples: 0 | Missing samples: 2", self.texts())
        self.assertNotIn("Peak: 0", self.texts())
        self.assertFalse(self.xml().findall(".//s:polyline", NS))
        self.assertFalse(self.xml().findall(".//s:circle", NS))
        # The full empty CSV is also a valid report, not an exception.
        self.assertEqual(self.render_rows([])["sample_count"], 0)
        self.assertIn("No valid samples", self.texts("overview"))

    def test_single_and_constant_zero_samples(self):
        summary = self.render_rows([self.row(42, 0, 0, 0, 0)])
        self.assertEqual(summary["metrics"]["cpu_pct"]["max"], 0)
        self.assertEqual(len(self.xml().findall(".//s:circle[@r='2']", NS)), 1)
        self.assertIn("Peak: 0 %", self.texts())
        self.render_rows([self.row(0, 7), self.row(10, 7), self.row(20, 7)])
        points = self.xml().find(".//s:polyline", NS).attrib["points"].split()
        self.assertEqual(len({p.split(",")[1] for p in points}), 1)

    def test_real_elapsed_coordinates_and_sorted_rows(self):
        self.render_rows([self.row(100, 3), self.row(0, 1), self.row(1, 2)])
        points = self.xml().find(".//s:polyline", NS).attrib["points"].split()
        x = [float(p.split(",")[0]) for p in points]
        self.assertAlmostEqual((x[1] - x[0]) / (x[2] - x[0]), .01, places=4)

    def test_xml_escaping_in_container_and_timestamp(self):
        container = 'demo <script>&"\' service'
        timestamp = '2026-11-10T13:40:00+08:00 <&"'
        summary = self.render_rows([self.row(0, 3, container=container, timestamp=timestamp)])
        self.assertEqual(summary["containers"], [container])
        self.assertIn(container, self.texts())
        self.assertIn(timestamp, self.texts())
        raw = (self.output / "cpu.svg").read_text()
        self.assertIn("&lt;script&gt;", raw)
        self.assertNotIn("<script>", raw)
        self.assertIsNone(self.xml().find(".//s:script", NS))

    def test_containers_are_not_connected(self):
        summary = self.render_rows([self.row(0, 1, container="a"), self.row(1, 10, container="b"),
                                    self.row(2, 2, container="a"), self.row(3, 11, container="b")])
        self.assertEqual(len(self.xml().findall(".//s:polyline", NS)), 2)
        self.assertEqual(summary["metrics"]["cpu_pct"]["peak_container"], "b")

    def test_large_series_bounds_size_and_preserves_peak(self):
        # Twelve hours at 0.3 s; a narrow spike must not be lost to stride sampling.
        peak_index = 72001
        summary = self.render_rows(self.row(i * .3, 999 if i == peak_index else 10,
                                           timestamp=f"sample-{i}") for i in range(144001))
        metric = summary["metrics"]["cpu_pct"]
        self.assertEqual(metric["valid_samples"], 144001)
        self.assertEqual(metric["max"], 999)
        self.assertEqual(metric["peak_timestamp"], f"sample-{peak_index}")
        self.assertIn(f"Peak: 999 % at sample-{peak_index}", self.texts())
        points = self.xml().find(".//s:polyline", NS).attrib["points"].split()
        self.assertLessEqual(len(points), 4 * (plot_metrics.WIDTH - plot_metrics.LEFT - plot_metrics.RIGHT))
        self.assertIn(85.0, [float(p.split(",")[1]) for p in points])
        self.assertLess((self.output / "cpu.svg").stat().st_size, 300000)
        self.assertLess((self.output / "overview.svg").stat().st_size, 1000000)

    def test_dense_missing_segments_stay_disconnected_and_bounded(self):
        self.render_rows(self.row(i * .3, i if i % 2 == 0 else "") for i in range(20000))
        self.assertFalse(self.xml().findall(".//s:polyline", NS))
        self.assertLessEqual(len(self.xml().findall(".//s:circle[@r='2']", NS)),
                             4 * (plot_metrics.WIDTH - plot_metrics.LEFT - plot_metrics.RIGHT))

    def test_extreme_finite_values_remain_valid_svg(self):
        summary = self.render_rows([self.row(-1e308, -1e308), self.row(1e308, 1e308)])
        self.assertEqual(summary["metrics"]["cpu_pct"]["max"], 1e308)
        for line in self.xml().findall(".//s:polyline", NS):
            for pair in line.attrib["points"].split():
                self.assertTrue(all(math.isfinite(float(v)) for v in pair.split(",")))

    def test_peak_marker_coordinates_tooltip_and_overview(self):
        timestamp = "2026-11-10T13:40:30.123456+08:00"
        self.render_rows([self.row(0, 1, 2, 3, 4),
                          self.row(30, 10.123456789, 20, 30, 40, timestamp=timestamp),
                          self.row(100, 3, 4, 5, 6)])
        marker = self.xml().find(".//s:g[@class='peak-marker']", NS)
        self.assertIsNotNone(marker)
        point = marker.find("s:circle[@class='peak-point']", NS)
        self.assertEqual(point.attrib["fill"], "#dc2626")
        self.assertAlmostEqual(float(point.attrib["cx"]), 352.0)
        self.assertAlmostEqual(float(point.attrib["cy"]), 85.0)
        self.assertIn("stroke-dasharray", marker.find("s:path", NS).attrib)
        self.assertEqual(marker.find("s:text", NS).text, "Peak")
        tooltip = marker.find("s:title", NS).text
        self.assertIn(timestamp, tooltip)
        self.assertIn("Peak CPU: 10.12 %", tooltip)
        self.assertIn("elapsed_s: 30.0", tooltip)
        self.assertIn("container: demo", tooltip)
        overview_markers = self.xml("overview").findall(".//s:g[@class='peak-marker']", NS)
        self.assertEqual(len(overview_markers), 4)
        for item in overview_markers:
            point = item.find("s:circle", NS)
            self.assertAlmostEqual(float(point.attrib["cx"]), 352.0)
            self.assertAlmostEqual(float(point.attrib["cy"]), 85.0)

    def test_peak_without_valid_elapsed_is_not_mislocated(self):
        summary = self.render_rows([self.row(0, 3), self.row(1, 100, elapsed_s="NaN")])
        self.assertEqual(summary["metrics"]["cpu_pct"]["max"], 100)
        self.assertIsNone(summary["metrics"]["cpu_pct"]["peak_elapsed_s"])
        self.assertIsNone(self.xml().find(".//s:g[@class='peak-marker']", NS))
        self.assertFalse(self.xml("overview").findall(".//s:g[@class='peak-marker']", NS))

    def test_cli(self):
        self.render_rows([self.row(0, 1)])
        target = self.root / "cli"
        result = subprocess.run([sys.executable, str(MODULE_PATH), str(self.source), "--output-dir", str(target)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(target.glob("*.png"))), 4)
        self.assertFalse(list(target.glob("*.svg")))
        self.assertEqual(json.loads((target / "summary.json").read_text())["sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
