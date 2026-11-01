import os
import unittest
from unittest.mock import patch
from auto_closing_stage6 import money, verify

class TestStage6(unittest.TestCase):
    def test_money(self):
        self.assertEqual(str(money('10.20')), '10.20')
    def test_precision(self):
        with self.assertRaises(ValueError): money('10.001')
    def test_nan(self):
        with self.assertRaises(ValueError): money('NaN')
    def test_inf(self):
        with self.assertRaises(ValueError): money('Infinity')
    def test_write_guard(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError): verify('postgresql://invalid', 'p', 'u', '1.00', dry_run=False)

if __name__ == '__main__': unittest.main()
