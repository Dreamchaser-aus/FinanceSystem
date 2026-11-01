import os
import unittest
from unittest.mock import patch
from auto_closing_stage5 import write_due_periods

class Stage5SafetyTests(unittest.TestCase):
    def test_writer_disabled_by_default(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'disabled'):
                write_due_periods('postgresql://invalid','p','b')

    def test_wrong_opt_in_rejected(self):
        with patch.dict(os.environ,{'BANKFLOW_STAGE5_WRITE':'YES'},clear=True):
            with self.assertRaises(RuntimeError):
                write_due_periods('postgresql://invalid','p','b')

if __name__=='__main__': unittest.main()
