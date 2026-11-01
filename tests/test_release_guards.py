import unittest
from unittest.mock import Mock, MagicMock
from release_guards import install_postgres_guards

class GuardTests(unittest.TestCase):
    def test_sqlite_skips_postgres_ddl(self):
        db=MagicMock()
        db.engine.dialect.name='sqlite'
        self.assertFalse(install_postgres_guards(db))
        db.engine.begin.assert_not_called()

    def test_postgres_installs_all_three_triggers(self):
        db=MagicMock()
        db.engine.dialect.name='postgresql'
        conn=db.engine.begin.return_value.__enter__.return_value
        self.assertTrue(install_postgres_guards(db))
        sql='\n'.join(str(call.args[0]) for call in conn.execute.call_args_list)
        self.assertIn('CREATE OR REPLACE FUNCTION',sql)
        for table in ('closing','closing_period','closing_financial_snapshot'):
            self.assertIn('ON '+table,sql)
        self.assertEqual(conn.execute.call_count,7)
