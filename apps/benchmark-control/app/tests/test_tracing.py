"""Kernel tracing opt-in, cleanup, bounded evidence collection and API ownership."""
import base64
from dataclasses import asdict, replace
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

from app.db import Database
from app import tracing
from app.tracing import capture, summarize_trace, remote_trace_command
from app.vllm import VllmProfile, build_start_script, build_vllm_command
from app.worker import Worker, BenchmarkExecutionError, assert_serving_profile


def profile(**changes):
    return VllmProfile(**(dict(model_ref='test/model',dtype='bfloat16',max_model_len=4096,
        max_num_batched_tokens=None,max_num_seqs=16,cpu_kvcache_space_gib=None,reserved_cpu=2,
        prefix_cache_enabled=False,chunked_prefill_enabled=False,request_timeout_seconds=30,port=8000)|changes))


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.directory=self.root/'traces-8000';self.directory.mkdir()
        self.spec=dict(port=8000,concurrency=1,requests=2,max_tokens=64,model_ref='test/model',server_pid=123,server_start_ticks='456')
        self.calls=[];self.active=False
        self.trace={'traceEvents':[{'ph':'X','name':'zentorch::zentorch_linear_unary','dur':20},
                                  {'ph':'X','name':'zentorch::weight_prepack','dur':500},
                                  {'ph':'X','name':'_C::onednn_scaled_mm','dur':40}]}

    def call(self,port,route,payload=None,timeout=None):
        self.calls.append(route)
        if route=='/start_profile':self.active=True
        if route=='/stop_profile':
            self.active=False
            (self.directory/'worker.pt.trace.json.gz').write_bytes(gzip.compress(json.dumps(self.trace).encode()))
        return {'choices':[{'text':'fixture'}]} if payload is not None else None

    def run_capture(self,call=None):
        with patch('app.tracing.validate_identity'):
            return capture(self.spec,self.root,call or self.call)

    def test_profile_off_by_default_and_normal_launch_clears_inherited_settings(self):
        p=profile();self.assertFalse(p.kernel_tracing_enabled)
        self.assertNotIn('--profiler-config',build_vllm_command(p))
        script=build_start_script(p)
        self.assertIn('unset ZENDNNL_ENABLE_PROFILER',script)
        assert_serving_profile(p,{'serving_config':{k:v for k,v in asdict(p).items() if k!='kernel_tracing_enabled'}})
        enabled=replace(p,kernel_tracing_enabled=True)
        command=build_vllm_command(enabled)
        self.assertIn('--profiler-config',command);self.assertIn('ZENDNNL_ENABLE_PROFILER=1',command)
        self.assertIn('torch_profiler_activities',command);self.assertIn('traces-8000',command)
        subprocess.run(['bash','-n'],input=build_start_script(enabled),text=True,check=True)
        compile(remote_trace_command(self.spec).split("<<'TRACE'\n",1)[1].rsplit('\nTRACE',1)[0],'<remote capture>','exec')

    def test_generated_remote_payload_executes_before_validating_capture(self):
        # Execute both encoding layers in a real interpreter. Invalid bounds
        # stop before touching runtime files or contacting an inference server.
        command=remote_trace_command(self.spec | {'requests':0})
        result=subprocess.run(['bash'],input=command,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('ValueError: Diagnostic capture requires',result.stderr)
        self.assertNotIn('SyntaxError',result.stderr)

    def test_capture_profiles_requests_only_and_packages_gzip_worker_trace(self):
        (self.directory/'old.json').write_text('{}')
        result=self.run_capture()
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.calls,['/v1/completions','/start_profile','/v1/completions','/v1/completions','/stop_profile'])
        self.assertTrue(result['zentorch_operations_observed'])
        self.assertEqual(result['decoder_int8_execution'],'unverified')
        self.assertEqual(result['trace_summaries'][0]['zentorch_operations'],{'zentorch::zentorch_linear_unary':1})
        raw=base64.b64decode(result['bundle']['content_b64'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),result['bundle']['sha256'])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertIn('traces/worker.pt.trace.json.gz',archive.namelist())
            self.assertNotIn('traces/old.json',archive.namelist())
            self.assertEqual(json.loads(archive.read('capture.json'))['server_pid'],123)

    def test_inference_failure_always_stops_profiler_and_retains_partial_trace(self):
        def fail(port,route,payload=None,timeout=None):
            if route=='/v1/completions' and self.active:raise RuntimeError('fixture request failed')
            return self.call(port,route,payload,timeout)
        result=self.run_capture(fail)
        self.assertFalse(result['ok']);self.assertTrue(result['profiler_stopped'])
        self.assertIn('/stop_profile',self.calls);self.assertIn('bundle',result)

    def test_failed_warmup_never_collects_old_trace(self):
        (self.directory/'old.json').write_text(json.dumps(self.trace))
        def fail(*a,**kw):raise RuntimeError('warmup failed')
        result=self.run_capture(fail)
        self.assertFalse(result['ok']);self.assertEqual(result['trace_files'],[])
        self.assertFalse(result['zentorch_operations_observed'])

    def test_missing_trace_and_stop_failure_are_explicit(self):
        def no_trace(*a,**kw):return {'choices':[{}]}
        result=self.run_capture(no_trace)
        self.assertFalse(result['ok']);self.assertTrue(any('No new' in x for x in result['errors']))
        def bad_stop(port,route,payload=None,timeout=None):
            if route=='/stop_profile':raise RuntimeError('flush timeout')
            return self.call(port,route,payload,timeout)
        result=self.run_capture(bad_stop)
        self.assertFalse(result['profiler_stopped']);self.assertTrue(any('restart runtime' in x for x in result['errors']))

    def test_uncertain_profiler_state_requires_runtime_restart(self):
        (self.root/'trace-active-8000.json').write_text(json.dumps({'server_pid':123,'server_start_ticks':'456'}))
        result=self.run_capture()
        self.assertFalse(result['ok']);self.assertEqual(self.calls,[])
        self.assertTrue(any('restart runtime' in error for error in result['errors']))
        (self.root/'trace-active-8000.json').write_text(json.dumps({'server_pid':111,'server_start_ticks':'222'}))
        self.assertTrue(self.run_capture()['ok'])

    def test_legacy_profile_migration_defaults_tracing_off(self):
        from app.db import SCHEMA
        import sqlite3
        path=self.root/'legacy.db'
        con=sqlite3.connect(path)
        con.executescript(SCHEMA.replace('  kernel_tracing_enabled INTEGER NOT NULL DEFAULT 0,\n',''))
        con.commit();con.close()
        db=Database(path)
        try:
            db.init();db.init()
            columns={row['name'] for row in db.connect().execute('PRAGMA table_info(vllm_profiles)')}
            self.assertIn('kernel_tracing_enabled',columns)
        finally:db.connect().close()

    def test_capture_limits_and_no_unsupported_int8_claim(self):
        for change in ({'requests':17},{'max_tokens':129},{'concurrency':3,'requests':2}):
            with self.assertRaises(ValueError):capture(self.spec|change,self.root,self.call)
        summary=summarize_trace(json.dumps({'traceEvents':[{'name':'aten::mm','ph':'X','dur':20}]}).encode(),'trace.json')
        self.assertEqual(summary['zentorch_operations'],{})
        with patch('app.tracing.MAX_TRACE_BYTES',10):
            with self.assertRaisesRegex(ValueError,'analysis limit'):
                summarize_trace(gzip.compress(b' '*11),'trace.gz')

    def test_trace_enabled_profiles_cannot_be_benchmarked(self):
        worker=Worker(None,None);worker._vm=lambda *a:None;worker._vllm=lambda *a:profile(kernel_tracing_enabled=True)
        with self.assertRaisesRegex(RuntimeError,'diagnostic only'):
            worker._run_benchmark(1,{'vm_profile_id':1,'vllm_profile_id':1})

    def test_missing_identity_does_not_start_profiler_or_reconstruct_evidence(self):
        with self.assertRaisesRegex(RuntimeError, 'identity file.*missing.*start the tracing-enabled runtime'):
            capture(self.spec, self.root, self.call)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root/'runtime-8000.json').exists())

    def test_changed_identity_and_concurrent_capture_do_not_collect_other_evidence(self):
        with patch('app.tracing.validate_identity',side_effect=RuntimeError('changed')):
            with self.assertRaisesRegex(RuntimeError,'changed'):capture(self.spec,self.root,self.call)
        import fcntl
        with (self.root/'trace-8000.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=capture(self.spec,self.root,self.call)
        self.assertFalse(result['ok']);self.assertNotIn('bundle',result)


class TraceApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with patch.dict(os.environ,APP_SECRET='trace-test-only-secret-12345678',APP_LOCAL_USER_EMAIL='trace@test',APP_DB_PATH=str(self.root/'bootstrap.db'),APP_ARTIFACT_DIR=str(self.root/'artifacts')):
            self.api=importlib.import_module('app.main')
        self.db=Database(self.root/'test.db');self.db.init();self.addCleanup(lambda:self.db.connect().close())
        for uid in (1,2):
            self.db.execute('INSERT INTO users(id,email,password_hash) VALUES(?,?,?)',(uid,str(uid),'x'))
            self.db.execute('INSERT INTO sessions(token,user_id) VALUES(?,?)',(f'trace-{uid}',uid))
        self.db.execute("INSERT INTO vm_profiles(id,owner_user_id,name,host,port,username,encrypted_private_key) VALUES(1,1,'fixture','example.invalid',22,'ubuntu','x')")
        from fastapi.testclient import TestClient
        self.patch=patch.multiple(self.api,db=self.db,settings=types.SimpleNamespace(artifact_dir=self.root/'artifacts'))
        self.patch.start();self.addCleanup(self.patch.stop)
        self.client=TestClient(self.api.app);self.addCleanup(self.client.close)
        self.headers={'Authorization':'Bearer trace-1'}

    def save_profile(self,enabled):
        response=self.client.post('/api/vllm-profiles',headers=self.headers,json={'name':'trace' if enabled else 'normal','model_ref':'test/model','kernel_tracing_enabled':enabled})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['id']

    def test_profile_persistence_ownership_and_capture_validation(self):
        normal=self.save_profile(False);traced=self.save_profile(True)
        profiles=self.client.get('/api/vllm-profiles',headers=self.headers).json()
        self.assertEqual([p['kernel_tracing_enabled'] for p in profiles],[0,1])
        payload={'vm_profile_id':1,'vllm_profile_id':traced}
        self.assertEqual(self.client.post('/api/runtime/traces',json=payload).status_code,401)
        self.assertEqual(self.client.post('/api/runtime/traces',headers={'Authorization':'Bearer trace-2'},json=payload).status_code,404)
        self.assertEqual(self.client.post('/api/runtime/traces',headers=self.headers,json=payload|{'vllm_profile_id':normal}).status_code,409)
        self.assertEqual(self.client.post('/api/runtime/traces',headers=self.headers,json=payload|{'concurrency':4,'requests':2}).status_code,422)
        response=self.client.post('/api/runtime/traces',headers=self.headers,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.db.one('SELECT job_type FROM jobs WHERE id=?',(response.json()['job_id'],))['job_type'],'trace_runtime')
        response=self.client.post('/api/benchmarks',headers=self.headers,json=payload|{'workload_name':'Chat','input_tokens':16,'output_tokens':8,'max_requests':2,'concurrency':1})
        self.assertEqual(response.status_code,409)
        self.db.init();self.db.init()
        self.assertEqual(self.db.one('SELECT kernel_tracing_enabled FROM vllm_profiles WHERE id=?',(traced,))['kernel_tracing_enabled'],1)

    def test_partial_bundle_is_retained_on_failure_and_download_is_owned(self):
        self.save_profile(True)
        worker=Worker(self.db,None,self.root/'artifacts')
        p=profile(kernel_tracing_enabled=True)
        evidence={'serving_config':asdict(p),'server_pid':123,'server_start_ticks':'456'}
        worker._vllm=lambda *a:p;worker._vm=lambda *a:None;worker._runtime_evidence=lambda *a:evidence
        jid=self.db.create_job(1,'trace_runtime',{})
        raw=b'fixture bundle bytes';response={'ok':False,'errors':['fixture failure'],'bundle':{'content_b64':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}}
        with patch('app.worker.SshClient') as ssh:
            ssh.return_value.__enter__.return_value.run_checked.return_value=json.dumps(response)
            with self.assertRaises(BenchmarkExecutionError) as caught:
                worker._trace_runtime(1,{'vm_profile_id':1,'vllm_profile_id':1,'_job_id':jid,'concurrency':1,'requests':2,'max_tokens':64})
        result=caught.exception.result
        self.assertIn('trace_artifact',result);self.assertNotIn('bundle',result)
        self.db.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",(json.dumps(result),jid))
        path=f'/api/jobs/{jid}/trace.zip'
        self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.get(path,headers={'Authorization':'Bearer trace-2'}).status_code,404)
        downloaded=self.client.get(path,headers=self.headers)
        self.assertEqual(downloaded.status_code,200,downloaded.text);self.assertEqual(downloaded.content,raw)
