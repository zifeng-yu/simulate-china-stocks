"""Local decision documents and execution binding; no strategy implementation imports."""

import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from china_stock_sim.common.decisions import (
    normalize_decision_ref,
    validate_decision_core,
    validate_provenance,
)
from china_stock_sim.common.fees import ExecutionSupportError, number
from china_stock_sim.common.sizing import intent_quantity
from china_stock_sim.config import local_path
from china_stock_sim.ledger.document_objects import document_layout, read_document_bytes
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest, reject_credentials
from china_stock_sim.market.contracts import aware_time
from china_stock_sim.market.quality import normalize_code

_SCOPE = ContextVar('ledger_verification_scope', default=None)
_DOCUMENTS = OrderedDict()
_VALIDATIONS = OrderedDict()
_DOCUMENT_STAMPS = OrderedDict()
_DOCUMENT_BYTES = 0
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 128 * 1024 * 1024
_RULE_CACHE = None
_REBIND_KINDS = frozenset({'frozen_plan_header', 'strategy_decision', 'strategy_index_binding',
    'plan_decision', 'public_plan_summary', 'opaque_evidence_object', 'backup_archive_layout_v2'})


@contextmanager
def verification_scope(root, *, persist=False):
    """Reuse immutable checks; only an explicit writing operation may save receipts."""
    previous = _SCOPE.get()
    if (previous is not None and previous['root'] == str(Path(root).resolve())
            and previous['persist'] == persist):
        yield previous['metrics']
        return
    scope = {'root': str(Path(root).resolve()), 'persist': persist,
             'touched': (previous['touched'] if previous is not None
                         and previous['root'] == str(Path(root).resolve()) else OrderedDict()),
             'metrics': previous['metrics'] if previous is not None else {
                 'full_checks': 0, 'cache_hits': 0, 'bytes_read': 0, 'receipts_written': 0}}
    token = _SCOPE.set(scope)
    try:
        yield scope['metrics']
    finally:
        _SCOPE.reset(token)


def _metric(key, amount=1):
    scope = _SCOPE.get()
    if scope is not None:
        scope['metrics'][key] = scope['metrics'].get(key, 0) + amount


def _touch_fixed(root, kind, identity):
    scope = _SCOPE.get()
    if (scope is not None and kind in _REBIND_KINDS
            and scope['root'] == str(Path(root).resolve())
            and identity not in scope['touched'] and len(scope['touched']) < 4096):
        # First-used execution/report proofs survive later bulk opaque checks.
        # This is only a bounded, in-memory candidate list, never validation.
        scope['touched'][identity] = None


def _stamp(path):
    stat = path.stat()
    if not path.is_file():
        raise ValueError('证据不是常规文件')
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def verification_identity():
    """Bind derived receipts to the actual validator implementation and dependencies."""
    global _RULE_CACHE
    package = Path(__file__).resolve().parents[1]
    names = ('ledger/decision_evidence.py', 'ledger/strategy_decisions.py',
             'ledger/kernel.py', 'common/decisions.py', 'common/wire.py',
             'common/fees.py', 'common/sizing.py', 'config.py', 'ledger/statement_inputs.py',
             'market/statement_fingerprint.py', 'market/service.py', 'market/events.py',
             'market/freshness.py', 'market/calendar.py', 'market/quality.py',
             'market/tax_facts.py', 'application/workflow.py', 'ledger/document_objects.py',
             'common/evidence_objects.py', 'ledger/maintenance.py', 'ledger/schema.py',
             'ledger/plan_storage.py', 'common/strategy_transitions.py', 'ledger/account_policies.py',
             'ledger/backup_links.py', 'common/event_accounting_scope.py', 'ledger/statements.py')
    stamps = tuple((name, tuple(_stamp(package / name))) for name in names)
    if _RULE_CACHE is None or _RULE_CACHE[0] != stamps:
        hashes = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in names}
        _RULE_CACHE = stamps, digest({'verification_schema': 1, 'sources': hashes})
    return _RULE_CACHE[1]


def _receipt_path(root, identity):
    return local_path(root, 'data/state/verification/' + identity + '.json')


