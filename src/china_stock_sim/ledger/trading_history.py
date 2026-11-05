"""Read-only economic history for display, without financial evidence trees."""

import json
from zoneinfo import ZoneInfo

FIELDS = {
    'fills': ('fill_id', 'order_id', 'plan_id', 'target_session', 'effective_date',
              'processed_at', 'code', 'side', 'quantity', 'price', 'amount_fen',
              'fees_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'net_cash_fen',
              'cost_fen', 'realized_pnl_fen', 'state', 'status'),
    'position_lots': ('lot_id', 'code', 'quantity', 'cost_fen', 'original_quantity',
                     'original_cost_fen', 'acquired_date', 'sellable_date', 'tax_start_date',
                     'tax_start_status', 'source_order_id', 'source_case_id', 'source_lot_id'),
    'lot_movements': ('movement_id', 'lot_id', 'code', 'effective_date', 'kind',
                     'quantity_delta', 'cost_delta_fen', 'reference_id', 'acquired_date',
                     'tax_start_date', 'tax_start_status'),
    'cash_entries': ('kind', 'amount_fen', 'effective_date', 'recorded_at', 'reference_id'),
    'corrections': ('correction_id', 'kind', 'status', 'effective_date', 'processed_at',
                    'code', 'lot_id', 'case_id', 'reference_id', 'reference_fill_id',
                    'parent_correction_id', 'old_amount_fen', 'new_amount_fen', 'delta_fen',
                    'cash_adjustment_fen', 'pnl_adjustment_fen', 'realized_cost_adjustment_fen',
                    'realized_pnl_adjustment_fen', 'remaining_cost_adjustment_fen',
                    'transaction_fee_adjustment_fen'),
    'corporate_action_cases': ('case_id', 'code', 'status', 'proposal_date', 'record_date',
                              'ex_date', 'payment_date', 'share_listing_date', 'merged_into'),
    'entitlements': ('entitlement_id', 'case_id', 'code', 'kind', 'status', 'recognition_date',
                     'payment_date', 'paid_date', 'listing_date', 'delivery_date',
                     'amount_fen', 'paid_fen', 'quantity', 'delivered_quantity',
                     'cost_fen', 'remaining_cost_fen'),
    'tax_items': ('tax_id', 'case_id', 'code', 'status', 'assessed_fen', 'paid_fen',
                  'effective_date', 'trigger_date', 'paid_date', 'due_date', 'potential_quantity',
                  'rate', 'basis_yuan', 'basis_per_share', 'reason'),
}
ALLOCATION = ('lot_id', 'code', 'order_id', 'quantity', 'cost_fen', 'cash_fen',
              'original_quantity', 'acquired_date', 'effective_date', 'tax_start_date',
              'tax_start_status', 'basis_yuan', 'reason')
PAYMENT = ('effective_date', 'amount_fen', 'payment_id')
ASSESSMENT = ('effective_date', 'amount_fen', 'due_date', 'order_id', 'lot_id')
ARRAYS = {
    'fills': {'lot_allocations': ALLOCATION},
    'corporate_action_cases': {'eligibility': ALLOCATION},
    'entitlements': {'allocations': ALLOCATION, 'payments': PAYMENT},
    'tax_items': {'allocations': ALLOCATION, 'assessments': ASSESSMENT, 'payments': PAYMENT},
}
EVENT_FIELDS = ('event_id', 'code', 'kind', 'status', 'proposal_date', 'record_date',
                'ex_date', 'payment_date', 'share_listing_date')
POSITION_FIELDS = ('code', 'shares', 'cost_fen', 'receivable_quantity', 'receivable_cost_fen',
                   'price', 'price_date', 'status', 'market_value_fen', 'share_receivable_value_fen')
STATEMENT_FIELDS = ('id', 'account_id', 'session', 'statement_revision', 'account_revision',
                    'accounting_status', 'valuation_status', 'plan_status', 'opening_cash_fen',
                    'closing_cash_fen', 'cash_receivable_fen', 'share_receivable_value_fen',
                    'market_value_fen', 'tax_payable_fen', 'net_assets_fen', 'daily_pnl_fen',
                    'cumulative_pnl_fen', 'realized_pnl_fen', 'unrealized_pnl_fen',
                    'cumulative_dividend_income_fen', 'cumulative_tax_expense_fen',
                    'cumulative_transaction_fees_fen', 'correction_pnl_fen',
                    'requires_restatement', 'published_accounting_status',
                    'published_valuation_status', 'published_net_assets_fen')


def _scalars(row, fields):
    return {key: row[key] for key in fields if key in row
            and isinstance(row[key], (str, int, float, bool, type(None)))}


def _decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def _array(value, fields, issues, *, table, identity, field):
    try:
        value = _decoded(value)
    except (ValueError, TypeError):
        value = False
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        issues.append({'table': table, 'id': identity, 'field': field,
                       'reason': 'INVALID_ECONOMIC_ARRAY'})
        return None
    return [_scalars(item, fields) for item in value]


