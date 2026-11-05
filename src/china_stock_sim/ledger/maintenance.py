"""Explicit migration, append-only corrections, integrity checks and recoverable backups."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tomllib
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

from china_stock_sim.common import evidence_objects
from china_stock_sim.config import local_path
from china_stock_sim.ledger.backup_links import VerifiedLinkBatch, owned_regular, permitted
from china_stock_sim.ledger.backup_links import identity as link_identity
from china_stock_sim.ledger.decision_evidence import (
    cached_validation,
    document_closure,
    read_business_document,
    rebind_verified_dependencies,
    remember_validation,
    verification_scope,
)
from china_stock_sim.ledger.document_objects import document_layout, file_identity
from china_stock_sim.ledger.kernel import (
    LedgerError,
    canonical,
    digest,
    ledger_lock,
    reject_credentials,
)
from china_stock_sim.ledger.schema import (
    SCHEMA_VERSION,
    check_schema,
    create_schema,
    create_strategy_schema,
    tables_for_version,
    validate_accounts,
)
from china_stock_sim.ledger.store import readonly_connection
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.storage import write_json

BACKUP_CONFIGS = ('default.toml', 'execution_rules.toml', 'tax_holding_rules.toml',
                  'preparation.toml')
BACKUP_VALIDATION_KIND = 'backup_archive_layout_v2'


def _config_files(root):
    """Only the ledger's non-secret operating configuration is portable."""
    files = []
    for name in BACKUP_CONFIGS:
        relative = f'configs/{name}'
        path = local_path(root, relative)
        if not path.is_file():
            if name == 'default.toml':
                raise ValueError('备份缺少必要的非密钥配置')
            continue
        raw = path.read_bytes()
        reject_credentials(tomllib.loads(raw.decode('utf-8')))
        files.append({'path': relative, 'sha256': hashlib.sha256(raw).hexdigest()})
    return files


