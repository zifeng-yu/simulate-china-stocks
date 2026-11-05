"""Read-only export of verified immutable snapshot/source dependencies for backups."""

import hashlib
import json

from china_stock_sim.config import local_path
from china_stock_sim.market.repository import CorruptData
from china_stock_sim.market.storage import digest


def evidence_manifest(repository, snapshot_ids, *, observation_ids=()):
    if not isinstance(snapshot_ids, (list, tuple)) or len(snapshot_ids) > 1000:
        raise ValueError('快照清单必须为最多1000项的列表或元组')
    if any(not isinstance(value, str) or len(value) != 64
           or any(c not in '0123456789abcdef' for c in value) for value in snapshot_ids):
        raise ValueError('快照ID无效')
    if not isinstance(observation_ids, (list, tuple)) or len(observation_ids) > 100000:
        raise ValueError('观测清单必须为最多100000项的列表或元组')
    if any(not isinstance(value, str) or len(value) != 64
           or any(c not in '0123456789abcdef' for c in value) for value in observation_ids):
        raise ValueError('观测ID无效')
    identities = sorted(set(snapshot_ids))
    files, observations = {}, set()

    def source(reference):
        if reference in files:
            return
        path = local_path(repository.root, reference)
        if not path.is_relative_to(repository.objects):
            raise CorruptData('原件依赖不在数据对象目录')
        raw = path.read_bytes()
        body = json.loads(raw)
        if not isinstance(body, dict) or digest(body) != path.stem:
            raise CorruptData('原件依赖摘要不符')
        files[reference] = {'path': reference, 'sha256': hashlib.sha256(raw).hexdigest(),
                            'kind': 'source_object'}
        dependencies = body.get('dependencies', [])
        if not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies):
            raise CorruptData('原件依赖列表不合法')
        for dependency in dependencies:
            source(dependency)

    for identity in identities:
        # Verifies index identities, knowledge times, payloads, taxes and recursive evidence.
        envelope = repository.read_snapshot(identity)
        ids = envelope['body']['observation_ids']
        observations.update(ids)
        for row in repository.by_ids(ids):
            source(row['raw_ref'])
        reference = f'data/normalized/snapshots/{identity}.json'
        path = local_path(repository.root, reference)
        raw = path.read_bytes()
        latest = json.loads(raw)
        if latest != envelope or digest(latest['body']) != identity:
            raise CorruptData('导出期间快照发生变化')
        files[reference] = {'path': reference, 'sha256': hashlib.sha256(raw).hexdigest(),
                            'kind': 'market_snapshot'}
    observations.update(observation_ids)
    for row in repository.by_ids(list(dict.fromkeys(observation_ids))):
        source(row['raw_ref'])
    return {'state': 'READY', 'snapshot_ids': identities,
            'observation_ids': sorted(observations), 'files': [files[p] for p in sorted(files)],
            'index_required': {'path': 'data/state/market.sqlite3',
                               'observation_ids': sorted(observations)},
            'read_only': True}
