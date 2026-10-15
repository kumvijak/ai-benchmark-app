import base64
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.db import Database, SCHEMA
from app.vllm import VllmProfile, build_start_script, build_vllm_command
from app.benchmark import GuideLLMBenchmarkSpec, guide_spec_payload, GUIDELLM_REMOTE_SCRIPT


class PrecisionProfileTests(unittest.TestCase):
    def profile(self, **overrides):
        args=dict(model_ref='Qwen/Qwen3-4B-Base',dtype='bfloat16',max_model_len=4096,
                  max_num_batched_tokens=None,max_num_seqs=16,cpu_kvcache_space_gib=None,
                  reserved_cpu=2,prefix_cache_enabled=False,chunked_prefill_enabled=False,
                  request_timeout_seconds=30,port=8000)
        return VllmProfile(**(args|overrides))

    def test_quantized_command_cannot_fall_back_to_source_weights(self):
        for mode in ['int8','int4']:
            p=self.profile(quantization=mode)
            with self.assertRaises(ValueError):build_vllm_command(p)
            command=build_vllm_command(p,'/home/ubuntu/checkpoint')
            self.assertIn('--quantization compressed-tensors',command)
            self.assertIn('--served-model-name Qwen/Qwen3-4B-Base',command)
            self.assertIn('serve /home/ubuntu/checkpoint',command)
            self.assertNotIn('VLLM_CPU_KVCACHE_SPACE=',command)
            self.assertIn('unset VLLM_CPU_KVCACHE_SPACE',build_start_script(p,'/checkpoint'))
            if mode=='int4':self.assertIn('VLLM_CPU_INT4_W4A8=0',command)

    def test_legacy_database_profiles_survive_additive_migration(self):
        legacy=SCHEMA.replace("  quantization TEXT NOT NULL DEFAULT 'none',\n",'').replace('  model_revision TEXT,\n','').replace('  cpu_kvcache_auto INTEGER NOT NULL DEFAULT 0,\n','')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'old.db';con=sqlite3.connect(path);con.executescript(legacy)
            con.execute("INSERT INTO users(id,email,password_hash)VALUES(1,'a','x')")
            con.execute("INSERT INTO vllm_profiles(owner_user_id,name,model_ref,dtype,max_model_len,max_num_seqs,cpu_kvcache_space_gib,reserved_cpu,prefix_cache_enabled,chunked_prefill_enabled,request_timeout_seconds,port)VALUES(1,'old','model','bfloat16',4096,8,8,2,0,0,30,8000)")
            con.commit();con.close();db=Database(path);db.init();db.init();row=db.one('SELECT * FROM vllm_profiles')
            self.assertEqual((row['quantization'],row['cpu_kvcache_auto'],row['cpu_kvcache_space_gib']),('none',0,8))
            self.assertIsNone(row['model_revision'])

    def test_no_error_guard_preserves_both_original_native_limits(self):
        spec=GuideLLMBenchmarkSpec('model',8000,'Chat',256,128,[1,2,4,8,16],100,30,max_errors=None,max_duration_seconds=1200)
        payload=guide_spec_payload(spec,1)
        self.assertIsNone(payload['max_errors']);self.assertEqual(payload['max_duration_seconds'],1200)
        # Exercise CLI construction only; no synthetic measurements are produced.
        import json,sys,subprocess
        script=GUIDELLM_REMOTE_SCRIPT.split('env = os.environ.copy()',1)[0]+'\nprint(json.dumps(args))\n'
        result=subprocess.run([sys.executable,'-c',script],input=json.dumps(payload),text=True,capture_output=True,check=True)
        args=json.loads(result.stdout)
        self.assertNotIn('kind=max_errors,count=5',args)
        self.assertIn('kind=max_requests,count=100',args);self.assertIn('kind=max_duration,seconds=1200',args)


if __name__=='__main__':unittest.main()