def _strategy_integrity(root, db):
    """Validate authoritative indexes against the archived or live originals."""
    if db.execute('PRAGMA user_version').fetchone()[0] < 3:
        return []
    from china_stock_sim.common.strategy_transitions import verify_transition_chain
    from china_stock_sim.ledger.account_policies import verified_policy
    from china_stock_sim.ledger.plan_storage import resolve_plan
    from china_stock_sim.ledger.strategy_decisions import (
        resolve_decision_index,
        verify_decision_record,
    )

    issues = []
    accounts = dict(db.execute('SELECT account_id,strategy_id FROM accounts'))
    bases = dict(db.execute("""SELECT account_id,COUNT(*) FROM account_policies
        WHERE json_extract(data,'$.effective_session') IS NULL GROUP BY account_id"""))
    if set(bases) != set(accounts) or any(count != 1 for count in bases.values()):
        issues.append({'kind': 'ACCOUNT_POLICY_INTEGRITY_ERROR', 'reason': '账户缺少唯一base政策'})
    records = {}
    for table in ('account_policies', 'strategy_decisions', 'model_attempts', 'plans', 'rebalance_tasks'):
        records[table] = {}
        for saved in db.execute(f'SELECT id,account_id,data FROM {table}'):
            try:
                records[table][saved['account_id'], saved['id']] = {
                    **json.loads(saved['data']), 'id': saved['id'], 'account_id': saved['account_id']}
            except (ValueError, TypeError):
                issues.append({'kind': 'STRATEGY_INDEX_INTEGRITY_ERROR', 'table': table,
                               'account_id': saved['account_id'], 'id': saved['id']})

    def issue(table, row):
        issues.append({'kind': 'STRATEGY_INDEX_INTEGRITY_ERROR', 'table': table,
                       'account_id': row['account_id'], 'id': row['id']})

    def selected_policy(account_id, target):
        target = session_date(target)
        applicable = [row for (account, _), row in records['account_policies'].items()
                      if account == account_id and (row['effective_session'] is None
                                                    or row['effective_session'] <= target)]
        return max(applicable, key=lambda row: (row['effective_session'] or '', row['policy_revision']))

    def original_hash(reference, expected):
        body, _ = read_business_document(root, reference)
        if digest(body) != expected:
            raise ValueError('索引摘要与原件不一致')
        return body

    for table, validator in (('account_policies', verified_policy),
                             ('strategy_decisions', verify_decision_record)):
        for row in records[table].values():
            try:
                if table == 'strategy_decisions':
                    validator(root, row, materialize=False)
                else:
                    validator(root, row)
                if table == 'account_policies' and 'transition' in row:
                    verify_transition_chain(row,
                        {identity: policy for (account, identity), policy in records['account_policies'].items()
                         if account == row['account_id']},
                        {identity: task for (account, identity), task in records['rebalance_tasks'].items()
                         if account == row['account_id']})
                if table == 'account_policies' and row['policy']['strategy_id'] != accounts[row['account_id']]:
                    raise ValueError('政策策略与账户登记不一致')
            except (ValueError, OSError, KeyError, TypeError):
                issue(table, row)
    for row in records['strategy_decisions'].values():
        try:
            policy = selected_policy(row['account_id'], row['target_session'])
            if (row['policy_id'] != policy['policy_id'] or row['policy_hash'] != policy['policy_hash']
                    or canonical(row['policy_document']) != canonical(policy['document'])
                    or row['strategy_id'] != policy['policy']['strategy_id']
                    or row['strategy_version'] != policy['policy']['strategy_version']
                    or row['strategy_implementation_hash'] != policy['definition']['implementation_hash']):
                raise ValueError('决策引用的目标日权威政策不一致')
            bound_plan = records['plans'].get((row['account_id'], row.get('plan_id')))
            bound_attempts = [records['model_attempts'].get((row['account_id'], identity))
                              for identity in row.get('model_attempt_ids', [])]
            binding_hash = digest({'decision': row, 'policy': policy,
                                   'plan': bound_plan, 'attempts': bound_attempts})
            if cached_validation(root, 'strategy_index_binding', binding_hash):
                continue
            row = resolve_decision_index(root, row)
            original_hash(row['input_document'], row['input_hash'])
            ref = row.get('decision_ref')
            if ref is not None:
                if (ref['decision_id'] != row['decision_id'] or ref['input_hash'] != row['input_hash']
                        or ref['account_policy_id'] != policy['policy_id']
                        or ref['account_policy_hash'] != policy['policy_hash']
                        or canonical(ref['decision_core']) != canonical(row['decision_core'])
                        or canonical(row['proposal']['decision_ref']) != canonical(ref)):
                    raise ValueError('决策引用不一致')
                core, _ = read_business_document(root, row['decision_core'])
                if (canonical(core['account_policy']) != canonical(policy['policy'])
                        or digest(core['input_bundle']) != row['input_hash']):
                    raise ValueError('决策原件政策或输入不一致')
            for identity in row.get('model_attempt_ids', []):
                attempt = records['model_attempts'][row['account_id'], identity]
                if attempt.get('decision_id') != row['decision_id'] or attempt.get('input_hash') != row['input_hash']:
                    raise ValueError('决策所列模型尝试不一致')
            # Only new decision indexes require this link. Historical plans may
            # legitimately predate schema3 and have no strategy_decisions row.
            if row['state'] == 'FROZEN':
                plan = resolve_plan(root, records['plans'][row['account_id'], row['plan_id']])
                body, _ = read_business_document(root, plan['document'])
                if (ref is None or plan['target_session'] != row['target_session']
                        or plan['provenance_mode'] != 'STRATEGY'
                        or canonical(plan['decision_ref']) != canonical(ref)
                        or canonical(row['plan_document']) != canonical(plan['document'])
                        or body['plan_id'] != row['plan_id'] or body['account_id'] != row['account_id']
                        or body['target_session'] != row['target_session']
                        or canonical(body['decision_ref']) != canonical(ref)):
                    raise ValueError('冻结决策与计划不一致')
            references = {}
            for value in (row.get('input_document'), row.get('decision_core'),
                          row.get('document'), policy.get('document'),
                          (bound_plan or {}).get('document')):
                if value is not None:
                    references.setdefault(value['path'], set()).add(value['content_hash'])
            closure = document_closure(root, references)
            remember_validation(root, 'strategy_index_binding', binding_hash, closure['files'])
        except (ValueError, OSError, KeyError, TypeError):
            issue('strategy_decisions', row)
    policy_fields = {'account_policy_id', 'account_policy_hash', 'account_policy',
                     'strategy_definition', 'definition_status', 'account_revision_at_claim'}
    for row in records['model_attempts'].values():
        try:
            policy = selected_policy(row['account_id'], row['target_session'])
            if not policy_fields.intersection(row):
                base = next(p for (account, _), p in records['account_policies'].items()
                            if account == row['account_id'] and p['effective_session'] is None)
                if aware_time(row['claimed_at']) < aware_time(base['policy']['known_at']):
                    continue  # A pre-migration attempt retains its original schema2 contract.
                raise ValueError('新模型尝试缺少持久政策绑定')
            if (row['account_policy_id'] != policy['policy_id']
                    or row['account_policy_hash'] != policy['policy_hash']
                    or canonical(row['account_policy']) != canonical(policy['policy'])
                    or canonical(row['strategy_definition']) != canonical(policy['definition'])
                    or row['definition_status'] != 'REGISTERED'
                    or policy['definition'].get('uses_model') is not True
                    or row['strategy_id'] != policy['policy']['strategy_id']
                    or row['strategy_version'] != policy['policy']['strategy_version']
                    or row['model_ref'] != policy['policy']['parameters'].get('model_ref')):
                raise ValueError('模型尝试政策绑定不一致')
            original_hash(row['request_document'], row['request_hash'])
            if row.get('response_document') is not None:
                original_hash(row['response_document'], row['response_hash'])
            elif row['status'] != 'CLAIMED':
                raise ValueError('已完成模型尝试缺少响应原件')
            decision_id = row.get('decision_id')
            if decision_id is None:
                if (any(row.get(key) is not None for key in ('input_hash', 'input_document', 'decision_sequence'))
                        or any(d['account_id'] == row['account_id'] and d['target_session'] == row['target_session']
                               for d in records['strategy_decisions'].values())):
                    raise ValueError('独立模型尝试混入决策输入')
                continue
            decision = records['strategy_decisions'][row['account_id'], decision_id]
            sequence = row['decision_sequence']
            if (decision['target_session'] != row['target_session']
                    or decision['input_hash'] != row['input_hash']
                    or decision['policy_id'] != row['account_policy_id']
                    or decision['policy_hash'] != row['account_policy_hash']
                    or canonical(decision['input_document']) != canonical(row['input_document'])
                    or decision['account_revision'] != row['account_revision_at_claim']
                    or type(sequence) is not int or not 1 <= sequence <= decision['sequence']):
                raise ValueError('模型尝试引用的决策输入不一致')
            original_hash(row['input_document'], row['input_hash'])
            history = next(item for item in decision['history'] if item['sequence'] == sequence)
            prepared, _ = read_business_document(root, history['document'])
            if (prepared['state'] != 'PREPARED' or prepared['decision_id'] != decision_id
                    or prepared['input_hash'] != row['input_hash']
                    or prepared['policy_hash'] != row['account_policy_hash']
                    or row['status'] == 'SUCCEEDED' and row.get('accepted_decision_id') != decision_id):
                raise ValueError('模型尝试与准备时原件不一致')
        except (ValueError, OSError, KeyError, TypeError, StopIteration):
            issue('model_attempts', row)
    return issues


