import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import Database, SCHEMA
from app.runtime import RUNTIME_VERIFY_PY, build_runtime_check_script, build_runtime_launcher
from app.ssh_client import VM_SETUP_SCRIPT
from app.vllm import VllmProfile, build_start_script
from app.worker import Worker
from app.benchmark import GuideLLMBenchmarkSpec, GUIDELLM_REMOTE_SCRIPT, guide_spec_payload


class RuntimeTests(unittest.TestCase):
    def verify(self, config='AMD ZENDNNL v"6.0.1" ( Git Hash abc )', zen=True,
               torch_version='2.13.0+cpu', isa=True, vendor='amd', flags=None,
               amx_runtime_supported=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'libzentorch.so').write_bytes(b'native-library-fixture')
            torch = types.ModuleType('torch')
            torch.__version__ = torch_version
            torch.version = types.SimpleNamespace(cuda=None)
            zentorch = types.ModuleType('zentorch')
            zentorch.__file__ = str(root/'__init__.py')
            zentorch.__config__ = config
            zentorch._C = types.SimpleNamespace(is_avx512_supported=lambda: isa)
            vllm = types.ModuleType('vllm'); vllm.__path__ = []
            vllm.__version__ = '0.31.0'
            platforms = types.ModuleType('vllm.platforms')
            platforms.current_platform = type('ZenCpuPlatform' if zen else 'CpuPlatform', (), {'__module__': 'vllm.platforms.zen_cpu' if zen else 'vllm.platforms.cpu', 'is_zen_cpu': lambda self: zen})()
            modules = {'torch': torch, 'zentorch': zentorch, 'vllm': vllm,
                       'vllm.platforms': platforms, 'vllm._C': types.ModuleType('vllm._C')}
            if vendor == 'intel':
                modules['zentorch'] = None  # Intel verification must not import ZenTorch.
            versions = {'vllm': '0.31.0+cpu', 'zentorch': '2.13.0.1', 'triton': '3.8.0+cpu'}
            flags = flags if flags is not None else 'avx2 avx512f amx_tile amx_bf16'
            if amx_runtime_supported is None:
                amx_runtime_supported = vendor == 'intel' and 'amx_tile' in flags and 'amx_bf16' in flags
            torch.cpu = types.SimpleNamespace(_is_amx_tile_supported=lambda: amx_runtime_supported)
            cpu_info = f"vendor_id : {'AuthenticAMD' if vendor == 'amd' else 'GenuineIntel' if vendor == 'intel' else 'OtherVendor'}\nflags : {flags}\n"
            output = io.StringIO()
            with patch.dict('sys.modules', modules), patch('importlib.metadata.version', side_effect=versions.__getitem__), patch('pathlib.Path.read_text', return_value=cpu_info), contextlib.redirect_stdout(output):
                exec(RUNTIME_VERIFY_PY, {'__name__': '__main__'})
            return json.loads(output.getvalue())

    def test_native_build_metadata_and_hashes_are_recorded(self):
        evidence = self.verify()
        self.assertEqual(evidence['zendnn_version'], '6.0.1')
        self.assertEqual(len(evidence['native_library_sha256']['libzentorch.so']), 64)

    def test_unknown_and_wrong_native_versions_fail(self):
        for config in ('AMD ZENDNNL v6.0.0', 'config unavailable', 'zentorch Version: 6.0.1'):
            with self.subTest(config=config), self.assertRaisesRegex(RuntimeError, 'zendnn_version'):
                self.verify(config=config)

    def test_standard_cpu_abi_mismatch_and_missing_isa_fail(self):
        for kwargs, message in (({'zen': False}, 'ZenDNN required'),
                                ({'torch_version': '2.14.0+cpu'}, 'torch_version'),
                                ({'isa': False}, 'AVX-512')):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(RuntimeError, message):
                self.verify(**kwargs)

    def test_intel_standard_platform_needs_no_zentorch(self):
        evidence = self.verify(vendor='intel', zen=False, flags='avx2')
        self.assertEqual(evidence['backend'], 'intel_cpu')
        self.assertIsNone(evidence['zendnn_version'])
        self.assertFalse(evidence['amx_runtime_supported'])
        self.assertFalse(evidence['amx_eligible'])
        self.assertEqual(evidence['amx_policy'], 'library_auto_dispatch')

    def test_intel_amx_capability_and_runtime_support_are_reported(self):
        evidence = self.verify(vendor='intel', zen=False)
        self.assertTrue(evidence['amx_eligible'])
        self.assertTrue(evidence['amx_runtime_supported'])
        evidence = self.verify(vendor='intel', zen=False, amx_runtime_supported=False)
        self.assertTrue(evidence['amx_eligible'])
        self.assertFalse(evidence['amx_runtime_supported'])

    def test_vendor_and_platform_mismatches_fail(self):
        for kwargs in ({'vendor': 'other'}, {'vendor': 'intel', 'zen': True},
                       {'vendor': 'intel', 'zen': False, 'flags': ''}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                self.verify(**kwargs)

    def test_amx_dispatch_uses_library_defaults_without_a_profile_knob(self):
        from app.vllm import build_vllm_command
        profile = VllmProfile('model', 'bfloat16', 4096, None, 8, None, 2, False, False, 30, 8000)
        self.assertNotIn('VLLM_CPU_SGL_KERNEL=', build_vllm_command(profile))
        self.assertIn('unset VLLM_CPU_SGL_KERNEL', build_start_script(profile))

    def test_launcher_records_actual_backend_before_cli_and_is_spawn_safe(self):
        from app import runtime
        definitions = "import os, json, pathlib\ndef verify_runtime():\n    return {'backend': 'intel_cpu', 'platform': 'vllm.platforms.cpu.CpuPlatform'}\nif __name__ == '__main__':\n    pass\n"
        cli = types.ModuleType('vllm.entrypoints.cli.main')
        calls = []
        cli.main = lambda: calls.append('started')
        with patch.object(runtime, 'RUNTIME_VERIFY_PY', definitions):
            launcher = build_runtime_launcher()
        output = io.StringIO()
        with patch.dict('sys.modules', {'vllm.entrypoints.cli.main': cli}), patch.dict(os.environ, {'AIG_RUNTIME_PORT': '8000'}), patch('sys.argv', ['launcher.py', 'serve', 'model']), patch('os.getpid', return_value=123), patch('pathlib.Path.read_text', return_value='123 (server) ' + ' '.join(['0'] * 19 + ['456'])), patch('pathlib.Path.write_text') as write, patch('pathlib.Path.replace'), contextlib.redirect_stdout(output):
            exec(launcher, {'__name__': '__main__'})
            evidence = json.loads(write.call_args.args[0])
            self.assertEqual(evidence['server_pid'], 123)
            self.assertEqual(evidence['server_start_ticks'], '456')
            self.assertEqual(evidence['backend'], 'intel_cpu')
            self.assertEqual(calls, ['started'])
            self.assertIn('AIG_RUNTIME_BACKEND=', output.getvalue())
            calls.clear()
            exec(launcher, {'__name__': '__mp_main__'})
            self.assertEqual(calls, [])

    def test_setup_installs_zentorch_only_for_amd(self):
        code = VM_SETUP_SCRIPT.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for vendor in ('amd', 'intel'):
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as temp:
                home = Path(temp); (home/'.aig-vllm').mkdir()
                calls = []; checks = 0; guides = 0
                cpu = 'AuthenticAMD' if vendor == 'amd' else 'GenuineIntel'
                def run(command, **kwargs):
                    nonlocal checks, guides
                    calls.append(command)
                    result = ''; status = 0
                    if command == 'lscpu': result = cpu
                    elif command.startswith('lscpu'):
                        result = 'avx2 avx512f amx_tile amx_bf16' if 'Flags|Features' in command else cpu
                    elif command.startswith('set -e'):
                        checks += 1
                        if checks == 1: status = 1
                        else: result = json.dumps({'vllm_version': '0.31.0+cpu', 'backend': 'zendnn' if vendor == 'amd' else 'intel_cpu'})
                    elif 'pip show guidellm' in command:
                        guides += 1
                        if guides == 1: status = 1
                        else: result = 'Version: 0.8.0'
                    elif 'findmnt' in command: result = '/dev/mapper/root'
                    return types.SimpleNamespace(returncode=status, stdout=result, stderr='')
                output = io.StringIO()
                def expanduser(path): return str(home / path[2:]) if path.startswith('~/') else path
                with patch('subprocess.run', side_effect=run), patch('os.path.expanduser', side_effect=expanduser), patch('shutil.which', return_value='/fake/bin'), patch('sys.version_info', (3, 12)), patch.dict(os.environ), contextlib.redirect_stdout(output):
                    exec(code, {'__name__': '__main__'})
                setup = json.loads(output.getvalue())
                self.assertTrue(setup['ok'])
                installs = [cmd for cmd in calls if 'pip install' in cmd]
                self.assertEqual(any("'zentorch==2.13.0.1'" in cmd for cmd in installs), vendor == 'amd')
                constraints = (home/'.aig-vllm/runtime-constraints.txt').read_text()
                self.assertEqual('zentorch==2.13.0.1' in constraints, vendor == 'amd')

    def test_generated_remote_programs_have_valid_syntax(self):
        compile(build_runtime_launcher(), '<launcher>', 'exec')
        compile(VM_SETUP_SCRIPT.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], '<setup>', 'exec')
        profile = VllmProfile('model', 'bfloat16', 4096, None, 8, None, 2, False, False, 30, 8000)
        for script in (build_runtime_check_script(), build_start_script(profile)):
            subprocess.run(['bash', '-n'], input=script, text=True, check=True)

    def test_legacy_setup_evidence_migration_and_upsert(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'db'
            con = sqlite3.connect(path)
            con.executescript(SCHEMA.replace("  runtime_evidence TEXT NOT NULL DEFAULT '{}',\n", ''))
            con.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'test','x')")
            con.execute("INSERT INTO vm_profiles(id,owner_user_id,name,host,port,username,encrypted_private_key) VALUES(1,1,'vm','host',22,'ubuntu','x')")
            con.commit(); con.close()
            db = Database(path); db.init(); db.init()
            worker = Worker(db, None)
            db.execute("INSERT INTO vllm_profiles(owner_user_id,name,model_ref,dtype,max_model_len,max_num_seqs,cpu_kvcache_space_gib,reserved_cpu,prefix_cache_enabled,chunked_prefill_enabled,request_timeout_seconds,port) VALUES(1,'legacy','model','bfloat16',4096,8,8,2,0,0,30,8000)")
            self.assertEqual(worker._vllm(1, 1).model_ref, 'model')
            for version in ('old', '6.0.1'):
                worker._store_vm_setup_status(1, 1, {'runtime_evidence': {'zendnn_version': version}})
            row = db.one('SELECT runtime_evidence FROM vm_setup_status')
            self.assertEqual(json.loads(row['runtime_evidence']), {'zendnn_version': '6.0.1'})
            db.connect().close()

    def test_benchmark_rejects_unverified_and_stale_servers(self):
        evidence = dict(vllm_version='0.31.0+cpu', torch_version='2.13.0+cpu',
                        zentorch_version='2.13.0.1', zendnn_version='6.0.1',
                        server_activation_verified=False, server_alive=True, backend='zendnn', cpu_vendor='amd',
                        platform='vllm.platforms.zen_cpu.ZenCpuPlatform', amx_runtime_supported=False,
                        amx_policy='library_auto_dispatch')
        ssh = types.SimpleNamespace(run_checked=lambda *a, **kw: json.dumps(evidence))
        worker = Worker(None, None)
        with self.assertRaisesRegex(RuntimeError, 'activation'):
            worker._runtime_evidence(ssh, 8000)
        evidence.update(server_activation_verified=True, server_alive=False)
        with self.assertRaisesRegex(RuntimeError, 'activation'):
            worker._runtime_evidence(ssh, 8000)
        evidence['server_alive'] = True
        self.assertTrue(worker._runtime_evidence(ssh, 8000)['server_activation_verified'])
        evidence.update(backend='intel_cpu', cpu_vendor='intel', platform='vllm.platforms.cpu.CpuPlatform',
                        amx_eligible=True, amx_runtime_supported=True)
        self.assertEqual(worker._runtime_evidence(ssh, 8000)['backend'], 'intel_cpu')
        evidence['amx_eligible'] = False
        with self.assertRaisesRegex(RuntimeError, 'AMX'):
            worker._runtime_evidence(ssh, 8000)
        evidence.update(backend='zendnn', cpu_vendor='amd', platform='vllm.platforms.zen_cpu.ZenCpuPlatform',
                        amx_runtime_supported=False)
        evidence['zendnn_version'] = '6.0.0'
        with self.assertRaisesRegex(RuntimeError, 'pinned'):
            worker._runtime_evidence(ssh, 8000)

    def test_markdown_reports_include_the_actual_backend(self):
        from app.benchmark import report_markdown
        result = {'normalized_rows': [{'requests': 1, 'success_requests': 1}],
                  'runtime_evidence': {'backend': 'intel_cpu', 'platform': 'vllm.platforms.cpu.CpuPlatform',
                                       'vllm_version': '0.31.0+cpu', 'torch_version': '2.13.0+cpu',
                                       'amx_runtime_supported': True,
                                       'amx_policy': 'library_auto_dispatch'}}
        report = report_markdown(result)
        self.assertIn('Backend: intel_cpu', report)
        self.assertIn('Intel AMX runtime support: available', report)
        self.assertIn('AMX selection: automatic', report)

    def test_guidellm_artifact_labels_include_detected_versions(self):
        spec = GuideLLMBenchmarkSpec('model', 8000, 'Smoke', 16, 8, [1], 2, 30,
                                    runtime_evidence={'backend': 'zendnn', 'zendnn_version': '6.0.1'})
        script = GUIDELLM_REMOTE_SCRIPT.split('env = os.environ.copy()', 1)[0] + '\nprint(json.dumps(args))\n'
        import sys
        result = subprocess.run([sys.executable, '-c', script], input=json.dumps(guide_spec_payload(spec, 1)), text=True, capture_output=True, check=True)
        args = json.loads(result.stdout)
        self.assertIn('backend=zendnn', args)
        self.assertIn('zendnn_version=6.0.1', args)


if __name__ == '__main__':
    unittest.main()
