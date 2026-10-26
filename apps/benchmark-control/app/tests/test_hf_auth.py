"""HF credentials stay out of profile snapshots, SSH argv and API responses."""
import importlib
import json
import os
from pathlib import Path
import shlex
import stat
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

from app.db import Database, SCHEMA
from app.hf_auth import credential_commands
from app.security import SecretBox
from app.worker import Worker

TOKEN = 'hf_fixtureReadTokenNeverUseThis1234'


class HfAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, APP_SECRET='hf-test-secret-long-enough-123456', APP_LOCAL_USER_EMAIL='hf@test',
                        APP_DB_PATH=str(Path(cls.bootstrap.name)/'bootstrap.db'), APP_ARTIFACT_DIR=str(Path(cls.bootstrap.name)/'artifacts')):
            cls.api = importlib.import_module('app.main')

    @classmethod
    def tearDownClass(cls):
        cls.bootstrap.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/'app.db'); self.db.init()
        self.addCleanup(lambda:self.db.connect().close())
        for uid in [1,2]:
            self.db.execute('INSERT INTO users(id,email,password_hash) VALUES(?,?,?)',(uid,f'{uid}@test','unused'))
            self.db.execute('INSERT INTO sessions(token,user_id) VALUES(?,?)',(f'session-{uid}',uid))
        self.box = SecretBox('hf-test-secret-long-enough-123456')
        self.worker = Worker(self.db,self.box)
        self.patcher = patch.multiple(self.api,db=self.db,secret_box=self.box)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        from fastapi.testclient import TestClient
        self.client = TestClient(self.api.app); self.addCleanup(self.client.close)
        self.headers = {'Authorization':'Bearer session-1'}

    def profile(self, **kwargs):
        r=self.client.post('/api/vllm-profiles',json={'name':'Gated model','model_ref':'google/gemma-7b',**kwargs},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['id']

    def test_old_database_migrates_without_changing_profiles(self):
        path=Path(self.temp.name)/'legacy.db'
        con=sqlite3.connect(path)
        con.executescript(SCHEMA.replace('  encrypted_hf_token TEXT,\n',''))
        con.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'legacy@test','unused')")
        con.execute("INSERT INTO vllm_profiles(owner_user_id,name,model_ref,dtype,max_model_len,max_num_seqs,cpu_kvcache_space_gib,reserved_cpu,prefix_cache_enabled,chunked_prefill_enabled,request_timeout_seconds,port) VALUES(1,'legacy','google/gemma-7b','bfloat16',4096,8,8,2,0,0,30,8000)")
        con.commit();con.close()
        db=Database(path)
        try:
            db.init();db.init()
            row=db.one('SELECT name,encrypted_hf_token FROM vllm_profiles')
            self.assertEqual(row,{'name':'legacy','encrypted_hf_token':None})
        finally:db.connect().close()

    def test_encrypted_save_status_only_and_clear(self):
        pid=self.profile(hf_token=TOKEN)
        row=self.db.one('SELECT * FROM vllm_profiles WHERE id=?',(pid,))
        self.assertNotIn(TOKEN,row['encrypted_hf_token'])
        self.assertEqual(self.box.decrypt(row['encrypted_hf_token']),TOKEN)
        r=self.client.get('/api/vllm-profiles',headers=self.headers)
        self.assertTrue(r.json()[0]['hf_token_configured'])
        self.assertNotIn(TOKEN,r.text);self.assertNotIn('encrypted_hf_token',r.text)
        self.assertNotIn('hf_token',asdict(self.worker._vllm(1,pid)))
        r=self.client.put(f'/api/vllm-profiles/{pid}/hf-token',json={'hf_token':None},headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertFalse(r.json()['hf_token_configured'])
        self.assertIsNone(self.db.one('SELECT encrypted_hf_token FROM vllm_profiles WHERE id=?',(pid,))['encrypted_hf_token'])
        self.assertNotIn(TOKEN,json.dumps(self.db.all('SELECT * FROM audit_events')))

    def test_credential_update_requires_ownership_and_does_not_echo_validation_input(self):
        pid=self.profile()
        route=f'/api/vllm-profiles/{pid}/hf-token'
        self.assertEqual(self.client.put(route,json={'hf_token':TOKEN}).status_code,401)
        self.assertEqual(self.client.put(route,json={'hf_token':TOKEN},headers={'Authorization':'Bearer session-2'}).status_code,404)
        self.assertEqual(self.client.put(route,json={'hf_token':TOKEN},headers=self.headers).status_code,200)
        self.assertEqual(self.client.put(route,json={},headers=self.headers).status_code,422)
        r=self.client.post('/api/vllm-profiles',json={'name':'invalid','model_ref':'google/gemma-7b','dtype':'float32','quantization':'int8','hf_token':TOKEN},headers=self.headers)
        self.assertEqual(r.status_code,422);self.assertNotIn(TOKEN,r.text)

    def test_worker_passes_secret_over_stdin_and_skips_legacy_profiles(self):
        pid=self.profile(hf_token=TOKEN);ssh=MagicMock();ssh.run.return_value=(0,'','')
        environment=self.worker._hf_environment(ssh,1,pid)
        args=ssh.run.call_args
        self.assertEqual(args.kwargs['input_text'],TOKEN)
        self.assertNotIn(TOKEN,args.args[0]);self.assertNotIn(TOKEN,environment)
        self.assertIn('HF_TOKEN_PATH=',environment)
        with self.assertRaises(RuntimeError):self.worker._hf_environment(ssh,2,pid)
        ssh.run.return_value=(1,TOKEN,TOKEN)
        with self.assertRaises(RuntimeError) as error:self.worker._hf_environment(ssh,1,pid)
        self.assertNotIn(TOKEN,str(error.exception))
        self.db.execute('UPDATE vllm_profiles SET encrypted_hf_token=NULL WHERE id=?',(pid,))
        ssh.reset_mock();self.assertEqual(self.worker._hf_environment(ssh,1,pid),'');ssh.run.assert_not_called()

    def test_ssh_transports_token_as_stdin_and_redacts_error_text(self):
        from app.ssh_client import SshClient, VmProfile
        from app.security import redact
        ssh = SshClient(VmProfile('fixture.invalid',22,'test','unused',None))
        ssh._key_path = '/fixture/key'
        with patch('app.ssh_client.subprocess.run',return_value=type('Result',(),{'returncode':0,'stdout':'','stderr':''})()) as run:
            ssh.run('credential-writer', input_text=TOKEN)
        self.assertEqual(run.call_args.kwargs['input'],TOKEN)
        self.assertNotIn(TOKEN,json.dumps(run.call_args.args[0]))
        self.assertNotIn(TOKEN,redact('Authorization failed for '+TOKEN))

    def test_remote_file_permissions_and_no_shell_interpolation(self):
        write,environment=credential_commands(self.box.app_secret,1)
        other,_=credential_commands('different-app-secret',1)
        self.assertNotEqual(write,other)
        self.assertNotEqual(write,credential_commands(self.box.app_secret,2)[0])
        source=shlex.split(write)[2].replace('pathlib.Path.home()',f'pathlib.Path({self.temp.name!r})')
        secret=TOKEN+"'; $(touch injected); '"
        r=subprocess.run([sys.executable,'-c',source],input=secret,text=True,capture_output=True,check=True)
        token_file=next(Path(self.temp.name).rglob('*.token'))
        self.assertEqual(token_file.read_text(),secret)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(token_file.parent.stat().st_mode),0o700)
        self.assertEqual(r.stdout,'');self.assertEqual(r.stderr,'')
        self.assertFalse((Path(self.temp.name)/'injected').exists())

    def test_token_environment_covers_revision_quantization_and_launch(self):
        pid=self.profile(hf_token=TOKEN,quantization='int8')
        evidence={'bits':8,'validation':{'status':'PASS'},'source_revision':'a'*40,
                  'model_ref':'google/gemma-7b','format':'int-quantized','checkpoint_path':'/checkpoint'}
        ssh=MagicMock()
        def run(command,**kwargs):
            if 'input_text' in kwargs:return (0,'','')
            self.assertIn('export HF_TOKEN_PATH=',command)
            self.assertNotIn(TOKEN,command)
            if 'model_info' in command:return (0,'a'*40,'')
            if 'PREPARE_CHECKPOINT' in command:return (0,json.dumps(evidence),'')
            if 'START_RUNTIME' in command:return (0,'123','')
            raise AssertionError(command)
        ssh.run.side_effect=run
        factory=MagicMock();factory.return_value.__enter__.return_value=ssh
        with patch('app.worker.SshClient',factory),patch.object(self.worker,'_vm',return_value=None), \
             patch('app.quantization.build_prepare_command',return_value='PREPARE_CHECKPOINT'), \
             patch('app.worker.build_start_script',return_value='START_RUNTIME'), \
             patch('app.worker.wait_for_http',return_value={'ready':True}), \
             patch.object(self.worker,'_runtime_evidence',return_value={}):
            result=self.worker._start_runtime(1,{'vm_profile_id':1,'vllm_profile_id':pid})
        self.assertEqual(ssh.run.call_count,4)
        self.assertNotIn(TOKEN,json.dumps(result))

    def test_token_environment_used_for_guidellm_tokenizer(self):
        pid=self.profile(hf_token=TOKEN);profile=self.worker._vllm(1,pid)
        ssh=MagicMock();ssh.run_checked.return_value='{}'
        envelope={'returncode':0,'guidellm_version':'fixture','guidellm_json':{'benchmarks':[{}]}}
        def run(command,**kwargs):
            if 'input_text' in kwargs:return (0,'','')
            self.assertIn('export HF_TOKEN_PATH=',command)
            self.assertNotIn(TOKEN,command)
            return (0,json.dumps(envelope),'')
        ssh.run.side_effect=run
        factory=MagicMock();factory.return_value.__enter__.return_value=ssh
        with patch('app.worker.SshClient',factory),patch.object(self.worker,'_vm',return_value=None), \
             patch.object(self.worker,'_runtime_evidence',return_value={'serving_config':asdict(profile)}):
            result=self.worker._run_benchmark(1,dict(vm_profile_id=1,vllm_profile_id=pid,concurrency=1,
                workload_name='fixture',input_tokens=1,output_tokens=1,max_requests=1))
        self.assertNotIn(TOKEN,json.dumps(result));self.assertEqual(ssh.run.call_count,2)

if __name__=='__main__':unittest.main()
