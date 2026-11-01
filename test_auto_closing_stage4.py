import unittest
from migrate_auto_closing_stage4 import DDL

class Stage4SchemaTests(unittest.TestCase):
    def test_policy_disabled_by_default(self):
        self.assertIn('enabled BOOLEAN NOT NULL DEFAULT FALSE', DDL[0])
    def test_verification_unique_per_period(self):
        self.assertIn('closing_period_id VARCHAR(36) NOT NULL UNIQUE', DDL[1])
    def test_actual_balance_optional_until_verified(self):
        self.assertIn('actual_balance NUMERIC(18,2)', DDL[1])
        self.assertIn("status = 'PENDING_VERIFICATION' AND actual_balance IS NULL", DDL[1])
    def test_platform_isolation_columns(self):
        self.assertIn('platform_id VARCHAR(36) PRIMARY KEY', DDL[0])
        self.assertIn('platform_id VARCHAR(36) NOT NULL REFERENCES platform(id)', DDL[1])

if __name__ == '__main__': unittest.main()
