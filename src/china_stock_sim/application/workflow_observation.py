"""Operational observations around whole stages, including expensive prechecks."""

from functools import wraps
from inspect import signature


def stage_outcome(phase, result):
    """Summarize nested public receipts; committing a provisional close is not FINAL."""
    if not isinstance(result, dict):
        return {'state': 'COMPLETE'}
    if phase == 'close':
        statement = result.get('statement') or {}
        receipt = result.get('receipt') or {}
        if receipt.get('state') in {'FAILED', 'ERROR', 'INTEGRITY_ERROR', 'INVALID_INPUT'}:
            return {'state': 'FAILED', 'reason': 'CLOSE_RECEIPT_FAILED'}
        return {'state': 'COMPLETE' if statement.get('accounting_status') == 'COMPLETE'
                and statement.get('valuation_status') == 'FINAL' else 'WAITING_DATA',
                'accounting_status': statement.get('accounting_status'),
                'valuation_status': statement.get('valuation_status')}
    if phase in {'entitlements_before', 'entitlements_after'}:
        receipts = [*result.get('registrations', []), *result.get('actions', [])]
        if result.get('tax'):
            receipts.append(result['tax'])
        states = [(row.get('result') or row).get('state') for row in receipts]
        if any(state in {'FAILED', 'ERROR', 'INTEGRITY_ERROR', 'INVALID_INPUT'} for state in states):
            return {'state': 'FAILED', 'reason': 'ENTITLEMENT_RECEIPT_FAILED'}
        if result.get('registration_wait') or any(state not in {
                'APPLIED', 'ALREADY_APPLIED', 'NOT_APPLICABLE', 'COMPLETE', 'SKIPPED'} for state in states):
            return {'state': 'WAITING_DATA', 'reason': 'ENTITLEMENTS_PENDING'}
        return {'state': 'COMPLETE'} if states else {'state': 'SKIPPED', 'reason': 'NO_WORK_REQUIRED'}
    if phase == 'execution' and result.get('completion_status') in {'PARTIAL', 'WAITING', 'ERROR'}:
        return {'state': 'FAILED' if result['completion_status'] == 'ERROR' else 'WAITING_DATA',
                'reason': 'EXECUTION_PENDING'}
    return result


def observed(phase):
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def call(self, *args, **kwargs):
            values = parameters.bind(self, *args, **kwargs).arguments
            schedule = values.get('schedule') or {}
            return self._observe(values.get('phase', phase),
                lambda: function(self, *args, **kwargs),
                session=values.get('day') or schedule.get('completed_session'),
                target_session=values.get('target') or schedule.get('planning_target'),
                account_id=values.get('account'))
        return call
    return decorate
