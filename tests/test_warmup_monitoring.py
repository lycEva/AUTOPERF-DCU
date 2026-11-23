"""Warmup integration contracts using mock JMeter/Docker/ROCm only."""
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

import test_direct_monitoring as direct


class WarmupMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.f = direct.DirectMonitoringTests(methodName='runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        # The legacy fake-JMeter fixture deliberately has no actual samplers.
        # Warmup validates execution scope, so give it a proper synthetic group.
        import xml.etree.ElementTree as ET
        tree = ET.parse(self.f.template)
        group = tree.find('.//ThreadGroup')
        main = ET.SubElement(group, 'elementProp', name='ThreadGroup.main_controller',
                             elementType='LoopController')
        ET.SubElement(main, 'intProp', name='LoopController.loops').text = '-1'
        group_tree = tree.find('hashTree/hashTree/hashTree')
        sampler = ET.SubElement(group_tree, 'JSR223Sampler', enabled='true')
        ET.SubElement(sampler, 'stringProp', name='scriptLanguage').text = 'groovy'
        ET.SubElement(sampler, 'stringProp', name='script').text = 'Thread.sleep(50)'
        ET.SubElement(group_tree, 'hashTree')
        tree.write(self.f.template, encoding='utf-8', xml_declaration=True)
        self.f.env.pop('WARMUP', None)
        self.f.env['JMETER'] = str(self.f.executable('warmup-jmeter', '''
            import json, os, pathlib, sys, time
            args = sys.argv[1:]
            def option(flag):
                return pathlib.Path(args[args.index(flag) + 1])
            run = option('-t').parent
            assert json.loads((run / 'ready.json').read_text())['ok']
            assert '-Jduration=3' in args
            time.sleep(.2)  # startup must not count as warmup
            window_path = run / 'measurement_window.json'
            if os.environ.get('WARMUP_MOCK_MODE') != 'no_marker':
                now = time.time()
                window = dict(load_started_epoch_s=now, measurement_start_epoch_s=now+1,
                              measurement_end_epoch_s=now+3, warmup_s=1, duration_s=2)
                if os.environ.get('WARMUP_MOCK_MODE') == 'bad_marker':
                    window['duration_s'] = -1
                temp = run / 'window.tmp'
                temp.write_text(json.dumps(window))
                temp.replace(window_path)
            mode = os.environ.get('WARMUP_MOCK_MODE', '')
            time.sleep(.2 if mode in ('early', 'no_marker', 'bad_marker') else
                       (1.4 if mode == 'partial' else 3.05))
            time.sleep(float(os.environ.get('WARMUP_REPORT_DELAY', '0')))
            for flag in ('-l', '-j'):
                option(flag).write_text('mock result\\n')
            option('-o').mkdir()
            (option('-o') / 'index.html').write_text('mock report')
        '''))

    def command(self, options=('--warmup', '1')):
        return self.f.runner_command(options=options)

    def run_command(self, options=('--warmup', '1')):
        return subprocess.run(self.command(options), cwd=self.f.base, env=self.f.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=16)

    def run_dir(self):
        runs = self.f.run_dirs()
        self.assertEqual(len(runs), 1)
        return runs[0]

    def raw_rows(self, run):
        with (run / 'metrics.csv').open(newline='') as stream:
            return list(csv.DictReader(stream))

    def test_records_only_formal_window_and_not_report_generation(self):
        self.f.env['WARMUP_REPORT_DELAY'] = '1'
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.run_dir()
        window = json.loads((run / 'measurement_window.json').read_text())
        status = self.f.status(run)
        self.assertTrue(status['measurement_completed'])
        self.assertFalse(status['preflight_recorded'])
        self.assertEqual(status['duration_s'], 2)
        rows = self.f.rows(run)
        self.assertEqual(rows[0]['cpu_pct'], '')  # baseline was reset after preflight
        for row in rows:
            stamp = datetime.fromisoformat(row['timestamp']).timestamp()
            self.assertGreaterEqual(stamp, window['measurement_start_epoch_s'] - .002)
            self.assertLess(stamp, window['measurement_end_epoch_s'])
            self.assertGreaterEqual(float(row['elapsed_s']), 0)
            self.assertLess(float(row['elapsed_s']), 2)
        starts = [r for r in self.f.records() if r['command'] == 'docker' and r['stage'] == 'start']
        self.assertEqual(len(starts), len(rows) + 1)  # exactly one unrecorded preflight
        self.assertIn('正式监控窗口已完成', result.stdout)
        self.f.assert_no_mock_processes()

    def test_no_periodic_sampling_or_csv_rows_during_warmup_then_term(self):
        proc = self.f.spawn(self.command())
        self.f.wait_for(lambda: self.f.output.exists() and self.f.run_dirs(), proc)
        run = self.run_dir()
        self.f.wait_for(lambda: (run / 'measurement_window.json').exists(), proc)
        self.assertEqual(self.raw_rows(run), [])
        starts = [r for r in self.f.records() if r['stage'] == 'start']
        self.assertEqual(len(starts), 2)  # one docker and one rocm-smi, preflight only
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=8)
        self.assertEqual(proc.returncode, 143, stdout)
        status = json.loads((run / 'monitor_status.json').read_text())
        self.assertFalse(status['measurement_completed'])
        self.assertFalse(status['accepted'])
        self.assertEqual(status['samples'], 0)
        self.assertEqual(self.raw_rows(run), [])
        self.f.assert_no_mock_processes()

    def test_successful_jmeter_exit_without_full_measurement_is_rejected(self):
        for mode in ('early', 'partial', 'no_marker', 'bad_marker'):
            with self.subTest(mode=mode):
                self.f.output = self.f.base / ('results-' + mode)
                self.f.env['WARMUP_MOCK_MODE'] = mode
                result = self.run_command()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                status = json.loads((self.run_dir() / 'monitor_status.json').read_text())
                self.assertFalse(status['accepted'])
                self.assertFalse(status['measurement_completed'])
                self.assertTrue(status['failure_reasons'])
        self.f.assert_no_mock_processes()

    def test_preflight_failure_is_not_a_formal_sample_and_prevents_load(self):
        self.f.env['MOCK_DOCKER_MODE'] = 'fail'
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        run = self.run_dir()
        self.assertFalse((run / 'measurement_window.json').exists())
        self.assertFalse(json.loads((run / 'ready.json').read_text())['ok'])
        self.assertEqual(self.raw_rows(run), [])
        status = json.loads((run / 'monitor_status.json').read_text())
        self.assertEqual(status['samples'], 0)
        self.assertFalse(status['accepted'])
        self.assertIn('initial sample failed', status['fatal_error'])

    def test_warmup_parameter_validation_and_perfmon_rejection_before_load(self):
        for value in ('-1', '1.5', 'nan', 'abc', '', '01'):
            with self.subTest(value=value):
                result = self.run_command(('--warmup', value))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('预热秒数必须', result.stdout)
        result = self.run_command(('--warmup',))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('选项缺少参数', result.stdout)
        result = self.run_command(('--warmup', '1', '--monitor-mode', 'perfmon'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PerfMon', result.stdout)
        self.assertFalse(self.f.output.exists())
        self.assertEqual(self.f.records(), [])

    @unittest.skipUnless(os.environ.get('XNCS_TEST_REAL_JMETER') == '1',
                         'set XNCS_TEST_REAL_JMETER=1 for local synthetic JMeter verification')
    def test_real_jmeter_end_to_end_with_mock_resources(self):
        import shutil
        import test_warmup_plan as plans
        jmeter = shutil.which('jmeter')
        if not jmeter:
            self.skipTest('JMeter not installed')
        plans.fixture(self.f.template)
        self.f.env['JMETER'] = jmeter
        self.f.env.pop('JAVA_HOME', None)
        self.f.env.pop('JRE_HOME', None)
        result = subprocess.run(self.command(), cwd=self.f.base, env=self.f.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.run_dir()
        self.assertTrue((run / 'report' / 'index.html').is_file(), result.stdout)
        self.assertNotIn('ERROR', (run / 'jmeter.log').read_text())
        window = json.loads((run / 'measurement_window.json').read_text())
        with (run / 'result.jtl').open(newline='') as stream:
            requests = list(csv.DictReader(stream))
        self.assertGreater(len(requests), 1)
        for request in requests:
            start = int(request['timeStamp']) / 1000.0
            self.assertGreaterEqual(start, window['measurement_start_epoch_s'])
            self.assertLess(start, window['measurement_end_epoch_s'])
        for row in self.f.rows(run):
            stamp = datetime.fromisoformat(row['timestamp']).timestamp()
            self.assertGreaterEqual(stamp, window['measurement_start_epoch_s'] - .002)
            self.assertLess(stamp, window['measurement_end_epoch_s'])
        status = self.f.status(run)
        self.assertTrue(status['measurement_completed'])
        self.assertEqual(status['duration_s'], 2)
        self.f.assert_no_mock_processes()

    def test_environment_warmup_and_cli_precedence(self):
        self.f.env['WARMUP'] = '1'
        result = self.run_command(options=())
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.run_dir() / 'measurement_window.json').exists())
        # Explicit --warmup 0 overrides WARMUP and restores the original flow.
        self.f.output = self.f.base / 'zero-results'
        self.f.env['JMETER'] = str(self.f.executable('zero-jmeter', '''
            import pathlib, sys, time
            args = sys.argv[1:]
            assert '-Jduration=2' in args
            time.sleep(.4)
            for flag in ('-l', '-j'):
                pathlib.Path(args[args.index(flag)+1]).write_text('mock')
            pathlib.Path(args[args.index('-o')+1]).mkdir()
        '''))
        result = self.run_command(('--warmup', '0'))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.run_dir() / 'measurement_window.json').exists())
        self.assertGreater(len(self.raw_rows(self.run_dir())), 0)


if __name__ == '__main__':
    unittest.main()
