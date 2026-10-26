"""Exercise real OCI models with simulated service responses; never launch VMs."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

import oci

from app.db import Database, SCHEMA
from app.oci_ops import instance_smt_enabled, smt_platform_config
from app.security import SecretBox
from app.worker import Worker


def shape_response(shape, platform_type, allowed):
    return oci.core.models.Shape(
        shape=shape,
        platform_config_options=oci.core.models.ShapePlatformConfigOptions(
            type=platform_type,
            symmetric_multi_threading_options=oci.core.models.ShapeSymmetricMultiThreadingEnabledPlatformOptions(
                allowed_values=allowed,
            ),
        ),
    )


class SmtProvisioningTests(unittest.TestCase):
    def test_vendor_and_bare_metal_configs_serialize_false_and_true(self):
        for shape, platform_type in [
            ('VM.Standard.E6.Ax.Flex', 'AMD_VM'),
            ('VM.Standard4.Ax.Flex', 'INTEL_VM'),
            ('BM.Standard.E6.Ax', 'GENERIC_BM'),
            ('BM.Standard4.Ax', 'GENERIC_BM'),
        ]:
            for enabled in (False, True):
                with self.subTest(shape=shape, enabled=enabled):
                    compute = MagicMock()
                    with patch('oci.pagination.list_call_get_all_results', return_value=NS(
                        data=[shape_response(shape, platform_type, [True, False])],
                    )) as listing:
                        config = smt_platform_config(compute, 'comp', 'ad', 'image', shape, enabled)
                    serializer = object.__new__(oci.BaseClient)
                    serializer.complex_type_mappings = oci.core.models.core_type_mapping
                    wire = serializer.sanitize_for_serialization(config)
                    self.assertEqual(wire['type'], platform_type)
                    self.assertIs(wire['isSymmetricMultiThreadingEnabled'], enabled)
                    listing.assert_called_once_with(compute.list_shapes, 'comp', availability_domain='ad', image_id='image')

    def test_unknown_or_unsupported_capabilities_do_not_fall_back(self):
        for response in [[], [NS(shape='shape', platform_config_options=None)],
                         [shape_response('shape', 'AMD_VM', [True])],
                         [shape_response('shape', 'UNKNOWN_ENUM_VALUE', [False])]]:
            with self.subTest(response=response), patch('oci.pagination.list_call_get_all_results', return_value=NS(data=response)):
                with self.assertRaises(RuntimeError):
                    smt_platform_config(MagicMock(), 'comp', 'ad', 'image', 'shape', False)
        with self.assertRaises(ValueError):
            smt_platform_config(MagicMock(), 'comp', 'ad', 'image', 'shape', 'false')
        compute = MagicMock()
        self.assertIsNone(smt_platform_config(compute, 'comp', 'ad', 'image', 'shape', None))
        compute.list_shapes.assert_not_called()
        self.assertIsNone(instance_smt_enabled(NS(platform_config=None)))
        self.assertIs(instance_smt_enabled(NS(platform_config=NS(is_symmetric_multi_threading_enabled=False))), False)

    def test_legacy_database_adds_nullable_columns_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.db'
            connection = sqlite3.connect(path)
            connection.executescript(SCHEMA.replace('  smt_requested INTEGER,\n', '').replace('  smt_enabled INTEGER,\n', ''))
            connection.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'test@example.com','unused')")
            connection.execute("""INSERT INTO oci_compute_instances(owner_user_id,display_name,compartment_id,availability_domain,
                subnet_id,shape,ocpus,memory_gb,boot_volume_gb,image_id,assign_public_ip)
                VALUES(1,'old','comp','ad','subnet','shape',16,64,200,'image',1)""")
            connection.commit()
            connection.close()
            db = Database(path)
            try:
                db.init()
                db.init()
                self.assertEqual(db.one('SELECT display_name,smt_requested,smt_enabled FROM oci_compute_instances'),
                                 {'display_name': 'old', 'smt_requested': None, 'smt_enabled': None})
            finally:
                db.connect().close()

    def test_api_queues_explicit_false_and_rejects_ambiguous_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, APP_SECRET='smt-test-secret-long-enough-123456', APP_LOCAL_USER_EMAIL='smt@test',
                            APP_DB_PATH=str(Path(tmp) / 'bootstrap.db'), APP_ARTIFACT_DIR=str(Path(tmp) / 'artifacts')):
                api = importlib.import_module('app.main')
            db = Database(Path(tmp) / 'test.db')
            db.init()
            db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'smt@test','unused')")
            db.execute("INSERT INTO sessions(token,user_id) VALUES('session',1)")
            payload = self.payload()
            payload.pop('_job_id')
            from fastapi.testclient import TestClient
            try:
                with patch.object(api, 'db', db):
                    client = TestClient(api.app)
                    try:
                        response = client.post('/api/oci/instances', json=payload, headers={'Authorization': 'Bearer session'})
                        self.assertEqual(response.status_code, 200, response.text)
                        job = db.one('SELECT payload FROM jobs WHERE id=?', (response.json()['job_id'],))
                        self.assertIs(json.loads(job['payload'])['smt_enabled'], False)
                        for invalid in ['false', 'off', 0, 1]:
                            response = client.post('/api/oci/instances', json={**payload, 'smt_enabled': invalid},
                                                   headers={'Authorization': 'Bearer session'})
                            self.assertEqual(response.status_code, 422, response.text)
                        payload.pop('smt_enabled')
                        self.assertIsNone(api.OciVmRequest(**payload).smt_enabled)
                    finally:
                        client.close()
            finally:
                db.connect().close()

    @staticmethod
    def payload():
        return dict(_job_id=1, display_name='smt-test', compartment_id='comp', availability_domain='ad',
                    subnet_id='subnet', image_id='image', shape='VM.Standard.E6.Ax.Flex', ocpus=16,
                    memory_gb=64, boot_volume_gb=200, ssh_public_key='ssh-ed25519 fixture', smt_enabled=False)

    def test_worker_verifies_readback_and_records_mismatches(self):
        for reported in (False, True, None):
            with self.subTest(reported=reported), tempfile.TemporaryDirectory() as tmp:
                db = Database(Path(tmp) / 'app.db')
                db.init()
                self.addCleanup(db.connect().close)
                db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'smt@test','unused')")
                db.execute("INSERT INTO oci_auth_profiles(id,owner_user_id,mode) VALUES(1,1,'instance_principal')")
                worker = Worker(db, SecretBox('smt-test-secret-long-enough-123456'))
                ctx = MagicMock()
                compute = MagicMock()
                compute.launch_instance.return_value.data = NS(id='instance', lifecycle_state='PROVISIONING')
                running = NS(lifecycle_state='RUNNING', platform_config=NS(is_symmetric_multi_threading_enabled=reported))
                config = oci.core.models.AmdVmLaunchInstancePlatformConfig(is_symmetric_multi_threading_enabled=False)
                with patch.object(worker, '_auth_context', return_value=({'id': 1}, ctx)), \
                     patch('app.worker._oci_clients', return_value={'compute': compute, 'network': MagicMock(), 'block': MagicMock()}), \
                     patch('app.worker.smt_platform_config', return_value=config), \
                     patch('oci.wait_until', return_value=NS(data=running)), \
                     patch('app.worker._wait_for_primary_vnic', return_value=NS(public_ip='192.0.2.1', private_ip='10.0.0.1')), \
                     patch('app.worker._first_boot_volume_id', return_value='boot'):
                    if reported is False:
                        result = worker._provision_vm(1, self.payload())
                        self.assertIs(result['smt_enabled'], False)
                        self.assertIs(result['smt_requested'], False)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Instance instance is running.*SMT verification failed'):
                            worker._provision_vm(1, self.payload())
                launch = compute.launch_instance.call_args.args[0]
                self.assertIs(launch.platform_config.is_symmetric_multi_threading_enabled, False)
                self.assertEqual(launch.shape_config.ocpus, 16)
                row = db.one('SELECT smt_requested,smt_enabled,lifecycle_state FROM oci_compute_instances')
                self.assertEqual(row, {'smt_requested': 0, 'smt_enabled': reported, 'lifecycle_state': 'RUNNING'})
                compute.terminate_instance.assert_not_called()


if __name__ == '__main__':
    unittest.main()
