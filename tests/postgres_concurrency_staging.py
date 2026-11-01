"""Standalone PostgreSQL locking/uniqueness stress probe.

Usage (ONLY a disposable isolated PostgreSQL database):
  BANKFLOW_DISPOSABLE_TEST_DATABASE_URL=postgresql://... python tests/postgres_concurrency_staging.py

Creates and removes a private test schema. Does not touch BankFlow application tables.
Not a substitute for end-to-end HTTP closing/claim concurrency tests.
"""
import os, uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import psycopg

url=os.environ.get('BANKFLOW_DISPOSABLE_TEST_DATABASE_URL','')
if not url:
    raise SystemExit('SKIP: Set BANKFLOW_DISPOSABLE_TEST_DATABASE_URL to a disposable PostgreSQL database.')
if not url.startswith(('postgresql://','postgres://')):
    raise SystemExit('Refusing non-PostgreSQL URL')
schema='bankflow_rc_'+uuid.uuid4().hex[:12]
with psycopg.connect(url,autocommit=True) as c:
    c.execute(f'CREATE SCHEMA {schema}')
try:
    with psycopg.connect(url,autocommit=True) as c:
        c.execute(f'CREATE TABLE {schema}.bank (id integer PRIMARY KEY, marker integer NOT NULL DEFAULT 0)')
        c.execute(f'CREATE TABLE {schema}.closing (bank_id integer NOT NULL, period_id integer NOT NULL, UNIQUE(bank_id,period_id))')
        c.execute(f'INSERT INTO {schema}.bank (id) VALUES (1)')
    barrier=Barrier(12)
    def attempt(_):
        with psycopg.connect(url) as c:
            barrier.wait(timeout=15)
            with c.transaction():
                c.execute(f'SELECT id FROM {schema}.bank WHERE id=1 FOR UPDATE')
                existing=c.execute(f'SELECT 1 FROM {schema}.closing WHERE bank_id=1 AND period_id=7').fetchone()
                if existing:return False
                c.execute(f'INSERT INTO {schema}.closing (bank_id,period_id) VALUES (1,7)')
                return True
    with ThreadPoolExecutor(max_workers=12) as pool:
        results=list(pool.map(attempt,range(12)))
    assert sum(results)==1, results
    with psycopg.connect(url) as c:
        assert c.execute(f'SELECT COUNT(*) FROM {schema}.closing').fetchone()[0]==1
    print('PASS: 12 concurrent simulated closing requests -> exactly one inserted')
finally:
    with psycopg.connect(url,autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
