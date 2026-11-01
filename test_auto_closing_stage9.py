import unittest
from auto_closing_stage9 import REQUIRED, check_contract

class ContractTests(unittest.TestCase):
    def test_complete(self):
        self.assertTrue(check_contract(REQUIRED)['ready_for_stage5_sql'])
    def test_missing_table(self):
        data = dict(REQUIRED); data.pop('entry')
        result = check_contract(data)
        self.assertFalse(result['ready_for_stage5_sql'])
        self.assertIn('entry', result['missing_columns'])
    def test_missing_column(self):
        data = {k:set(v) for k,v in REQUIRED.items()}
        data['bank'].remove('opening_balance')
        self.assertEqual(check_contract(data)['missing_columns']['bank'], ['opening_balance'])

if __name__ == '__main__': unittest.main()
