"""Closing at Melbourne 00:00: UTC instants across AEDT, AEST and both DST switches."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from datetime import date
from closing_engine import next_boundary, period_business_day

UTC = timezone.utc
MEL = ZoneInfo('Australia/Melbourne')

def nb(y, m, d, h=0):
    return next_boundary(datetime(y, m, d, h, tzinfo=UTC), 0, 0, True, 600)

class MelbourneMidnight(unittest.TestCase):
    def test_summer_aedt_is_13_utc(self):
        self.assertEqual(nb(2026, 11, 10, 5), datetime(2026, 11, 10, 13, tzinfo=UTC))
    def test_winter_aest_is_14_utc(self):
        self.assertEqual(nb(2026, 7, 10, 5), datetime(2026, 7, 10, 14, tzinfo=UTC))
    def test_every_boundary_is_local_midnight(self):
        for m in (1, 4, 7, 10, 12):
            self.assertEqual(nb(2026, m, 15, 3).astimezone(MEL).time().isoformat(), '00:00:00')
    def test_dst_start_day_is_23h(self):   # 2026-10-04
        s, e = nb(2026, 10, 2, 20), nb(2026, 10, 3, 15)
        self.assertEqual((e - s).total_seconds() / 3600, 23)
    def test_dst_end_day_is_25h(self):     # 2026-04-05
        s, e = nb(2026, 4, 3, 15), nb(2026, 4, 4, 14)
        self.assertEqual((e - s).total_seconds() / 3600, 25)
    def test_exact_boundary_moves_to_next_day(self):
        b = datetime(2026, 11, 10, 13, tzinfo=UTC)
        self.assertEqual(next_boundary(b, 0, 0), datetime(2026, 11, 11, 13, tzinfo=UTC))
class ClosingBusinessDay(unittest.TestCase):
    def test_midnight_close_labels_previous_day(self):
        end = datetime(2026, 11, 1, 13, tzinfo=UTC)          # 2 Nov 00:00 AEDT
        self.assertEqual(period_business_day(end, True, 600), date(2026, 11, 1))
    def test_winter_midnight_close_labels_previous_day(self):
        end = datetime(2026, 7, 10, 14, tzinfo=UTC)          # 11 Jul 00:00 AEST
        self.assertEqual(period_business_day(end, True, 600), date(2026, 7, 10))
    def test_dst_start_and_end_days(self):
        self.assertEqual(period_business_day(datetime(2026, 10, 4, 13, tzinfo=UTC)), date(2026, 10, 4))
        # DST ends 5 Apr: 5 Apr 00:00 AEDT = 4 Apr 13:00 UTC; 6 Apr 00:00 AEST = 5 Apr 14:00 UTC
        self.assertEqual(period_business_day(datetime(2026, 4, 4, 13, tzinfo=UTC)), date(2026, 4, 4))
        self.assertEqual(period_business_day(datetime(2026, 4, 5, 14, tzinfo=UTC)), date(2026, 4, 5))
    def test_chain_of_closings_covers_each_day_once(self):
        start = datetime(2026, 10, 2, 14, tzinfo=UTC)         # 3 Oct 00:00 AEST
        days = []
        for _ in range(4):
            start = next_boundary(start, 0, 0, True, 600)
            days.append(period_business_day(start))
        self.assertEqual(days, [date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6)])

if __name__ == '__main__': unittest.main()
