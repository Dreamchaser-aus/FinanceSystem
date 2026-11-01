"""Stage 15: isolated ledger snapshot persistence, NOT an automatic closing scheduler.

Only persists a snapshot for an EXISTING closing_period, in a test database,
with an explicit opt-in. Never writes actual_bank_balance or legacy Closing.
"""
import os
from sqlalchemy import create_engine, text
from auto_closing_stage14 import prepare_snapshot

OPT_IN = 'YES_ISOLATED_TEST_DATABASE'


def persist_existing_period(database_url, *, period_id, platform_id, bank_id,
                            opening, incoming, outgoing, pending_unclaimed,
                            write=False):
    snapshot = prepare_snapshot(opening, incoming, outgoing, pending_unclaimed)
    result = {'period_id': period_id, 'platform_id': platform_id, 'bank_id': bank_id,
              'ledger_closing': str(snapshot.ledger_closing),
              'operating_closing': str(snapshot.operating_closing),
              'pending_unclaimed': str(snapshot.pending_unclaimed),
              'actual_bank_balance': None, 'status': snapshot.status,
              'written': False}
    if not write:
        return result
    if os.getenv('BANKFLOW_STAGE15_WRITE') != OPT_IN:
        raise RuntimeError('Stage 15 writes disabled: isolated test DB opt-in required')
    if not all((database_url, period_id, platform_id, bank_id)):
        raise ValueError('Database and period/platform/bank IDs are required')
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            period = conn.execute(text('''SELECT cp.id FROM closing_period cp
                JOIN bank b ON b.id=cp.bank_id
                WHERE cp.id=:period AND cp.bank_id=:bank AND b.platform_id=:platform
                FOR UPDATE OF cp'''), {'period': period_id, 'bank': bank_id,
                                        'platform': platform_id}).scalar_one_or_none()
            if period is None:
                raise ValueError('Closing period not found or platform/bank mismatch')
            # Never overwrite an existing snapshot; duplicate writes fail atomically.
            conn.execute(text('''INSERT INTO auto_closing_ledger_snapshot
                (closing_period_id,platform_id,bank_id,ledger_opening,ledger_incoming,
                 ledger_outgoing,ledger_closing,pending_unclaimed,operating_closing,
                 actual_bank_balance)
                VALUES (:period,:platform,:bank,:opening,:incoming,:outgoing,
                        :closing,:pending,:operating,NULL)'''),
                {'period': period_id, 'platform': platform_id, 'bank': bank_id,
                 'opening': snapshot.opening, 'incoming': snapshot.incoming,
                 'outgoing': snapshot.outgoing, 'closing': snapshot.ledger_closing,
                 'pending': snapshot.pending_unclaimed,
                 'operating': snapshot.operating_closing})
        result['written'] = True
        return result
    finally:
        engine.dispose()
