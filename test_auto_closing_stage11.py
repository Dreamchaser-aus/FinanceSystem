import unittest
from unittest.mock import patch
from auto_closing_stage11 import require_verified_accounting

class Stage11SafetyTests(unittest.TestCase):
    def test_blocks_known_accounting_issues(self):
        with self.assertRaisesRegex(RuntimeError, 'ACTUAL_IS_LEDGER'):
            require_verified_accounting()
    def test_blocks_unverified_postgres_even_without_blockers(self):
        with patch('auto_closing_stage11.audit_source', return_value={'issues': [], 'database_integration_tested': False}):
            with self.assertRaisesRegex(RuntimeError, 'POSTGRES_INTEGRATION'):
                require_verified_accounting()
    def test_stage5_and_stage6_write_paths_have_gate(self):
        from pathlib import Path
        root=Path(__file__).resolve().parent
        for filename in ('auto_closing_stage5.py','auto_closing_stage6.py'):
            self.assertIn('require_verified_accounting()', (root/filename).read_text())

if __name__=='__main__': unittest.main()
