import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from closing_engine import period_contains, validate_continuity, next_boundary

class BoundaryTests(unittest.TestCase):
    def test_half_open(self):
        a=datetime(2026,10,4,0,tzinfo=timezone.utc);b=a+timedelta(hours=1)
        self.assertTrue(period_contains(a,b,a))
        self.assertFalse(period_contains(a,b,b))
        self.assertTrue(period_contains(b,b+timedelta(hours=1),b))
    def test_continuity(self):
        a=datetime(2026,10,4,tzinfo=timezone.utc);b=a+timedelta(hours=1)
        self.assertTrue(validate_continuity([(a,b),(b,b+timedelta(hours=1))]))
        self.assertFalse(validate_continuity([(a,b),(b+timedelta(seconds=1),b+timedelta(hours=1))]))
    def test_dst_23_25(self):
        a=datetime(2026,10,3,21,0,tzinfo=timezone.utc)
        b=next_boundary(a,7,0,True,600)
        self.assertEqual((b-a).total_seconds()/3600,23)
        a=datetime(2026,4,3,20,0,tzinfo=timezone.utc)
        b=next_boundary(a,7,0,True,600)
        self.assertEqual((b-a).total_seconds()/3600,25)
if __name__=='__main__':unittest.main()
