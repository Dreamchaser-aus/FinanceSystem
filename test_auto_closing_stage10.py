import unittest
from auto_closing_stage10 import audit_source

class Stage10SafetyGateTests(unittest.TestCase):
    def test_never_approves_untested_writes(self):
        self.assertFalse(audit_source()['safe_to_enable_auto_closing'])
    def test_detects_ledger_actual_conflation(self):
        self.assertIn('ACTUAL_IS_LEDGER', {i['code'] for i in audit_source()['issues']})
    def test_detects_verifier_field_mismatch(self):
        self.assertIn('VERIFIER_USES_ACTUAL_AS_LEDGER', {i['code'] for i in audit_source()['issues']})
    def test_has_actionable_remediation(self):
        self.assertGreaterEqual(len(audit_source()['next_actions']), 3)

if __name__ == '__main__': unittest.main()
