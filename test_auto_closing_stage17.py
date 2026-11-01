import unittest
from pathlib import Path
from auto_closing_stage17 import check_schema, REQUIRED

class Stage17Tests(unittest.TestCase):
    def test_missing_tables(self):
        self.assertIn('transaction', check_schema({}))
    def test_complete_schema(self):
        self.assertEqual(check_schema(REQUIRED), {})
    def test_missing_field(self):
        schema = {k:set(v) for k,v in REQUIRED.items()}
        schema['entry'].remove('delta')
        self.assertEqual(check_schema(schema)['entry'], ['delta'])
    def test_read_only(self):
        src=Path(__file__).with_name('auto_closing_stage17.py').read_text()
        self.assertNotIn('conn.execute(text("INSERT',src)
        self.assertNotIn('conn.execute(text("UPDATE',src)
        self.assertNotIn('conn.execute(text("DELETE',src)
    def test_overlap_check(self):
        src=Path(__file__).with_name('auto_closing_stage17.py').read_text()
        self.assertIn('a.start_utc < b.end_utc',src)

if __name__ == '__main__': unittest.main()
