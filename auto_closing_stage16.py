"""Stage 16: read-only ledger calculation for an EXISTING closing period.

No scheduler, no DB writes. Period timestamps must be stored as UTC instants.
"""
from decimal import Decimal
from sqlalchemy import create_engine, text
from auto_closing_stage14 import prepare_snapshot


def compute_period(database_url, *, period_id, platform_id, bank_id):
    if not all((database_url, period_id, platform_id, bank_id)):
        raise ValueError('Database URL, period, platform and bank IDs required')
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            period = conn.execute(text('''SELECT cp.start_utc,cp.end_utc,b.opening_balance
                FROM closing_period cp JOIN bank b ON b.id=cp.bank_id
                WHERE cp.id=:period AND cp.bank_id=:bank AND b.platform_id=:platform'''),
                {'period':period_id,'bank':bank_id,'platform':platform_id}).mappings().one_or_none()
            if period is None: raise ValueError('Period/bank/platform mismatch')
            start,end=period['start_utc'],period['end_utc']
            if start >= end: raise ValueError('Invalid closing boundaries')
            params={'bank':bank_id,'platform':platform_id,'start':start,'end':end}
            # Confirmed entries only; timestamps are compared as UTC instants.
            row=conn.execute(text('''SELECT
                COALESCE(SUM(CASE WHEN t.created_at < :start THEN e.delta ELSE 0 END),0) AS before_delta,
                COALESCE(SUM(CASE WHEN t.created_at >= :start AND e.delta > 0 THEN e.delta ELSE 0 END),0) AS incoming,
                COALESCE(SUM(CASE WHEN t.created_at >= :start AND e.delta < 0 THEN -e.delta ELSE 0 END),0) AS outgoing
                FROM entry e JOIN "transaction" t ON t.id=e.transaction_id
                WHERE e.bank_id=:bank AND t.platform_id=:platform
                  AND t.status='confirmed' AND t.created_at < :end'''),params).mappings().one()
            pending=conn.execute(text('''SELECT
                COALESCE((SELECT SUM(e.delta) FROM entry e JOIN "transaction" t ON t.id=e.transaction_id
                    WHERE e.bank_id=:bank AND t.platform_id=:platform AND t.kind='unclaim'
                      AND t.status='confirmed' AND t.created_at < :end),0)
                - COALESCE((SELECT SUM(a.amount) FROM unclaim_allocation a
                    WHERE a.bank_id=:bank AND a.platform_id=:platform AND a.created_at < :end),0)'''),params).scalar_one()
            opening=Decimal(str(period['opening_balance']))+Decimal(str(row['before_delta']))
            snapshot=prepare_snapshot(opening,row['incoming'],row['outgoing'],pending)
            return {'period_id':period_id,'platform_id':platform_id,'bank_id':bank_id,
                    'start_utc':start.isoformat() if hasattr(start, 'isoformat') else str(start),
                    'end_utc':end.isoformat() if hasattr(end, 'isoformat') else str(end),
                    'ledger_opening':str(snapshot.opening),
                    'ledger_incoming':str(snapshot.incoming),
                    'ledger_outgoing':str(snapshot.outgoing),
                    'ledger_closing':str(snapshot.ledger_closing),
                    'pending_unclaimed':str(snapshot.pending_unclaimed),
                    'operating_closing':str(snapshot.operating_closing),
                    'actual_bank_balance':None,'status':'PENDING_VERIFICATION',
                    'written':False}
    finally:
        engine.dispose()
