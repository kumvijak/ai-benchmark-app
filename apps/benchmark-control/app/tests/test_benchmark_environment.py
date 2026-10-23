"""Execute the generated runner in an isolated venv with an explicit CLI fixture."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import venv

from app.benchmark import GUIDELLM_REMOTE_SCRIPT, GuideLLMBenchmarkSpec, remote_guidellm_command


class BenchmarkEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = self.root / 'environment'
        venv.EnvBuilder(with_pip=False).create(self.environment)
        self.python = self.environment / 'bin/python'
        self.cli = self.environment / 'bin/guidellm'
        self.artifacts = self.root / 'artifacts'
        self.spec = GuideLLMBenchmarkSpec('fixture/model', 8000, 'Smoke', 16, 8, [1], 2, 30)
        # Explicit test double: never sends requests or generates performance measurements.
        self.cli.write_text(f'#!{self.python}\n' + '''
import json, os, pathlib, sys
for i, value in enumerate(sys.argv):
    if value == '--output':
        output = dict(item.split('=', 1) for item in sys.argv[i+1].split(','))
        pathlib.Path(output['path']).write_text('fixture artifact')
pathlib.Path(os.environ['GUIDELLM__LOGGING__LOG_FILE']).write_text('fixture native log')
print('fixture CLI output')
print('fixture CLI error', file=sys.stderr)
raise SystemExit(int(os.environ.get('AIG_FIXTURE_EXIT', '0')))
''')
        self.cli.chmod(0o755)

    def install_fixture_metadata(self):
        purelib = subprocess.check_output([str(self.python), '-c',
            'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
        metadata = Path(purelib) / 'guidellm-0.8.0.dist-info'
        metadata.mkdir()
        (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: guidellm\nVersion: 0.8.0\n')

    def run_runner(self, exit_code=0, python=None):
        source = GUIDELLM_REMOTE_SCRIPT.replace("'/tmp/aig-benchmark-control'", repr(str(self.artifacts)))
        with patch('app.benchmark.GUIDELLM_REMOTE_SCRIPT', source):
            command = remote_guidellm_command(self.spec, 5)
        command = command.replace('$HOME/.aig-vllm/bin/python', str(python or self.python))
        env = dict(os.environ, AIG_FIXTURE_EXIT=str(exit_code), PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        return subprocess.run(['bash'], input=command, text=True, capture_output=True, env=env)

    def test_managed_environment_version_cli_and_native_artifacts_agree(self):
        self.install_fixture_metadata()
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope['guidellm_version'], '0.8.0')
        self.assertEqual(Path(envelope['benchmark_python']).parent.resolve(), self.python.parent.resolve())
        self.assertEqual(Path(envelope['benchmark_python']).name, self.python.name)
        self.assertEqual(Path(envelope['command'][0]).resolve(), self.cli.resolve())
        self.assertEqual(base64.b64decode(envelope['artifacts']['log']['content_b64']), b'fixture native log')

    def test_native_failure_is_retained_and_reused_job_id_does_not_overwrite(self):
        self.install_fixture_metadata()
        first = json.loads(self.run_runner(exit_code=3).stdout)
        second_result = self.run_runner(exit_code=7)
        second = json.loads(second_result.stdout)
        self.assertEqual(first['returncode'], 3)
        self.assertEqual(second_result.returncode, 7)
        self.assertEqual(second['returncode'], 7)
        self.assertIn('fixture CLI error', second['stderr'])
        self.assertEqual(set(second['artifacts']), {'json', 'csv', 'html', 'log'})
        self.assertNotEqual(first['job_dir'], second['job_dir'])
        self.assertTrue(Path(first['job_dir'], 'benchmarks.json').exists())

    def test_missing_metadata_fails_before_cli_and_returns_structured_result(self):
        result = self.run_runner()
        self.assertEqual(result.returncode, 127)
        envelope = json.loads(result.stdout)
        self.assertIsNone(envelope['guidellm_version'])
        self.assertIn('Run VM Setup', envelope['stderr'])
        self.assertEqual(envelope['stdout'], '')
        self.assertEqual(envelope['artifacts'], {})
        self.assertNotIn('Traceback', result.stderr)

    def test_missing_managed_interpreter_has_actionable_error(self):
        result = self.run_runner(python=self.root / 'missing/python')
        self.assertEqual(result.returncode, 127)
        self.assertIn('Run VM Setup', result.stderr)

    def test_full_run_profile_disables_future_cooldown_boundary_in_both_modes(self):
        self.install_fixture_metadata()
        for mode, legacy_cooldown in [('concurrent', 0), ('concurrent', 5), ('sweep', 5)]:
            with self.subTest(mode=mode, legacy_cooldown=legacy_cooldown):
                self.spec = replace(self.spec, mode=mode, cooldown_seconds=legacy_cooldown)
                result = self.run_runner()
                self.assertEqual(result.returncode, 0, result.stderr)
                envelope = json.loads(result.stdout)
                args = envelope['command']
                profile = json.loads(args[args.index('--profile')+1])
                self.assertIsNone(profile['cooldown'])
                self.assertIsNone(profile['warmup'])
                self.assertEqual(envelope['profile'], profile)
                self.assertIn('kind=max_requests,count=2', args)
                self.assertIn('kind=max_duration,seconds=600', args)
                self.assertEqual(envelope['timing_policy']['name'], 'full-request-window-v1')
                self.assertEqual(envelope['timing_policy']['legacy_requested_cooldown_seconds'], legacy_cooldown)
