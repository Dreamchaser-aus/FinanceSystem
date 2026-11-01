import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parent

class Stage8SafetyTests(unittest.TestCase):
    def test_module_imports(self):
        import auto_closing_stage8
        self.assertTrue(callable(auto_closing_stage8.inspect_due))

    def test_read_only_queries(self):
        src = (ROOT / 'auto_closing_stage8.py').read_text()
        self.assertNotIn('engine.begin(', src)
        for word in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'TRUNCATE ', 'BANKFLOW_STAGE5_WRITE'):
            self.assertNotIn(word, src)

    def test_valid_python(self):
        ast.parse((ROOT / 'auto_closing_stage8.py').read_text())

    def test_limit_validation(self):
        from auto_closing_stage8 import inspect_due
        with self.assertRaises(ValueError):
            inspect_due('not-used', max_periods=0)
        with self.assertRaises(ValueError):
            inspect_due('not-used', max_periods=32)

if __name__ == '__main__':
    unittest.main()
