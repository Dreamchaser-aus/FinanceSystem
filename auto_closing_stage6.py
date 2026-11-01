"""Stage 6: opt-in, transaction-safe Admin verification of Stage 5 snapshots.

Not connected to Flask routes or Railway scheduling. Test database only.
"""
import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from sqlalchemy import create_engine, text


def money(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid balance') from None
    if not result.is_finite() or result.as_tuple().exponent < -2:
        raise ValueError('Balance must be finite with at most two decimal places')
    return result.quantize(Decimal('0.01'))


def verify(database_url, period_id, admin_id, actual_balance, *, remark='', dry_run=True):
    actual = money(actual_balance)
    if not dry_run and os.environ.get('BANKFLOW_STAGE6_VERIFY') != 'YES_ISOLATED_TEST_DATABASE':
        raise RuntimeError('Write disabled: set BANKFLOW_STAGE6_VERIFY=YES_ISOLATED_TEST_DATABASE')
    if not dry_run:
        from auto_closing_stage11 import require_verified_accounting
        require_verified_accounting()
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            row = conn.execute(text('''SELECT v.id, v.status, v.platform_id, v.bank_id, v.system_balance,
                       p.status AS period_status, p.bank_id AS period_bank,
                       s.closing_balance AS snapshot_balance, u.actual_bank_closing AS operating_system,
                       usr.role AS actor_role
                FROM auto_closing_verification v
                JOIN closing_period p ON p.id=v.closing_period_id
                JOIN closing_financial_snapshot s ON s.closing_period_id=p.id
                JOIN operating_closing_snapshot u ON u.closing_period_id=p.id
                JOIN bank b ON b.id=v.bank_id AND b.platform_id=v.platform_id
                JOIN "user" usr ON usr.id=:admin
                WHERE v.closing_period_id=:period FOR UPDATE OF v'''),
                {'admin':admin_id,'period':period_id}).mappings().one_or_none()
            if row is None:
                raise ValueError('Period not found, missing snapshot, or invalid actor')
            if row['actor_role'] != 'admin':
                raise PermissionError('Only Admin may verify a closing')
            if row['status'] != 'PENDING_VERIFICATION' or row['period_status'] != 'auto_pending':
                raise ValueError('Period is not pending verification; no repeat writes allowed')
            if row['period_bank'] != row['bank_id']:
                raise ValueError('Bank mismatch')
            system = money(row['system_balance'])
            if system != money(row['snapshot_balance']) or system != money(row['operating_system']):
                raise ValueError('Inconsistent system snapshots; refuse verification')
            diff = actual - system
            status = 'VERIFIED' if diff == 0 else 'DISPUTED'
            result = {'period_id':period_id, 'system_balance':str(system), 'actual_balance':str(actual),
                      'difference':str(diff), 'status':status, 'dry_run':dry_run}
            if not dry_run:
                conn.execute(text('''UPDATE auto_closing_verification
                    SET status=:status, actual_balance=:actual, difference=:difference,
                        verified_by=:admin, verified_at=NOW()
                    WHERE id=:id AND status='PENDING_VERIFICATION' '''),
                    {'status':status,'actual':actual,'difference':diff,'admin':admin_id,'id':row['id']})
                # Keep the original closing_period and snapshots unchanged; disputed closings
                # must remain visible for follow-up and never be silently rewritten.
                conn.execute(text('''INSERT INTO audit(id,actor_id,action,detail,created_at)
                    VALUES(CAST(gen_random_uuid() AS VARCHAR),:admin,'auto_closing_verify',:detail,NOW())'''),
                    {'admin':admin_id,'detail':json.dumps({**result,'remark':remark},ensure_ascii=False)})
            return result
    finally:
        engine.dispose()


if __name__ == '__main__':
    p=argparse.ArgumentParser(description='Stage 6 TEST verification; dry-run by default')
    p.add_argument('--period-id',required=True)
    p.add_argument('--admin-id',required=True)
    p.add_argument('--actual',required=True)
    p.add_argument('--remark',default='')
    p.add_argument('--write',action='store_true')
    args=p.parse_args()
    print(json.dumps(verify(os.environ['DATABASE_URL'],args.period_id,args.admin_id,args.actual,
                            remark=args.remark,dry_run=not args.write),indent=2))
