"""Per-platform bank name constraint migration."""
import os
from sqlalchemy import create_engine, text

def migrate():
    url = os.environ['DATABASE_URL']
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(79640)'))
        duplicates = conn.execute(text("SELECT platform_id, lower(name), count(*) FROM bank GROUP BY platform_id, lower(name) HAVING count(*) > 1 LIMIT 5")).all()
        if duplicates:
            raise RuntimeError(f'Duplicate bank names within a platform: {duplicates}')
        conn.execute(text("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_bank_platform_name' AND conrelid='bank'::regclass) THEN ALTER TABLE bank ADD CONSTRAINT uq_bank_platform_name UNIQUE (platform_id,name); END IF; END $$;"))
        conn.execute(text('ALTER TABLE bank DROP CONSTRAINT IF EXISTS bank_name_key'))
    print('Per-platform bank name constraint verified.')

if __name__ == '__main__':
    migrate()