def _rows(tx, table, issues):
    """One account-scoped SQL projection per history table; no get/list decoding."""
    arrays = ARRAYS.get(table, {})
    extra = ('actions',) if table == 'corporate_action_cases' else ()
    expressions = ['entry_id AS id' if table == 'cash_entries' else 'id',
                   'account_id', 'rowid AS storage_sequence']
    expressions.extend(field if table == 'cash_entries' else
                       f"json_extract(data,'$.{field}') AS {field}"
                       for field in (*FIELDS[table], *arrays, *extra))
    if table == 'cash_entries':
        expressions.append("json_extract(details,'$.correction_id') AS _correction_id")
    if table == 'corrections':
        expressions.append("""CASE WHEN json_extract(data,'$.reason')='LATE_SHARE_COST_ALLOCATION'
            THEN 'LATE_SHARE_COST_ALLOCATION' ELSE NULL END AS reason""")
    if table == 'corporate_action_cases':
        expressions.extend(f"json_extract(data,'$.event.{field}') AS _event_{field}"
                           for field in EVENT_FIELDS)
        expressions.extend((
            """CASE WHEN json_type(data,'$.event.conflicts')='array'
                THEN json_array_length(data,'$.event.conflicts')>0
                ELSE json_type(data,'$.event.conflicts') IS NOT NULL
                     AND json_type(data,'$.event.conflicts')!='null' END AS _has_conflicts""",
            """json_type(data,'$.revision_candidate') IS NOT NULL
                AND json_type(data,'$.revision_candidate')!='null' AS _revision_pending"""))
    rows = [dict(row) for row in tx.db.execute(
        f"SELECT {','.join(expressions)} FROM {table} WHERE account_id=? ORDER BY rowid",
        (tx.account_id,))]
    for row in rows:
        for field, fields in arrays.items():
            row[field] = _array(row[field], fields, issues, table=table,
                                identity=row['id'], field=field)
        if table == 'corporate_action_cases':
            row['event'] = {field: row.pop(f'_event_{field}') for field in EVENT_FIELDS}
            row['event']['conflicts'] = ['RECORDED_CONFLICT'] if row.pop('_has_conflicts') else []
            row['revision_candidate'] = bool(row.pop('_revision_pending'))
            try:
                actions = _decoded(row.pop('actions'))
            except (ValueError, TypeError):
                actions = False
            if actions is not None and (not isinstance(actions, dict)
                    or any(not isinstance(action, dict) for action in actions.values())):
                issues.append({'table': table, 'id': row['id'], 'field': 'actions',
                               'reason': 'INVALID_ECONOMIC_ACTIONS'})
                row['actions'] = None
            else:
                row['actions'] = {name: _scalars(action, ('state', 'operation_date'))
                                  for name, action in (actions or {}).items()}
        if table == 'cash_entries':
            correction_id = row.pop('_correction_id')
            row['entry_id'] = row['id']
            row['details'] = {'correction_id': correction_id} if correction_id else {}
    return rows


def _statement(row, issues):
    result = _scalars(row, STATEMENT_FIELDS)
    result['positions'] = _array(row.get('positions'), POSITION_FIELDS, issues,
                                table='daily_nav', identity=row['id'], field='positions')
    if result['positions'] is None:
        if row.get('positions') is None:
            issues.append({'table': 'daily_nav', 'id': row['id'], 'field': 'positions',
                           'reason': 'MISSING_ECONOMIC_ARRAY'})
        result.update(positions=[], valuation_status='UNAVAILABLE', net_assets_fen=None,
                      daily_pnl_fen=None, cumulative_pnl_fen=None)
    return result


def _boundary(tx, before, issues):
    if before is None:
        return []
    fields = ','.join((f"json_extract(data,'$.{key}') AS {key}"
                       if key not in {'id', 'account_id'} else key)
                      for key in (*STATEMENT_FIELDS, 'positions'))
    row = tx.db.execute(f"""SELECT {fields} FROM daily_nav WHERE account_id=?
        AND json_extract(data,'$.session')<?
        ORDER BY json_extract(data,'$.session') DESC,
                 json_extract(data,'$.statement_revision') DESC LIMIT 1""",
        (tx.account_id, before)).fetchone()
    if row is None:
        return []
    result = _statement(dict(row), issues)
    marker = tx.state.get('restate_from')
    if marker and result['session'] >= marker:
        result.update(requires_restatement=True, accounting_status='PARTIAL',
                      valuation_status='PROVISIONAL', net_assets_fen=None,
                      daily_pnl_fen=None, cumulative_pnl_fen=None)
    return [result]


def trading_history_projection(tx, *, now, target_session, statements):
    """Complete recorded economics, with the report's transaction and revisions.

    Complete denotes retrieval, not validated tax/equity completion. Unresolved
    financial states are retained for the caller's conservative attribution.
    Boundary statements are calculation dependencies, never display-day additions.
    """
    issues = []
    values = {table: _rows(tx, table, issues) for table in FIELDS}
    selected = [_statement(row, issues) for row in statements]
    boundary = _boundary(tx, min((row['session'] for row in selected), default=None), issues)
    result = {'schema_version': 1, 'account_id': tx.account_id, 'snapshot_at': now.isoformat(),
              'account_revision': tx.state['account_revision'],
              'report_revision': tx.state['report_revision'], 'scope': 'all_time',
              'as_of_session': now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
              'target_session': target_session, 'fills': values['fills'],
              'lots': values['position_lots'], 'lot_movements': values['lot_movements'],
              'cash_entries': values['cash_entries'], 'corrections': values['corrections'],
              'cases': values['corporate_action_cases'], 'entitlements': values['entitlements'],
              'tax_items': values['tax_items'], 'statements': selected,
              'boundary_statements': boundary, 'issues': issues}
    result['row_counts'] = {key: len(value) for key, value in result.items()
                            if isinstance(value, list) and key != 'issues'}
    result.update(state='PARTIAL' if issues else 'READY', complete=not issues)
    return result