def _receipt(root, identity):
    """A malformed derived receipt is ignored; it can never turn failure into success."""
    try:
        path = _receipt_path(root, identity)
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or not isinstance(value.get('verification'), dict):
            return None
        body = value['verification']
        if value.get('sha256') != digest(body) or body.get('identity') != identity:
            return None
        if body.get('state') != 'VERIFIED' or body.get('validator') != verification_identity():
            return None
        return body
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _save_receipt(root, identity, **data):
    scope = _SCOPE.get()
    if scope is None or not scope['persist'] or scope['root'] != str(Path(root).resolve()):
        return
    body = {'identity': identity, 'state': 'VERIFIED', 'validator': verification_identity(),
            'recorded_at': datetime.now(timezone.utc).isoformat(), **data}
    raw = (json.dumps({'verification': body, 'sha256': digest(body)},
                      ensure_ascii=False, sort_keys=True) + '\n').encode()
    if len(raw) > 2_000_000:
        return
    path = _receipt_path(root, identity)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.verification-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _metric('receipts_written')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        # A cache failure must not change a completed validation or financial result.
        return


def _document_key(root, relative, expected, stamp):
    return digest({'kind': 'business_document', 'root': str(Path(root).resolve()),
                   'path': relative, 'content_hash': expected,
                   'file_identity': stamp, 'validator': verification_identity()})


def _remember_document(key, body, info, size):
    global _DOCUMENT_BYTES
    if size > _MAX_CACHE_BYTES // 4:
        return
    old = _DOCUMENTS.pop(key, None)
    if old:
        _DOCUMENT_BYTES -= old[2]
    _DOCUMENTS[key] = deepcopy(body), deepcopy(info), size
    _DOCUMENT_BYTES += size
    while len(_DOCUMENTS) > 128 or _DOCUMENT_BYTES > _MAX_CACHE_BYTES:
        _, (_, _, removed) = _DOCUMENTS.popitem(last=False)
        _DOCUMENT_BYTES -= removed


def _validation_key(root, kind, content_hash):
    return digest({'root': str(Path(root).resolve()), 'kind': kind,
                   'content_hash': content_hash, 'validator': verification_identity()})


def cached_validation(root, kind, content_hash):
    """Reuse semantic validation only while every verified dependency is unchanged."""
    from china_stock_sim.ledger.backup_links import owned_regular

    identity = _validation_key(root, kind, content_hash)
    def current(proof):
        if not proof or proof.get('kind') != kind or proof.get('content_hash') != content_hash:
            return False
        dependencies = proof.get('dependencies')
        if not isinstance(dependencies, list) or len(dependencies) > 4096:
            return False
        try:
            return all(_stamp(owned_regular(root, d['path'])) == d['file_identity'] for d in dependencies)
        except (OSError, KeyError, TypeError, ValueError):
            return False
    proof = _VALIDATIONS.get(identity)
    if not current(proof):
        # A separate backup writer may have atomically rebound the durable proof.
        # Reload it once, using every ordinary guard, without writing from a reader.
        proof = _receipt(root, identity)
        if not current(proof):
            return False
    _VALIDATIONS[identity] = proof
    _VALIDATIONS.move_to_end(identity)
    while len(_VALIDATIONS) > 128:
        _VALIDATIONS.popitem(last=False)
    _metric('cache_hits')
    _touch_fixed(root, kind, identity)
    return True


def remember_validation(root, kind, content_hash, files, *, details=None, persist=True):
    dependencies = []
    for info in files:
        if 'file_identity' in info:
            if _stamp(local_path(root, info['path'])) != info['file_identity']:
                raise LedgerError('INTEGRITY_ERROR', '核验过程中业务CAS依赖变化')
            dependencies.append(dict(info))
            continue
        key = (str(Path(root).resolve()), info['path'], info['content_hash'])
        stamp = _DOCUMENT_STAMPS.get(key)
        if stamp is None:
            return  # A bounded metadata eviction only loses this optimization.
        try:
            if _stamp(local_path(root, info['path'])) != stamp:
                raise LedgerError('INTEGRITY_ERROR', '核验过程中业务证据变化', path=info['path'])
        except OSError:
            raise LedgerError('INTEGRITY_ERROR', '核验过程中业务证据缺失', path=info['path']) from None
        dependencies.append({**info, 'file_identity': stamp})
    identity = _validation_key(root, kind, content_hash)
    proof = {'identity': identity, 'kind': kind, 'content_hash': content_hash,
             'state': 'VERIFIED', 'validator': verification_identity(), 'dependencies': dependencies,
             'details': details}
    _VALIDATIONS[identity] = proof
    _VALIDATIONS.move_to_end(identity)
    while len(_VALIDATIONS) > 128:
        _VALIDATIONS.popitem(last=False)
    _touch_fixed(root, kind, identity)
    if persist:
        _save_receipt(root, identity, kind=kind, content_hash=content_hash,
                      dependencies=dependencies, details=details)


