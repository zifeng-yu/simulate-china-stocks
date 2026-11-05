"""Calendar wake scheduling: exact time projection, coverage, and no duplicate triggers."""

from collections import Counter
from datetime import time

import pytest
from test_scheduler import calendar

from china_stock_sim.scheduler.calendar_schedule import calendar_entries, next_calendar_check
from china_stock_sim.scheduler.planning import plan_schedule


def expanded(entries):
    return Counter(hour * 60 + entry['Minute']
                   for entry in entries
                   for hour in ([entry['Hour']] if 'Hour' in entry else range(24)))


def test_every_supported_cadence_has_unique_day_coverage_and_daily_priority():
    for minutes in range(1, 61):
        counts = expanded(calendar_entries(minutes, daily_times=(time(18, 10), time(18, 10))))
        assert set(counts.values()) == {1}
        assert 0 in counts and 1090 in counts
        regular = sorted(set(counts) - ({1090} if 1090 % minutes else set()))
        assert all(right - left == minutes for left, right in zip(regular, regular[1:]))
        assert 1 <= 1440 - regular[-1] <= minutes


def test_quarter_hour_upload_covers_both_slots_exactly_once():
    entries = calendar_entries(15)
    assert entries == [{'Minute': 0}, {'Minute': 15}, {'Minute': 30}, {'Minute': 45}]
    counts = expanded(entries)
    assert len(counts) == 96 and counts[9 * 60] == counts[21 * 60] == 1
    assert calendar_entries(15, daily_times=(time(9), time(21))) == entries
    assert calendar_entries(5, daily_times=(time(18, 10),)) == calendar_entries(5)


@pytest.mark.parametrize(('clock', 'expected'), [
    ('2026-11-05T10:07:00+08:00', '2026-11-05T10:15:00+08:00'),
    ('2026-11-05T10:15:00+08:00', '2026-11-05T10:30:00+08:00'),
    ('2026-11-05T10:38:00+08:00', '2026-11-05T10:45:00+08:00'),
    ('2026-11-05T18:09:59.999999+08:00', '2026-11-05T18:10:00+08:00'),
    ('2026-11-05T18:10:00+08:00', '2026-11-05T18:15:00+08:00'),
    ('2026-11-05T18:59:59+08:00', '2026-11-05T19:00:00+08:00'),
    ('2026-11-05T23:59:59+08:00', '2026-11-06T00:00:00+08:00'),
    ('2026-12-31T23:59:59+08:00', '2027-01-01T00:00:00+08:00'),
])
def test_actual_next_calendar_tick_not_elapsed_interval(clock, expected):
    assert next_calendar_check(clock, 15, daily_times=(time(18, 10),)).isoformat() == expected


def test_status_uses_same_ticks_even_without_market_calendar_and_preserves_cutoff():
    for rows in (calendar(), []):
        result = plan_schedule('2026-11-05T02:07:00+00:00', rows)
        assert result['next_check_at'] == '2026-11-05T10:15:00+08:00'
    # The cutoff remains a financial eligibility boundary, not an invented
    # launchd firing for a custom cadence which has no 09:00 calendar entry.
    result = plan_schedule('2026-11-05T08:59:30+08:00', calendar(), awake_check_minutes=7)
    assert result['next_check_at'] == '2026-11-05T09:06:00+08:00'
    assert result['planning_deadline'] == '2026-11-05T09:00:00+08:00'
    assert result['plan_allowed']


def test_non_hour_divisor_is_anchored_to_midnight_without_hourly_short_gap():
    assert next_calendar_check('2026-11-05T00:45:00+08:00', 45).hour == 1
    assert next_calendar_check('2026-11-05T00:45:00+08:00', 45).minute == 30
    assert next_calendar_check('2026-11-05T23:55:00+08:00', 7).isoformat() == \
        '2026-11-06T00:00:00+08:00'


@pytest.mark.parametrize('minutes', [True, False, 0, 61, -1, 15.0, '15', None])
def test_invalid_cadence_rejected_consistently(minutes):
    with pytest.raises(ValueError):
        calendar_entries(minutes)
    with pytest.raises(ValueError):
        next_calendar_check('2026-11-05T10:00:00+08:00', minutes)


@pytest.mark.parametrize('extra', ['18:10', 1090, time(18, 10, 1)])
def test_calendar_entries_reject_non_minute_daily_times(extra):
    with pytest.raises(ValueError):
        calendar_entries(15, daily_times=(extra,))
