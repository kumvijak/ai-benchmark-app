"""Verified report copies in private OCI Object Storage; no app URLs in download links."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid
import zipfile

FILES = ('report.zip', 'metrics.csv', 'report.json', 'manifest.json')


def enabled():
    return bool(os.getenv('APP_REPORT_ARCHIVE_BUCKET'))


def client():
    import oci
    from .oci_ops import principal_signer
    region = os.getenv('APP_REPORT_ARCHIVE_REGION', 'us-ashburn-1')
    if os.getenv('APP_REPORT_ARCHIVE_AUTH', 'principal') == 'config':
        config = oci.config.from_file(profile_name=os.getenv('OCI_CLI_PROFILE', 'DEFAULT'))
        config['region'] = region
        return oci.object_storage.ObjectStorageClient(config, timeout=(10, 120))
    return oci.object_storage.ObjectStorageClient({'region': region}, signer=principal_signer(), timeout=(10, 120))


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive_report(root: Path, job, storage=None):
    storage = storage or client()
    namespace = os.getenv('APP_REPORT_ARCHIVE_NAMESPACE') or storage.get_namespace().data
    bucket = os.environ['APP_REPORT_ARCHIVE_BUCKET']
    if storage.get_bucket(namespace, bucket).data.public_access_type != 'NoPublicAccess':
        raise RuntimeError('Report archive bucket must be private')
    manifest = json.loads((root/'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = (root/name).resolve()
        if not path.is_relative_to(root.resolve()) or sha256(path) != expected:
            raise RuntimeError('Report manifest verification failed')
    with zipfile.ZipFile(root/'report.zip') as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError('Report ZIP verification failed')
        for name in (*manifest['files'], 'manifest.json'):
            expected = sha256(root/name)
            if hashlib.sha256(bundle.read(name)).hexdigest() != expected:
                raise RuntimeError('Report ZIP contents do not match manifest')
    prefix = f"reports/{job['owner_user_id']}/{job['id']}/{job['artifact_token']}"
    hashes = {}
    for name in FILES:
        path = root/name
        checksum = sha256(path)
        with path.open('rb') as stream:
            storage.put_object(namespace, bucket, f'{prefix}/{name}', stream,
                content_length=path.stat().st_size,
                content_type={'report.zip':'application/zip','metrics.csv':'text/csv'}.get(name, 'application/json'),
                content_disposition=f'attachment; filename="{name}"',
                opc_checksum_algorithm='SHA256',
                opc_content_sha256=base64.b64encode(bytes.fromhex(checksum)).decode())
        # Read back actual bytes: metadata alone cannot establish integrity.
        response = storage.get_object(namespace, bucket, f'{prefix}/{name}')
        try:
            digest = hashlib.sha256()
            for chunk in response.data.raw.stream(1024*1024, decode_content=False):
                digest.update(chunk)
            if digest.hexdigest() != checksum:
                raise RuntimeError('Archived object checksum mismatch')
        finally:
            response.data.close()
        hashes[name] = checksum
    return dict(namespace=namespace, bucket=bucket, region=os.getenv('APP_REPORT_ARCHIVE_REGION', 'us-ashburn-1'),
                prefix=prefix, sha256=hashes, verified_at=datetime.now(timezone.utc).isoformat())


def download_link(archive, storage=None):
    import oci
    storage = storage or client()
    # Use the archive's region, even if the current deployment default changed.
    storage.base_client.set_region(archive['region'])
    hours = int(os.getenv('APP_REPORT_ARCHIVE_LINK_HOURS', '168'))
    if not 1 <= hours <= 8760:
        raise ValueError('Archive link lifetime must be between 1 and 8760 hours')
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    result = storage.create_preauthenticated_request(archive['namespace'], archive['bucket'],
        oci.object_storage.models.CreatePreauthenticatedRequestDetails(
            name=f'report-download-{uuid.uuid4().hex}', access_type='ObjectRead',
            object_name=f"{archive['prefix']}/report.zip", time_expires=expires)).data
    return dict(url=storage.base_client.endpoint.rstrip('/') + result.access_uri,
                expires_at=expires.isoformat())