def file_hash(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def backup_member_layout(evidence_root, item, *, verify=True):
    """Resolve one manifest member without changing its original restore semantics.

    A matching physical SHA always wins, including native CAS wrappers. Only an
    old data/plans member may instead be represented by a wrapper whose raw SHA
    is the manifest's original byte SHA. ``verify=False`` checks layout and file
    identities only; it is not proof that the compressed content is intact.
    """
    evidence_root = Path(evidence_root)
    relative, expected = item['path'], item['sha256']
    if (not isinstance(relative, str) or Path(relative).as_posix() != relative
            or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
        raise ValueError('备份成员路径或摘要无效')
    path = local_path(evidence_root, relative)
    current = evidence_root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('备份成员路径不可含符号链接')
    before = file_identity(path)
    physical = file_hash(path)
    if file_identity(path) != before:
        raise ValueError('备份成员核验期间变化')
    result = {'path': relative, 'physical_sha256': physical, 'file_identity': before,
              'legacy_compressed': False, 'object_ref': None, 'storage_dependencies': []}
    if physical == expected:
        return result
    logical = item.get('content_hash')
    if (not relative.startswith('data/plans/') or Path(relative).suffix != '.json'
            or not isinstance(logical, str) or not re.fullmatch(r'[0-9a-f]{64}', logical)
            or Path(relative).stem != logical):
        raise ValueError('备份成员摘要不符且不允许压缩表示')
    layout = document_layout(evidence_root, relative, logical)
    wrapper = layout['wrapper']
    if (not wrapper or layout['physical_sha256'] != physical
            or layout['file_identity'] != before
            or wrapper['object_ref']['_evidence_ref']['sha256'] != expected):
        raise ValueError('旧备份压缩表示与原件身份不符')
    if verify:
        evidence_objects.verify(evidence_root, wrapper['object_ref'])
    for dependency in layout['storage_dependencies']:
        if file_identity(local_path(evidence_root, dependency['path'])) != dependency['file_identity']:
            raise ValueError('备份压缩对象核验期间变化')
    if file_identity(path) != before:
        raise ValueError('备份成员核验期间变化')
    return {**result, 'legacy_compressed': True, 'object_ref': wrapper['object_ref'],
            'storage_dependencies': layout['storage_dependencies']}


def _backup_evidence_layouts(evidence_root, members):
    """Only verified legacy members may add storage-only CAS closure entries."""
    layouts, physical = {}, {}
    for item in members:
        relative = item['path']
        if relative in layouts:
            raise ValueError('备份证据清单重复')
        layout = backup_member_layout(evidence_root, item)
        layouts[relative] = layout
        candidates = [{'path': relative, 'sha256': layout['physical_sha256'],
                       'file_identity': layout['file_identity']}]
        candidates += layout['storage_dependencies']
        for dependency in candidates:
            previous = physical.get(dependency['path'])
            if previous and previous['sha256'] != dependency['sha256']:
                raise ValueError('备份压缩表示存在冲突依赖')
            physical[dependency['path']] = dependency
    return layouts, physical


def _references(value, paths, snapshots, observations, documents, objects=None, *, opaque=False):
    if isinstance(value, dict):
        if '_evidence_ref' in value:
            if objects is None or not evidence_objects.is_reference(value):
                raise ValueError('账本CAS引用格式不完整')
            metadata = value['_evidence_ref']
            previous = objects.get(metadata['path'])
            if previous and previous['reference'] != value:
                raise ValueError('同一CAS对象存在冲突引用')
            objects[metadata['path']] = {'reference': value,
                'expand': not opaque or bool(previous and previous['expand'])}
            return
        if isinstance(value.get('path'), str) and value['path'].startswith('data/plans/') and value.get('content_hash'):
            documents.setdefault(value['path'], set()).add(value['content_hash'])
        for key, item in value.items():
            if key == 'observation_ids' and isinstance(item, (list, tuple)):
                observations.update(x for x in item if isinstance(x, str))
            if key in {'market_snapshot_ids', 'strategy_market_snapshot_ids', 'snapshot_ids'} and isinstance(item, (list, tuple)):
                snapshots.update(x for x in item if isinstance(x, str))
            elif key in {'market_snapshot_id', 'snapshot_id'} and isinstance(item, str):
                snapshots.add(item)
            _references(item, paths, snapshots, observations, documents, objects,
                        opaque=key in {'original_record_ref', 'account_view_evidence'})
    elif isinstance(value, (list, tuple)):
        for item in value:
            _references(item, paths, snapshots, observations, documents, objects, opaque=opaque)
    elif isinstance(value, str) and value.startswith('data/plans/'):
        paths.add(value)


def _ledger_closure(root, db):
    """Collect the union once; opaque historical records are streamed, never expanded."""
    paths, snapshots, observations, documents, objects = set(), set(), set(), {}, {}
    version = db.execute('PRAGMA user_version').fetchone()[0]
    for table in tables_for_version(version):
        for row in db.execute(f'SELECT data FROM {table}'):
            _references(json.loads(row['data']), paths, snapshots, observations, documents, objects)
    visited_documents, visited_objects, files = set(), {}, {}

    def add(item):
        previous = files.get(item['path'])
        if previous and previous['sha256'] != item['sha256']:
            raise ValueError('备份证据存在冲突摘要')
        files[item['path']] = item

    while paths - visited_documents or any(visited_objects.get(p) != item['expand']
                                            for p, item in objects.items()):
        if len(paths) + len(objects) > 100_000:
            raise ValueError('备份引用闭包超出范围')
        expected = {path: documents.get(path, {Path(path).stem}) for path in paths - visited_documents}
        if expected:
            closure = document_closure(root, expected)
            for item in closure['files']:
                add(item)
            snapshots.update(closure['snapshots'])
            observations.update(closure['observations'])
            visited_documents.update(closure['bodies'])
            for body in closure['bodies'].values():
                _references(body, paths, snapshots, observations, documents, objects)
        for relative, item in list(objects.items()):
            if visited_objects.get(relative) == item['expand']:
                continue
            reference, expand = item['reference'], item['expand']
            metadata = reference['_evidence_ref']
            path = local_path(root, relative)
            before = file_identity(path)
            # Strict public verification rejects symlinks and both stored/raw digest damage.
            if expand:
                body = evidence_objects.read_json(root, reference)
                _references(body, paths, snapshots, observations, documents, objects)
            elif not cached_validation(root, 'opaque_evidence_object', digest(reference)):
                evidence_objects.verify(root, reference)
            info = {'path': relative, 'sha256': metadata['stored_sha256'],
                    'kind': 'evidence_object', 'file_identity': before}
            if file_identity(path) != before:
                raise ValueError('备份CAS核验期间变化')
            if not expand:
                remember_validation(root, 'opaque_evidence_object', digest(reference), [info])
            add(info)
            visited_objects[relative] = expand
    if any(len(hashes) != 1 or files.get(path, {}).get('content_hash') not in hashes
           for path, hashes in documents.items()):
        raise ValueError('备份递归业务原件存在冲突摘要')
    return {'files': list(files.values()), 'snapshots': snapshots, 'observations': observations}


def _copy_evidence(root, destination_root, item, *, link_batch=None, verified_files=None):
    """Only immutable evidence can share an inode; database/configuration never use this path."""
    source = owned_regular(root, item['path'])
    before = link_identity(source)
    if file_hash(source) != item['sha256'] or link_identity(source) != before:
        raise ValueError('备份证据摘要不符')
    # Check the archive root's ancestors before resolving a member within it.
    # Resolving the archive root first would hide an internal symlink replacement.
    archive_relative = str(Path(destination_root).relative_to(Path(root).resolve()))
    destination_root = owned_regular(root, archive_relative)
    destination = owned_regular(destination_root, item['path'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    immutable = (item.get('kind') in {'evidence_object', 'source_object', 'market_snapshot'}
                 or item['path'].startswith('data/plans/'))
    linked = False
    if immutable:
        try:
            os.link(source, destination)
            linked = True
        except OSError:
            pass
    if not linked:
        shutil.copyfile(source, destination)
    after = link_identity(source)
    destination_guard = link_identity(destination)
    unchanged = permitted(before, after, 1) if linked else before == after
    if (not unchanged or (linked and not destination.samefile(source))
            or file_hash(destination) != item['sha256'] or link_identity(source) != after
            or link_identity(destination) != destination_guard):
        raise ValueError('备份期间证据变化')
    if linked and link_batch is not None:
        link_batch.linked(source, before, after, item['sha256'])
    if verified_files is not None:
        verified_files.append({'path': item['path'], 'sha256': item['sha256'],
                               'verified_guard': destination_guard})
    return 'HARDLINK' if linked else 'COPY'


def _backup_result(root, folder, manifest):
    evidence = manifest['evidence']
    incomplete = manifest['state'] != 'COMPLETE' or evidence['state'] != 'COMPLETE' or bool(evidence['issues'])
    return {'backup_id': manifest['backup_id'], 'read_only': True,
            'state': 'PARTIAL' if incomplete else 'READY', 'schema_version': manifest['schema_version'],
            'evidence_file_count': len(evidence['files']),
            'market_archive_required': bool(evidence['market_snapshots']),
            'manifest': str((folder / 'manifest.json').relative_to(root)),
            'manifest_sha256': file_hash(folder / 'manifest.json')}


def _verified_backup_file(folder, relative, expected=None):
    path = owned_regular(folder, relative)
    before = file_identity(path)
    checksum = file_hash(path)
    if (expected is not None and checksum != expected) or file_identity(path) != before:
        raise ValueError('备份文件摘要或核验期间身份变化')
    return {'path': relative, 'sha256': checksum, 'file_identity': before}


def _remember_backup(root, folder, manifest, *, core_files, evidence_files, remember=True):
    # Never bless the file's current stamp just because its bytes were verified
    # earlier. Every guard here must come from that actual read/hash operation.
    dependencies = [{**item, 'path': str((folder / item['path']).relative_to(root)),
                     'kind': 'backup_archive_file'} for item in core_files]
    dependencies += [{**item, 'path': str((folder / 'evidence' / item['path']).relative_to(root)),
                      'kind': 'backup_archive_file'} for item in evidence_files]
    for item in dependencies:
        if file_identity(owned_regular(root, item['path'])) != item['file_identity']:
            raise ValueError('备份文件在核验后发生变化')
    if remember:
        remember_validation(root, BACKUP_VALIDATION_KIND, digest(manifest), dependencies)


class MaintenanceMixin:
    def migrate(self):
        """Explicit schema upgrade; a failed transaction leaves the old ledger intact."""
        from china_stock_sim.ledger.account_policies import initialize_base_policy
        from china_stock_sim.ledger.store import initialize

        if not self.settings.database.exists():
            return initialize(self.settings, recorded_at=self._now().isoformat())
        with ledger_lock(self.root):
            with closing(readonly_connection(self.settings.database)) as db:
                version = check_schema(db, legacy=True)
                validate_accounts(db, self.settings)
            if version == SCHEMA_VERSION:
                return {'state': 'ALREADY_APPLIED', 'schema_version': SCHEMA_VERSION}
            if version == 3:
                raise LedgerError('LEDGER_COMPACTION_REQUIRED',
                    'schema3须先执行显式隔离压缩/核验切换；不可重新初始化base政策')
            backup = self._backup_locked('migration', legacy=True)
            if backup['state'] != 'COMPLETE':
                raise ValueError('迁移前备份证据不完整，拒绝升级账本')
            with closing(sqlite3.connect(self.settings.database, timeout=0)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                try:
                    check_schema(db, legacy=True)
                    validate_accounts(db, self.settings)
                    if db.execute('PRAGMA user_version').fetchone()[0] != version:
                        raise ValueError('迁移期间账本结构已变化')
                    if version == 1:
                        create_schema(db, migrate=True, advance_version=False)
                    elif version == 2:
                        create_strategy_schema(db)
                    else:
                        raise ValueError('不支持的源账本版本')
                    recorded_at = self._now().isoformat()
                    accounts = [dict(row) for row in db.execute('SELECT * FROM accounts ORDER BY account_id')]
                    for account in accounts:
                        initialize_base_policy(db, root=self.root, settings=self.settings,
                                               account=account, recorded_at=recorded_at)
                    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                    validate_accounts(db, self.settings)
                    check_schema(db)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            return {'state': 'APPLIED', 'schema_version': SCHEMA_VERSION,
                    'backup_id': backup['backup_id'], 'accounts_preserved': True}

    def _evidence_inventory(self, db, *, legacy=False):
        files, market, issues = [], [], _strategy_integrity(self.root, db)
        snapshots, observations = set(), set()
        try:
            closure = _ledger_closure(self.root, db)
            files.extend(closure['files'])
            snapshots.update(closure['snapshots'])
            observations.update(closure['observations'])
        except (ValueError, OSError) as exc:
            issues.append({'kind': 'MISSING_EVIDENCE_FILE',
                           'path': getattr(exc, 'details', {}).get('path'), 'reason': str(exc)})
            # Preserve independently known market diagnostics when a document is unavailable.
            version = db.execute('PRAGMA user_version').fetchone()[0]
            for table in tables_for_version(version):
                for row in db.execute(f'SELECT data FROM {table}'):
                    try:
                        _references(json.loads(row['data']), set(), snapshots, observations, {}, {})
                    except (ValueError, TypeError):
                        pass
        snapshot_batches = [sorted(snapshots)[i:i + 1000] for i in range(0, len(snapshots), 1000)]
        observation_batches = [sorted(observations)[i:i + 100000] for i in range(0, len(observations), 100000)]
        for index in range(max(len(snapshot_batches), len(observation_batches))):
            batch = snapshot_batches[index] if index < len(snapshot_batches) else []
            observed = observation_batches[index] if index < len(observation_batches) else []
            try:
                if self.market is None or not callable(getattr(self.market, 'evidence_manifest', None)):
                    raise ValueError('未提供公开数据证据导出接口')
                exported = self.market.evidence_manifest(batch, observation_ids=observed)
                if exported.get('state') != 'READY':
                    raise ValueError('市场证据清单未就绪')
                files.extend(exported['files'])
                market.append({'snapshot_ids': batch,
                               'observation_ids': exported['observation_ids'],
                               'index_required': exported['index_required'],
                               'verified': True, 'verification': 'public_evidence_manifest'})
            except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
                market.append({'snapshot_ids': batch, 'observation_ids': observed, 'verified': False})
                issues.append({'kind': 'MARKET_EVIDENCE_UNAVAILABLE',
                               'snapshot_ids': batch, 'reason': str(exc)})
        return {'files': list({item['path']: item for item in files}.values()), 'market_snapshots': market, 'issues': issues,
                'state': 'COMPLETE' if not issues else 'PARTIAL',
                'market_archive_scope': 'Verified snapshot and recursive source files are copied; the mutable market index must be restored separately.'}

    def audit_status(self, account_id=None):
        try:
            with closing(readonly_connection(self.settings.database)) as db:
                db.execute('BEGIN')
                version = check_schema(db, legacy=True)
                validate_accounts(db, self.settings)
                evidence = self._evidence_inventory(db, legacy=version == 1)
                accounts = [dict(row) for row in db.execute('''
                    SELECT a.*, COALESCE(SUM(c.amount_fen),0) cash_fen,
                    SUM(CASE WHEN c.kind='INITIAL_CAPITAL' THEN 1 ELSE 0 END) opening_entries
                    FROM accounts a LEFT JOIN cash_entries c USING(account_id)
                    GROUP BY a.account_id ORDER BY a.account_id''')]
                if version >= 2:
                    states = {row['account_id']: json.loads(row['data'])
                              for row in db.execute('SELECT * FROM account_state')}
                    if set(states) != {row['account_id'] for row in accounts}:
                        raise ValueError('账户运行状态缺失')
                    for account in accounts:
                        account.update(states[account['account_id']])
                elif db.execute('SELECT COUNT(*) FROM cash_entries').fetchone()[0] != len(accounts):
                    raise ValueError('旧初始化账本包含未知流水')
                if account_id is not None:
                    accounts = [a for a in accounts if a['account_id'] == account_id]
                    if not accounts:
                        raise ValueError('未知账户')
                checks = []
                if version >= 2:
                    for row in db.execute('SELECT account_id,operation_id,result FROM operations'):
                        result = json.loads(row['result'])
                        if result.get('operation_id') != row['operation_id']:
                            checks.append({'kind': 'OPERATION_ID_MISMATCH', 'operation_id': row['operation_id']})
                    for row in db.execute('SELECT account_id,data FROM position_lots'):
                        lot = json.loads(row['data'])
                        for key in ('remaining_quantity', 'remaining_cost_fen', 'quantity', 'cost_fen'):
                            if key in lot and (type(lot[key]) is not int or lot[key] < 0):
                                checks.append({'kind': 'INVALID_LOT', 'account_id': row['account_id'], 'field': key})
                return {'state': 'INTEGRITY_ERROR' if checks else 'PARTIAL' if evidence['issues'] else 'READY',
                        'schema_version': version, 'accounts': accounts,
                        'evidence': evidence, 'issues': checks, 'read_only': True}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return {'state': 'INTEGRITY_ERROR', 'reason': str(exc), 'read_only': True}

    def _backup_locked(self, label, *, legacy=False):
        if label not in {'daily', 'manual', 'migration'}:
            raise ValueError('备份类型必须是daily/manual/migration')
        identity = f'{label}-{self._now().strftime("%Y%m%dT%H%M%S")}-{uuid4().hex[:12]}'
        folder = local_path(self.root, f'data/backups/ledger/{identity}')
        folder.mkdir(parents=True)
        destination = folder / 'ledger.sqlite3'
        link_batch = VerifiedLinkBatch(self.root)
        try:
            with closing(readonly_connection(self.settings.database)) as source:
                source.execute('BEGIN')
                version = check_schema(source, legacy=legacy)
                validate_accounts(source, self.settings)
                evidence = self._evidence_inventory(source, legacy=version == 1)
                configuration = {'files': _config_files(self.root),
                                 'source': 'backup_time_non_secret_configuration'}
                with closing(sqlite3.connect(destination)) as copy:
                    source.backup(copy)
                    if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('备份库完整性检查失败')
                amounts = dict(source.execute('SELECT account_id,SUM(amount_fen) FROM cash_entries GROUP BY account_id'))
            storage = {'hardlinked_files': 0, 'copied_files': 0,
                       'database': 'SQLITE_BACKUP_INDEPENDENT', 'configuration': 'INDEPENDENT_COPY'}
            core_files = [_verified_backup_file(folder, 'ledger.sqlite3')]
            copied_evidence = []
            for item in evidence['files']:
                mode = _copy_evidence(self.root, folder / 'evidence', item,
                                      link_batch=link_batch, verified_files=copied_evidence)
                storage['hardlinked_files' if mode == 'HARDLINK' else 'copied_files'] += 1
            for item in configuration['files']:
                copied = folder / 'configuration' / item['path']
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_path(self.root, item['path']), copied)
                core_files.append(_verified_backup_file(folder,
                    'configuration/' + item['path'], item['sha256']))
            manifest = {'backup_id': identity, 'label': label, 'created_at': self._now().isoformat(),
                        'schema_version': version, 'database_sha256': core_files[0]['sha256'],
                        'balances_fen': amounts, 'evidence': evidence, 'configuration': configuration,
                        'storage': storage,
                        'state': 'COMPLETE' if not evidence['issues'] else 'PARTIAL'}
            write_json(folder / 'manifest.json', manifest)
            expected_manifest = hashlib.sha256((json.dumps(manifest, ensure_ascii=False,
                indent=2, allow_nan=False) + '\n').encode()).hexdigest()
            core_files.append(_verified_backup_file(folder, 'manifest.json', expected_manifest))
            if label == 'daily':
                candidates = sorted((p for p in folder.parent.glob('daily-*')
                                     if (p / 'manifest.json').is_file()
                                     and not (p / 'failure.json').exists()
                                     and json.loads((p / 'manifest.json').read_text()).get('state') == 'COMPLETE'),
                                    key=lambda p: (json.loads((p / 'manifest.json').read_text())['created_at'],
                                                   p.stat().st_mtime_ns), reverse=True)
                for old in candidates[30:]:
                    link_batch.remove_backup(old, file_hash)
            # One bounded cache pass after all links and retention unlinks. A
            # cache failure cannot invalidate the completed backup operation.
            try:
                cache_result = rebind_verified_dependencies(self.root, link_batch.transitions())
            except Exception:
                cache_result = {'state': 'CACHE_UNAVAILABLE'}
            verified_evidence = [{'path': item['path'], 'sha256': item['sha256'],
                'file_identity': link_batch.current_guard(
                    owned_regular(folder / 'evidence', item['path']),
                    item['verified_guard'], item['sha256'])['stamp']} for item in copied_evidence]
            _remember_backup(self.root, folder, manifest,
                             core_files=core_files, evidence_files=verified_evidence)
            return {**manifest, 'validation_cache': cache_result,
                    'manifest': str((folder / 'manifest.json').relative_to(self.root))}
        except Exception:
            # Keep previous successful backups untouched; this incomplete attempt is diagnostic only.
            write_json(folder / 'failure.json', {'backup_id': identity, 'state': 'FAILED',
                                                'at': self._now().isoformat()})
            raise

    def backup(self, *, label='daily'):
        with ledger_lock(self.root), verification_scope(self.root, persist=True):
            return self._backup_locked(label)

    def verify_backup(self, backup_id):
        with verification_scope(self.root, persist=False):
            return self._verify_backup(backup_id)

    def _verify_backup(self, backup_id):
        """Verify archived facts without restoring or consulting current configuration."""
        result = {'backup_id': backup_id, 'read_only': True}
        try:
            if (not isinstance(backup_id, str) or not backup_id or backup_id in {'.', '..'}
                    or Path(backup_id).name != backup_id):
                raise ValueError('无效备份身份')
            folder = local_path(self.root, f'data/backups/ledger/{backup_id}')
            if (folder / 'failure.json').exists():
                raise ValueError('备份尝试已记录失败')
            manifest_path = owned_regular(folder, 'manifest.json')
            manifest_guard = file_identity(manifest_path)
            manifest_raw = manifest_path.read_bytes()
            if file_identity(manifest_path) != manifest_guard:
                raise ValueError('备份清单在读取期间变化')
            manifest = json.loads(manifest_raw)
            core_files = [{'path': 'manifest.json', 'sha256': hashlib.sha256(manifest_raw).hexdigest(),
                           'file_identity': manifest_guard}]
            reject_credentials(manifest)
            if (manifest['backup_id'] != backup_id
                    or manifest['state'] not in {'COMPLETE', 'PARTIAL'}):
                raise ValueError('备份清单身份或状态无效')
            if cached_validation(self.root, BACKUP_VALIDATION_KIND, digest(manifest)):
                if file_identity(manifest_path) != manifest_guard:
                    raise ValueError('备份清单在读取后变化')
                return _backup_result(self.root, folder, manifest)
            source = local_path(folder, 'ledger.sqlite3')
            core_files.append(_verified_backup_file(folder, 'ledger.sqlite3', manifest['database_sha256']))
            configuration = manifest.get('configuration', {}).get('files')
            if not isinstance(configuration, list) or not configuration:
                raise ValueError('备份缺少自身配置')
            allowed = {f'configs/{name}' for name in BACKUP_CONFIGS}
            seen, default = set(), None
            for item in configuration:
                relative = item['path']
                if relative not in allowed or relative in seen:
                    raise ValueError('备份配置路径未知或重复')
                seen.add(relative)
                copied = local_path(folder / 'configuration', relative)
                core_files.append(_verified_backup_file(folder,
                    'configuration/' + relative, item['sha256']))
                parsed = tomllib.loads(copied.read_text(encoding='utf-8'))
                reject_credentials(parsed)
                if relative == 'configs/default.toml':
                    default = parsed
            if default is None:
                raise ValueError('备份缺少必要配置')
            configured = local_path(folder / 'configuration', default['paths']['database'])
            if not configured.is_relative_to(folder / 'configuration/data/state'):
                raise ValueError('备份自身账本路径无效')
            evidence = manifest['evidence']
            layouts, archived = _backup_evidence_layouts(folder / 'evidence', evidence['files'])
            with closing(readonly_connection(source)) as db:
                db.execute('BEGIN')
                version = check_schema(db, legacy=True)
                if version != manifest['schema_version']:
                    raise ValueError('备份结构版本不符')
                # validate_accounts deliberately uses only the database's identities
                # and opening entries; a current TOML must never repair this archive.
                validate_accounts(db, None)
                balances = dict(db.execute(
                    'SELECT account_id,SUM(amount_fen) FROM cash_entries GROUP BY account_id'))
                if balances != manifest['balances_fen']:
                    raise ValueError('备份账户余额不符')
                if _strategy_integrity(folder / 'evidence', db):
                    raise ValueError('备份政策或决策索引与原件不一致')
                closure = _ledger_closure(folder / 'evidence', db)
                if any(archived.get(item['path'], {}).get('sha256') != item['sha256']
                       for item in closure['files']):
                    raise ValueError('备份清单遗漏账本引用原件')
                snapshots, observations = closure['snapshots'], closure['observations']
                market = evidence['market_snapshots']
                covered_snapshots = {identity for row in market if row.get('verified')
                    for identity in ([row['snapshot_id']] if row.get('snapshot_id') else row.get('snapshot_ids', []))}
                covered_observations = {identity for row in market if row.get('verified')
                                        for identity in row.get('observation_ids', [])}
                incomplete = (manifest['state'] != 'COMPLETE' or evidence.get('state') != 'COMPLETE'
                    or bool(evidence['issues']) or not snapshots <= covered_snapshots
                    or not observations <= covered_observations
                    or any(row.get('verified') is not True for row in market))
            _remember_backup(self.root, folder, manifest, core_files=core_files,
                             evidence_files=archived.values(), remember=not incomplete)
            return {**result, 'state': 'PARTIAL' if incomplete else 'READY',
                    'schema_version': version, 'evidence_file_count': len(layouts),
                    'market_archive_required': bool(market),
                    'manifest': str((folder / 'manifest.json').relative_to(self.root)),
                    'manifest_sha256': file_hash(folder / 'manifest.json')}
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, AttributeError):
            return {**result, 'state': 'INTEGRITY_ERROR', 'reason': 'BACKUP_ARCHIVE_UNAVAILABLE'}

    def restore_backup(self, backup_id, target_root):
        """Restore only into a new isolated directory; never overwrite a running ledger."""
        if not isinstance(backup_id, str) or Path(backup_id).name != backup_id:
            raise ValueError('备份ID无效')
        folder = local_path(self.root, f'data/backups/ledger/{backup_id}')
        verification = self.verify_backup(backup_id)
        if verification['state'] == 'INTEGRITY_ERROR':
            raise ValueError('备份摘要、配置或引用完整性核验失败')
        manifest = json.loads((folder / 'manifest.json').read_text())
        source = folder / 'ledger.sqlite3'
        if manifest['backup_id'] != backup_id or file_hash(source) != manifest['database_sha256']:
            raise ValueError('备份摘要不符')
        target = Path(target_root).resolve()
        if target == self.root or target == Path(__file__).resolve().parents[3]:
            raise ValueError('只能恢复到隔离目录')
        if target.exists() and any(target.iterdir()):
            raise ValueError('恢复目标必须为空隔离目录')
        with closing(readonly_connection(source)) as db:
            version = check_schema(db, legacy=True)
            if version != manifest['schema_version']:
                raise ValueError('备份结构版本与清单不符')
            validate_accounts(db, self.settings)
        configuration = manifest.get('configuration', {}).get('files')
        if not isinstance(configuration, list) or not configuration:
            raise ValueError('备份缺少自身配置，拒绝使用当前配置恢复')
        allowed = {f'configs/{name}' for name in BACKUP_CONFIGS}
        seen, default = set(), None
        for item in configuration:
            relative = item.get('path')
            if relative not in allowed or relative in seen:
                raise ValueError('备份配置清单包含未知或重复路径')
            seen.add(relative)
            copied = local_path(folder / 'configuration', relative)
            if file_hash(copied) != item.get('sha256'):
                raise ValueError('备份配置摘要不符')
            parsed = tomllib.loads(copied.read_text(encoding='utf-8'))
            reject_credentials(parsed)
            if relative == 'configs/default.toml':
                default = parsed
        if default is None:
            raise ValueError('备份缺少必要的非密钥配置')
        database = local_path(target, default['paths']['database'])
        if not database.is_relative_to(target / 'data/state'):
            raise ValueError('备份账本路径必须保存于data/state下')
        # Verify every copied evidence byte before creating the recovery tree.
        layouts, _ = _backup_evidence_layouts(folder / 'evidence', manifest['evidence']['files'])
        with closing(readonly_connection(source)) as db:
            if _strategy_integrity(folder / 'evidence', db):
                raise ValueError('备份政策或决策索引与原件不一致')
        target.mkdir(parents=True, exist_ok=True)
        database.parent.mkdir(parents=True)
        with closing(readonly_connection(source)) as db, closing(sqlite3.connect(database)) as dest:
            db.backup(dest)
        with closing(readonly_connection(database)) as verified:
            check_schema(verified, legacy=True)
            validate_accounts(verified, self.settings)
            balances = dict(verified.execute('SELECT account_id,SUM(amount_fen) FROM cash_entries GROUP BY account_id'))
            if balances != manifest['balances_fen']:
                raise ValueError('恢复后的账户余额与备份不符')
        for item in manifest['evidence']['files']:
            dest = local_path(target, item['path'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            layout = layouts[item['path']]
            if layout['legacy_compressed']:
                with dest.open('wb') as stream:
                    for chunk in evidence_objects.iter_bytes(folder / 'evidence', layout['object_ref']):
                        stream.write(chunk)
            else:
                shutil.copyfile(folder / 'evidence' / item['path'], dest)
            if file_hash(dest) != item['sha256']:
                raise ValueError('恢复后的证据文件摘要不符')
        for item in configuration:
            dest = local_path(target, item['path'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder / 'configuration' / item['path'], dest)
            if file_hash(dest) != item['sha256']:
                raise ValueError('恢复后的配置摘要不符')
        missing_market = bool(manifest['evidence']['market_snapshots'])
        receipt = {'state': 'RESTORED_AUDIT_PARTIAL' if missing_market or manifest['evidence']['issues'] else 'RESTORED',
                   'backup_id': backup_id, 'restored_at': self._now().isoformat(),
                   'target': str(target), 'market_archive_required': missing_market,
                   'reason': '市场原件须由数据层独立恢复和核验' if missing_market else None}
        write_json(target / 'data/state/restore-receipt.json', receipt)
        return receipt

    def _correct_fee_cost(self, tx, reference, correction, delta):
        """Bounded FIFO cost adjustment for a plain buy lot, preserving all original fills."""
        identity, day = correction['correction_id'], correction['effective_date']
        fill = tx.get('fills', reference['reference_id'])
        if fill is None or reference['kind'] not in {'COMMISSION_FEN', 'STAMP_FEN', 'TRANSFER_FEN'}:
            raise LedgerError('CONFLICT', '费用更正必须关联已确认成交的费用分项')
        if correction['new_amount_fen'] > 0:
            raise LedgerError('INVALID_INPUT', '调整后交易费用不能为正现金收入')
        metadata = {'pnl_adjustment_fen': 0, 'transaction_fee_adjustment_fen': -delta}
        if fill['side'] == 'SELL':
            return {**metadata, 'realized_pnl_adjustment_fen': delta}
        lot_id = f'buy:{fill["order_id"]}'
        lot = tx.get('position_lots', lot_id)
        if lot is None:
            raise LedgerError('CONFLICT', '买入费用没有可核查原批次')
        movements = tx.list('lot_movements', lot_id=lot_id)
        allowed = {'BUY', 'SELL', 'FEE_COST_ADJUSTMENT', 'FEE_SALE_COST_ADJUSTMENT'}
        if any(m.get('kind') not in allowed for m in movements):
            raise LedgerError('CONFLICT', '批次涉及送转或复杂成本链，费用更正须维护核查')
        buys = [m for m in movements if m['kind'] == 'BUY']
        if len(buys) != 1:
            raise LedgerError('CONFLICT', '原买入批次不唯一')
        acquired = buys[0]['effective_date']
        if day != acquired:
            raise LedgerError('CONFLICT', '费用更正有效日必须对应原买入日')
        initial_quantity = buys[0]['quantity_delta']
        basis = buys[0]['cost_delta_fen'] + sum(m['cost_delta_fen'] for m in movements
                                               if m['kind'] == 'FEE_COST_ADJUSTMENT')
        new_basis = basis - delta
        if new_basis < 0:
            raise LedgerError('CONFLICT', '费用更正导致批次成本为负')
        old_remaining, new_remaining, quantity = basis, new_basis, initial_quantity
        allocations = []
        sells = sorted((m for m in movements if m['kind'] == 'SELL'),
                       key=lambda m: (m['effective_date'], movements.index(m)))
        for movement in sells:
            sold = -movement['quantity_delta']
            if sold <= 0 or sold > quantity:
                raise LedgerError('CONFLICT', '历史FIFO消耗数量矛盾')
            old_cost = old_remaining if sold == quantity else int((Decimal(old_remaining) * sold / quantity).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            new_cost = new_remaining if sold == quantity else int((Decimal(new_remaining) * sold / quantity).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            allocations.append((movement, new_cost - old_cost))
            old_remaining -= old_cost
            new_remaining -= new_cost
            quantity -= sold
        if quantity != lot['quantity'] or old_remaining != lot['cost_fen']:
            raise LedgerError('CONFLICT', '当前批次成本与历史FIFO不一致')
        lot['cost_fen'] = new_remaining
        tx.save('position_lots', lot_id, lot)
        movement_id = f'fee-cost:{digest([tx.account_id, identity])}'
        tx.append('lot_movements', movement_id, {
            'movement_id': movement_id, 'lot_id': lot_id, 'code': lot['code'],
            'effective_date': acquired, 'quantity_delta': 0, 'cost_delta_fen': -delta,
            'kind': 'FEE_COST_ADJUSTMENT', 'reference_id': identity})
        for movement, cost_change in allocations:
            if not cost_change:
                continue
            part_id = f'fifo:{digest([identity, movement["id"]])}'
            tx.append('lot_movements', f'fee-sale:{part_id}', {
                'movement_id': f'fee-sale:{part_id}', 'lot_id': lot_id, 'code': lot['code'],
                'effective_date': movement['effective_date'], 'quantity_delta': 0,
                'cost_delta_fen': -cost_change, 'kind': 'FEE_SALE_COST_ADJUSTMENT',
                'reference_id': identity})
            tx.append('corrections', part_id, {
                'correction_id': part_id, 'parent_correction_id': identity,
                'kind': 'FIFO_FEE_COST', 'effective_date': movement['effective_date'],
                'realized_cost_adjustment_fen': -cost_change, 'pnl_adjustment_fen': 0,
                'delta_fen': 0, 'reason': correction['reason'], 'processed_at': tx.now,
                'lot_id': lot_id, 'reference_id': movement['reference_id']})
        return {**metadata, 'lot_id': lot_id, 'remaining_cost_adjustment_fen': new_remaining-old_remaining}

    def apply_correction(self, account_id, correction, *, operation_id, expected_revision):
        """Apply a bounded confirmed amount/fee difference, never replay a past trade."""
        day = correction.get('effective_date')

        def apply(tx):
            identity = correction.get('correction_id')
            kind = correction.get('kind')
            if kind not in {'CASH_ADJUSTMENT', 'FEE_ADJUSTMENT'}:
                raise LedgerError('CONFLICT', '该更正需要维护核查，不自动更改股份或成交成立性')
            if not identity or not correction.get('reason') or not correction.get('reference_id'):
                raise LedgerError('INVALID_INPUT', '更正须含身份、原因和原记录关联')
            old = tx.get('corrections', identity)
            if old:
                raise LedgerError('CONFLICT', '更正ID已经处理')
            reference = tx.get('cash_entries', correction['reference_id'])
            if reference is None:
                raise LedgerError('INVALID_INPUT', '更正关联的原现金流水不存在')
            before, after = correction.get('old_amount_fen'), correction.get('new_amount_fen')
            revised_amount = reference['amount_fen'] + sum(
                item.get('delta_fen', 0) for item in tx.list('corrections')
                if item.get('reference_id') == correction['reference_id'])
            if type(before) is not int or type(after) is not int or revised_amount != before:
                raise LedgerError('CONFLICT', '更正原值不匹配或已有更正')
            if reference['kind'] == 'INITIAL_CAPITAL':
                raise LedgerError('CONFLICT', '不能通过更正改变初始本金')
            delta = after - before
            if reference.get('effective_date') != day or day > self._now().astimezone(self.settings.timezone).date().isoformat():
                raise LedgerError('INVALID_INPUT', '更正须使用原流水有效日且不能提前入账')
            if kind == 'CASH_ADJUSTMENT' and reference['kind'] in {'BUY', 'SELL', 'COMMISSION_FEN', 'STAMP_FEN', 'TRANSFER_FEN'}:
                raise LedgerError('CONFLICT', '成交本金不可自动更改，费用须使用费用更正')
            for plan in tx.list('plans'):
                target = plan.get('target_session', '')
                orders = tx.list('orders', plan_id=plan.get('plan_id', plan['id']))
                pending = any(order.get('status') not in {'FILLED', 'NOT_FILLED'} for order in orders)
                unresolved_plan = not orders and plan.get('status', plan.get('state')) != 'EMPTY'
                if (target >= day and (pending or unresolved_plan)) or (target > day and delta < 0):
                    raise LedgerError('CONFLICT', '更正影响后续或未完成冻结计划，须维护核查')
            self._verify_evidence(correction, required=True)
            if not delta:
                raise LedgerError('INVALID_INPUT', '更正没有金额差异')
            if tx.balance() + delta < 0:
                raise LedgerError('CONFLICT', '更正会造成负现金')
            cost_adjustment = self._correct_fee_cost(tx, reference, correction, delta) if kind == 'FEE_ADJUSTMENT' else {}
            tx.cash(f'correction:{digest([account_id, identity])}', kind, delta, day,
                    correction['reference_id'], {'correction_id': identity, 'reason': correction['reason']})
            record = {**correction, 'delta_fen': delta, 'processed_at': tx.now,
                      'affected_from': day, 'status': 'APPLIED', **cost_adjustment}
            tx.append('corrections', identity, record)
            # Flag affected published statements for an explicit new revision, retaining originals.
            for statement in tx.list('daily_nav'):
                if statement.get('session', statement.get('target_session', '')) >= day:
                    tx.save('job_runs', f'correction:{identity}:{statement["id"]}', {
                        'kind': 'RESTATE_SESSION', 'session': statement.get('session', statement.get('target_session')),
                        'state': 'PENDING', 'correction_id': identity, 'reason': correction['reason']})
            affected = [statement.get('session', statement.get('target_session'))
                        for statement in tx.list('daily_nav')
                        if statement.get('session', statement.get('target_session', '')) >= day]
            if affected:
                tx.state['restate_from'] = min(tx.state.get('restate_from') or day, day)
                tx.state['restate_through'] = max(tx.state.get('restate_through') or day, *affected)
            prior = tx.state.get('settled_through')
            if prior and prior >= day:
                previous = self.market.calendar_operation('previous_session', day) if self.market else None
                tx.state['settled_through'] = previous.data if previous and previous.ready else None
                tx.state['valued_through'] = tx.state['settled_through']
            return {'state': 'APPLIED', 'correction_id': identity, 'delta_fen': delta,
                    'requires_statement_revision': True}

        return self._mutate(account_id, operation_id, correction, expected_revision, day, apply)
