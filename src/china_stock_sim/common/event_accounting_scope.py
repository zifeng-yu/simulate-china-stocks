"""Conservative daily accounting scope, independent of source fact readiness."""

from datetime import date


def event_accounting_scope(event, day, cases=()):
    """Only a clearly unimplemented, unencumbered ordinary proposal is observation.

    REQUIRED means the existing accounting checks still apply, not permission to
    recognize an asset. Source records and their settlement readiness stay intact.
    """
    if (event.get('kind') not in {'DISTRIBUTION', 'NO_DISTRIBUTION'}
            or event.get('status') not in {'PROPOSED', 'CANCELLED'}
            or event.get('conflicts')):
        return 'REQUIRED'
    for field in ('record_date', 'ex_date', 'payment_date', 'share_listing_date'):
        value = event.get(field)
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value or value <= day:
                    return 'REQUIRED'
            except (TypeError, ValueError):
                return 'REQUIRED'
    aliases = {event.get('event_id'), *event.get('event_ids', ())}
    aliases.update(item.get('event_id') for item in event.get('evidence', ()))
    aliases.discard(None)
    for case in cases:
        if case.get('status') == 'MERGED' or case.get('code') != event.get('code'):
            continue
        if (aliases.intersection(case.get('event_ids', ())) or
                (event.get('proposal_date') and
                 case.get('proposal_date') == event['proposal_date'])):
            # Existing case identity/eligibility/history must be resolved by the
            # corporate-action lifecycle, including withdrawals of old facts.
            return 'REQUIRED'
    return 'OBSERVE' if event['status'] == 'PROPOSED' else 'INACTIVE'
