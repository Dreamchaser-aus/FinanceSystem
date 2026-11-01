"""Database-enforced write-once protections for finalized financial records.

Executed by the Railway bootstrap command. PostgreSQL only: SQLite is allowed
for unit tests, but does not claim to provide production immutability.
"""
from sqlalchemy import text

PROTECTED_TABLES = ('closing_financial_snapshot', 'closing_period', 'closing')


def install_postgres_guards(db):
    if db.engine.dialect.name != 'postgresql':
        return False
    # All DDL is transactional. Failure aborts Railway bootstrap rather than
    # silently starting without financial-record protection.
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION bankflow_block_financial_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'DELETE' AND current_setting('bankflow.allow_platform_reset', true) = 'true' THEN
                RETURN OLD;
              END IF;
              RAISE EXCEPTION 'BankFlow finalized financial record is immutable: %', TG_TABLE_NAME
                USING ERRCODE = '23514';
            END;
            $$;
        """))
        for table in PROTECTED_TABLES:
            # Static, internal table names; never interpolate user input.
            conn.execute(text(f'DROP TRIGGER IF EXISTS bankflow_immutable_guard ON {table}'))
            conn.execute(text(f"""CREATE TRIGGER bankflow_immutable_guard
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION bankflow_block_financial_mutation()"""))
        # Concurrent inserts are additionally prevented by the model's unique
        # constraints (closing bank/date; period bank/start/end; snapshot period).
    return True
