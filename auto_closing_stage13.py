"""Stage 13: opt-in, non-destructive TEST schema for auto-closing ledger snapshots.

This is a schema preparation module, NOT an auto-closing scheduler or writer.
The Stage 11 safety gate remains in force.
"""
import os

DDL = [
    '''CREATE TABLE IF NOT EXISTS auto_closing_ledger_snapshot (
        closing_period_id VARCHAR(36) PRIMARY KEY REFERENCES closing_period(id),
        platform_id VARCHAR(36) NOT NULL REFERENCES platform(id),
        bank_id VARCHAR(36) NOT NULL REFERENCES bank(id),
        ledger_opening NUMERIC(18,2) NOT NULL,
        ledger_incoming NUMERIC(18,2) NOT NULL,
        ledger_outgoing NUMERIC(18,2) NOT NULL,
        ledger_closing NUMERIC(18,2) NOT NULL,
        pending_unclaimed NUMERIC(18,2) NOT NULL,
        operating_closing NUMERIC(18,2) NOT NULL,
        actual_bank_balance NUMERIC(18,2),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (ledger_closing = ledger_opening + ledger_incoming - ledger_outgoing),
        CHECK (operating_closing = ledger_closing - pending_unclaimed),
        CHECK (pending_unclaimed >= 0),
        CHECK (actual_bank_balance IS NULL)
    )''',
    '''CREATE INDEX IF NOT EXISTS ix_auto_closing_ledger_snapshot_platform_bank
       ON auto_closing_ledger_snapshot(platform_id,bank_id)''',
]


def migrate(connection):
    """Creates an isolated, immutable-intent ledger schema without modifying legacy balances."""
    with connection:
        with connection.cursor() as cursor:
            for statement in DDL:
                cursor.execute(statement)
    return {'schema': 'auto_closing_ledger_snapshot', 'legacy_tables_modified': False,
            'auto_closing_enabled': False}


if __name__ == '__main__':
    if os.getenv('BANKFLOW_STAGE13_MIGRATE') != 'YES_ISOLATED_TEST_DATABASE':
        raise SystemExit('Refusing migration: set BANKFLOW_STAGE13_MIGRATE=YES_ISOLATED_TEST_DATABASE')
    if not os.getenv('DATABASE_URL'):
        raise SystemExit('DATABASE_URL is required')
    import psycopg2
    with psycopg2.connect(os.environ['DATABASE_URL']) as connection:
        print(migrate(connection))
