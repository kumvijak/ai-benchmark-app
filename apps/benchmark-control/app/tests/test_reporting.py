"""Report integrity, queue fencing, access control and benchmark evidence regressions."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from app.benchmark import normalize_guidellm_result
from app.db import Database
from app.report_worker import ReportWorker
from app.reporting import enqueue_report, groups_from_snapshot, snapshot_job, sanitized
from app.vllm import VllmProfile
from app.worker import Worker, BenchmarkExecutionError, assert_serving_profile


def fixture(precision='int8', failed=False):
    profile = dict(model_ref='test/model', model_revision='a'*40, dtype='bfloat16', quantization=precision,
                   max_model_len=4096, max_num_seqs=16, cpu_kvcache_space_gib=None)
    spec = dict(model_ref='test/model', workload_name='Chat', input_tokens=256, output_tokens=128,
                max_requests=10, max_errors=None, max_duration_seconds=1200, concurrencies=[1,2])
    benchmarks = []
    for c in [1,2]:
        benchmarks.append(dict(config={'strategy':{'streams':c,'type_':'concurrent'}}, duration=10,
            metrics=dict(request_totals={'total':10,'successful':8 if failed else 10,'errored':1 if failed else 0,'incomplete':1 if failed else 0},
                output_tokens_per_second={'successful':{'mean':100*c}},
                request_latency={'successful':{'mean':2.5,'percentiles':{'p50':2,'p95':4}}},
                time_to_first_token_ms={'successful':{'mean':20,'percentiles':{'p50':10,'p95':50}}},
                time_per_output_token_ms={'successful':{'percentiles':{'p95':12}}})))
    native = dict(benchmarks=benchmarks)
    return dict(spec=spec, guidellm_json=native,
        runtime_evidence={'backend':'intel_cpu','vllm_version':'0.31.0+cpu','serving_config':profile},
        execution_snapshot={'profile':profile,'system':{'shape':'VM.Standard4.Ax.Flex','ocpus':16,'memory_gb':64},'serving_identity_verified':True})


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = Database(self.root/'test.db'); self.db.init()
        self.addCleanup(lambda: self.db.connect().close())
        for uid in [1,2]:
            self.db.execute('INSERT INTO users(id,email,password_hash) VALUES(?,?,?)', (uid,f'{uid}@test','x'))
        self.worker = ReportWorker(self.db,self.root/'artifacts')

    def job(self, result=None, user=1, status='succeeded'):
        jid = self.db.create_job(user,'run_benchmark',{'vm_profile_id':1})
        self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',(status,json.dumps(fixture() if result is None else result),jid))
        return jid

    def queue(self, ids, title='Test report'):
        return enqueue_report(self.db,1,title,ids)['id']


class ReportingTests(ReportTestCase):
    def test_snapshot_is_idempotent_owned_and_survives_job_deletion(self):
        jid = self.job(); rid = self.queue([jid,jid]); self.assertEqual(self.queue([jid]),rid)
        foreign = self.job(user=2)
        with self.assertRaises(LookupError): self.queue([foreign])
        self.db.execute('DELETE FROM jobs WHERE id=?',(jid,))
        self.worker.process(self.worker.claim())
        row = self.db.one('SELECT * FROM report_jobs WHERE id=?',(rid,))
        self.assertEqual(row['status'],'succeeded')
        root = self.worker.root/'1'/str(rid)/row['artifact_token']
        manifest = json.loads((root/'manifest.json').read_text())
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)
        with zipfile.ZipFile(root/'report.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('index.html',archive.namelist())
            self.assertIn(f'evidence/job-{jid}.json',archive.namelist())
        self.assertEqual(len(list(root.glob('*.html'))),2)

    def test_grouping_preserves_retries_and_separates_precision_and_limits(self):
        ids = [self.job(),self.job(),self.job(fixture('none'))]
        different = fixture(); different['spec']['max_requests']=100; ids.append(self.job(different))
        rid = self.queue(ids)
        groups=groups_from_snapshot(json.loads(self.db.one('SELECT snapshot FROM report_jobs WHERE id=?',(rid,))['snapshot']))
        self.assertEqual(len(groups),3)
        retry_group=next(g for g in groups if len(g['attempts'])==2)
        self.assertEqual(len(retry_group['points']),4)

    def test_latency_units_and_explicit_metric_semantics(self):
        result=fixture(); row=normalize_guidellm_result(result)['normalized_rows'][0]
        self.assertEqual(row['e2e_ms_p95'],4000)
        jid=self.job(result); rid=self.queue([jid]); snapshot=json.loads(self.db.one('SELECT snapshot FROM report_jobs WHERE id=?',(rid,))['snapshot'])
        point=groups_from_snapshot(snapshot)[0]['points'][0]
        self.assertEqual(point['latency_ms']['latency']['mean'],2500)
        self.assertEqual(point['ttft_p95_ms'],50)
        self.assertIsNone(point['reconstructed_system_tps'])
        self.assertEqual(point['outcome'],'complete')
        self.assertEqual(point['evidence_status'],'warning')

    def test_missing_native_evidence_does_not_create_measurements_or_success(self):
        jid=self.job({},status='failed'); rid=self.queue([jid]); snapshot=json.loads(self.db.one('SELECT snapshot FROM report_jobs WHERE id=?',(rid,))['snapshot'])
        point=groups_from_snapshot(snapshot)[0]['points'][0]
        self.assertEqual(point['outcome'],'failed')
        self.assertIsNone(point['native_system_tps'])
        self.assertTrue(any('Native JSON unavailable' in w for w in point['warnings']))

    def test_error_and_incomplete_counts_are_retained(self):
        jid=self.job(fixture(failed=True),status='failed'); rid=self.queue([jid]); snapshot=json.loads(self.db.one('SELECT snapshot FROM report_jobs WHERE id=?',(rid,))['snapshot'])
        point=groups_from_snapshot(snapshot)[0]['points'][0]
        self.assertEqual((point['errored_requests'],point['incomplete_requests'],point['failed_requests']),(1,1,2))
        self.assertEqual(point['outcome'],'failed')

    def test_atomic_claim_does_not_consume_execution_jobs(self):
        jid=self.job(); rid=self.queue([jid]); execution=self.db.create_job(1,'health_check',{})
        def claim(_):
            db=Database(self.db.path)
            try: return ReportWorker(db,self.root/'artifacts').claim()
            finally: db.connect().close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(claim,range(2)))
        self.assertEqual(sum(c is not None for c in claims),1)
        self.assertEqual(self.db.one('SELECT status FROM jobs WHERE id=?',(execution,))['status'],'queued')
        self.assertEqual(next(c['id'] for c in claims if c),rid)

    def test_expired_lease_recovers_with_bounded_retries_and_fences_old_worker(self):
        rid=self.queue([self.job()]); old=self.worker.claim()
        self.db.execute('UPDATE report_jobs SET lease_until=0 WHERE id=?',(rid,))
        newer=ReportWorker(self.db,self.root/'artifacts').claim()
        self.assertNotEqual(newer['lease_token'],old['lease_token'])
        with self.assertLogs('app.report_worker',level='ERROR'): self.worker.process(old)
        row=self.db.one('SELECT * FROM report_jobs WHERE id=?',(rid,))
        self.assertEqual(row['status'],'running');self.assertEqual(row['lease_token'],newer['lease_token'])
        self.db.execute('UPDATE report_jobs SET lease_until=0,attempts=3 WHERE id=?',(rid,))
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.db.one('SELECT status FROM report_jobs WHERE id=?',(rid,))['status'],'failed')

    def test_sanitization_and_html_escaping(self):
        result=fixture(); result['guidellm_json']['api_key']='test-sensitive'
        result['guidellm_json']['response_text']='<sensitive>'
        jid=self.job(result);rid=self.queue([jid],'<img src=x onerror=alert(1)>')
        row=self.db.one('SELECT * FROM report_jobs WHERE id=?',(rid,))
        self.assertNotIn('test-sensitive',row['snapshot']);self.assertNotIn('<sensitive>',row['snapshot'])
        self.worker.process(self.worker.claim())
        row=self.db.one('SELECT * FROM report_jobs WHERE id=?',(rid,))
        root=self.worker.root/'1'/str(rid)/row['artifact_token']
        self.assertNotIn('<img src=x', (root/'index.html').read_text())
        self.assertIn('&lt;img', (root/'index.html').read_text())

    def test_serving_precision_mismatch_is_rejected(self):
        profile=VllmProfile('model','bfloat16',4096,None,8,None,2,False,False,30,8000,'int8')
        with self.assertRaisesRegex(RuntimeError,'does not match'): assert_serving_profile(profile,{'serving_config':asdict(profile)|{'quantization':'none'}})
        assert_serving_profile(profile,{'serving_config':asdict(profile)})

    def test_failed_guidellm_retains_native_artifacts_and_execution_snapshot(self):
        profile=VllmProfile('model','bfloat16',4096,None,8,None,2,False,False,30,8000)
        w=Worker(self.db,None);w._vm=lambda *a:None;w._vllm=lambda *a:profile
        w._runtime_evidence=lambda *a:{'serving_config':asdict(profile)}
        raw=fixture(failed=True)['guidellm_json']
        envelope={'returncode':1,'spec':fixture()['spec'],'artifacts':{'json':{'content_b64':base64.b64encode(json.dumps(raw).encode()).decode()}}}
        def check_remote(command, **kwargs):
            compile(command.split("<<'SYSTEM'\n",1)[1].rsplit('\nSYSTEM',1)[0], '<hardware snapshot>', 'exec')
            return '{}'
        ssh=types.SimpleNamespace(run_checked=check_remote,run=lambda *a,**kw:(1,json.dumps(envelope),''))
        with patch('app.worker.SshClient') as cls:
            cls.return_value.__enter__.return_value=ssh
            with self.assertRaises(BenchmarkExecutionError) as caught:
                w._run_benchmark(1,dict(vm_profile_id=1,vllm_profile_id=1,workload_name='Chat',input_tokens=256,output_tokens=128,concurrencies=[1],max_requests=10))
        self.assertEqual(caught.exception.result['guidellm_json'],raw)
        self.assertTrue(caught.exception.result['execution_snapshot']['serving_identity_verified'])

    def test_standalone_worker_needs_no_credentials_and_publishes_bundle(self):
        rid=self.queue([self.job()])
        env={k:v for k,v in os.environ.items() if k not in ('APP_SECRET','APP_LOCAL_USER_EMAIL')}
        env.update(APP_DB_PATH=str(self.db.path),APP_ARTIFACT_DIR=str(self.root/'artifacts'))
        process=subprocess.Popen([sys.executable,'-B','-m','app.report_worker'],cwd=Path(__file__).resolve().parents[2],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            deadline=time.time()+8
            while time.time()<deadline:
                row=self.db.one('SELECT status FROM report_jobs WHERE id=?',(rid,))
                if row['status'] in ('succeeded','failed'):break
                if process.poll() is not None:break
                time.sleep(0.05)
            self.assertEqual(row['status'],'succeeded')
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_worker_persists_failure_envelope(self):
        jid=self.db.create_job(1,'run_benchmark',{})
        worker=Worker(self.db,None)
        def fail(job):
            worker._stop.set()
            raise BenchmarkExecutionError('test failure',{'artifacts':{'json':{'content':'retained'}}})
        worker._run_job=fail
        worker._loop()
        row=self.db.one('SELECT status,result FROM jobs WHERE id=?',(jid,))
        self.assertEqual(row['status'],'failed')
        self.assertEqual(json.loads(row['result'])['artifacts']['json']['content'],'retained')


class ReportApiTests(ReportTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_temp=tempfile.TemporaryDirectory()
        with patch.dict(os.environ,APP_SECRET='test-secret-for-reports-only-1234',APP_LOCAL_USER_EMAIL='review@test',APP_DB_PATH=str(Path(cls.app_temp.name)/'bootstrap.db'),APP_ARTIFACT_DIR=str(Path(cls.app_temp.name)/'artifacts')):
            cls.api_module=importlib.import_module('app.main')

    @classmethod
    def tearDownClass(cls):
        cls.api_module.db.connect().close()
        cls.app_temp.cleanup()

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        for uid in [1,2]:self.db.execute('INSERT INTO sessions(token,user_id) VALUES(?,?)',(f'token-{uid}',uid))
        self.patch=patch.multiple(self.api_module,db=self.db,settings=types.SimpleNamespace(artifact_dir=self.root/'artifacts'))
        self.patch.start();self.addCleanup(self.patch.stop)
        self.client=TestClient(self.api_module.app);self.addCleanup(self.client.close)
        self.headers={'Authorization':'Bearer token-1'}

    def test_api_ownership_download_cookie_and_traversal(self):
        jid=self.job()
        self.assertEqual(self.client.post('/api/generated-reports',json={'job_ids':[jid]}).status_code,401)
        self.assertEqual(self.client.post('/api/generated-reports',json={'job_ids':[jid]},headers={'Authorization':'Bearer token-2'}).status_code,404)
        response=self.client.post('/api/generated-reports',json={'job_ids':[jid]},headers=self.headers)
        self.assertEqual(response.status_code,200,response.text);rid=response.json()['id']
        prefix=f'/api/generated-reports/{rid}/files/'
        self.assertEqual(self.client.get(prefix+'index.html',headers=self.headers).status_code,409)
        self.worker.process(self.worker.claim())
        self.assertEqual(self.client.post(f'/api/generated-reports/{rid}/access').status_code,401)
        response=self.client.post(f'/api/generated-reports/{rid}/access',headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        response=self.client.get(prefix+'index.html')
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('sandbox',response.headers['content-security-policy'])
        self.assertEqual(self.client.get(prefix+'metrics.csv').status_code,200)
        self.assertEqual(self.client.get(prefix+'index.html',headers={'Authorization':'Bearer token-2'}).status_code,404)
        self.assertEqual(self.client.get(prefix+'%2e%2e%2f%2e%2e%2ftest.db',headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/api/generated-reports',headers={'Authorization':'Bearer token-2'}).json(),[])
        self.assertEqual(self.client.post(f'/api/generated-reports/{rid}/retry',headers=self.headers).status_code,409)

    def test_failed_report_retry_and_frozen_sources(self):
        rid=self.queue([self.job()]);self.db.execute("UPDATE report_jobs SET status='failed' WHERE id=?",(rid,))
        response=self.client.post(f'/api/generated-reports/{rid}/retry',headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['status'],'queued')
        self.db.execute("UPDATE jobs SET status='running'")
        self.assertEqual(self.client.get('/api/generated-reports/sources',headers=self.headers).json(),[])
        self.worker.process(self.worker.claim())
        self.assertEqual(self.db.one('SELECT status FROM report_jobs WHERE id=?',(rid,))['status'],'succeeded')
