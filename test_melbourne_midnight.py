"""Closing at Melbourne 00:00: UTC instants across AEDT, AEST and both DST switches."""
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from closing_engine import next_boundary

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
if __name__ == '__main__': unittest.main()
