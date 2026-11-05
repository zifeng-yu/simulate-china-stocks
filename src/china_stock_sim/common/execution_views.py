"""Bounded execution witnesses; historical results never nest inside new ones."""

from copy import deepcopy

from china_stock_sim.common.policies import policy_digest


def waiting_identity(value):
    relevant = {k: v for k, v in value.items() if k not in {
        'result_id', 'computed_at', 'processed_at', 'account_view_hash',
        'account_view_evidence', 'account_revision', 'expected_revision',
        'original_record_ref', 'storage_format', 'knowledge_cutoff', 'snapshot_at'}}
    if isinstance(relevant.get('data_evidence'), (list, tuple)):
        relevant['data_evidence'] = [{**query, 'kwargs': {
            key: child for key, child in query['kwargs'].items() if key != 'knowledge_cutoff'}}
            if isinstance(query, dict) and isinstance(query.get('kwargs'), dict) else query
            for query in relevant['data_evidence']]
    # Do not recursively strip source fields with the same spelling. Actual
    # results, source dates and observation identities remain fully binding.
    return policy_digest(relevant)


def plan_binding(plan):
    return {key: deepcopy(plan.get(key)) for key in (
        'plan_id', 'plan_hash', 'account_id', 'target_session', 'document')}


def result_summary(result):
    if not isinstance(result, dict):
        return None
    return {k: deepcopy(v) for k, v in result.items() if k not in {
        'account_view_evidence', 'data_evidence', 'execution_context',
        'original_record_ref', 'storage_format'}}


def order_projection(order):
    value = {k: deepcopy(v) for k, v in order.items() if k not in {
        'last_attempt', 'result', 'original_record_ref', 'storage_format'}}
    prior = order.get('last_attempt')
    if isinstance(prior, dict):
        value['last_attempt_identity'] = waiting_identity(prior)
    if isinstance(order.get('result'), dict):
        value['result'] = result_summary(order['result'])
    return value


def compact_view_evidence(view):
    """Hash-bind frozen identity/resources, current guards and bounded orders."""
    witness = {k: deepcopy(v) for k, v in view.items()
               if k not in {'frozen_plan', 'orders', 'view_hash', 'snapshot_at'}}
    witness.update(evidence_schema=2, frozen_plan_binding=plan_binding(view['frozen_plan']),
                   orders=[{**{k: deepcopy(v) for k, v in row.items()
                               if k not in {'order', 'result'}},
                            'order': order_projection(row['order']),
                            'result': result_summary(row.get('result'))}
                           for row in view['orders']])
    witness['view_hash'] = policy_digest(witness)
    witness['snapshot_at'] = view['snapshot_at']
    return witness


def stored_result(root, result):
    """Archive a validated witness once; economic fields remain directly readable."""
    from china_stock_sim.common.evidence_objects import is_reference, put_json
    value = dict(result)
    witness = value.get('account_view_evidence')
    if isinstance(witness, dict) and not is_reference(witness):
        value['account_view_evidence'] = put_json(root, witness)
    return value


def statement_record(root, row, table):
    """Daily statements bind authoritative rows without copying their replay trees."""
    from china_stock_sim.common.evidence_objects import put_json
    value = order_projection(row) if table == 'orders' else result_summary(row)
    value['record_reference'] = {'table': table, 'id': row['id'],
        'account_id': row['account_id'], 'content_hash': policy_digest(row),
        'snapshot': put_json(root, row)}
    return value
