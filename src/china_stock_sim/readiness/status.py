"""Public, bounded local shutdown-readiness query; no business or cache writes."""

import traceback
from datetime import datetime, timedelta
from pathlib import Path

from china_stock_sim.common.fees import aware_time
from china_stock_sim.ledger.readiness import read_facts
from china_stock_sim.market.readiness_calendar import dependencies_current, read_calendar
from china_stock_sim.readiness import probe
from china_stock_sim.readiness.domain import TZ, calendar_window, envelope, evaluate, reason


def build_status(settings, now=None):
    now = aware_time(now or datetime.now(TZ)).astimezone(TZ)
    result = envelope(now)
    try:
        if (len(settings.accounts) != 3 or {s.id: s.strategy for s in settings.accounts}
                != {'S1': 'S1', 'S1-LT': 'S1-LT', 'LLM': 'LLM-Daily'}):
            return {**result, 'reasons': [reason('ACCOUNT_CONFIGURATION_INVALID')]}
        before_market = probe.market_version(settings, now)
        calendar = read_calendar(settings.root, knowledge_cutoff=now.isoformat(),
            start=(now.date() - timedelta(days=370)).isoformat(),
            end=(now.date() + timedelta(days=370)).isoformat())
        if not calendar.ready:
            return {**result, 'reasons': [reason('CALENDAR_UNAVAILABLE')]}
        window = calendar_window(calendar.data, now)
        result = envelope(now, window)
        if window['daytime']:
            return {**evaluate({'accounts': []}, now, window), 'accounts': [
                {'account_id': spec.id, 'ready': False, 'summary': '今晚尚未开始核验',
                 'reasons': [reason('DAYTIME_WINDOW')]} for spec in settings.accounts]}
        day, target = window['business_session'], window['target_session']
        before_journal = probe.journal_facts(settings, day, target, now)
        before_maintenance = probe.maintenance(settings)
        before_locks = probe.locks(settings)
        facts = read_facts(settings, day, target, now)
        after = read_facts(settings, day, target, now)
        after_market = probe.market_version(settings, now)
        after_journal = probe.journal_facts(settings, day, target, now)
        after_maintenance = probe.maintenance(settings)
        after_locks = probe.locks(settings)
        if (facts['fingerprint'] != after['fingerprint'] or before_market != after_market
                or before_journal != after_journal or before_locks != after_locks
                or before_maintenance != after_maintenance
                or not dependencies_current(settings.root, calendar)):
            return {**result, 'reasons': [reason('INPUT_CHANGED_DURING_PROBE')]}
        for account in facts['accounts']:
            review = before_journal['reviews'].get(account['account_id'])
            if review is None:
                account['reasons'].append('BUSINESS_REVIEW_UNAVAILABLE')
            elif before_market['latest_at'] is None or before_market['latest_at'] > review:
                account['reasons'].append('NEW_FACTS_PENDING_REVIEW')
        return evaluate(facts, now, window, operational=probe.operational(
            before_journal, before_locks, before_maintenance))
    except Exception as exc:
        # No exception text, source data, paths outside this project, or credentials.
        # Failure diagnostics remain finite and observable without writing logs.
        frames = [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)[-12:]]
        return {**result, 'reasons': [reason('PROBE_FAILED')],
                'diagnostic': {'exception_type': type(exc).__name__, 'component': 'readiness', 'stack': frames}}
