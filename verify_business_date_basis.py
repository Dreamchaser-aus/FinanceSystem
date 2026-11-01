"""Read-only check: compare the old (created_at / UTC instant) and new (business_date)
closing basis for one Melbourne business day, per bank, and list the transactions
that fall on different sides of the two definitions (back-dated entries).

Usage:  python verify_business_date_basis.py 2026-11-01
Needs DATABASE_URL. Makes no writes.
"""
import os, sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PER_BANK_SQL = '''
SELECT e.bank_id,
  COALESCE(SUM(CASE WHEN t.business_date = :day THEN e.delta END),0) AS by_business_date,
  COALESCE(SUM(CASE WHEN t.created_at >= :start AND t.created_at < :end THEN e.delta END),0) AS by_created_at
FROM entry e JOIN "transaction" t ON t.id = e.transaction_id
WHERE t.status = 'confirmed'
GROUP BY e.bank_id
'''

DIFF_SQL = '''
SELECT t.id, t.reference, t.kind, t.business_date, t.created_at, t.signed_amount
FROM "transaction" t
WHERE t.status = 'confirmed'
  AND ((t.business_date = :day) <> (t.created_at >= :start AND t.created_at < :end))
  AND (t.business_date = :day OR (t.created_at >= :start AND t.created_at < :end))
ORDER BY t.created_at
'''

def day_bounds(day, zone='Australia/Melbourne'):
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(timezone.utc)
    return start, end

def main():
    from sqlalchemy import create_engine, text
    day = date.fromisoformat(sys.argv[1])
    start, end = day_bounds(day)
    uri = os.environ['DATABASE_URL']
    if uri.startswith('postgres://'): uri = 'postgresql://' + uri[len('postgres://'):]
    if uri.startswith('postgresql://'): uri = 'postgresql+psycopg://' + uri[len('postgresql://'):]
    eng = create_engine(uri)
    params = {'day': day, 'start': start, 'end': end}
    with eng.connect() as c:
        print(f'Business day {day}  UTC window [{start.isoformat()}, {end.isoformat()})')
        rows = c.execute(text(PER_BANK_SQL), params).all()
        bad = [r for r in rows if r.by_business_date != r.by_created_at]
        print(f'banks compared: {len(rows)}; banks whose movement differs: {len(bad)}')
        for r in bad: print(' ', r.bank_id, 'business_date=', r.by_business_date, 'created_at=', r.by_created_at)
        diffs = c.execute(text(DIFF_SQL), params).all()
        print(f'transactions on different sides of the two bases: {len(diffs)}')
        for d in diffs: print(' ', tuple(d))

if __name__ == '__main__':
    main()
