import unittest
from auto_closing_stage13 import DDL, migrate


class Stage13Tests(unittest.TestCase):
    def test_actual_must_be_null(self):
        self.assertIn('CHECK (actual_bank_balance IS NULL)', DDL[0])

    def test_ledger_identity(self):
        self.assertIn('ledger_closing = ledger_opening + ledger_incoming - ledger_outgoing', DDL[0])

    def test_operating_identity(self):
        self.assertIn('operating_closing = ledger_closing - pending_unclaimed', DDL[0])

    def test_one_snapshot_per_period(self):
        self.assertIn('closing_period_id VARCHAR(36) PRIMARY KEY', DDL[0])

    def test_idempotent_ddl(self):
        self.assertTrue(all('IF NOT EXISTS' in statement for statement in DDL))

    def test_no_legacy_table_mutation(self):
        self.assertTrue(all('ALTER TABLE' not in statement and 'DROP TABLE' not in statement for statement in DDL))

    def test_write_gate_still_enabled(self):
        from pathlib import Path
        code = (Path(__file__).parent / 'auto_closing_stage5.py').read_text()
        self.assertIn('require_verified_accounting()', code)


if __name__ == '__main__':
    unittest.main()
