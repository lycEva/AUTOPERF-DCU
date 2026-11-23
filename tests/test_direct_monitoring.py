"""Direct-monitoring black-box contracts; no Docker/ROCm/JMeter service needed.

Run: python3 -B -m unittest discover -s tests -p 'test_direct_monitoring.py' -v
All entrypoints run against temporary data or a temporary project copy.
"""
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PERFMON = "kg.apc.jmeter.perfmon.PerfMonCollector"
COLUMNS = ("timestamp", "elapsed_s", "container", "cpu_pct", "vram_mib",
           "vram_pct", "memory_gib", "status", "error", "sample_duration_s")


class DirectMonitoringTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xncs-direct-contract-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        for name in ("run_test.sh", "prepare_run.py", "update_data.py", "ports.json"):
            if (ROOT / name).is_file():
                shutil.copy2(ROOT / name, self.project / name)
        for name in ("summary_monitor", "run_scripts"):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.project / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.events = self.base / "events.jsonl"
        self.output = self.base / "results"
        self.env = os.environ.copy()
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        PROC_ROOT=str(self.proc), MOCK_STATE=str(self.state),
                        MOCK_EVENTS=str(self.events), TMPDIR=str(self.base),
                        PYTHONDONTWRITEBYTECODE="1", MONITOR_MODE="direct")
        for key in ("MOCK_DOCKER_MODE", "MOCK_ROCM_MODE", "MOCK_DELAY",
                    "MOCK_JMETER_FAIL", "MOCK_JMETER_BARRIER", "MOCK_JMETER_SECONDS",
                    "MOCK_TREE_CPU", "MOCK_STRAY_CPU"):
            self.env.pop(key, None)
        for pid in (111, 112, 222):
            self.write_stat(pid, 20)
        # Both external commands use the same mock program. Per-command sequence
        # counters are locked, including during two simultaneous collector runs.
        mock = '''
            import fcntl, json, os, pathlib, subprocess, sys, time
            name = pathlib.Path(sys.argv[0]).name
            args = sys.argv[1:]
            state = pathlib.Path(os.environ["MOCK_STATE"])
            def event(**extra):
                value = dict(command=name, argv=args, pid=os.getpid(), time=time.monotonic())
                value.update(extra)
                fd = os.open(os.environ["MOCK_EVENTS"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.write(fd, (json.dumps(value) + "\\n").encode())
                os.close(fd)
            key = name + ("-" + args[1] if name == "docker" and len(args) > 1 else "")
            with open(state / (key + ".count"), "a+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.seek(0)
                count = int(stream.read() or "0") + 1
                stream.seek(0); stream.truncate(); stream.write(str(count)); stream.flush()
            mode = os.environ.get("MOCK_" + ("DOCKER" if name == "docker" else "ROCM") + "_MODE", "ok")
            event(stage="start", count=count)
            if mode == "hang" or (mode == "hang_after_first" and count > 1):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                event(stage="child", child=child.pid)
                time.sleep(60)
            time.sleep(float(os.environ.get("MOCK_DELAY", "0")))
            if mode == "fail" or (mode == "fail_after_first" and count > 1):
                print("mock service failure", file=sys.stderr)
                sys.exit(7)
            if name == "docker":
                if len(args) != 4 or args[0] != "top" or args[2:] != ["-eo", "pid,ppid,rss"]:
                    sys.exit("expected exactly docker top NAME -eo pid,ppid,rss")
                pids = (222, 333) if args[1] == "worker-b" else (111, 112, 333)
                pids = (100, 900) + pids  # 100 = init/root, 900 = stray docker-exec shell
                print("PID PPID RSS")
                for pid in pids:
                    fields = ["S"] + ["0"] * 49
                    stray_only = os.environ.get("MOCK_STRAY_CPU") == "1"
                    if pid == 900:
                        ticks = 20 + 2000 * count if stray_only else 20
                    elif stray_only:
                        ticks = 20
                    elif os.environ.get("MOCK_TREE_CPU") == "1" and pid not in (333,):
                        ticks = 20
                    else:
                        ticks = 20 + 200 * count
                    fields[11], fields[12], fields[19] = str(ticks), "10", str(pid * 10)
                    folder = pathlib.Path(os.environ["PROC_ROOT"]) / str(pid)
                    if mode == "missing_proc_after_first" and count > 1:
                        try:
                            (folder / "stat").unlink()
                        except OSError:
                            pass
                    else:
                        folder.mkdir(exist_ok=True)
                        (folder / "stat").write_text(str(pid) + " (worker with spaces) " + " ".join(fields) + "\\n")
                    rss = 0 if pid == 100 else (2097152 if pid == 333 else
                                                (2097152 if pid == 222 else 524288))
                    print(pid, 1 if pid in (100, 900) else 100, rss)
            else:
                if args != ["--showpids"]:
                    sys.exit("expected exactly rocm-smi --showpids")
                if mode == "unknown":
                    print("GPU output schema changed; unsupported firmware")
                else:
                    for pid, mib, pct in ((111, 101, 11), (112, 7, 2), (222, 202, 22), (999, 9999, 99)):
                        print("PID: " + str(pid))
                        print("VRAM USED(MiB): " + str(mib))
                        print("VRAM USED(%): " + str(pct))
            event(stage="end", count=count)
        '''
        self.executable("docker", mock)
        self.executable("rocm-smi", mock)
        self.executable("jmeter", 'raise SystemExit("JMETER override was ignored")')
        self.env["JMETER"] = str(self.executable("fake-jmeter", '''
            import json, os, pathlib, sys, time, xml.etree.ElementTree as ET
            args = sys.argv[1:]
            def option(flag):
                return pathlib.Path(args[args.index(flag) + 1])
            run = option("-t").parent
            ready_files = list(run.rglob("ready.json"))
            if not ready_files or not all(json.loads(p.read_text())["ok"] for p in ready_files):
                sys.exit("JMeter started before successful monitoring readiness")
            plan = ET.parse(option("-t")).getroot()
            for node in plan.iter():
                if "PerfMon" in node.tag and node.get("enabled", "true").lower() != "false":
                    sys.exit("PerfMon was not disabled in direct mode")
            state = pathlib.Path(os.environ["MOCK_STATE"])
            token = str(os.getpid())
            (state / (token + ".jmeter-entered")).write_text(json.dumps(dict(run=str(run), time=time.monotonic())))
            deadline = time.monotonic() + 6
            while len(list(state.glob("*.jmeter-entered"))) < int(os.environ.get("MOCK_JMETER_BARRIER", "1")):
                if time.monotonic() > deadline:
                    sys.exit("concurrent JMeter barrier timed out")
                time.sleep(.02)
            time.sleep(float(os.environ.get("MOCK_JMETER_SECONDS", ".7")))
            if os.environ.get("MOCK_JMETER_FAIL"):
                sys.exit(9)
            for flag in ("-l", "-j"):
                option(flag).write_text(token + "\\n")
            option("-o").mkdir()
            (option("-o") / "index.html").write_text(token)
        '''))
        self.template = self.base / "source.jmx"
        root = ET.Element("jmeterTestPlan", version="1.2", properties="5.0", jmeter="5.4.1")
        tree = ET.SubElement(root, "hashTree")
        ET.SubElement(tree, "TestPlan", testname="source-plan")
        tree = ET.SubElement(tree, "hashTree")
        group = ET.SubElement(tree, "ThreadGroup")
        for name, value in (("num_threads", "1"), ("duration", "1")):
            ET.SubElement(group, "stringProp", name="ThreadGroup." + name).text = value
        ET.SubElement(group, "boolProp", name="ThreadGroup.scheduler").text = "false"
        ET.SubElement(tree, "hashTree")
        # An unreachable Agent host is intentional: direct must not depend on it.
        node = ET.SubElement(tree, PERFMON, testclass=PERFMON, enabled="true")
        ET.SubElement(node, "stringProp", name="filename").text = "old.jtl"
        connections = ET.SubElement(node, "collectionProp", name="metricConnections")
        row = ET.SubElement(connections, "collectionProp", name="0")
        for key, value in (("1505998205", "192.0.2.55"), ("1693120", "7777"),
                           ("66952", "EXEC"), ("-318877419", "/missing/agent/script")):
            ET.SubElement(row, "stringProp", name=key).text = value
        ET.SubElement(tree, "hashTree")
        ET.ElementTree(root).write(self.template, encoding="utf-8", xml_declaration=True)

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body).lstrip())
        path.chmod(0o755)
        return path

    def write_stat(self, pid, ticks):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        fields = ["S"] + ["0"] * 49
        fields[11], fields[12], fields[19] = str(ticks), "10", str(pid * 10)
        (folder / "stat").write_text(str(pid) + " (worker with spaces) " + " ".join(fields) + "\n")

    def collector_command(self, output, container="worker-a", duration=".8", timeout="2"):
        script = self.project / "summary_monitor" / "collect_metrics.py"
        self.assertTrue(script.is_file(), "collect_metrics.py implementation is not ready")
        args = [sys.executable, "-B", str(script), "--container", container,
                "--output-dir", str(output), "--interval", ".3", "--timeout", timeout]
        if duration is not None:
            args += ["--duration", duration]
        return args

    def run_collector(self, name="standalone", **kwargs):
        output = self.base / name
        result = subprocess.run(self.collector_command(output, **kwargs), cwd=self.base,
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10)
        return result, output

    def rows(self, output):
        with (output / "metrics.csv").open(newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(tuple(reader.fieldnames), COLUMNS)
            rows = list(reader)
        self.assertTrue(rows)
        for row in rows:
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            self.assertGreaterEqual(float(row["elapsed_s"]), 0)
            self.assertGreaterEqual(float(row["sample_duration_s"]), 0)
        return rows

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def status(self, output):
        status = json.loads((output / "monitor_status.json").read_text())
        self.assertIsInstance(status, dict)
        rows = self.rows(output)
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        self.assertEqual(status["samples"], len(rows))
        self.assertEqual(status["statuses"], counts)
        failed = sum(row["status"] in ("error", "timeout", "partial") for row in rows)
        self.assertEqual(status["failed_samples"], failed)
        self.assertIs(status["healthy"], failed == 0)
        self.assertGreaterEqual(status["overruns"], 0)
        self.assertGreaterEqual(status["duration_s"], 0)
        return status

    def wait_for(self, predicate, process, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if process.poll() is not None:
                self.fail("process exited before expected event: " + process.communicate()[0])
            time.sleep(.02)
        self.fail("timed out waiting for mock event")

    def spawn(self, command):
        process = subprocess.Popen(command, cwd=self.base, env=self.env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def assert_no_mock_processes(self):
        # A killed orphan may briefly be a zombie awaiting host init reaping;
        # zombies cannot run or retain service resources and count as stopped.
        for event in self.records():
            for key in ("pid", "child"):
                if key not in event:
                    continue
                stat = Path("/proc") / str(event[key]) / "stat"
                if stat.exists():
                    self.assertEqual(stat.read_text().rsplit(")", 1)[1].split()[0], "Z",
                                     "mock subprocess survived cleanup: " + str(event))

    def runner_command(self, container="worker-a", options=()):
        return ["bash", str(self.project / "run_test.sh"), str(self.template),
                str(self.output), container, "2", "2", "--service", "fs", *options]

    def run_runner(self, **kwargs):
        return subprocess.run(self.runner_command(**kwargs), cwd=self.base, env=self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=18)

    def run_dirs(self):
        return sorted(path for path in self.output.iterdir() if path.is_dir())

    def test_collector_normal_units_pid_filtering_cpu_warmup_and_single_calls(self):
        result, output = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["cpu_pct"], "")
        self.assertTrue(any(float(row["cpu_pct"]) > 100 for row in rows[1:] if row["cpu_pct"]))
        for row in rows:
            self.assertEqual(row["container"], "worker-a")
            self.assertAlmostEqual(float(row["memory_gib"]), 3)
            self.assertAlmostEqual(float(row["vram_mib"]), 108)
            self.assertAlmostEqual(float(row["vram_pct"]), 13)
        ready = json.loads((output / "ready.json").read_text())
        self.assertIs(ready["ok"], True)
        self.assertIsInstance(ready["error"], str)
        self.status(output)
        starts = [event for event in self.records() if event["stage"] == "start"]
        self.assertEqual(sum(e["command"] == "docker" for e in starts), len(rows))
        self.assertEqual(sum(e["command"] == "rocm-smi" for e in starts), len(rows))
        self.assert_no_mock_processes()

    def test_external_commands_run_in_parallel(self):
        self.env["MOCK_DELAY"] = ".3"
        result, output = self.run_collector(duration=".1", timeout=".5")
        self.assertEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        self.assertLess(float(row["sample_duration_s"]), .57)
        starts = [event for event in self.records() if event["stage"] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertLess(abs(starts[0]["time"] - starts[1]["time"]), .2)

    def test_cpu_and_memory_count_the_whole_process_tree(self):
        # Only PID 333 (in the container but NOT holding GPU memory) burns CPU.
        self.env["MOCK_TREE_CPU"] = "1"
        result, output = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(any(float(row["cpu_pct"]) > 0 for row in rows[1:] if row["cpu_pct"]),
                        "CPU of a non-GPU container process must be counted")
        for row in rows:
            # GPU columns still only cover the GPU-holding processes.
            self.assertAlmostEqual(float(row["vram_mib"]), 108)
            self.assertAlmostEqual(float(row["vram_pct"]), 13)
            # Memory is the whole service tree (100 + 111 + 112 + 333 = 3 GiB).
            self.assertAlmostEqual(float(row["memory_gib"]), 3)

    def test_stray_exec_shell_outside_the_service_tree_is_not_counted(self):
        # PID 900 is listed by docker top but is its own root (a docker exec
        # shell, parent outside the container). Only it burns CPU here, so any
        # non-zero cpu_pct would mean the service tree was computed wrongly.
        self.env["MOCK_STRAY_CPU"] = "1"
        result, output = self.run_collector(duration=".5")
        self.assertEqual(result.returncode, 0, result.stdout)
        for row in self.rows(output):
            self.assertIn(row["cpu_pct"], ("", "0.0", "0"), row)
            self.assertAlmostEqual(float(row["vram_mib"]), 108)

    def test_unrecognized_gpu_output_is_error_and_missing_not_zero(self):
        self.env["MOCK_ROCM_MODE"] = "unknown"
        result, output = self.run_collector(duration=".1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        self.assertEqual(row["vram_mib"], "")
        self.assertEqual(row["vram_pct"], "")
        self.assertTrue(row["error"])
        ready = json.loads((output / "ready.json").read_text())
        self.assertIs(ready["ok"], False)
        self.assertTrue(ready["error"])
        self.status(output)

    def test_docker_failure_has_no_fabricated_memory_or_cpu(self):
        self.env["MOCK_DOCKER_MODE"] = "fail"
        result, output = self.run_collector(duration=".1")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        row = self.rows(output)[0]
        for column in ("memory_gib", "cpu_pct", "vram_mib", "vram_pct"):
            self.assertEqual(row[column], "", column)
        self.assertTrue(row["error"])
        self.assertIs(json.loads((output / "ready.json").read_text())["ok"], False)
        self.status(output)

    def test_missing_proc_cpu_remains_missing_after_warmup(self):
        self.env["MOCK_DOCKER_MODE"] = "missing_proc_after_first"
        result, output = self.run_collector()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(row["cpu_pct"] == "" for row in rows))
        self.assertTrue(any(row["error"] for row in rows[1:]))
        self.assertIs(json.loads((output / "ready.json").read_text())["ok"], True)
        self.status(output)

    def test_shared_deadline_kills_both_command_process_groups(self):
        self.env.update(MOCK_DOCKER_MODE="hang", MOCK_ROCM_MODE="hang")
        started = time.monotonic()
        result, output = self.run_collector(duration=".1", timeout=".4")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertLess(time.monotonic() - started, 2)
        row = self.rows(output)[0]
        self.assertLess(float(row["sample_duration_s"]), .9)
        for column in ("cpu_pct", "memory_gib", "vram_mib", "vram_pct"):
            self.assertEqual(row[column], "")
        self.assertTrue(row["error"])
        children = [record for record in self.records() if record["stage"] == "child"]
        self.assertEqual(len(children), 2, "both mock command trees must really have started")
        self.assert_no_mock_processes()
        self.status(output)

    def test_standalone_term_flushes_csv_and_final_status(self):
        output = self.base / "signal-output"
        process = self.spawn(self.collector_command(output, duration=None))
        self.wait_for(lambda: (output / "ready.json").exists(), process)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=7)
        self.assertEqual(process.returncode, 0, stdout)
        self.rows(output)
        self.status(output)
        self.assert_no_mock_processes()

    def test_term_during_sample_kills_inflight_command_trees(self):
        self.env.update(MOCK_DOCKER_MODE="hang_after_first", MOCK_ROCM_MODE="hang_after_first")
        output = self.base / "inflight-term"
        process = self.spawn(self.collector_command(output, duration=None, timeout="10"))
        self.wait_for(lambda: len([r for r in self.records() if r["stage"] == "child"]) == 2, process)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout)
        self.assertEqual(len(self.rows(output)), 1)
        self.status(output)
        self.assert_no_mock_processes()

    def test_default_direct_uses_ready_disables_perfmon_and_generates_plots(self):
        before = self.template.read_bytes()
        self.env.pop("MONITOR_MODE", None)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.template.read_bytes(), before)
        self.assertTrue(list(self.state.glob("*.jmeter-entered")))
        self.assertTrue((runs[0] / "result.jtl").is_file())
        metrics = list(runs[0].rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.rows(metrics[0].parent)
        self.status(metrics[0].parent)
        self.assertTrue(list(runs[0].rglob("*.png")), "direct run must generate metric PNGs")
        self.assert_no_mock_processes()

    def test_failed_preflight_prevents_jmeter(self):
        self.env["MOCK_DOCKER_MODE"] = "fail"
        result = self.run_runner(options=("--monitor-interval", ".3", "--monitor-timeout", ".5"))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(list(self.state.glob("*.jmeter-entered")))
        ready = list(self.output.rglob("ready.json"))
        self.assertEqual(len(ready), 1)
        self.assertIs(json.loads(ready[0].read_text())["ok"], False)
        self.assert_no_mock_processes()

    def test_monitor_failure_after_ready_is_not_a_successful_run(self):
        self.env["MOCK_ROCM_MODE"] = "fail_after_first"
        result = self.run_runner(options=("--monitor-interval", ".3", "--monitor-timeout", ".5"))
        self.assertTrue(list(self.state.glob("*.jmeter-entered")), result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertTrue(metrics)
        rows = self.rows(metrics[0].parent)
        self.assertTrue(any(row["error"] for row in rows[1:]))
        self.assert_no_mock_processes()

    def test_two_container_runs_overlap_and_are_isolated(self):
        self.env["MOCK_JMETER_BARRIER"] = "2"
        processes = [self.spawn(self.runner_command(container, ("--monitor-mode", "direct")))
                     for container in ("worker-a", "worker-b")]
        for process in processes:
            stdout, _ = process.communicate(timeout=18)
            self.assertEqual(process.returncode, 0, stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 2)
        contents = []
        for run in runs:
            container = "worker-b" if "worker-b" in run.name else "worker-a"
            metrics = list(run.rglob("metrics.csv"))
            self.assertEqual(len(metrics), 1)
            for row in self.rows(metrics[0].parent):
                self.assertEqual(row["container"], container)
                self.assertEqual(float(row["vram_mib"]), 202 if container == "worker-b" else 108)
                self.assertEqual(float(row["memory_gib"]), 4 if container == "worker-b" else 3)
            contents.append((run / "result.jtl").read_text())
        self.assertNotEqual(*contents)
        self.assert_no_mock_processes()

    def test_one_run_monitors_two_containers_with_shared_probes(self):
        result, output = self.run_collector(name="multi", container="worker-a,worker-b")
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows(output)
        self.assertEqual({row["container"] for row in rows}, {"worker-a", "worker-b"})
        self.assertEqual(len(rows) % 2, 0, "every round writes one row per container")
        expect = {"worker-a": (108, 13, 3), "worker-b": (202, 22, 4)}
        for row in rows:
            vram, pct, mem = expect[row["container"]]
            self.assertAlmostEqual(float(row["vram_mib"]), vram)
            self.assertAlmostEqual(float(row["vram_pct"]), pct)
            self.assertAlmostEqual(float(row["memory_gib"]), mem)
        # Exactly one docker top per container per round: no duplicated GPU probe.
        starts = [r for r in self.records() if r["stage"] == "start"]
        self.assertEqual(sum(r["command"] == "docker" for r in starts), len(rows))
        self.assertEqual(sum(r["command"] == "rocm-smi" for r in starts), len(rows) // 2)
        summary = json.loads((output / "plots" / "summary.json").read_text())
        self.assertEqual(sorted(summary["containers"]), ["worker-a", "worker-b"])
        self.assertEqual(len(list((output / "plots").glob("*.png"))), 4)
        self.assertFalse(list((output / "plots").glob("*.svg")), "默认不落 SVG 文件")
        legend = self.base / "legend-check"
        rendered = subprocess.run(
            [sys.executable, str(ROOT / "summary_monitor" / "plot_metrics.py"),
             str(output / "metrics.csv"), "--output-dir", str(legend), "--keep-svg"],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        svg = (legend / "cpu.svg").read_text()
        for name in ("worker-a", "worker-b"):
            self.assertIn(name, svg, "each container needs its own legend entry")
        self.assertIn('#2563eb', svg)
        self.assertIn('#d97706', svg)
        self.status(output)

    def test_scenario1_two_unrelated_services_are_monitored_separately(self):
        """Two unrelated load tests must not share metrics or charts."""
        self.env["MOCK_JMETER_BARRIER"] = "2"
        results = {name: self.base / ("results-" + name) for name in ("worker-a", "worker-b")}
        processes = []
        for name, out in results.items():
            command = ["bash", str(self.project / "run_test.sh"), str(self.template),
                       str(out), name, "2", "2", "--service", "fs",
                       "--monitor-interval", ".3"]
            processes.append(self.spawn(command))
        for process in processes:
            stdout, _ = process.communicate(timeout=18)
            self.assertEqual(process.returncode, 0, stdout)
        for name, out in results.items():
            runs = sorted(path for path in out.iterdir() if path.is_dir())
            self.assertEqual(len(runs), 1, "each load test owns exactly one run directory")
            rows = self.rows(runs[0])
            self.assertEqual({row["container"] for row in rows}, {name},
                             "a run must only contain its own container")
            summary = json.loads((runs[0] / "plots" / "summary.json").read_text())
            self.assertEqual(summary["containers"], [name])
        self.assert_no_mock_processes()

    def test_scenario2_caller_and_callee_appear_in_one_chart_set(self):
        """A calls B: one load test, one run directory, both containers charted."""
        result = self.run_runner(options=("--monitor-containers", "worker-a,worker-b",
                                          "--monitor-interval", ".3"))
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 1)
        rows = self.rows(runs[0])
        self.assertEqual({row["container"] for row in rows}, {"worker-a", "worker-b"})
        self.assertEqual(len(rows) % 2, 0)
        summary = json.loads((runs[0] / "plots" / "summary.json").read_text())
        self.assertEqual(sorted(summary["containers"]), ["worker-a", "worker-b"])
        self.assert_no_mock_processes()

    def test_repeated_same_container_gets_distinct_runs(self):
        for _ in range(2):
            result = self.run_runner(options=("--monitor-interval", ".3"))
            self.assertEqual(result.returncode, 0, result.stdout)
        runs = self.run_dirs()
        self.assertEqual(len(runs), 2)
        self.assertNotEqual((runs[0] / "result.jtl").read_text(), (runs[1] / "result.jtl").read_text())
        self.assert_no_mock_processes()

    def test_jmeter_failure_still_stops_collector_and_plots(self):
        self.env["MOCK_JMETER_FAIL"] = "1"
        result = self.run_runner()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(list(self.state.glob("*.jmeter-entered")), result.stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.status(metrics[0].parent)
        self.assertTrue(list(self.output.rglob("*.png")))
        self.assert_no_mock_processes()

    def test_runner_term_stops_children_finalizes_status_and_plots(self):
        self.env["MOCK_JMETER_SECONDS"] = "30"
        process = self.spawn(self.runner_command(options=("--monitor-interval", ".3")))
        self.wait_for(lambda: list(self.state.glob("*.jmeter-entered")), process)
        entered = list(self.state.glob("*.jmeter-entered"))
        jmeter_pid = int(entered[0].name.split(".")[0])
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=9)
        self.assertNotEqual(process.returncode, 0, stdout)
        metrics = list(self.output.rglob("metrics.csv"))
        self.assertEqual(len(metrics), 1)
        self.status(metrics[0].parent)
        self.assertTrue(list(self.output.rglob("*.png")))
        self.assertFalse(Path("/proc", str(jmeter_pid)).exists(), "fake JMeter survived runner TERM")
        self.assert_no_mock_processes()


if __name__ == "__main__":
    unittest.main()
