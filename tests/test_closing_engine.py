import unittest
from datetime import datetime, timezone, timedelta
from closing_engine import next_boundary, period_contains, validate_continuity
UTC=timezone.utc
class ClosingEngineTests(unittest.TestCase):
    def test_spring_forward_23_hours(self):
        a=datetime(2026,10,2,21,0,tzinfo=UTC) # 07:00 Melbourne (AEST) on Oct 3
        b=next_boundary(a,7,0)
        c=next_boundary(b,7,0)
        self.assertEqual((b-a).total_seconds()/3600,23)
        self.assertEqual((c-b).total_seconds()/3600,24)
    def test_fall_back_25_hours(self):
        a=datetime(2026,4,3,20,0,tzinfo=UTC) # 07:00 Melbourne (AEDT) on Apr 4
        b=next_boundary(a,7,0)
        self.assertEqual((b-a).total_seconds()/3600,25)
    def test_no_double_count_at_boundary(self):
        a=datetime(2026,10,3,0,tzinfo=UTC); b=a+timedelta(hours=23); c=b+timedelta(hours=24)
        self.assertFalse(period_contains(a,b,b))
        self.assertTrue(period_contains(b,c,b))
        self.assertTrue(validate_continuity([(a,b),(b,c)]))
        self.assertFalse(validate_continuity([(a,b),(b+timedelta(seconds=1),c)]))
    def test_manual_offset(self):
        a=datetime(2026,10,1,21,tzinfo=UTC)
        b=next_boundary(a,7,0,False,600)
        self.assertEqual((b-a).total_seconds()/3600,24)
if __name__=='__main__':unittest.main()
