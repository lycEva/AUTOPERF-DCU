"""CLI contracts from an unrelated working directory, using only fake services.

Run: python3 -B -m unittest discover -s tests -p 'test_cli.py' -v
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import test_direct_monitoring as direct_fixture
import test_port_config as port_fixture

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "dcu-bench"
INSTALLER = ROOT / "install.sh"


class CliTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dcu-cli-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.cwd = self.base / "external working directory"
        self.cwd.mkdir()
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["PYTHON_BIN"] = "/usr/bin/python3"
        for key in ("PORT", "SERVICE", "PORTS_CONFIG", "SERVER_CONFIG", "WORKERS",
                    "SERVER_THREADS", "TARGET_PATH", "CONFIG_WAIT", "WARMUP",
                    "MONITOR_MODE", "MONITOR_CONTAINERS", "MONITOR_INTERVAL",
                    "MONITOR_TIMEOUT", "MONITOR_POLICY",
                    "MONITOR_MAX_CONSECUTIVE_FAILURES", "MONITOR_MAX_FAILURE_PERCENT"):
            self.env.pop(key, None)

    def command(self, argv, timeout=25, env=None):
        return subprocess.run([str(arg) for arg in argv], cwd=self.cwd,
                              env=self.env if env is None else env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout)

    def cli(self, *args, **kwargs):
        return self.command([CLI, *args], **kwargs)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout)

    def install(self, prefix, *args):
        return self.command(["bash", INSTALLER, "--prefix", prefix, *args])

    def test_help_and_version_from_external_directory(self):
        for args in ((), ("help",), ("--help",), ("-h",)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assert_success(result)
                for expected in ("dcu-bench", "run", "collect", "plot", "ports"):
                    self.assertIn(expected, result.stdout)
        versions = []
        for flag in ("version", "--version", "-V"):
            result = self.cli(flag)
            self.assert_success(result)
            self.assertRegex(result.stdout.strip(), r"^dcu-bench \d+\.\d+\.\d+$")
            versions.append(result.stdout)
        self.assertEqual(len(set(versions)), 1)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_ports_default_ignores_caller_ports_json(self):
        (self.cwd / "ports.json").write_text('{"caller-only": 12345}', encoding="utf-8")
        expected = json.loads((ROOT / "ports.json").read_text(encoding="utf-8"))
        result = self.cli("ports", "--json")
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout), expected)
        result = self.cli("ports")
        self.assert_success(result)
        for service, port in expected.items():
            self.assertRegex(result.stdout, rf"(?m)^{service}\s+{port}$")

    def test_ports_custom_relative_configuration_with_spaces(self):
        config = self.cwd / "input files" / "custom ports.json"
        config.parent.mkdir()
        expected = {"custom-service": 32123, "second": 6543}
        config.write_text(json.dumps(expected), encoding="utf-8")
        result = self.cli("ports", "--ports-config", "input files/custom ports.json", "--json")
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout), expected)
        result = self.cli("ports", "--ports-config", "input files/custom ports.json")
        self.assert_success(result)
        self.assertRegex(result.stdout, r"(?m)^custom-service\s+32123$")

    def test_collect_and_plot_help_reach_underlying_argparse(self):
        for command, script, options in (
                ("collect", "collect_metrics.py", ("--container", "--output-dir", "--interval", "--timeout")),
                ("plot", "plot_metrics.py", ("--output-dir", "--keep-svg"))):
            with self.subTest(command=command):
                result = self.cli(command, "--help")
                self.assert_success(result)
                self.assertIn("usage:", result.stdout.lower())
                self.assertIn(script, result.stdout)
                for option in options:
                    self.assertIn(option, result.stdout)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_config_help_does_not_require_service_arguments(self):
        result = self.cli("config", "--help")
        self.assert_success(result)
        self.assertIn("--target-path", result.stdout)
        self.assertIn("workers", result.stdout)
        self.assertIn("threads", result.stdout)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_complete_run_relative_paths_spaces_and_default_ports(self):
        # Reuse the established fake docker/rocm-smi/JMeter runtime without
        # inheriting its TestCase (which would run all monitoring tests again).
        fake = direct_fixture.DirectMonitoringTests()
        self.addCleanup(fake.doCleanups)
        fake.setUp()
        env = fake.env.copy()
        env.update(self.env)
        env["PATH"] = str(fake.bin) + os.pathsep + self.env.get("PATH", "")
        env["JMETER"] = str(fake.bin / "fake-jmeter")
        env["MOCK_JMETER_SECONDS"] = ".7"
        inputs = self.cwd / "input files"
        inputs.mkdir()
        template = port_fixture.fixture(inputs / "source plan.jmx")
        original = template.read_bytes()
        defaults = json.loads((ROOT / "ports.json").read_text(encoding="utf-8"))
        service = "bsocr"
        # A conflicting caller config detects accidentally resolving the default
        # ports.json relative to cwd instead of the tool directory.
        (self.cwd / "ports.json").write_text(json.dumps({service: 12345}), encoding="utf-8")
        custom = inputs / "custom ports.json"
        custom.write_text(json.dumps({service: 32123}), encoding="utf-8")
        for explicit_run, output, extra, expected_port in (
                (True, "output results", (), defaults[service]),
                (False, "other results", ("--ports-config", "input files/custom ports.json"), 32123)):
            with self.subTest(explicit_run=explicit_run):
                args = (["run"] if explicit_run else []) + [
                    "input files/source plan.jmx", output, "worker-a", "2", "2",
                    "--service", service, "--monitor-interval", ".3", *extra]
                result = self.cli(*args, env=env)
                self.assert_success(result)
                runs = list((self.cwd / output).glob("run_*"))
                self.assertEqual(len(runs), 1, result.stdout)
                run = runs[0]
                plan = ET.parse(run / "plan.jmx").getroot()
                enabled_ports = [sampler.find('stringProp[@name="HTTPSampler.port"]').text
                                 for sampler in plan.iter("HTTPSamplerProxy")
                                 if sampler.get("enabled", "true").lower() != "false"]
                self.assertEqual(enabled_ports, [str(expected_port)])
                for path in ("result.jtl", "jmeter.log", "report/index.html",
                             "metrics.csv", "monitor_status.json", "plots/summary.json"):
                    self.assertTrue((run / path).is_file(), path + "\n" + result.stdout)
                self.assertEqual(len(list((run / "plots").glob("*.png"))), 4)
                status = json.loads((run / "monitor_status.json").read_text())
                self.assertIs(status["healthy"], True)
                summary = json.loads((run / "plots" / "summary.json").read_text())
                self.assertEqual(summary["containers"], ["worker-a"])
        self.assertEqual(template.read_bytes(), original)
        records = fake.records()
        self.assertTrue(records)
        self.assertEqual({record["command"] for record in records}, {"docker", "rocm-smi"})
        self.assertTrue(list(fake.state.glob("*.jmeter-entered")))
        fake.assert_no_mock_processes()

    def test_install_repeat_use_and_uninstall_in_temporary_prefix(self):
        prefix = self.cwd / "local bin"
        target = prefix / "dcu-bench"
        for _ in range(2):
            self.assert_success(self.install(prefix))
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), CLI.resolve())
            result = self.command([target, "version"])
            self.assert_success(result)
            self.assertRegex(result.stdout, r"^dcu-bench \d+\.\d+\.\d+")
            self.assert_success(self.command([target, "ports", "--json"]))
        for _ in range(2):
            self.assert_success(self.install(prefix, "--uninstall"))
            self.assertFalse(os.path.lexists(target))

    def test_installer_refuses_unrelated_file_and_symlinks(self):
        foreign = self.cwd / "foreign executable"
        foreign.write_text("unrelated executable\n", encoding="utf-8")
        for kind in ("file", "symlink", "dangling symlink"):
            with self.subTest(kind=kind):
                prefix = self.cwd / ("protected bin " + kind)
                prefix.mkdir()
                target = prefix / "dcu-bench"
                if kind == "file":
                    target.write_bytes(foreign.read_bytes())
                else:
                    target.symlink_to(foreign if kind == "symlink" else self.cwd / "missing executable")
                before = target.read_bytes() if kind == "file" else os.readlink(target)
                for options in ((), ("--uninstall",)):
                    result = self.install(prefix, *options)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertTrue(os.path.lexists(target))
                    after = target.read_bytes() if kind == "file" else os.readlink(target)
                    self.assertEqual(after, before)
                self.assertEqual(foreign.read_text(), "unrelated executable\n")


if __name__ == "__main__":
    unittest.main()
