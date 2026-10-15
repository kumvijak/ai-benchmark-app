"""Dedicated durable reporting queue; runnable separately with python -m app.report_worker."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
import uuid

from .db import Database
from .reporting import LEASE_SECONDS, MAX_ATTEMPTS, build_bundle

log = logging.getLogger(__name__)


class ReportWorker:
    def __init__(self, db: Database, artifact_dir: Path):
        self.db = db
        self.root = Path(artifact_dir)/'reports'
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.run, name='report-worker', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def claim(self):
        # A transaction fences competing processes, including expired-lease recovery.
        con = self.db.connect()
        with self.db._lock:
            con.execute('BEGIN IMMEDIATE')
            try:
                now = time.time()
                con.execute("UPDATE report_jobs SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END, stage='interrupted', error_message='Report lease expired; retry limit applies', lease_token=NULL, lease_until=NULL WHERE status='running' AND lease_until<?", (MAX_ATTEMPTS, now))
                row = con.execute("SELECT * FROM report_jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    con.commit()
                    return None
                token = uuid.uuid4().hex
                con.execute("UPDATE report_jobs SET status='running',stage='starting',progress=1,attempts=attempts+1,lease_token=?,lease_until=?,error_message=NULL,finished_at=NULL WHERE id=?", (token, now+LEASE_SECONDS, row['id']))
                con.commit()
                return dict(row) | {'lease_token': token}
            except Exception:
                con.rollback()
                raise

    def heartbeat(self, job):
        self.db.execute("UPDATE report_jobs SET lease_until=? WHERE id=? AND status='running' AND lease_token=?",
                        (time.time()+LEASE_SECONDS, job['id'], job['lease_token']))

    def process(self, job):
        heartbeat_stop = threading.Event()
        def maintain_lease():
            try:
                while not heartbeat_stop.wait(10):
                    self.heartbeat(job)
            finally:
                self.db.connect().close()
        keeper = threading.Thread(target=maintain_lease, daemon=True)
        keeper.start()
        def progress(stage, percent):
            updated = self.db.execute("UPDATE report_jobs SET stage=?,progress=? WHERE id=? AND status='running' AND lease_token=?",
                                     (stage,percent,job['id'],job['lease_token']))
            if updated.rowcount != 1:
                raise RuntimeError('Report lease lost')
        try:
            # Unique attempt directories prevent stale workers overwriting published output.
            destination = self.root/str(job['owner_user_id'])/str(job['id'])/job['lease_token']
            summary = build_bundle(json.loads(job['snapshot']), job['id'], destination, progress)
            self.db.execute("UPDATE report_jobs SET status='succeeded',stage='ready',progress=100,summary=?,artifact_token=?,lease_until=NULL,lease_token=NULL,finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running' AND lease_token=?",
                            (json.dumps(summary),job['lease_token'],job['id'],job['lease_token']))
        except Exception as exc:
            log.exception('Report %s failed', job['id'])
            self.db.execute("UPDATE report_jobs SET status='failed',stage='failed',error_message=?,lease_until=NULL,lease_token=NULL,finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running' AND lease_token=?",
                            (str(exc)[-1000:],job['id'],job['lease_token']))
        finally:
            heartbeat_stop.set()
            keeper.join(timeout=2)

    def run(self):
        try:
            while not self._stop.is_set():
                try:
                    job = self.claim()
                    if job:
                        self.process(job)
                    else:
                        self._stop.wait(1)
                except Exception:
                    log.exception('Reporting queue polling failed')
                    self._stop.wait(3)
        finally:
            self.db.connect().close()


def main():
    import signal
    import os
    db_path = Path(os.getenv('APP_DB_PATH', '/data/app.db'))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path); db.init()
    worker = ReportWorker(db, Path(os.getenv('APP_ARTIFACT_DIR', '/data/artifacts')))
    signal.signal(signal.SIGTERM, lambda *_: worker._stop.set())
    signal.signal(signal.SIGINT, lambda *_: worker._stop.set())
    worker.run()


if __name__ == '__main__':
    main()
