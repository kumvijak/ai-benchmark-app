"""Archive integrity and download scope without cloud credentials."""
import hashlib
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from app.report_archive import FILES, archive_report, download_link
from app.tests.test_reporting import ReportTestCase

class ArchiveTests(ReportTestCase):
    def bundle(self):
        rid = self.queue([self.job()]); self.worker.process(self.worker.claim())
        row = self.db.one('SELECT * FROM report_jobs WHERE id=?', (rid,))
        return row, self.worker.root/'1'/str(rid)/row['artifact_token']

    def storage(self, corrupt=False):
        objects = {}; client = Mock()
        client.get_namespace.return_value.data = 'namespace'
        client.get_bucket.return_value.data.public_access_type = 'NoPublicAccess'
        def put(ns,bucket,name,stream,**kwargs):
            objects[name] = stream.read()
            self.assertEqual(hashlib.sha256(objects[name]).digest(), __import__('base64').b64decode(kwargs['opc_content_sha256']))
        def get(ns,bucket,name):
            body = b'corrupt' if corrupt else objects[name]
            return NS(data=NS(raw=NS(stream=lambda *a,**kw: iter([body])),close=lambda: None))
        client.put_object.side_effect = put; client.get_object.side_effect = get
        return client, objects

    @patch.dict(os.environ, {'APP_REPORT_ARCHIVE_BUCKET':'benchmark'})
    def test_upload_and_readback_all_files(self):
        job, root = self.bundle(); client, objects = self.storage()
        metadata = archive_report(root,job,client)
        self.assertEqual(len(objects),4); self.assertEqual(set(metadata['sha256']),set(FILES))
        self.assertEqual(metadata['sha256']['report.zip'],hashlib.sha256((root/'report.zip').read_bytes()).hexdigest())

    @patch.dict(os.environ, {'APP_REPORT_ARCHIVE_BUCKET':'benchmark'})
    def test_corrupt_readback_prevents_ready(self):
        job, root = self.bundle(); client, _ = self.storage(corrupt=True)
        with self.assertRaisesRegex(RuntimeError,'checksum mismatch'): archive_report(root,job,client)

    @patch.dict(os.environ, {'APP_REPORT_ARCHIVE_BUCKET':'benchmark'})
    def test_reject_public_bucket_and_modified_bundle(self):
        job, root = self.bundle(); client, _ = self.storage()
        client.get_bucket.return_value.data.public_access_type = 'ObjectRead'
        with self.assertRaisesRegex(RuntimeError,'private'): archive_report(root,job,client)
        client.get_bucket.return_value.data.public_access_type = 'NoPublicAccess'
        (root/'metrics.csv').write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'manifest'): archive_report(root,job,client)
        client.put_object.assert_not_called()

    @patch.dict(os.environ, {'APP_REPORT_ARCHIVE_BUCKET':'benchmark'})
    def test_worker_backfills_and_failure_preserves_local_report(self):
        job, _ = self.bundle()
        with patch('app.report_archive.archive_report',side_effect=RuntimeError('sensitive SDK data')): self.assertTrue(self.worker.archive_one())
        row = self.db.one('SELECT * FROM report_jobs WHERE id=?',(job['id'],))
        self.assertEqual(row['status'],'succeeded'); self.assertEqual(row['archive_status'],'pending')
        self.assertNotIn('sensitive',row['archive_error'])
        self.db.execute('UPDATE report_jobs SET archive_lease_until=NULL WHERE id=?',(job['id'],))
        with patch('app.report_archive.archive_report',return_value={'bucket':'benchmark'}): self.worker.archive_one()
        self.assertEqual(self.db.one('SELECT archive_status FROM report_jobs WHERE id=?',(job['id'],))['archive_status'],'ready')

    @patch.dict(os.environ, {'APP_REPORT_ARCHIVE_BUCKET':'benchmark'})
    def test_expired_archive_lease_reclaimed_and_stale_worker_fenced(self):
        job, _ = self.bundle()
        def expire_and_reclaim(root, current):
            self.db.execute("UPDATE report_jobs SET archive_token='replacement',archive_status='ready',archive_metadata='{}' WHERE id=?",(job['id'],))
            return {'stale':True}
        self.db.execute("UPDATE report_jobs SET archive_status='uploading',archive_token='old',archive_lease_until=0,archive_attempts=1 WHERE id=?",(job['id'],))
        with patch('app.report_archive.archive_report',side_effect=expire_and_reclaim): self.worker.archive_one()
        self.assertEqual(self.db.one('SELECT archive_metadata FROM report_jobs WHERE id=?',(job['id'],))['archive_metadata'],'{}')

    def test_link_is_single_object_read_only_and_expiring(self):
        fake_oci = NS(object_storage=NS(models=NS(CreatePreauthenticatedRequestDetails=lambda **kw:NS(**kw))))
        storage = Mock(); storage.base_client.endpoint = 'https://objectstorage.us-ashburn-1.oraclecloud.com'
        storage.create_preauthenticated_request.return_value.data.access_uri='/p/secret/n/ns/b/benchmark/o/report.zip'
        with patch.dict('sys.modules',{'oci':fake_oci}), patch.dict(os.environ,{'APP_REPORT_ARCHIVE_LINK_HOURS':'24'}):
            result=download_link({'namespace':'ns','bucket':'benchmark','prefix':'reports/uuid','region':'us-ashburn-1'},storage)
        details=storage.create_preauthenticated_request.call_args.args[2]
        self.assertEqual(details.access_type,'ObjectRead'); self.assertEqual(details.object_name,'reports/uuid/report.zip')
        self.assertTrue(result['url'].startswith('https://objectstorage.')); self.assertIn('expires_at',result)

if __name__ == '__main__': unittest.main()
