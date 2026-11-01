import unittest
from decimal import Decimal
from auto_closing_stage14 import prepare_snapshot, verify_snapshot, amount

class Stage14Tests(unittest.TestCase):
    def test_pending_actual_is_none(self):
        s=prepare_snapshot('100','40','20','15')
        self.assertIsNone(s.actual_bank_balance)
        self.assertEqual(s.status,'PENDING_VERIFICATION')
        self.assertEqual(s.ledger_closing,Decimal('120.00'))
        self.assertEqual(s.operating_closing,Decimal('105.00'))
    def test_verification_equal(self):
        self.assertEqual(verify_snapshot(prepare_snapshot(100,40,20,15),120)['status'],'VERIFIED')
    def test_verification_disputed(self):
        v=verify_snapshot(prepare_snapshot(100,40,20,15),119)
        self.assertEqual(v['difference'],Decimal('-1.00'))
        self.assertEqual(v['status'],'DISPUTED')
    def test_negative_pending(self):
        with self.assertRaises(ValueError): prepare_snapshot(100,0,0,-1)
    def test_pending_exceeds_closing(self):
        with self.assertRaises(ValueError): prepare_snapshot(10,0,0,11)
    def test_reject_nonfinite(self):
        with self.assertRaises(ValueError): amount('NaN')
    def test_reject_excess_precision(self):
        with self.assertRaises(ValueError): amount('1.001')
    def test_no_mutation(self):
        s=prepare_snapshot(100,40,20,15)
        verify_snapshot(s,120)
        self.assertIsNone(s.actual_bank_balance)

if __name__=='__main__': unittest.main()
