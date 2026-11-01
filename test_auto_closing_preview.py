import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from auto_closing_preview import business_day, schedule
MY=ZoneInfo('Asia/Kuala_Lumpur')
class TestClosingPreview(unittest.TestCase):
    def test_october_boundary(self):
        self.assertEqual(str(business_day(datetime(2026,10,27,20,59,tzinfo=MY))),'2026-10-27')
        self.assertEqual(str(business_day(datetime(2026,10,27,21,0,tzinfo=MY))),'2026-10-28')
    def test_winter_boundary(self):
        self.assertEqual(str(business_day(datetime(2026,6,10,21,0,tzinfo=MY))),'2026-06-10')
        self.assertEqual(str(business_day(datetime(2026,6,10,22,0,tzinfo=MY))),'2026-06-11')
    def test_schedule_dst(self):
        octrow=next(schedule(datetime(2026,10,27,12,tzinfo=timezone.utc)))
        junrow=next(schedule(datetime(2026,6,10,12,tzinfo=timezone.utc)))
        self.assertIn('21:00:00',octrow['malaysia'])
        self.assertIn('22:00:00',junrow['malaysia'])
    def test_reject_naive(self):
        with self.assertRaises(ValueError): business_day(datetime(2026,10,27))
if __name__=='__main__': unittest.main()
