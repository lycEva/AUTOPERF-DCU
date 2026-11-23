"""Contract tests for concurrent direct-monitored runs; no real JMeter/Docker/ROCm.

Run: /usr/bin/python3 -m unittest discover -s tests -p 'test_parallel_monitoring.py' -v
Entrypoints run in temporary copies, protecting the project even on regressions.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PERFMON = "kg.apc.jmeter.perfmon.PerfMonCollector"


class ParallelRunTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dcu-parallel-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        for name in ("run_test.sh", "prepare_run.py", "update_data.py",
                     "update_server_config.sh", "ports.json"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, self.project / name)
        shutil.copytree(ROOT / "summary_monitor", self.project / "summary_monitor",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        for pid in (111, 222):
            folder = self.proc / str(pid)
            folder.mkdir()
            fields = ["S"] + ["0"] * 49
            fields[11], fields[12], fields[19] = "20", "10", str(pid * 10)
            (folder / "stat").write_text(str(pid) + " (fake worker) " + " ".join(fields) + "\n")
        self.docker_log = self.base / "docker.jsonl"
        self.barrier = self.base / "barrier"
        self.barrier.mkdir()
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        PROC_ROOT=str(self.proc), DOCKER_LOG=str(self.docker_log),
                        BARRIER_DIR=str(self.barrier), TMPDIR=str(self.base),
                        PYTHONDONTWRITEBYTECODE="1")
        for key in ("MONITOR_MODE", "WARMUP", "MONITOR_POLICY", "SERVER_CONFIG",
                    "WORKERS", "SERVER_THREADS", "TARGET_PATH", "CONFIG_WAIT",
                    "MONITOR_CONTAINERS", "MONITOR_INTERVAL", "MONITOR_TIMEOUT"):
            self.env.pop(key, None)
        self.write_fakes()
        self.template = self.base / "source.jmx"
        self.write_template()
        self.output = self.base / "results"

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body).lstrip())
        path.chmod(0o755)
        return path

    def write_fakes(self):
        self.executable("docker", '''
            import json, os, sys
            args = sys.argv[1:]
            with open(os.environ["DOCKER_LOG"], "a") as stream:
                stream.write(json.dumps(args) + "\\n")
            if args[:1] == ["top"]:
                print("PID PPID RSS")
                print(111, 1, 1048576)
                if args[1] == "worker-b":
                    print(222, 1, 2097152)
                sys.exit(0)
            if args[:1] == ["inspect"]:
                print("true")
                sys.exit(0)
            if args[:1] in (["cp"], ["restart"], ["exec"]):
                sys.exit(0)
            sys.exit("unexpected docker invocation: %r" % (args,))
        ''')
        self.executable("rocm-smi", '''
            print("PID: 111\\nVRAM USED(MiB): 101\\nVRAM USED(%): 11")
            print("PID: 222\\nVRAM USED(MiB): 202\\nVRAM USED(%): 22")
        ''')
        self.executable("jmeter", '''
            import json, os, pathlib, sys, time
            args = sys.argv[1:]
            def option(flag):
                return pathlib.Path(args[args.index(flag) + 1])
            barrier = pathlib.Path(os.environ["BARRIER_DIR"])
            expected = int(os.environ.get("BARRIER_EXPECTED", "1"))
            token = str(os.getpid())
            record = {"argv": args, "pid": os.getpid(), "entered": time.monotonic()}
            (barrier / (token + ".entered")).write_text(json.dumps(record))
            deadline = time.monotonic() + 8
            while len(list(barrier.glob("*.entered"))) < expected:
                if time.monotonic() >= deadline:
                    sys.exit("concurrent-entry barrier timed out (serialized runs?)")
                time.sleep(0.02)
            time.sleep(float(os.environ.get("MOCK_JMETER_SECONDS", "1.2")))
            for flag in ("-l", "-j"):
                option(flag).write_text(token + "\\n")
            option("-o").mkdir()
            (option("-o") / "index.html").write_text(token)
            record["exited"] = time.monotonic()
            (barrier / (token + ".done")).write_text(json.dumps(record))
        ''')
        self.env["JMETER"] = str(self.bin / "jmeter")

    def write_template(self):
        root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.4.1")
        tree = ET.SubElement(root, "hashTree")
        ET.SubElement(tree, "TestPlan", testname="unchanged-plan")
        tree = ET.SubElement(tree, "hashTree")
        group = ET.SubElement(tree, "ThreadGroup", testname="group-0")
        ET.SubElement(group, "stringProp", name="ThreadGroup.num_threads").text = "2"
        ET.SubElement(group, "stringProp", name="ThreadGroup.duration").text = "9"
        ET.SubElement(group, "boolProp", name="ThreadGroup.scheduler").text = "false"
        holders = ET.SubElement(tree, "hashTree")
        sampler = ET.SubElement(holders, "HTTPSamplerProxy", testname="request")
        ET.SubElement(sampler, "stringProp", name="HTTPSampler.domain").text = "127.0.0.1"
        ET.SubElement(sampler, "stringProp", name="HTTPSampler.port").text = "58096"
        dataset = ET.SubElement(tree, "CSVDataSet")
        self.csv_relative = "data/input.csv"
        ET.SubElement(dataset, "stringProp", name="filename").text = self.csv_relative
        ET.SubElement(tree, "hashTree")
        collector = ET.SubElement(tree, PERFMON, testclass=PERFMON, enabled="true")
        ET.SubElement(collector, "stringProp", name="filename").text = "old-perfmon.jtl"
        ET.ElementTree(root).write(self.template, encoding="utf-8", xml_declaration=True)

    def command(self, *options, container="worker-a", threads="2", duration="2", out=None):
        return ["bash", str(self.project / "run_test.sh"), str(self.template),
                str(out or self.output), container, threads, duration,
                "--service", "fs", *options]

    def run_runner(self, *options, **kwargs):
        return subprocess.run(self.command(*options, **kwargs), cwd=self.base, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=30)

    def runs(self, output=None):
        return sorted(path for path in (output or self.output).iterdir() if path.is_dir())

    def only_run(self, output=None):
        runs = self.runs(output)
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def test_single_run_is_direct_only_and_plots(self):
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.only_run()
        for name in ("plan.jmx", "result.jtl", "jmeter.log", "report/index.html",
                     "metrics.csv", "ready.json", "monitor_status.json"):
            self.assertTrue((run / name).is_file(), name)
        for name in ("cpu", "vram", "vram_utilization", "memory"):
            self.assertTrue((run / "plots" / (name + ".png")).is_file(), name)
        self.assertTrue((run / "plots" / "summary.json").is_file())
        self.assertFalse(list((run / "plots").glob("*.svg")), "默认只输出 PNG")
        for gone in ("run_scripts", "cpu_state", "perfmon.jtl"):
            self.assertFalse((run / gone).exists(), gone)
        tree = ET.parse(run / "plan.jmx").getroot()
        collector = tree.find(".//" + PERFMON)
        self.assertIsNotNone(collector)
        self.assertEqual(collector.get("enabled"), "false")
        group = tree.find(".//ThreadGroup")
        self.assertEqual(group.find("./stringProp[@name='ThreadGroup.num_threads']").text, "2")
        self.assertEqual(group.find("./stringProp[@name='ThreadGroup.duration']").text, "2")
        status = json.loads((run / "monitor_status.json").read_text())
        self.assertTrue(status["accepted"])
        self.assertGreater(status["samples"], 0)
        docker_args = [json.loads(line) for line in self.docker_log.read_text().splitlines() if line.strip()]
        self.assertTrue(all(args[0] == "top" for args in docker_args), docker_args)

    def test_two_runs_overlap_and_never_overwrite(self):
        self.env["BARRIER_EXPECTED"] = "2"
        processes = [subprocess.Popen(self.command(container=name, threads=str(n), duration=str(d)),
                                      cwd=self.base, env=self.env, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                     for name, n, d in (("worker-a", 3, 17), ("worker-b", 5, 19))]
        outputs = []
        for process in processes:
            try:
                stdout, _ = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                self.fail("run_test.sh timed out at the concurrent-entry barrier")
            outputs.append(stdout)
            self.assertEqual(process.returncode, 0, stdout)
        records = [json.loads(path.read_text()) for path in self.barrier.glob("*.done")]
        self.assertEqual(len(records), 2, "both runs must reach JMeter")
        self.assertLess(max(record["entered"] for record in records),
                        min(record["exited"] for record in records), "runs did not overlap")
        runs = self.runs()
        self.assertEqual(len(runs), 2, outputs)
        for name, threads, duration in (("worker-a", "3", "17"), ("worker-b", "5", "19")):
            matching = [path for path in runs if path.name.startswith("run_" + name + "_")]
            self.assertEqual(len(matching), 1, runs)
            run = matching[0]
            self.assertRegex(run.name, r"^run_" + re.escape(name) + r"_\d{8}_\d{6}_[A-Za-z0-9]{6}$")
            group = ET.parse(run / "plan.jmx").getroot().find(".//ThreadGroup")
            self.assertEqual(group.find("./stringProp[@name='ThreadGroup.num_threads']").text, threads)
            self.assertEqual(group.find("./stringProp[@name='ThreadGroup.duration']").text, duration)
            rows = (run / "metrics.csv").read_text().strip().splitlines()[1:]
            self.assertTrue(rows, run)
            self.assertEqual({line.split(",")[2] for line in rows}, {name})
        self.assertNotEqual((runs[0] / "result.jtl").read_text(), (runs[1] / "result.jtl").read_text())

    def test_server_config_is_applied_from_a_run_copy(self):
        config = self.base / "server_config.json"
        original = {"workers": 1, "threads": 2, "model": "keep-me"}
        config.write_text(json.dumps(original, indent=4) + "\n")
        result = self.run_runner("--server-config", str(config), "--workers", "4",
                                 "--server-threads", "8", "--config-wait", "0")
        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.only_run()
        copy = json.loads((run / "server_config.json").read_text())
        self.assertEqual((copy["workers"], copy["threads"], copy["model"]), (4, 8, "keep-me"))
        self.assertEqual(json.loads(config.read_text()), original, "源配置不能被改写")
        calls = [json.loads(line) for line in self.docker_log.read_text().splitlines() if line.strip()]
        self.assertTrue(any(call[:1] == ["cp"] and "worker-a:" in call[2] for call in calls), calls)
        self.assertTrue(any(call[:2] == ["restart", "worker-a"] for call in calls), calls)

    def test_perfmon_mode_is_rejected_before_creating_output(self):
        result = self.run_runner("--monitor-mode", "perfmon")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("PerfMon", result.stdout)
        self.assertFalse(self.output.exists(), "失败前不能建结果目录")

    def test_invalid_warmup_is_rejected_before_creating_output(self):
        for value in ("-1", "1.5", "nan", "abc", "", "01"):
            with self.subTest(value=value):
                result = self.run_runner("--warmup", value)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("预热秒数必须", result.stdout)
        self.assertFalse(self.output.exists(), "失败前不能建结果目录")


if __name__ == "__main__":
    unittest.main()
