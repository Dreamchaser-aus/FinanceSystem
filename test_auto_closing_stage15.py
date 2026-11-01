import os
import unittest
from unittest.mock import patch
from auto_closing_stage15 import persist_existing_period

ARGS = dict(database_url='postgresql://not-connected', period_id='p', platform_id='a',
            bank_id='b', opening='1000', incoming='500', outgoing='200', pending_unclaimed='100')

class Stage15Tests(unittest.TestCase):
    def test_dry_run_math(self):
        result = persist_existing_period(**ARGS)
        self.assertEqual(result['ledger_closing'], '1300.00')
        self.assertEqual(result['operating_closing'], '1200.00')
        self.assertIsNone(result['actual_bank_balance'])
        self.assertFalse(result['written'])

    def test_dry_run_no_database_connection(self):
        with patch('auto_closing_stage15.create_engine', side_effect=AssertionError('connected')):
            persist_existing_period(**ARGS)

    def test_write_requires_opt_in(self):
        with patch.dict(os.environ, {'BANKFLOW_STAGE15_WRITE': ''}):
            with self.assertRaises(RuntimeError):
                persist_existing_period(**ARGS, write=True)

    def test_invalid_pending_rejected(self):
        args = dict(ARGS, pending_unclaimed='2000')
        with self.assertRaises(ValueError):
            persist_existing_period(**args)

    def test_nonfinite_rejected(self):
        args = dict(ARGS, incoming='NaN')
        with self.assertRaises(ValueError):
            persist_existing_period(**args)

if __name__ == '__main__':
    unittest.main()
