"""Stage 12: read-only accounting invariants audit for experimental auto closings.

Never modifies DB, never disables Stage 11 safety gate. Run against TEST DB.
"""
from decimal import Decimal
from sqlalchemy import create_engine, text

AUDIT_SQL = text('''
SELECT v.closing_period_id, v.platform_id, v.bank_id, v.status,
       v.system_balance, v.actual_balance, v.difference,
       s.closing_balance AS ledger_balance,
       s.unclaimed_amount AS pending_unclaimed,
       o.actual_bank_closing AS legacy_actual_field,
       o.pending_unclaimed_closing AS operating_pending,
       o.operating_closing AS operating_balance,
       p.start_utc, p.end_utc
FROM auto_closing_verification v
JOIN closing_period p ON p.id=v.closing_period_id
JOIN closing_financial_snapshot s ON s.closing_period_id=p.id
JOIN operating_closing_snapshot o ON o.closing_period_id=p.id
WHERE (:platform_id IS NULL OR v.platform_id=:platform_id)
ORDER BY p.end_utc DESC
LIMIT :limit
''')


def check_record(row):
    """Check stored numeric identities without mistaking legacy actual for verified actual."""
    r = dict(row)
    errors = []
    money = lambda x: Decimal(str(x))
    system = money(r['system_balance'])
    ledger = money(r['ledger_balance'])
    pending = money(r['pending_unclaimed'])
    operating = money(r['operating_balance'])
    if system != ledger:
        errors.append('SYSTEM_LEDGER_MISMATCH')
    if pending != money(r['operating_pending']):
        errors.append('PENDING_UNCLAIMED_MISMATCH')
    if operating != ledger - pending:
        errors.append('OPERATING_BALANCE_MISMATCH')
    if r['status'] == 'PENDING_VERIFICATION':
        if r['actual_balance'] is not None or r['difference'] is not None:
            errors.append('PENDING_HAS_ACTUAL')
    elif r['status'] in ('VERIFIED', 'DISPUTED'):
        if r['actual_balance'] is None or r['difference'] is None:
            errors.append('VERIFIED_MISSING_ACTUAL')
        else:
            difference = money(r['actual_balance']) - system
            if difference != money(r['difference']):
                errors.append('DIFFERENCE_MISMATCH')
            if (difference == 0) != (r['status'] == 'VERIFIED'):
                errors.append('VERIFICATION_STATUS_MISMATCH')
    else:
        errors.append('UNKNOWN_STATUS')
    if r['start_utc'] >= r['end_utc']:
        errors.append('INVALID_PERIOD')
    # Deliberately do not assert legacy_actual_field is a true actual balance:
    # Stage 5 incorrectly populated it from the ledger; schema repair is still required.
    return {'closing_period_id': r['closing_period_id'], 'errors': errors,
            'legacy_actual_field_untrusted': True}


def audit(database_url, *, platform_id=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be 1..1000')
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(AUDIT_SQL, {'platform_id': platform_id, 'limit': limit}).mappings().all()
        checks = [check_record(row) for row in rows]
        return {'checked': len(checks), 'failed': sum(bool(x['errors']) for x in checks),
                'records': checks, 'read_only': True, 'auto_closing_enabled': False}
    finally:
        engine.dispose()


if __name__ == '__main__':
    import argparse, json, os
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform-id')
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(audit(os.environ['DATABASE_URL'], platform_id=args.platform_id,
                           limit=args.limit), indent=2, default=str))
