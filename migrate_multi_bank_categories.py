"""Idempotent PostgreSQL migration to many-to-many bank category assignment."""
import os
from pathlib import Path
import psycopg
url=os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://',1)
sql=(Path(__file__).parent/'migrations/005_multi_bank_categories.sql').read_text()
with psycopg.connect(url,autocommit=True) as conn:
    conn.execute(sql,prepare=False)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conrelid='bank_label_assignment'::regclass AND conname='bank_label_assignment_pkey' AND array_length(conkey,1)=2")
        assert cur.fetchone()[0]==1,'Multi-category primary key not installed'
print('V7.9.6.7 multi-category schema verified')
