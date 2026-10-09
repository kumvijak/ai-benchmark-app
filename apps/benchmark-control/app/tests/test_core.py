import json
import os
import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.benchmark import GuideLLMBenchmarkSpec, remote_guidellm_command, report_csv, report_markdown
from app.db import Database
from app.oci_ops import DEFAULT_VM_DEPLOYMENT, DOC_APPROVED_SHAPES, profile_text_with_key_file, shape_config_details
from app.security import SecretBox, hash_password, verify_password
from app.vllm import VllmProfile, build_start_script, build_vllm_command


class CoreTests(unittest.TestCase):
    def test_vllm_command_uses_profile_values(self):
        profile = VllmProfile(
            model_ref="example/model",
            dtype="bfloat16",
            max_model_len=4096,
            max_num_batched_tokens=8192,
            max_num_seqs=8,
            cpu_kvcache_space_gib=40,
            reserved_cpu=2,
            prefix_cache_enabled=True,
            chunked_prefill_enabled=False,
            request_timeout_seconds=30,
            port=9000,
        )
        command = build_vllm_command(profile)
        self.assertIn("example/model", command)
        self.assertIn("VLLM_TARGET_DEVICE=cpu", command)
        self.assertIn("VLLM_CPU_KVCACHE_SPACE=40", command)
        self.assertIn("--max-num-batched-tokens 8192", command)
        self.assertIn("--enable-prefix-caching", command)
        self.assertNotIn("--enable-chunked-prefill", command)
        self.assertIn("--port 9000", command)

    def test_start_script_checks_vllm(self):
        profile = VllmProfile(
            model_ref="example/model",
            dtype="bfloat16",
            max_model_len=2048,
            max_num_batched_tokens=None,
            max_num_seqs=4,
            cpu_kvcache_space_gib=8,
            reserved_cpu=1,
            prefix_cache_enabled=False,
            chunked_prefill_enabled=False,
            request_timeout_seconds=10,
            port=8001,
        )
        script = build_start_script(profile)
        self.assertIn("$HOME/.aig-vllm/bin", script)
        self.assertIn("exec env", script)
        self.assertIn("command -v vllm", script)
        self.assertIn("ss -ltn", script)
        self.assertIn("nohup", script)
        self.assertIn("vllm-8001.log", script)

    def test_profile_text_rewrites_selected_key_file(self):
        text = """[DEFAULT]
user=ocid1.user.oc1..example
fingerprint=aa:bb
tenancy=ocid1.tenancy.oc1..example
region=us-ashburn-1
key_file=/Users/example/.oci/key.pem

[OTHER]
key_file=/keep/other.pem
region=us-phoenix-1
"""
        rewritten = profile_text_with_key_file(text, "DEFAULT", Path("/tmp/uploaded.pem"))
        self.assertIn("key_file=/tmp/uploaded.pem", rewritten)
        self.assertNotIn("key_file=/Users/example/.oci/key.pem", rewritten)
        self.assertIn("key_file=/keep/other.pem", rewritten)

    def test_secret_box_encrypts_and_decrypts(self):
        box = SecretBox("a-secret-value-that-is-long-enough")
        encrypted = box.encrypt("private-key")
        self.assertNotEqual(encrypted, "private-key")
        self.assertEqual(box.decrypt(encrypted), "private-key")

    def test_password_hash_round_trip(self):
        stored = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", stored))
        self.assertFalse(verify_password("wrong", stored))

    def test_database_scopes_profiles_by_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "app.db")
            db.init()
            one = db.execute("INSERT INTO users(email, password_hash) VALUES (?, ?)", ("a@example.com", "x")).lastrowid
            two = db.execute("INSERT INTO users(email, password_hash) VALUES (?, ?)", ("b@example.com", "x")).lastrowid
            db.execute(
                "INSERT INTO vm_profiles(owner_user_id, name, host, port, username, encrypted_private_key) VALUES (?, ?, ?, ?, ?, ?)",
                (one, "vm", "host", 22, "ubuntu", "secret"),
            )
            self.assertEqual(len(db.all("SELECT * FROM vm_profiles WHERE owner_user_id=?", (one,))), 1)
            self.assertEqual(len(db.all("SELECT * FROM vm_profiles WHERE owner_user_id=?", (two,))), 0)

    def test_guidellm_command_contains_no_custom_runner_or_model_fallback(self):
        profile = GuideLLMBenchmarkSpec(
            model_ref="provided/model",
            port=8000,
            workload_name="Smoke",
            input_tokens=16,
            output_tokens=8,
            concurrencies=[1],
            max_requests=2,
            request_timeout_seconds=30,
        )
        command = remote_guidellm_command(profile, 123)
        self.assertIn("provided/model", command)
        self.assertNotIn("Qwen/Qwen3", command)
        self.assertNotIn("urllib.request", command)
        encoded = command.split("base64.b64decode('", 1)[1].split("')", 1)[0]
        script = base64.b64decode(encoded).decode("utf-8")
        self.assertIn("guidellm", script)
        self.assertIn("--output", script)
        self.assertNotIn("ThreadPoolExecutor", script)

    def test_report_formats_missing_metrics_as_nc(self):
        result = {
            "workload_name": "Smoke",
            "concurrency": 1,
            "requests": 1,
            "success_requests": 0,
            "failed_requests": 1,
        }
        self.assertIn("N/C", report_markdown(result))
        self.assertIn("N/C", report_csv(result))

    def test_report_formats_guidellm_normalized_rows(self):
        result = {
            "benchmark_command_kind": "guidellm",
            "normalized_rows": [
                {"workload_name": "Smoke", "profile": "concurrent", "concurrency": 1, "requests": 2, "success_requests": 2},
                {"workload_name": "Smoke", "profile": "concurrent", "concurrency": 2, "requests": 2, "success_requests": 2},
            ],
        }
        markdown = report_markdown(result)
        csv = report_csv(result)
        self.assertIn("| Smoke | concurrent | 1 | 2 | 2 |", markdown)
        self.assertIn("| Smoke | concurrent | 2 | 2 | 2 |", markdown)
        self.assertEqual(len(csv.strip().splitlines()), 3)

    def test_oci_shapes_are_limited_to_initial_guide(self):
        self.assertEqual(
            set(DOC_APPROVED_SHAPES),
            {"VM.Standard.E6.Ax.Flex", "VM.Standard4.Ax.Flex", "BM.Standard.E6.Ax", "BM.Standard4.Ax"},
        )
        self.assertEqual(DEFAULT_VM_DEPLOYMENT["ocpus"], 16.0)
        self.assertEqual(DEFAULT_VM_DEPLOYMENT["memory_gb"], 64.0)
        self.assertEqual(DEFAULT_VM_DEPLOYMENT["boot_volume_gb"], 200)
        self.assertEqual(DEFAULT_VM_DEPLOYMENT["operating_system_version"], "24.04")

    def test_shape_config_only_for_flex_shapes_without_oci_import(self):
        self.assertIsNone(shape_config_details("BM.Standard4.Ax", 16, 64))


if __name__ == "__main__":
    unittest.main()
