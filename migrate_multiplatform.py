"""Apply and verify V6 schema on deployment startup."""
import os
from pathlib import Path
import psycopg

dsn = os.environ.get("DATABASE_URL")
if not dsn:
    raise SystemExit("DATABASE_URL is required")
sql = (Path(__file__).resolve().parent / "migrations" / "002_multiplatform.sql").read_text(encoding="utf-8")
with psycopg.connect(dsn, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("""SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='platform_id' AND table_name IN ('bank','transaction','dealer')""")
        present = {r[0] for r in cur.fetchall()}
        missing = {'bank','transaction','dealer'} - present
        if missing:
            raise RuntimeError("V6 migration incomplete; missing platform_id: " + ", ".join(sorted(missing)))
        cur.execute("SELECT count(*) FROM platform")
        count = cur.fetchone()[0]
print(f"V6 schema verified: {count} platform(s), required platform_id columns present")

with psycopg.connect(dsn, autocommit=True) as conn:
    with conn.cursor() as cur:
        cur.execute((Path(__file__).resolve().parent / 'migrations' / '003_unclaimed_deposit.sql').read_text(encoding='utf-8'))
        cur.execute("SELECT to_regclass('public.unclaimed_deposit')")
        if cur.fetchone()[0] is None:
            raise RuntimeError('V6.1 unclaimed_deposit table missing')
print('V6.1 unclaimed deposit schema verified')
