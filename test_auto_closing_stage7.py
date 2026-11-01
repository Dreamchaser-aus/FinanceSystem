import unittest
from pathlib import Path

class Stage7Tests(unittest.TestCase):
    def test_prefix(self):
        self.assertIn("url_prefix='/admin/auto-closing'",Path('auto_closing_stage7.py').read_text())
    def test_admin_guard(self):
        source=Path('auto_closing_stage7.py').read_text()
        self.assertIn("current_user.role != 'admin'",source)
    def test_csrf(self):
        self.assertIn('csrf_token()',Path('templates/auto_closing_stage7.html').read_text())
    def test_disabled_write_by_default(self):
        self.assertIn("STAGE7_EXPERIMENTAL_WRITE",Path('auto_closing_stage7.py').read_text())
    def test_no_scheduler(self):
        self.assertNotIn('BackgroundScheduler',Path('auto_closing_stage7.py').read_text())
if __name__=='__main__': unittest.main()
