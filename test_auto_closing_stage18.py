"""Stage 18: disposable SQLite integration tests for Stage 16 calculation.
These are NOT PostgreSQL integration tests and do not enable production writes.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy import create_engine, text
from auto_closing_stage16 import compute_period
from auto_closing_stage17 import audit

class Stage18Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.url = 'sqlite:///' + os.path.join(self.tmp.name, 'fixture.db')
        engine = create_engine(self.url)
        with engine.begin() as c:
            for statement in [
                'CREATE TABLE bank (id INTEGER PRIMARY KEY, platform_id INTEGER, opening_balance NUMERIC)',
                'CREATE TABLE closing_period (id INTEGER PRIMARY KEY, bank_id INTEGER, start_utc DATETIME, end_utc DATETIME)',
                'CREATE TABLE "transaction" (id INTEGER PRIMARY KEY, platform_id INTEGER, created_at DATETIME, status TEXT, kind TEXT)',
                'CREATE TABLE entry (id INTEGER PRIMARY KEY, bank_id INTEGER, transaction_id INTEGER, delta NUMERIC)',
                'CREATE TABLE unclaim_allocation (id INTEGER PRIMARY KEY, bank_id INTEGER, platform_id INTEGER, amount NUMERIC, created_at DATETIME)',
            ]: c.execute(text(statement))
            c.execute(text('INSERT INTO bank VALUES (1, 10, 1000), (2, 20, 999)'))
            c.execute(text("INSERT INTO closing_period VALUES (1, 1, '2026-10-27 13:00:00', '2026-10-28 13:00:00')"))
            transactions = [
                (1,10,'2026-10-27 12:59:59','confirmed','deposit',50),
                (2,10,'2026-10-27 13:00:00','confirmed','deposit',200),
                (3,10,'2026-10-28 12:59:59','confirmed','withdrawal',-80),
                (4,10,'2026-10-28 13:00:00','confirmed','deposit',900),
                (5,10,'2026-10-27 14:00:00','pending','deposit',700),
                (6,10,'2026-10-27 15:00:00','confirmed','unclaim',100),
                (7,20,'2026-10-27 15:00:00','confirmed','deposit',999),
            ]
            for i,p,when,status,kind,delta in transactions:
                c.execute(text('INSERT INTO "transaction" VALUES (:i,:p,:when,:status,:kind)'),locals())
                c.execute(text('INSERT INTO entry VALUES (:i,:bank,:i,:delta)'),{'i':i,'bank':2 if i==7 else 1,'delta':delta})
            c.execute(text("INSERT INTO unclaim_allocation VALUES (1,1,10,30,'2026-10-28 10:00:00')"))
        engine.dispose()

    def tearDown(self): self.tmp.cleanup()

    def test_period_boundaries_and_accounting(self):
        r = compute_period(self.url,period_id=1,platform_id=10,bank_id=1)
        self.assertEqual(r['ledger_opening'],'1050.00')
        self.assertEqual(r['ledger_incoming'],'300.00')
        self.assertEqual(r['ledger_outgoing'],'80.00')
        self.assertEqual(r['ledger_closing'],'1270.00')
        self.assertEqual(r['pending_unclaimed'],'70.00')
        self.assertEqual(r['operating_closing'],'1200.00')
        self.assertIsNone(r['actual_bank_balance'])
        self.assertEqual(r['status'],'PENDING_VERIFICATION')
        self.assertFalse(r['written'])

    def test_cross_platform_rejected(self):
        with self.assertRaises(ValueError):
            compute_period(self.url,period_id=1,platform_id=20,bank_id=1)

    def test_read_only_audit(self):
        r = audit(self.url)
        self.assertTrue(r['schema_ready'])
        self.assertTrue(r['read_only'])
        self.assertEqual(r['counts']['cross_platform_entries'],0)
        self.assertEqual(r['counts']['overlapping_period_pairs'],0)

    def test_invalid_period_detected(self):
        engine=create_engine(self.url)
        with engine.begin() as c:
            c.execute(text("INSERT INTO closing_period VALUES (2,1,'2026-10-28 13:00:00','2026-10-27 13:00:00')"))
        engine.dispose()
        r=audit(self.url)
        self.assertEqual(r['counts']['invalid_period_boundaries'],1)

if __name__ == '__main__': unittest.main()
