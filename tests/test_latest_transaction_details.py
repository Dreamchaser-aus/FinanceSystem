import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import *
import re

def test_latest_details_and_atomic_void():
    a=create_app(dict(TESTING=True,SQLALCHEMY_DATABASE_URI='sqlite://',WTF_CSRF_ENABLED=False,SECRET_KEY='test'))
    with a.app_context():
     db.create_all()
     db.session.add_all([Platform(id='p',name='TEST'),Platform(id='q',name='OTHER'),User(id='s',username='s',role='staff',password_hash=generate_password_hash('test'))]);db.session.flush()
     db.session.add_all([UserPlatformAccess(user_id='s',platform_id='p'),Bank(id='b',platform_id='p',name='TEST Bank',opening_balance=100),Dealer(id='d',platform_id='p',name='TEST Dealer')])
     for perm in ['create_transaction','view_bank_balances','view_bank_movements','edit_transaction']:db.session.add(UserPermission(user_id='s',permission=perm))
     db.session.commit()
     pair=record_deposit_back('WR','DR',10,'s','00012','<script>test</script>',platform_id='p');pairids=[t.id for t in pair]
     buy=record_dealer('dealer_buy','',20,'b','s','d',2.8,platform_id='p');buyid=buy.id
     payout=record_dealer('dealer_payout','PAYOUT',5,None,'s','d',2.8,'00012',platform_id='p');payoutid=payout.id
    c=a.test_client();c.post('/login',data={'username':'s','password':'test'})
    r=c.get('/transactions/new');assert r.status_code==200
    assert r.text.count('data-transaction-detail=')==3
    assert 'WR' in r.text and 'DR' in r.text and '&lt;script&gt;test' in r.text
    assert r.text.count('class="modal fade" id="newtx-detail-')==3
    def delete(i):
     return c.post('/transactions/'+i+'/delete',data={'reason':'test correction','return_to':'new_transaction'},follow_redirects=True)
    assert delete(pairids[0]).status_code==200
    with a.app_context():
     assert all(db.session.get(Transaction,i).status=='voided' for i in pairids)
     assert TransactionAudit.query.filter(TransactionAudit.transaction_id.in_(pairids),TransactionAudit.action=='VOID').count()==2
    assert delete(pairids[1]).status_code==200
    assert delete(buyid).status_code==200
    assert delete(payoutid).status_code==200
    with a.app_context():
     assert ledger_balance('b')==100
     assert db.session.get(Transaction,buyid).status=='voided'
     assert db.session.get(Transaction,payoutid).status=='voided'
     assert TransactionAudit.query.filter(TransactionAudit.transaction_id.in_(pairids),TransactionAudit.action=='VOID').count()==2
    print('PASS: rendering, linked pair atomic void/retry, Dealer Buy balance, Dealer Payout void')
    with a.app_context():
     tx=Transaction(id='u',platform_id='p',reference='UNCLAIM',kind='unclaim',signed_amount=20,created_by='s',business_date=business_today())
     db.session.add(tx);db.session.flush();db.session.add(Entry(transaction_id='u',bank_id='b',delta=20))
     db.session.add(UnclaimAllocation(original_transaction_id='u',platform_id='p',bank_id='b',deposit_reference='CLAIM',customer_ref='c',amount=5,reason='test',created_by='s'))
     db.session.add(Transaction(id='foreign',platform_id='q',reference='FOREIGN',kind='deposit',signed_amount=1,created_by='s',business_date=business_today()))
     db.session.add(Transaction(id='old',platform_id='p',reference='OLD',kind='deposit',signed_amount=1,created_by='s',business_date=business_today()-timedelta(days=1)))
     db.session.commit()
    assert 'Unclaim has allocations' in delete('u').text
    assert c.post('/transactions/foreign/delete',data={'reason':'test correction'}).status_code==403
    assert 'Only current-day' in delete('old').text
    with a.app_context():
     pair=record_deposit_back('BAD-W','BAD-D',10,'s',platform_id='p')
     bad=pair[0].id;db.session.delete(pair[1]);db.session.commit()
    assert 'incomplete or inconsistent' in delete(bad).text
    with a.app_context():
     assert db.session.get(Transaction,'u').status=='confirmed'
     assert db.session.get(Transaction,bad).status=='confirmed'
     db.session.add(Closing(bank_id='b',business_date=business_today(),system_balance=120,actual_balance=120,difference=0,created_by='s'))
     tx=Transaction(id='closed',platform_id='p',reference='CLOSED',kind='deposit',signed_amount=1,created_by='s',business_date=business_today());db.session.add(tx);db.session.flush();db.session.add(Entry(transaction_id='closed',bank_id='b',delta=1));db.session.commit()
    assert 'closed for this business date' in delete('closed').text
    with a.app_context():
     UserPermission.query.filter_by(user_id='s',permission='edit_transaction').delete();db.session.commit()
    assert c.post('/transactions/u/delete',data={'reason':'test correction'}).status_code==403
    assert 'No delete permission' in c.get('/transactions/new').text
    print('PASS: allocated Unclaim, platform isolation, old date, incomplete pair, closing and permission guards')
