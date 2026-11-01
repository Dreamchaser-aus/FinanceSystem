"""V7.9.6.43 TEST: non-mutating Melbourne closing schedule preview.

This tool DOES NOT create financial closings or change the database.
"""
import argparse
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
from closing_engine import next_boundary

UTC = timezone.utc
MEL = ZoneInfo('Australia/Melbourne')
MY = ZoneInfo('Asia/Kuala_Lumpur')

def business_day(instant):
    """Melbourne midnight business day for an aware UTC instant."""
    if instant.tzinfo is None:
        raise ValueError('aware timestamp required')
    return instant.astimezone(MEL).date()

def schedule(instant, count=5):
    if instant.tzinfo is None:
        raise ValueError('aware timestamp required')
    cursor = instant.astimezone(UTC)
    for _ in range(count):
        cursor = next_boundary(cursor, 0, 0, True, 600)
        yield {'utc': cursor.isoformat(), 'melbourne': cursor.astimezone(MEL).isoformat(),
               'malaysia': cursor.astimezone(MY).isoformat(), 'next_business_day': str(business_day(cursor))}

if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--at', help='ISO timestamp with UTC offset (default current UTC)')
    p.add_argument('--count',type=int,default=5)
    a=p.parse_args()
    instant=datetime.fromisoformat(a.at.replace('Z','+00:00')) if a.at else datetime.now(UTC)
    if instant.tzinfo is None or not 1 <= a.count <= 50:
        p.error('aware --at timestamp and count 1..50 required')
    print('Current business date:', business_day(instant))
    for row in schedule(instant,a.count): print(row)
