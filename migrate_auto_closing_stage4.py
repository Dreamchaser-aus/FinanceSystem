"""V7.9.6.43 Stage 4 opt-in schema migration. No scheduler or financial writes.

Run manually on a backed-up TEST database: python migrate_auto_closing_stage4.py
"""
import os

DDL = [
    '''CREATE TABLE IF NOT EXISTS platform_closing_policy (
        platform_id VARCHAR(36) PRIMARY KEY REFERENCES platform(id) ON DELETE CASCADE,
        timezone_name VARCHAR(64) NOT NULL DEFAULT 'Australia/Melbourne',
        auto_timezone BOOLEAN NOT NULL DEFAULT TRUE,
        manual_offset_minutes INTEGER NOT NULL DEFAULT 600,
        closing_hour INTEGER NOT NULL DEFAULT 0 CHECK (closing_hour BETWEEN 0 AND 23),
        closing_minute INTEGER NOT NULL DEFAULT 0 CHECK (closing_minute BETWEEN 0 AND 59),
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (timezone_name = 'Australia/Melbourne' OR auto_timezone = FALSE)
    )''',
    '''CREATE TABLE IF NOT EXISTS auto_closing_verification (
        id VARCHAR(36) PRIMARY KEY,
        closing_period_id VARCHAR(36) NOT NULL UNIQUE REFERENCES closing_period(id),
        platform_id VARCHAR(36) NOT NULL REFERENCES platform(id),
        bank_id VARCHAR(36) NOT NULL REFERENCES bank(id),
        status VARCHAR(30) NOT NULL DEFAULT 'PENDING_VERIFICATION'
            CHECK (status IN ('PENDING_VERIFICATION','VERIFIED','DISPUTED')),
        system_balance NUMERIC(18,2) NOT NULL,
        actual_balance NUMERIC(18,2),
        difference NUMERIC(18,2),
        verified_by VARCHAR(36) REFERENCES "user"(id),
        verified_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK ((status = 'PENDING_VERIFICATION' AND actual_balance IS NULL
                AND difference IS NULL AND verified_by IS NULL AND verified_at IS NULL)
            OR (status <> 'PENDING_VERIFICATION' AND actual_balance IS NOT NULL
                AND difference IS NOT NULL AND verified_by IS NOT NULL AND verified_at IS NOT NULL))
    )''',
    'CREATE INDEX IF NOT EXISTS ix_auto_closing_verification_platform_status ON auto_closing_verification(platform_id,status)',
]


def migrate(connection):
    """Transactional and rerunnable. Does NOT enable automatic closing."""
    with connection:
        with connection.cursor() as cursor:
            for statement in DDL:
                cursor.execute(statement)
            cursor.execute('''INSERT INTO platform_closing_policy(platform_id)
                SELECT id FROM platform ON CONFLICT (platform_id) DO NOTHING''')
            cursor.execute('SELECT COUNT(*) FROM platform_closing_policy')
            return cursor.fetchone()[0]


if __name__ == '__main__':
    if os.environ.get('BANKFLOW_STAGE4_MIGRATE') != 'YES_TEST_DATABASE':
        raise SystemExit('Refusing migration: set BANKFLOW_STAGE4_MIGRATE=YES_TEST_DATABASE after backing up your TEST DB.')
    import psycopg2
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise SystemExit('DATABASE_URL is required')
    with psycopg2.connect(url) as conn:
        count = migrate(conn)
    print(f'Stage 4 schema ready; {count} platform policy rows. All auto-closing policies disabled.')
