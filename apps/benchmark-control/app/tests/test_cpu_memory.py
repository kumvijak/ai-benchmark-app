import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from app.db import Database, SCHEMA
from app.vllm import build_start_script, build_vllm_command
from app.worker import Worker, assert_serving_profile
from app.tests import test_precision_profiles as precision, test_tracing as tracing


class CpuMemoryTests(unittest.TestCase):
    def test_auto_cache_budget_and_legacy_default_commands(self):
        profile = precision.PrecisionProfileTests().profile(cpu_memory_utilization=0.75)
        command = build_vllm_command(profile)
        self.assertIn('--gpu-memory-utilization 0.75', command)
        self.assertNotIn('VLLM_CPU_KVCACHE_SPACE=', command)
        self.assertIn('--gpu-memory-utilization 0.75', build_start_script(profile))
        self.assertNotIn('--gpu-memory-utilization', build_vllm_command(replace(profile, cpu_memory_utilization=None)))
        for invalid in (0, -0.1, 1.1, float('nan')):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                build_vllm_command(replace(profile, cpu_memory_utilization=invalid))

    def test_old_database_migration_preserves_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.db'
            con = sqlite3.connect(path)
            con.executescript(SCHEMA.replace('  cpu_memory_utilization REAL,\n', ''))
            con.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'old@test','x')")
            con.execute("INSERT INTO vllm_profiles(owner_user_id,name,model_ref,dtype,max_model_len,max_num_seqs,cpu_kvcache_space_gib,reserved_cpu,prefix_cache_enabled,chunked_prefill_enabled,request_timeout_seconds,port) VALUES(1,'old','model','bfloat16',512,2,8,1,0,0,120,8000)")
            con.commit(); con.close()
            db = Database(path)
            try:
                db.init(); db.init()
                row = db.one('SELECT * FROM vllm_profiles')
                self.assertIsNone(row['cpu_memory_utilization'])
                self.assertEqual(row['cpu_kvcache_space_gib'], 8)
            finally:
                db.connect().close()


class CpuMemoryApiTests(unittest.TestCase):
    setUp = tracing.TraceApiTests.setUp

    def test_round_trip_worker_evidence_and_validation(self):
        payload = {'name': 'auto cache 75%', 'model_ref': 'test/model',
                   'cpu_kvcache_space_gib': None, 'cpu_memory_utilization': 0.75}
        response = self.client.post('/api/vllm-profiles', headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        profile_id = response.json()['id']
        rows = self.client.get('/api/vllm-profiles', headers=self.headers).json()
        self.assertEqual(rows[0]['cpu_memory_utilization'], 0.75)
        self.assertIsNone(rows[0]['cpu_kvcache_space_gib'])
        profile = Worker(self.db, None)._vllm(1, profile_id)
        self.assertIn('--gpu-memory-utilization 0.75', build_vllm_command(profile))
        # Reject evidence from a server started with a different memory budget.
        with self.assertRaises(RuntimeError):
            assert_serving_profile(profile, {'serving_config': asdict(replace(profile, cpu_memory_utilization=None))})
        for invalid in (0, -0.1, 1.01):
            with self.subTest(value=invalid):
                result = self.client.post('/api/vllm-profiles', headers=self.headers,
                                          json=payload | {'cpu_memory_utilization': invalid})
                self.assertEqual(result.status_code, 422)
        self.assertEqual(self.client.get('/api/vllm-profiles', headers={'Authorization': 'Bearer trace-2'}).json(), [])
