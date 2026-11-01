"""V7.9.6.43 stage-2: pure auto-closing planner (no database writes).

Each plan is for one bank and one explicitly verified UTC anchor. Financial
snapshotting, transaction routing, and scheduler are deliberately NOT enabled.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from closing_engine import as_utc, next_boundary

@dataclass(frozen=True)
class ClosingPlan:
    platform_id: str
    bank_id: str
    start_utc: datetime
    end_utc: datetime
    status: str = 'PENDING_VERIFICATION'


def due_periods(platform_id, bank_id, anchor_utc, now_utc, *, hour=0,
                minute=0, auto_timezone=True, manual_offset_minutes=600,
                limit=31):
    """Return all elapsed closing windows; never infer a missing anchor.

    Half-open periods [start,end) make transactions at exactly end belong to
    the next business day. Caller must persist with unique bank/end constraint.
    """
    if not platform_id or not bank_id:
        raise ValueError('Platform and bank are required')
    if limit < 1 or limit > 366:
        raise ValueError('Invalid period limit')
    start = as_utc(anchor_utc)
    instant = as_utc(now_utc)
    if start > instant:
        raise ValueError('Anchor is in the future')
    results = []
    for _ in range(limit):
        end = next_boundary(start, hour, minute, auto_timezone, manual_offset_minutes)
        if end > instant:
            break
        results.append(ClosingPlan(platform_id, bank_id, start, end))
        start = end
    else:
        if next_boundary(start, hour, minute, auto_timezone, manual_offset_minutes) <= instant:
            raise ValueError('Backlog exceeds limit; manual review required')
    return results


def period_for_instant(instant, *, hour=0, minute=0, auto_timezone=True,
                       manual_offset_minutes=600):
    """Return (previous_boundary,next_boundary) containing instant.

    DST-aware and deterministic at exact boundaries. Does not modify DB.
    """
    from datetime import timedelta
    instant = as_utc(instant)
    start = instant - timedelta(days=4)
    for _ in range(8):
        end = next_boundary(start, hour, minute, auto_timezone, manual_offset_minutes)
        if end > instant:
            return start, end
        start = end
    raise ValueError('Unable to find period')
