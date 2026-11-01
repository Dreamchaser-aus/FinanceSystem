import unittest
from datetime import datetime, timezone, timedelta
from auto_closing_stage12 import check_record


def sample(**kwargs):
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    r = dict(closing_period_id='period1', status='PENDING_VERIFICATION',
             system_balance='120', ledger_balance='120', pending_unclaimed='20',
             operating_pending='20', operating_balance='100',
             actual_balance=None, difference=None, start_utc=now,
             end_utc=now + timedelta(days=1))
    r.update(kwargs)
    return r


class Stage12Test(unittest.TestCase):
    def test_valid_pending(self):
        self.assertEqual(check_record(sample())['errors'], [])
    def test_pending_has_actual(self):
        self.assertIn('PENDING_HAS_ACTUAL', check_record(sample(actual_balance='120'))['errors'])
    def test_operating_mismatch(self):
        self.assertIn('OPERATING_BALANCE_MISMATCH', check_record(sample(operating_balance='120'))['errors'])
    def test_verified(self):
        self.assertEqual(check_record(sample(status='VERIFIED', actual_balance='120', difference='0'))['errors'], [])
    def test_disputed(self):
        self.assertEqual(check_record(sample(status='DISPUTED', actual_balance='121', difference='1'))['errors'], [])
    def test_bad_difference(self):
        self.assertIn('DIFFERENCE_MISMATCH', check_record(sample(status='DISPUTED', actual_balance='121', difference='0'))['errors'])
    def test_bad_status(self):
        self.assertIn('VERIFICATION_STATUS_MISMATCH', check_record(sample(status='VERIFIED', actual_balance='121', difference='1'))['errors'])

if __name__ == '__main__':
    unittest.main()
