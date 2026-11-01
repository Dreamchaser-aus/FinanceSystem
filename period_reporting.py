"""UTC-only financial aggregates; all amounts are Decimal, boundaries [start,end)."""
from decimal import Decimal
from sqlalchemy import select, func, case

def utc_ledger(db, Bank, Entry, Transaction, bank_ids, end_utc):
    if not bank_ids: return {}
    rows=db.session.execute(select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0)).join(Transaction,Entry.transaction_id==Transaction.id).where(Entry.bank_id.in_(bank_ids),Transaction.status=='confirmed',Transaction.created_at<end_utc).group_by(Entry.bank_id)).all()
    return dict(rows)

def utc_bank_overview(db, Bank, Entry, Transaction, banks, start_utc, end_utc):
    if end_utc<=start_utc: raise ValueError('Invalid UTC period')
    ids=[b.id for b in banks]
    if not ids:return {}
    before=utc_ledger(db,Bank,Entry,Transaction,ids,start_utc)
    period=db.session.execute(select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0).label('delta'),func.coalesce(func.sum(case((Entry.delta>0,Entry.delta),else_=0)),0).label('in_amount'),func.coalesce(func.sum(case((Entry.delta<0,-Entry.delta),else_=0)),0).label('out_amount'),func.coalesce(func.sum(case((Entry.delta>0,1),else_=0)),0).label('in_count'),func.coalesce(func.sum(case((Entry.delta<0,1),else_=0)),0).label('out_count')).join(Transaction,Entry.transaction_id==Transaction.id).where(Entry.bank_id.in_(ids),Transaction.status=='confirmed',Transaction.created_at>=start_utc,Transaction.created_at<end_utc).group_by(Entry.bank_id)).all()
    by_id={r.bank_id:r for r in period}
    result={}
    for b in banks:
        r=by_id.get(b.id)
        opening=Decimal(str(b.opening_balance))+Decimal(str(before.get(b.id,0)))
        incoming=Decimal(str(r.in_amount)) if r else Decimal('0')
        outgoing=Decimal(str(r.out_amount)) if r else Decimal('0')
        result[b.id]={'opening':opening,'in_amount':incoming,'out_amount':outgoing,'in_count':int(r.in_count) if r else 0,'out_count':int(r.out_count) if r else 0,'daily':incoming-outgoing,'day_close':opening+incoming-outgoing}
    return result