def rebind_verified_dependencies(root, transitions, *, max_receipts=4096, max_bytes=64 * 1024 * 1024):
    """Move only exact, writer-verified filesystem identities in fixed-key proofs.

    This optimization is confined to an explicit persistent writer scope. It
    never validates a new document, upgrades a stale validator, or changes a
    proof's semantic details. Every unaffected dependency must still be valid.
    """
    from china_stock_sim.ledger.backup_links import identity as link_identity
    from china_stock_sim.ledger.backup_links import owned_regular

    scope = _SCOPE.get()
    root = Path(root).resolve()
    stats = {'state': 'SKIPPED', 'scanned': 0, 'read_bytes': 0, 'rebound': 0,
             'priority_candidates': 0, 'priority_rebound': 0,
             'memory_rebound': 0, 'invalid': 0, 'write_failures': 0, 'truncated': False}
    if scope is None or not scope['persist'] or scope['root'] != str(root):
        return stats
    approved = {}
    for record in transitions:
        try:
            old, new = record['old'], record['new']
            if (old['stamp'][:4] != new['stamp'][:4] or old['mode'] != new['mode']
                    or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                    or link_identity(owned_regular(root, record['source'])) != new):
                continue
            approved[tuple(old['stamp'])] = record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not approved:
        return stats
    validator = verification_identity()

    def project(proof):
        if (not isinstance(proof, dict) or proof.get('state') != 'VERIFIED'
                or proof.get('validator') != validator or proof.get('kind') not in _REBIND_KINDS
                or proof.get('identity') != digest({'root': str(root), 'kind': proof['kind'],
                    'content_hash': proof.get('content_hash'), 'validator': validator})):
            return None
        files = proof.get('dependencies')
        if not isinstance(files, list) or len(files) > 4096:
            return None
        changed = False
        updated = []
        for item in files:
            path = owned_regular(root, item['path'])
            current = link_identity(path)
            old = item['file_identity']
            if current['stamp'] == old:
                updated.append(deepcopy(item))
                continue
            record = approved.get(tuple(old))
            if (record is None or current != record['new']
                    or item.get('sha256', record['sha256']) != record['sha256']):
                return None
            updated.append({**deepcopy(item), 'file_identity': current['stamp']})
            changed = True
        if not changed:
            return None
        # Nested storage layout is metadata too; only already-bound dependencies
        # can supply its replacement. The persistent details object is untouched.
        by_path = {item['path']: item for item in updated}
        for item in updated:
            for dependency in item.get('storage_dependencies', []):
                known = by_path.get(dependency['path'])
                if known is None or known.get('sha256') != dependency.get('sha256'):
                    return None
                dependency['file_identity'] = known['file_identity']
        return {**proof, 'dependencies': updated}

    # In-memory proofs are separately checked, including any newer than disk.
    for key, proof in list(_VALIDATIONS.items()):
        try:
            replacement = project(proof)
            if replacement is not None:
                _VALIDATIONS[key] = replacement
                stats['memory_rebound'] += 1
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for key, stamp in list(_DOCUMENT_STAMPS.items()):
        if key[0] == str(root) and tuple(stamp) in approved:
            _DOCUMENT_STAMPS.pop(key, None)
    # _DOCUMENTS and business-document receipt keys include the full old stamp;
    # they naturally miss. Explicit full reads retain all original checks.
    directory = root / 'data/state/verification'
    if directory.is_symlink() or not directory.is_dir():
        return stats

    def candidates():
        for identity in scope['touched']:
            yield directory / (identity + '.json'), True
        with os.scandir(directory) as entries:
            for entry in entries:
                yield Path(entry.path), False

    visited = set()
    try:
        for path, priority in candidates():
            if stats['scanned'] >= max_receipts:
                stats['truncated'] = True
                break
            stats['scanned'] += 1
            stats['priority_candidates'] += priority
            if path.name in visited:
                continue
            visited.add(path.name)
            if not path.name.endswith('.json'):
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    stats['invalid'] += 1
                    continue
                size = path.stat().st_size
                if size > 2_000_000:
                    stats['invalid'] += 1
                    continue
                if stats['read_bytes'] + size > max_bytes:
                    stats['truncated'] = True
                    break
                stats['read_bytes'] += size
                before = _stamp(path)
                envelope = json.loads(path.read_bytes())
                proof = envelope['verification']
                if (envelope.get('sha256') != digest(proof)
                        or path.name != proof.get('identity', '') + '.json'
                        or _stamp(path) != before):
                    stats['invalid'] += 1
                    continue
                replacement = project(proof)
                if replacement is not None:
                    prior_writes = scope['metrics']['receipts_written']
                    _save_receipt(root, replacement['identity'], kind=replacement['kind'],
                        content_hash=replacement['content_hash'],
                        dependencies=replacement['dependencies'], details=replacement.get('details'))
                    written = scope['metrics']['receipts_written'] - prior_writes
                    stats['rebound'] += written
                    stats['priority_rebound'] += written if priority else 0
                    stats['write_failures'] += not written
                    _VALIDATIONS[replacement['identity']] = replacement
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                stats['invalid'] += 1
    except OSError:
        stats['state'] = 'CACHE_UNAVAILABLE'
        return stats
    while len(_VALIDATIONS) > 128:
        _VALIDATIONS.popitem(last=False)
    stats['state'] = 'PARTIAL' if stats['truncated'] or stats['write_failures'] else 'COMPLETE'
    return stats


