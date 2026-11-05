"""Read-only planning inputs shared by validation and ledger admission."""

from china_stock_sim.common.fees import aware_time
from china_stock_sim.common.wire import to_wire
from china_stock_sim.market.quality import normalize_code


def _query(reader, method, **kwargs):
    result = getattr(reader, method)(**kwargs)
    return {'method': method, 'kwargs': kwargs,
            'result': to_wire(result.as_dict() if hasattr(result, 'as_dict') else result)}


def collect_planning_market(reader, proposal):
    proposal = to_wire(proposal)
    cutoff = proposal['input_cutoff']
    aware_time(cutoff)
    day, target = proposal['market_as_of'], proposal['target_session']
    intents = proposal['ordered_intents']
    queries = [_query(reader, 'calendar_operation', operation='previous_session',
                      day=target, knowledge_cutoff=cutoff),
               _query(reader, 'calendar', start=target, end=target, knowledge_cutoff=cutoff)]
    if any(i.get('side') == 'BUY' for i in intents):
        queries.extend([
            _query(reader, 'calendar_operation', operation='window', day=day,
                   size=121, knowledge_cutoff=cutoff),
            _query(reader, 'pool', as_of=target, knowledge_cutoff=cutoff),
        ])
    codes = list(dict.fromkeys(normalize_code(i['code']) for i in intents
                              if i.get('side') in {'BUY', 'SELL'}))
    for code in codes:
        for method, args in (
            ('prices', {'code': code, 'start': day, 'end': day}),
            ('security', {'code': code}), ('events', {'code': code}),
            ('event_coverage', {'code': code, 'start': day, 'end': day}),
        ):
            queries.append(_query(reader, method, **args, knowledge_cutoff=cutoff))
    return tuple(queries)

