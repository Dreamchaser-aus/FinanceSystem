import unittest
from datetime import datetime, timezone, timedelta
from closing_engine import next_boundary,period_contains,validate_continuity
U=timezone.utc
class TestBoundaries(unittest.TestCase):
    def test_dst_forward_23_hours(self):
        a=datetime(2026,10,3,21,0,tzinfo=U)
        b=next_boundary(a,7,0,True,600)
        self.assertEqual((b-a).total_seconds()/3600,23)
    def test_dst_backward_25_hours(self):
        a=datetime(2026,4,3,20,0,tzinfo=U)
        b=next_boundary(a,7,0,True,600)
        self.assertEqual((b-a).total_seconds()/3600,25)
    def test_exclusive_end(self):
        a=datetime(2026,10,3,21,tzinfo=U);b=a+timedelta(hours=25)
        self.assertTrue(period_contains(a,b,a));self.assertFalse(period_contains(a,b,b))
        self.assertTrue(validate_continuity([(a,b),(b,b+timedelta(hours=24))]))
        self.assertFalse(validate_continuity([(a,b),(b-timedelta(seconds=1),b+timedelta(hours=24))]))
if __name__=='__main__':unittest.main()
