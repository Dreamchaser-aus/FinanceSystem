"""Per-platform dealer name constraint migration.

Dealer.name used to be unique across the whole system, so two platforms could not
both have a dealer called "Store". Names are now unique per platform only.

Idempotent and transactional: the per-platform constraint is added first, then any
single-column unique constraint on dealer.name is dropped. Existing dealers keep
their ids and platforms. Every old name was globally unique, so adding the
(platform_id, name) constraint can never conflict with existing data.
"""
import os

ADD_PLATFORM_UNIQUE = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname='uq_dealer_platform_name' AND conrelid='dealer'::regclass) THEN
    ALTER TABLE dealer ADD CONSTRAINT uq_dealer_platform_name UNIQUE (platform_id,name);
  END IF;
END $$;
"""

# Name-agnostic: the old constraint is normally dealer_name_key, but drop whatever
# single-column UNIQUE constraint covers "name".
DROP_GLOBAL_UNIQUE = """
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT c.conname FROM pg_constraint c
            WHERE c.conrelid='dealer'::regclass AND c.contype='u' AND array_length(c.conkey,1)=1
              AND (SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid=c.conrelid AND a.attnum=c.conkey[1])='name'
  LOOP
    EXECUTE 'ALTER TABLE dealer DROP CONSTRAINT ' || quote_ident(r.conname);
  END LOOP;
END $$;
"""

HAS_PLATFORM_UNIQUE = ("SELECT count(*) FROM pg_constraint "
                       "WHERE conname='uq_dealer_platform_name' AND conrelid='dealer'::regclass")
HAS_GLOBAL_UNIQUE = ("SELECT count(*) FROM pg_constraint c "
                     "WHERE c.conrelid='dealer'::regclass AND c.contype='u' AND array_length(c.conkey,1)=1 "
                     "AND (SELECT a.attname FROM pg_attribute a "
                     "WHERE a.attrelid=c.conrelid AND a.attnum=c.conkey[1])='name'")


def migrate():
    from sqlalchemy import create_engine, text
    url = os.environ['DATABASE_URL']
    if url.startswith('postgres://'):
        url = 'postgresql+psycopg://' + url[len('postgres://'):]
    elif url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url[len('postgresql://'):]
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(79641)'))
        conn.execute(text(ADD_PLATFORM_UNIQUE))
        conn.execute(text(DROP_GLOBAL_UNIQUE))
        if conn.execute(text(HAS_PLATFORM_UNIQUE)).scalar() != 1:
            raise RuntimeError('Per-platform dealer name constraint was not installed')
        if conn.execute(text(HAS_GLOBAL_UNIQUE)).scalar() != 0:
            raise RuntimeError('Global dealer name uniqueness is still present')
    engine.dispose()
    print('Per-platform dealer name constraint verified.')


if __name__ == '__main__':
    migrate()
