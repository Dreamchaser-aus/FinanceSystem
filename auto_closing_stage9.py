"""Stage 9: read-only PostgreSQL contract audit before enabling the snapshot writer.

No database mutation or automatic closing occurs in this module.
"""
import json
import os
from sqlalchemy import create_engine, text

REQUIRED = {
    'platform_closing_policy': {'platform_id','auto_timezone','manual_offset_minutes','closing_hour','closing_minute','enabled'},
    'bank': {'id','platform_id','name','opening_balance','status','active'},
    'closing_period': {'id','bank_id','start_utc','end_utc','status','created_at'},
    'closing_financial_snapshot': {'id','closing_period_id','bank_id','opening','incoming','outgoing','closing_balance','unclaimed_amount','unclaimed_count','unclaimed_json','created_at'},
    'operating_closing_snapshot': {'id','closing_period_id','bank_id','actual_bank_closing','pending_unclaimed_closing','operating_closing','created_at'},
    'auto_closing_verification': {'id','closing_period_id','platform_id','bank_id','status','system_balance','created_at'},
    'entry': {'bank_id','transaction_id','delta'},
    'transaction': {'id','created_at','status','kind'},
    'unclaim_allocation': {'bank_id','amount','created_at'},
}

def check_contract(existing):
    """Pure function; existing maps table names to sets of column names."""
    missing = {table: sorted(cols - set(existing.get(table, ())))
               for table, cols in REQUIRED.items()}
    missing = {table: cols for table, cols in missing.items() if cols}
    return {'ready_for_stage5_sql': not missing, 'missing_columns': missing,
            'note': 'Schema compatibility alone does not validate accounting correctness or authorize writes.'}

def inspect(database_url):
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text('''SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema=current_schema()''')).all()
        existing = {}
        for table, column in rows:
            existing.setdefault(table, set()).add(column)
        return check_contract(existing)
    finally:
        engine.dispose()

if __name__ == '__main__':
    print(json.dumps(inspect(os.environ['DATABASE_URL']), indent=2, ensure_ascii=False))
