"""UTC continuous-period primitives. No historical boundary inference.

This module intentionally does not mutate existing Closing rows or transaction dates.
"""
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

UTC = timezone.utc
MYT = ZoneInfo('Asia/Kuala_Lumpur')
MELBOURNE = ZoneInfo('Australia/Melbourne')

def as_utc(dt):
    if dt.tzinfo is None:
        raise ValueError('A timezone-aware instant is required')
    return dt.astimezone(UTC)

def effective_timezone(auto_enabled=True, manual_offset_minutes=600):
    if auto_enabled:
        return MELBOURNE
    if not isinstance(manual_offset_minutes, int) or not -720 <= manual_offset_minutes <= 840 or manual_offset_minutes % 30:
        raise ValueError('Invalid manual GMT offset')
    return timezone(timedelta(minutes=manual_offset_minutes))

def next_boundary(previous_end_utc, hour, minute, auto_enabled=True, manual_offset_minutes=600):
    """Next occurrence of configured wall-clock closing, strictly after previous UTC end.

    Search dates in the target zone, roundtrip to reject nonexistent DST times,
    and consider both folds for ambiguous times. This is deterministic on UTC.
    """
    prev = as_utc(previous_end_utc)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError('Invalid closing clock')
    tz = effective_timezone(auto_enabled, manual_offset_minutes)
    local_day = prev.astimezone(tz).date()
    candidates = []
    for days in range(4):
        day = local_day + timedelta(days=days)
        for fold in (0, 1):
            local = datetime.combine(day, time(hour, minute)).replace(tzinfo=tz, fold=fold)
            instant = local.astimezone(UTC)
            if instant > prev and instant.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
                candidates.append(instant)
    if not candidates:
        raise ValueError('No next closing boundary found')
    return min(candidates)

def period_contains(start_utc, end_utc, transaction_utc):
    start, end, tx = map(as_utc, (start_utc, end_utc, transaction_utc))
    if end <= start:
        raise ValueError('Closing end must follow start')
    return start <= tx < end

def validate_continuity(periods):
    """Periods are (start_utc,end_utc) in chronological order."""
    last_end = None
    for start, end in periods:
        start, end = as_utc(start), as_utc(end)
        if end <= start or (last_end is not None and start != last_end):
            return False
        last_end = end
    return True
