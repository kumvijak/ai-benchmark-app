"""Local HTTP integration checks; no OCI calls or remote model execution."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from app.db import Database


class ContainerSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "app.db"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(APP_SECRET=secrets.token_urlsafe(32),
                   APP_LOCAL_USER_EMAIL="operator@container.test",
                   APP_DB_PATH=str(cls.db_path),
                   APP_ARTIFACT_DIR=str(Path(cls.tmp.name) / "artifacts"))
        cls.log = open(Path(cls.tmp.name) / "server.log", "w+")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=cls.log, stderr=cls.log,
        )
        cls.addClassCleanup(cls.cleanup)
        for _ in range(100):
            try:
                cls.call("GET", "/api/health")
                break
            except (OSError, urllib.error.URLError):
                if cls.server.poll() is not None:
                    cls.log.seek(0)
                    raise RuntimeError(cls.log.read())
                time.sleep(0.1)
        else:
            raise RuntimeError("Application did not become ready")
        _, local = cls.call("POST", "/api/local-session", {})
        cls.token = local["token"]
        cls.user_id = local["user"]["id"]
        _, other = cls.call("POST", "/api/register", {
            "email": "other@container.test", "password": "test-only-password"})
        cls.other_token = other["token"]

    @classmethod
    def cleanup(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)
        cls.log.close()
        cls.tmp.cleanup()

    @classmethod
    def call(cls, method, path, data=None, token=None):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(cls.url + path, body, headers, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read().decode()
            content = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, content

    def test_health_ui_and_auth(self):
        self.assertEqual(self.call("GET", "/api/health"), (200, {"status": "ok"}))
        for path, expected in (("/", "VM Connection"), ("/static/app.js", "workloadProfiles"), ("/static/styles.css", ".tabs")):
            status, body = self.call("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", token=self.token)[1]["email"], "operator@container.test")
        self.assertEqual(self.call("POST", "/api/login", {
            "email": "other@container.test", "password": "test-only-password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {
            "email": "other@container.test", "password": "wrong"})[0], 401)

    def test_profiles_and_ownership(self):
        _, vm = self.call("POST", "/api/vm-profiles", {
            "name": "test-vm", "host": "127.0.0.1", "username": "test",
            "private_key": "test-only-placeholder-not-a-key"}, self.token)
        _, runtime = self.call("POST", "/api/vllm-profiles", {
            "name": "test-runtime", "model_ref": "test/fixture"}, self.token)
        for route, item in (("vm-profiles", vm), ("vllm-profiles", runtime)):
            self.assertTrue(self.call("GET", "/api/" + route, token=self.token)[1])
            self.assertEqual(self.call("GET", "/api/" + route, token=self.other_token)[1], [])
            self.assertEqual(self.call("DELETE", f"/api/{route}/{item['id']}", token=self.other_token)[0], 404)
            self.assertEqual(self.call("DELETE", f"/api/{route}/{item['id']}", token=self.token)[0], 200)

    def test_queue_processes_failure_without_remote_access(self):
        db = Database(self.db_path)
        job_id = db.create_job(self.user_id, "test_unsupported_job", {})
        for _ in range(50):
            _, job = self.call("GET", f"/api/jobs/{job_id}", token=self.token)
            if job["status"] == "failed":
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Unknown job type", job["error_message"])
        self.assertEqual(self.call("GET", f"/api/jobs/{job_id}", token=self.other_token)[0], 404)
        self.assertEqual(self.call("GET", f"/api/reports/{job_id}.json", token=self.token)[0], 404)
        failed = db.execute("INSERT INTO jobs(owner_user_id,job_type,status,payload) VALUES (?, 'run_benchmark', 'failed', '{}')", (self.user_id,))
        self.assertEqual(self.call("GET", f"/api/reports/{failed.lastrowid}.json", token=self.token)[0], 409)
        self.assertEqual(self.call("DELETE", f"/api/jobs/{job_id}", token=self.token)[0], 200)

    def test_report_routes_with_explicit_test_fixture(self):
        # Exercises storage and export routes, not benchmark execution or model performance.
        db = Database(self.db_path)
        fixture = {"normalized_rows": [{"workload_name": "TEST FIXTURE", "requests": 2}],
                   "artifacts": {"csv": {"content": "workload,requests\nTEST FIXTURE,2\n"},
                                 "html": {"content": "<p>TEST FIXTURE</p>"}}}
        cur = db.execute("INSERT INTO jobs(owner_user_id,job_type,status,payload,result) VALUES (?, 'run_benchmark', 'succeeded', '{}', ?)",
                         (self.user_id, json.dumps(fixture)))
        for fmt in ("json", "csv", "html", "md"):
            status, body = self.call("GET", f"/api/reports/{cur.lastrowid}.{fmt}", token=self.token)
            self.assertEqual(status, 200)
            self.assertIn("TEST FIXTURE", str(body))
            self.assertEqual(self.call("GET", f"/api/reports/{cur.lastrowid}.{fmt}", token=self.other_token)[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
