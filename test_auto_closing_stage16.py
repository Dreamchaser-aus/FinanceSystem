import unittest
from pathlib import Path
from auto_closing_stage14 import prepare_snapshot

class Stage16Tests(unittest.TestCase):
    def test_accounting_model(self):
        x=prepare_snapshot('1000','500','200','100')
        self.assertEqual(str(x.ledger_closing),'1300.00')
        self.assertEqual(str(x.operating_closing),'1200.00')
    def test_read_only_source(self):
        source=Path(__file__).with_name('auto_closing_stage16.py').read_text()
        self.assertNotIn('INSERT INTO',source)
        self.assertNotIn('UPDATE ',source)
        self.assertNotIn('DELETE FROM',source)
    def test_strict_exclusive_boundary(self):
        source=Path(__file__).with_name('auto_closing_stage16.py').read_text()
        self.assertIn('t.created_at < :end',source)
        self.assertIn('t.created_at >= :start',source)
    def test_no_actual_bank_balance(self):
        source=Path(__file__).with_name('auto_closing_stage16.py').read_text()
        self.assertIn("'actual_bank_balance':None",source)

if __name__=='__main__': unittest.main()
