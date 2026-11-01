"""Stage 17: read-only preflight for Stage 16 ledger calculation.

Checks schema, orphan/cross-platform entries, period overlap, and UTC timestamp types.
No automatic closing and no writes. Run ONLY against a disposable test database.
"""
import json
import os
from sqlalchemy import create_engine, inspect, text

REQUIRED = {
    'bank': {'id', 'platform_id', 'opening_balance'},
    'closing_period': {'id', 'bank_id', 'start_utc', 'end_utc'},
    'transaction': {'id', 'platform_id', 'created_at', 'status', 'kind'},
    'entry': {'id', 'bank_id', 'transaction_id', 'delta'},
    'unclaim_allocation': {'id', 'bank_id', 'platform_id', 'amount', 'created_at'},
}

def check_schema(tables):
    missing = {name: sorted(fields - set(tables.get(name, ()))) for name, fields in REQUIRED.items()}
    return {name: cols for name, cols in missing.items() if cols}

def audit(database_url):
    if not database_url:
        raise ValueError('DATABASE_URL required')
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        schema = inspect(engine)
        present = set(schema.get_table_names())
        columns = {table: {col['name'] for col in schema.get_columns(table)}
                   for table in REQUIRED if table in present}
        missing = check_schema(columns)
        result = {'schema_ready': not missing, 'missing': missing,
                  'read_only': True, 'auto_closing_enabled': False,
                  'warnings': [], 'counts': {}}
        if missing:
            return result
        with engine.connect() as conn:
            # No writes. Count integrity hazards without exporting any personal data.
            queries = {
                'cross_platform_entries': '''SELECT COUNT(*) FROM entry e
                    JOIN bank b ON b.id=e.bank_id
                    JOIN "transaction" t ON t.id=e.transaction_id
                    WHERE b.platform_id <> t.platform_id''',
                'cross_platform_allocations': '''SELECT COUNT(*) FROM unclaim_allocation a
                    JOIN bank b ON b.id=a.bank_id
                    WHERE a.platform_id <> b.platform_id''',
                'invalid_period_boundaries': '''SELECT COUNT(*) FROM closing_period
                    WHERE start_utc >= end_utc''',
                'overlapping_period_pairs': '''SELECT COUNT(*) FROM closing_period a
                    JOIN closing_period b ON a.bank_id=b.bank_id AND a.id < b.id
                    WHERE a.start_utc < b.end_utc AND b.start_utc < a.end_utc''',
            }
            for key, query in queries.items():
                result['counts'][key] = int(conn.execute(text(query)).scalar_one())
            # PostgreSQL only: ensure timestamps are offset-aware.
            if engine.dialect.name == 'postgresql':
                timestamp_columns = [('transaction','created_at'),
                                     ('closing_period','start_utc'),
                                     ('closing_period','end_utc'),
                                     ('unclaim_allocation','created_at')]
                for table, column in timestamp_columns:
                    typ = next((c['type'] for c in schema.get_columns(table)
                                if c['name'] == column), None)
                    if not getattr(typ, 'timezone', False):
                        result['warnings'].append(f'{table}.{column} is not TIMESTAMPTZ')
        if any(result['counts'].values()):
            result['warnings'].append('Integrity hazards found: do not enable snapshot writer.')
        result['safe_to_proceed_to_manual_integration_tests'] = (
            result['schema_ready'] and not result['warnings'])
        return result
    finally:
        engine.dispose()

if __name__ == '__main__':
    print(json.dumps(audit(os.environ.get('DATABASE_URL')), indent=2, ensure_ascii=False))
