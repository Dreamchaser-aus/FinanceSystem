"""Stage 5 opt-in auto-closing snapshot writer (TEST DB only).

Creates period + immutable financial/operating snapshots + verification row.
Does NOT create a legacy Closing row (which requires a verified actual balance).
Does NOT schedule itself or change transaction business dates.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import text
from auto_closing_planner import due_periods


def _uuid():
    return str(uuid.uuid4())


def write_due_periods(database_url, platform_id, bank_id, *, instant=None, limit=7):
    from auto_closing_stage11 import require_verified_accounting
    require_verified_accounting()
    if os.getenv('BANKFLOW_STAGE5_WRITE') != 'YES_ISOLATED_TEST_DATABASE':
        raise RuntimeError('Stage 5 write disabled; set BANKFLOW_STAGE5_WRITE=YES_ISOLATED_TEST_DATABASE')
    from sqlalchemy import create_engine
    engine = create_engine(database_url, pool_pre_ping=True)
    instant = instant or datetime.now(timezone.utc)
    created = []
    try:
        with engine.begin() as conn:
            # Serialize per bank across Railway workers. A failure rolls back the batch.
            conn.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'bankflow:auto:'+bank_id})
            policy = conn.execute(text('''SELECT p.platform_id,p.auto_timezone,p.manual_offset_minutes,p.closing_hour,p.closing_minute,p.enabled,
                        b.opening_balance,b.status,b.active,b.platform_id AS bank_platform
                        FROM platform_closing_policy p JOIN bank b ON b.platform_id=p.platform_id
                        WHERE p.platform_id=:pid AND b.id=:bid FOR UPDATE OF b'''), {'pid':platform_id,'bid':bank_id}).mappings().one_or_none()
            if not policy or policy['bank_platform'] != platform_id or not policy['enabled']:
                raise ValueError('Platform/bank mismatch or auto-closing not explicitly enabled')
            if policy['status']=='closed' or not policy['active']:
                raise ValueError('Closed or inactive bank cannot be auto-closed')
            anchor=conn.execute(text('''SELECT end_utc FROM closing_period WHERE bank_id=:bid
                        ORDER BY end_utc DESC LIMIT 1'''),{'bid':bank_id}).scalar_one_or_none()
            if anchor is None:
                raise ValueError('Verified UTC closing anchor required; refusing historical inference')
            plans=due_periods(platform_id,bank_id,anchor,instant,hour=policy['closing_hour'],minute=policy['closing_minute'],
                auto_timezone=policy['auto_timezone'],manual_offset_minutes=policy['manual_offset_minutes'],limit=limit)
            for plan in plans:
                # Refuse to snapshot if unconfirmed transactions exist before boundary.
                unresolved=conn.execute(text('''SELECT COUNT(*) FROM entry e JOIN transaction t ON t.id=e.transaction_id
                     WHERE e.bank_id=:bid AND t.created_at<:end AND t.status NOT IN ('confirmed','voided')'''),
                     {'bid':bank_id,'end':plan.end_utc}).scalar_one()
                if unresolved:
                    raise ValueError('Unresolved transaction status before closing boundary')
                amounts=conn.execute(text('''SELECT
                    COALESCE(SUM(CASE WHEN t.created_at<:start THEN e.delta ELSE 0 END),0) AS prior,
                    COALESCE(SUM(CASE WHEN t.created_at>=:start AND e.delta>0 THEN e.delta ELSE 0 END),0) AS incoming,
                    COALESCE(SUM(CASE WHEN t.created_at>=:start AND e.delta<0 THEN -e.delta ELSE 0 END),0) AS outgoing
                    FROM entry e JOIN transaction t ON t.id=e.transaction_id
                    WHERE e.bank_id=:bid AND t.status='confirmed' AND t.created_at<:end'''),
                    {'bid':bank_id,'start':plan.start_utc,'end':plan.end_utc}).mappings().one()
                opening=Decimal(policy['opening_balance'])+Decimal(amounts['prior'])
                incoming=Decimal(amounts['incoming']); outgoing=Decimal(amounts['outgoing'])
                system=opening+incoming-outgoing
                pending=conn.execute(text('''SELECT COALESCE((SELECT SUM(e.delta) FROM entry e JOIN transaction t ON t.id=e.transaction_id
                    WHERE e.bank_id=:bid AND t.kind='unclaim' AND t.status='confirmed' AND t.created_at<:end),0)
                    -COALESCE((SELECT SUM(a.amount) FROM unclaim_allocation a WHERE a.bank_id=:bid AND a.created_at<:end),0)'''),
                    {'bid':bank_id,'end':plan.end_utc}).scalar_one()
                pending=Decimal(pending)
                if pending<0: raise ValueError('Negative Pending Unclaimed; manual review required')
                # Lead status is mutable; don't reconstruct historical lead lists from current status.
                # Persist an empty list and the immutable ledger-derived pending amount instead.
                period_id=_uuid()
                conn.execute(text('''INSERT INTO closing_period(id,bank_id,start_utc,end_utc,status,created_at)
                    VALUES(:id,:bid,:start,:end,'auto_pending',NOW())'''),
                    {'id':period_id,'bid':bank_id,'start':plan.start_utc,'end':plan.end_utc})
                conn.execute(text('''INSERT INTO closing_financial_snapshot(id,closing_period_id,bank_id,opening,incoming,outgoing,
                    closing_balance,unclaimed_amount,unclaimed_count,unclaimed_json,created_at)
                    VALUES(:id,:period,:bid,:opening,:incoming,:outgoing,:system,:pending,0,:lead_json,NOW())'''),
                    {'id':_uuid(),'period':period_id,'bid':bank_id,'opening':opening,'incoming':incoming,
                     'outgoing':outgoing,'system':system,'pending':pending,'lead_json':json.dumps({'lead_list_not_reconstructed':True})})
                conn.execute(text('''INSERT INTO operating_closing_snapshot(id,closing_period_id,bank_id,
                    actual_bank_closing,pending_unclaimed_closing,operating_closing,created_at)
                    VALUES(:id,:period,:bid,:system,:pending,:operating,NOW())'''),
                    {'id':_uuid(),'period':period_id,'bid':bank_id,'system':system,'pending':pending,'operating':system-pending})
                conn.execute(text('''INSERT INTO auto_closing_verification(id,closing_period_id,platform_id,bank_id,status,system_balance,created_at)
                    VALUES(:id,:period,:pid,:bid,'PENDING_VERIFICATION',:system,NOW())'''),
                    {'id':_uuid(),'period':period_id,'pid':platform_id,'bid':bank_id,'system':system})
                created.append({'period_id':period_id,'end_utc':plan.end_utc.isoformat(),'system_balance':str(system),
                    'pending_unclaimed':str(pending),'status':'PENDING_VERIFICATION'})
        return created
    finally:
        engine.dispose()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--platform-id',required=True)
    p.add_argument('--bank-id',required=True)
    args=p.parse_args()
    print(json.dumps(write_due_periods(os.environ['DATABASE_URL'],args.platform_id,args.bank_id),indent=2))