def _project_cached_files(proof):
    """Return current physical metadata without rewriting frozen proof details."""
    known = {item['path']: item for item in proof['dependencies']}

    def project(item):
        current = known.get(item['path'])
        if (current is None or current.get('sha256') != item.get('sha256')
                or current.get('content_hash') != item.get('content_hash')):
            raise ValueError('缓存闭包文件与已验证依赖不一致')
        value = {**deepcopy(item), 'file_identity': current['file_identity']}
        if 'storage_dependencies' in value:
            value['storage_dependencies'] = [project(child) for child in value['storage_dependencies']]
        return value

    return [project(item) for item in proof['details']['files']]


def _invalid_json(_):
    raise ValueError('业务证据不允许浮点数或非有限值')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('业务证据含重复字段')
        result[key] = value
    return result


def read_business_document(root, reference):
    """Read a hash-bound local business document, without creating any files."""
    relative = None
    try:
        relative, expected = reference['path'], reference['content_hash']
        if (not isinstance(relative, str) or not relative.startswith('data/plans/')
                or not relative.endswith('.json') or '..' in PurePosixPath(relative).parts
                or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise ValueError('无效原件引用')
        path = local_path(root, relative)
        if not path.is_relative_to((root / 'data/plans').resolve()):
            raise ValueError('业务原件路径越界')
        layout = document_layout(root, relative, expected)
        stamp = layout['file_identity']
        if stamp[2] > _MAX_DOCUMENT_BYTES:
            raise ValueError('业务原件超出有界核验大小，须显式迁移')
        key = _document_key(root, relative, expected, [stamp, layout['storage_dependencies']])
        cached = _DOCUMENTS.get(key)
        if cached is not None:
            _DOCUMENTS.move_to_end(key)
            _metric('cache_hits')
            if _receipt(root, key) is None:
                _save_receipt(root, key, path=relative, content_hash=expected,
                              sha256=cached[1].get('original_sha256', cached[1]['sha256']),
                              file_identity=stamp, storage_dependencies=layout['storage_dependencies'])
            return deepcopy(cached[0]), deepcopy(cached[1])
        proof = _receipt(root, key)
        raw = read_document_bytes(root, layout, max_bytes=_MAX_DOCUMENT_BYTES)
        _metric('bytes_read', len(raw))
        if _stamp(path) != stamp:
            raise ValueError('读取过程中原件变化')
        body = json.loads(raw, parse_float=_invalid_json, parse_constant=_invalid_json,
                          object_pairs_hook=_unique_object)
        raw_hash = hashlib.sha256(raw).hexdigest()
        reused = (proof is not None and proof.get('path') == relative
                  and proof.get('content_hash') == expected and proof.get('sha256') == raw_hash
                  and proof.get('file_identity') == stamp
                  and proof.get('storage_dependencies', []) == layout['storage_dependencies'])
        if not isinstance(body, dict) or not reused and digest(body) != expected:
            raise ValueError('业务证据摘要不符')
        if reused:
            _metric('cache_hits')
        else:
            reject_credentials(body)
            _metric('full_checks')
            _save_receipt(root, key, path=relative, content_hash=expected,
                          sha256=raw_hash, file_identity=stamp,
                          storage_dependencies=layout['storage_dependencies'])
        info = {'path': relative, 'content_hash': expected,
                'sha256': layout['physical_sha256'] or raw_hash}
        if layout['wrapper']:
            info.update(original_sha256=raw_hash, storage_format='cas-json-v1',
                        storage_dependencies=layout['storage_dependencies'])
        stamp_key = (str(Path(root).resolve()), relative, expected)
        _DOCUMENT_STAMPS[stamp_key] = stamp
        _DOCUMENT_STAMPS.move_to_end(stamp_key)
        while len(_DOCUMENT_STAMPS) > 4096:
            _DOCUMENT_STAMPS.popitem(last=False)
        _remember_document(key, body, info, len(raw))
        return body, info
    except LedgerError:
        raise
    except (KeyError, TypeError, ValueError, OSError):
        raise LedgerError('INTEGRITY_ERROR', '业务决策原件缺失、路径无效或内容损坏',
                          path=relative) from None


def business_references(value):
    """Collect documents and market references from a JSON value (not file contents)."""
    references, snapshots, observations = {}, set(), set()

    def walk(item):
        if isinstance(item, dict):
            if isinstance(item.get('path'), str) and item['path'].startswith('data/plans/'):
                expected = item.get('content_hash', PurePosixPath(item['path']).stem)
                if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                    raise LedgerError('INTEGRITY_ERROR', '业务原件引用摘要无效', path=item['path'])
                references.setdefault(item['path'], set()).add(expected)
            for key, child in item.items():
                if key in {'market_snapshot_ids', 'strategy_market_snapshot_ids', 'snapshot_ids'} and isinstance(child, (list, tuple)):
                    snapshots.update(x for x in child if isinstance(x, str))
                if key in {'snapshot_id', 'market_snapshot_id'} and isinstance(child, str):
                    snapshots.add(child)
                if key == 'observation_ids' and isinstance(child, (list, tuple)):
                    observations.update(x for x in child if isinstance(x, str))
                if key != 'path':
                    walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
        elif isinstance(item, str) and item.startswith('data/plans/'):
            references.setdefault(item, set()).add(PurePosixPath(item).stem)

    walk(value)
    return references, snapshots, observations


def document_closure(root, references):
    """Verify every linked business document once; conflicting hashes/cycles fail."""
    expected = {p: set(hashes) for p, hashes in references.items()}
    visited, active, files, bodies = set(), set(), {}, {}
    snapshots, observations = set(), set()

    def visit(relative):
        if len(active) >= 64 or len(visited) + len(active) >= 4096:
            raise LedgerError('INTEGRITY_ERROR', '业务证据依赖超出有界核验范围')
        if len(expected[relative]) != 1:
            raise LedgerError('INTEGRITY_ERROR', '同一业务原件存在冲突摘要', path=relative)
        if relative in active:
            raise LedgerError('INTEGRITY_ERROR', '业务证据引用形成循环', path=relative)
        if relative in visited:
            if files[relative]['content_hash'] not in expected[relative]:
                raise LedgerError('INTEGRITY_ERROR', '业务原件摘要不一致')
            return
        active.add(relative)
        body, info = read_business_document(root, {
            'path': relative, 'content_hash': next(iter(expected[relative]))})
        files[relative], bodies[relative] = info, body
        children, child_snapshots, child_observations = business_references(body)
        snapshots.update(child_snapshots)
        observations.update(child_observations)
        for path, hashes in children.items():
            expected.setdefault(path, set()).update(hashes)
            visit(path)
        active.remove(relative)
        visited.add(relative)

    for relative in list(expected):
        visit(relative)
    physical_files = dict(files)
    for info in files.values():
        for dependency in info.get('storage_dependencies', []):
            physical_files[dependency['path']] = dependency
    return {'files': list(physical_files.values()), 'bodies': bodies,
            'snapshots': snapshots, 'observations': observations}


def read_decision_core(root, reference):
    try:
        ref = normalize_decision_ref(reference)
        return read_business_document(root, ref['decision_core'])[0]
    except ExecutionSupportError as exc:
        raise LedgerError(exc.code, exc.message, **exc.details) from None


def verify_plan_decision(root, payload, *, market=None, materialize=True):
    """Check retained proposal/core/envelope without reselecting stocks or reading policy config.

    The account-policy snapshot is internally bound here. The authoritative policy
    registry and unique decision index are checked by StrategyDecisionsMixin in the same transaction.
    """
    try:
        mode = payload.get('provenance_mode', 'MANUAL')
        report = payload.get('validation_report', {})
        if not isinstance(report, dict) or mode not in {'MANUAL', 'STRATEGY'}:
            raise ValueError('来源模式或验证报告无效')
        if mode == 'MANUAL':
            if (payload.get('decision_ref') is not None
                    or payload.get('strategy_market_snapshot_ids') or payload.get('strategy_data_evidence')
                    or report.get('provenance_mode', 'MANUAL') != 'MANUAL'
                    or report.get('decision_ref') is not None
                    or report.get('strategy_market_snapshot_ids') or report.get('strategy_data_evidence')):
                raise ValueError('不得移除决策引用降级为人工模式')
            if 'source_proposal' in report:
                original = report['source_proposal']
                if not isinstance(original, dict) or original.get('provenance_mode', 'MANUAL') != 'MANUAL':
                    raise ValueError('验证报告来源不得降级为人工模式')
                validate_provenance(original)
            return None
        source = report['source_proposal']
        if not isinstance(source, dict):
            raise ValueError('来源提案须为对象')
        if report.get('status') != 'VALID' or report.get('provenance_mode') != 'STRATEGY':
            raise ValueError('缺少策略验证报告')
        ref = normalize_decision_ref(payload['decision_ref'])
        content_hash = digest(payload)
        if not materialize and cached_validation(root, 'plan_decision', content_hash):
            proof = _VALIDATIONS[_validation_key(root, 'plan_decision', content_hash)]
            details = proof.get('details')
            if isinstance(details, dict) and {'snapshots', 'observations', 'files'} <= details.keys():
                core = read_decision_core(root, ref)
                closure = {**deepcopy(details), 'files': _project_cached_files(proof),
                           'bodies': {ref['decision_core']['path']: core}}
                if market is not None and (closure['snapshots'] or closure['observations']):
                    manifest = market.evidence_manifest(closure['snapshots'],
                                                       observation_ids=closure['observations'])
                    if manifest.get('state') != 'READY':
                        raise LedgerError('INTEGRITY_ERROR', '策略依赖来源原件无法完整核验')
                return {'source_proposal': source, 'core': core, 'closure': closure,
                        'materialized': False}
        if canonical(ref) != canonical(source.get('decision_ref')) or canonical(ref) != canonical(report.get('decision_ref')):
            raise ValueError('决策引用不一致')
        closure = document_closure(root, {ref['decision_core']['path']: {ref['decision_core']['content_hash']}})
        core = closure['bodies'][ref['decision_core']['path']]
        validate_decision_core(source, core)
        if digest(source) != report.get('proposal_hash'):
            raise ValueError('来源提案摘要不符')
        for field in ['account_id', 'strategy_id', 'strategy_version', 'target_session',
                      'market_as_of', 'account_snapshot_id', 'account_revision']:
            if source[field] != payload[field]:
                raise ValueError('封存上下文与原提案不一致')
        for field in ['input_cutoff', 'generated_at']:
            if aware_time(source[field]) != aware_time(payload[field]):
                raise ValueError('封存时间与原提案不一致')
        if source.get('rebalance_task_id') != payload.get('task_id'):
            raise ValueError('父任务引用与原提案不一致')
        for destination, origin in [('strategy_market_snapshot_ids', 'market_snapshot_ids'),
                                    ('strategy_data_evidence', 'data_evidence')]:
            if (canonical(payload.get(destination, [])) != canonical(source.get(origin, []))
                    or canonical(payload.get(destination, [])) != canonical(report.get(destination, []))):
                raise ValueError('策略来源证据被更改或遗漏')
        if report.get('account_snapshot_hash') != payload.get('account_snapshot_hash'):
            raise ValueError('账户快照摘要与验证报告不一致')
        accepted, orders = report['accepted_orders'], payload['orders']
        if not isinstance(accepted, list) or not isinstance(orders, list) or len(accepted) != len(orders):
            raise ValueError('封存订单与验证报告不一致')
        seen_inputs = set()
        for order_index, (accepted_order, order) in enumerate(zip(accepted, orders)):
            for field in ['code', 'side', 'quantity', 'limit_price', 'reason', 'reserved_cash_fen',
                          'reserved_shares', 'order_index']:
                if canonical(accepted_order[field]) != canonical(order[field]):
                    raise ValueError('封存订单内容与验证报告不一致')
            index = accepted_order['input_order_index']
            if type(index) is not int or index < 0 or index in seen_inputs:
                raise ValueError('接受订单的原意图位置无效或重复')
            seen_inputs.add(index)
            intent = source['ordered_intents'][index]
            if intent['order_index'] != index or order['order_index'] != order_index:
                raise ValueError('接受订单顺序与原意图不一致')
            if (normalize_code(intent['code']) != order['code'] or intent['side'] != order['side']
                    or intent['reason'] != order['reason']
                    or canonical(intent['limit_price']) != canonical(order['limit_price'])
                    or order['order_id'] != f"{payload['plan_id']}:{order['code']}"):
                raise ValueError('接受订单不属于原始意图')
            quantity, sizing = intent_quantity(
                {**intent, 'code': normalize_code(intent['code']),
                 'price': number(intent['limit_price'], positive=True)},
                payload['account_snapshot']['positions'], payload['fee_policy'], payload['target_session'])
            if order['quantity'] != quantity or canonical(sizing) != canonical(accepted_order['sizing']):
                raise ValueError('封存数量不符合原意图的数量、预算或卖出比例')
        if canonical(report['skipped_intents']) != canonical(payload.get('skipped_intents', [])):
            raise ValueError('跳过原因与验证报告不一致')
        for skipped in report['skipped_intents']:
            matches = [index for index, intent in enumerate(source['ordered_intents'])
                       if normalize_code(intent['code']) == skipped['code']
                       and intent['side'] == skipped['side'] and intent['reason'] == skipped['reason']]
            if len(matches) != 1 or matches[0] in seen_inputs:
                raise ValueError('跳过记录不属于唯一原意图')
            seen_inputs.add(matches[0])
        expected_inputs = {i for i, intent in enumerate(source['ordered_intents']) if intent['side'] != 'HOLD'}
        if seen_inputs != expected_inputs:
            raise ValueError('验证报告遗漏或增加原始买卖意图')
        if not orders:
            expected_reason = source.get('no_order_reason') or (
                '; '.join(i['reason_code'] for i in report['skipped_intents'])
                if report['skipped_intents'] else
                '; '.join(i['reason'] for i in source['ordered_intents'] if i['side'] == 'HOLD')) or None
            if payload.get('no_order_reason') != expected_reason:
                raise ValueError('空计划原因与原提案不一致')
        if market is not None and (closure['snapshots'] or closure['observations']):
            if not callable(getattr(market, 'evidence_manifest', None)):
                raise LedgerError('INTEGRITY_ERROR', '策略依赖来源缺少公开证据清单接口')
            manifest = market.evidence_manifest(sorted(closure['snapshots']),
                                                 observation_ids=sorted(closure['observations']))
            if manifest.get('state') != 'READY':
                raise LedgerError('INTEGRITY_ERROR', '策略依赖来源原件无法完整核验')
        remember_validation(root, 'plan_decision', content_hash, closure['files'], details={
            'snapshots': sorted(closure['snapshots']), 'observations': sorted(closure['observations']),
            'files': closure['files']})
        return {'source_proposal': source, 'core': core, 'closure': closure,
                'materialized': materialize}
    except LedgerError:
        raise
    except ExecutionSupportError as exc:
        raise LedgerError(exc.code, exc.message, **exc.details) from None
    except (KeyError, TypeError, ValueError, IndexError):
        raise LedgerError('INTEGRITY_ERROR', '策略决策、来源提案与冻结计划不一致') from None
