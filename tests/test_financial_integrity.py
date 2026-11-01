import unittest
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from financial_integrity import verify_period

class FinancialIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,10,4,0,tzinfo=timezone.utc)
        self.end=self.start+timedelta(hours=23)
    def test_valid(self):
        self.assertEqual(verify_period('100','20','5','115',self.start,self.end,self.start),Decimal('115.00'))
    def test_mismatch(self):
        with self.assertRaises(ValueError):verify_period(100,20,5,116,self.start,self.end)
    def test_gap(self):
        with self.assertRaises(ValueError):verify_period(100,20,5,115,self.start,self.end,self.start-timedelta(hours=1))
    def test_invalid_time(self):
        with self.assertRaises(ValueError):verify_period(100,20,5,115,self.end,self.start)
