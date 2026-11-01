import os, sys, pytest, hashlib
pytest.importorskip("flask")
pytest.importorskip("werkzeug")
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app, db, User, Platform, Bank, Transaction, Entry, Audit, PlatformResetBackupProof


def test_reset_requires_admin_and_preserves_other_platform():
    app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:','WTF_CSRF_ENABLED':False,'SECRET_KEY':'test-secret'})
    with app.app_context():
        db.create_all()
        admin=User(id='admin',username='admin',password_hash=generate_password_hash('password123'),role='admin')
        staff=User(id='staff',username='staff',password_hash=generate_password_hash('password123'),role='staff')
        p1=Platform(id='p1',name='P1');p2=Platform(id='p2',name='P2')
        db.session.add_all([admin,staff,p1,p2]);db.session.flush()
        b1=Bank(id='b1',platform_id='p1',name='A');b2=Bank(id='b2',platform_id='p2',name='B')
        t1=Transaction(id='t1',platform_id='p1',reference='T1',kind='deposit',signed_amount=10,created_by='admin',business_date=datetime.now().date())
        t2=Transaction(id='t2',platform_id='p2',reference='T2',kind='deposit',signed_amount=10,created_by='admin',business_date=datetime.now().date())
        db.session.add_all([b1,b2,t1,t2]);db.session.flush()
        db.session.add_all([Entry(transaction_id='t1',bank_id='b1',delta=10),Entry(transaction_id='t2',bank_id='b2',delta=10)]);db.session.commit()
        client=app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id']='staff';sess['_fresh']=True
        assert client.get('/admin/clear-platform').status_code==403
        with client.session_transaction() as sess:
            sess['_user_id']='admin';sess['_fresh']=True
        from datetime import timedelta
        token='test-backup-token'
        proof=PlatformResetBackupProof(id='proof1',admin_id='admin',platform_id='p1',
            nonce_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc)+timedelta(minutes=15))
        db.session.add(proof);db.session.commit()
        client.set_cookie('bankflow_reset_token',token,path='/admin/clear-platform')
        bad=client.post('/admin/clear-platform/execute',data={'platform_id':'p1','backup_proof_id':'proof1','password':'wrong','confirm_text':'CLEAR ALL DATA','backup_saved':'yes'})
        assert bad.status_code==302
        assert db.session.get(Bank,'b1') is not None
        good=client.post('/admin/clear-platform/execute',data={'platform_id':'p1','backup_proof_id':'proof1','password':'password123','confirm_text':'CLEAR ALL DATA','backup_saved':'yes'})
        assert good.status_code==302
        assert db.session.get(Bank,'b1') is None
        assert db.session.get(Transaction,'t1') is None
        assert db.session.get(Bank,'b2') is not None
        assert db.session.get(Transaction,'t2') is not None
        assert db.session.get(Platform,'p1') is not None
        assert db.session.get(User,'admin') is not None
        assert Audit.query.filter_by(action='CLEAR_PLATFORM_DATA').count()==1
