import os
from pathlib import Path
import psycopg
url=os.environ['DATABASE_URL'].replace('postgresql+psycopg://','postgresql://',1)
sql=(Path(__file__).parent/'migrations/004_bank_labels.sql').read_text()
with psycopg.connect(url,autocommit=True) as conn:
    conn.execute(sql,prepare=False)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.bank_label'),to_regclass('public.bank_label_assignment')")
        assert all(cur.fetchone()),'Bank label tables missing'
print('V7.2 bank tags schema verified')
