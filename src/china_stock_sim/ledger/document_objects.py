"""Versioned storage for original business-document bytes, never a new logical identity."""

import hashlib
import json
import os
import tempfile

from china_stock_sim.common import evidence_objects
from china_stock_sim.config import local_path
from china_stock_sim.ledger.kernel import LedgerError, digest

FORMAT = 'cas-json-v1'
_FIELDS = {'storage_format', 'logical_content_hash', 'object_ref'}


def file_identity(path):
    value = path.stat()
    if not path.is_file():
        raise ValueError('业务证据不是常规文件')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def document_layout(root, relative, expected):
    """Inspect a small wrapper without inflating its original bytes."""
    path = local_path(root, relative)
    identity = file_identity(path)
    wrapper = None
    raw = None
    if identity[2] <= 8192:
        raw = path.read_bytes()
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        if isinstance(value, dict) and 'storage_format' in value:
            if (set(value) != _FIELDS or value.get('storage_format') != FORMAT
                    or value.get('logical_content_hash') != expected
                    or not evidence_objects.is_reference(value.get('object_ref'))):
                raise LedgerError('INTEGRITY_ERROR', '业务证据存储封装无效', path=relative)
            wrapper = value
    dependencies = []
    if wrapper:
        metadata = wrapper['object_ref']['_evidence_ref']
        stored = local_path(root, metadata['path'])
        # The CAS reader also rejects symlinks; check before trusting a cached body.
        current = root
        for part in metadata['path'].split('/'):
            current = current / part
            if current.is_symlink():
                raise LedgerError('INTEGRITY_ERROR', '业务CAS依赖路径不可含符号链接')
        dependency = file_identity(stored)
        if dependency[2] != metadata['stored_bytes']:
            raise LedgerError('INTEGRITY_ERROR', '业务CAS依赖大小不符')
        dependencies.append({'path': metadata['path'], 'sha256': metadata['stored_sha256'],
                             'file_identity': dependency, 'kind': 'evidence_object'})
    if file_identity(path) != identity:
        raise LedgerError('INTEGRITY_ERROR', '业务证据读取期间变化')
    return {'path': relative, 'file_identity': identity, 'wrapper': wrapper,
            'storage_dependencies': dependencies,
            'physical_sha256': hashlib.sha256(raw).hexdigest() if wrapper else None}


def read_document_bytes(root, layout, *, max_bytes):
    """Resolve exact original bytes with the CAS stream's stored/raw hash checks."""
    wrapper = layout['wrapper']
    if wrapper:
        raw = b''.join(evidence_objects.iter_bytes(root, wrapper['object_ref'], max_bytes=max_bytes))
    else:
        if layout['file_identity'][2] > max_bytes:
            raise LedgerError('INTEGRITY_ERROR', '业务原件超出有界核验大小，须显式迁移')
        raw = local_path(root, layout['path']).read_bytes()
    if file_identity(local_path(root, layout['path'])) != layout['file_identity']:
        raise LedgerError('INTEGRITY_ERROR', '业务证据读取期间变化')
    for dependency in layout['storage_dependencies']:
        if file_identity(local_path(root, dependency['path'])) != dependency['file_identity']:
            raise LedgerError('INTEGRITY_ERROR', '业务CAS依赖读取期间变化')
    return raw


def archive_document(root, reference):
    """Explicit maintenance operation: preserve original bytes, atomically publish a wrapper.

    Never called by an ordinary reader. The caller owns the maintenance lock and
    rollback inventory; an interrupted pre-publication leaves only a safe orphan.
    """
    from china_stock_sim.ledger.decision_evidence import read_business_document

    _, verified = read_business_document(root, reference)
    layout = document_layout(root, reference['path'], reference['content_hash'])
    if layout['wrapper']:
        return {'state': 'UNCHANGED', 'document': dict(reference),
                'object_ref': layout['wrapper']['object_ref'],
                'original_sha256': layout['wrapper']['object_ref']['_evidence_ref']['sha256']}
    path = local_path(root, reference['path'])
    with path.open('rb') as stream:
        archived = evidence_objects.put_blob(root, stream)
    if (file_identity(path) != layout['file_identity']
            or archived['_evidence_ref']['sha256'] != verified['sha256']):
        raise LedgerError('INTEGRITY_ERROR', '归档期间业务原件变化')
    wrapper = {'storage_format': FORMAT, 'logical_content_hash': reference['content_hash'],
               'object_ref': archived}
    raw = (json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    descriptor, temporary = tempfile.mkstemp(prefix='.document-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if file_identity(path) != layout['file_identity']:
            raise LedgerError('INTEGRITY_ERROR', '发布封装前业务原件变化')
        os.replace(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'state': 'APPLIED', 'document': dict(reference), 'object_ref': archived,
            'original_sha256': archived['_evidence_ref']['sha256'],
            'wrapper_sha256': hashlib.sha256(raw).hexdigest(), 'wrapper_hash': digest(wrapper)}
