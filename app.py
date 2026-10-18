import os, uuid
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, select, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

MYT=ZoneInfo('Asia/Kuala_Lumpur')
db=SQLAlchemy(); login_manager=LoginManager(); csrf=CSRFProtect()

def now(): return datetime.now(timezone.utc)
def money(v):
    try:
        d=Decimal(str(v)).quantize(Decimal('0.01'))
        if not d.is_finite(): raise ValueError()
        return d
    except (InvalidOperation,ValueError,TypeError): raise ValueError('Invalid amount')

def amount_positive(v):
    d=money(v)
    if d<=0: raise ValueError('Amount must be greater than zero')
    return d

def idgen(): return str(uuid.uuid4())

class User(UserMixin,db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    username=db.Column(db.String(80),unique=True,nullable=False)
    password_hash=db.Column(db.String(255),nullable=False)
    role=db.Column(db.String(20),nullable=False,default='staff')
    active=db.Column(db.Boolean,nullable=False,default=True)
    @property
    def is_active(self): return self.active

class Bank(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    name=db.Column(db.String(120),unique=True,nullable=False)
    kind=db.Column(db.String(20),nullable=False,default='deposit')
    opening_balance=db.Column(db.Numeric(18,2),nullable=False,default=0)
    active=db.Column(db.Boolean,nullable=False,default=True)
    status=db.Column(db.String(30),nullable=False,default='active')
    status_remark=db.Column(db.Text)
    status_updated_at=db.Column(db.DateTime(timezone=True))
    status_updated_by=db.Column(db.String(36),db.ForeignKey('user.id'))
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)

class BankStatusHistory(db.Model):
    __tablename__='bank_status_history'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    old_status=db.Column(db.String(30),nullable=False)
    new_status=db.Column(db.String(30),nullable=False)
    remark=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    bank=db.relationship('Bank')
    actor=db.relationship('User')

BANK_STATUSES={
    'active':'Active',
    'cannot_transfer':'Cannot Transfer',
    'login_failed':'Login Failed',
    'wrong_password':'Wrong Password',
    'closed':'Closed',
    'other_issue':'Other Issue',
}

class Transaction(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    reference=db.Column(db.String(120),unique=True,nullable=False)
    kind=db.Column(db.String(30),nullable=False)
    signed_amount=db.Column(db.Numeric(18,2),nullable=False)
    status=db.Column(db.String(20),nullable=False,default='confirmed')
    customer_ref=db.Column(db.String(120))
    remark=db.Column(db.Text)
    created_by=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    business_date=db.Column(db.Date,nullable=False)

class Entry(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    transaction_id=db.Column(db.String(36),db.ForeignKey('transaction.id'),nullable=False,index=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    delta=db.Column(db.Numeric(18,2),nullable=False)
    bank=db.relationship('Bank')
    transaction=db.relationship('Transaction')

class Closing(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False)
    business_date=db.Column(db.Date,nullable=False)
    system_balance=db.Column(db.Numeric(18,2),nullable=False)
    actual_balance=db.Column(db.Numeric(18,2),nullable=False)
    difference=db.Column(db.Numeric(18,2),nullable=False)
    remark=db.Column(db.Text)
    created_by=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('bank_id','business_date',name='uq_bank_date'),)
    bank=db.relationship('Bank')

class Audit(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    action=db.Column(db.String(60),nullable=False)
    detail=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)

def role_required(*roles):
    def outer(fn):
        @wraps(fn)
        @login_required
        def inner(*a,**kw):
            if current_user.role not in roles: abort(403)
            return fn(*a,**kw)
        return inner
    return outer

def balances():
    rows=db.session.execute(select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0)).group_by(Entry.bank_id)).all()
    changes=dict(rows)
    return {b.id:money(b.opening_balance)+money(changes.get(b.id,0)) for b in Bank.query.all()}

def ledger_balance(bank_id,through=None):
    q=select(func.coalesce(func.sum(Entry.delta),0)).where(Entry.bank_id==bank_id).join(Transaction,Entry.transaction_id==Transaction.id)
    if through is not None: q=q.where(Transaction.business_date<=through)
    bank=db.session.get(Bank,bank_id)
    return money(bank.opening_balance)+money(db.session.execute(q).scalar_one())

def business_today(): return datetime.now(MYT).date()

def check_open_day(day,bank_ids):
    if day!=business_today(): raise ValueError('V1 only accepts current Malaysia business date')
    if Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(bank_ids)).first(): raise ValueError('Bank is closed for today')

def lock_banks(ids):
    # Postgres row locks serialize balance checks and updates; SQLite is for local demo only.
    return db.session.execute(select(Bank).where(Bank.id.in_(sorted(set(ids)))).order_by(Bank.id).with_for_update()).scalars().all()

def record(kind,reference,amount,bank_id,user_id,customer_ref='',remark='',to_bank_id=None):
    if not reference.strip(): raise ValueError('Transaction ID required')
    if kind not in ('deposit','withdrawal','transfer'): raise ValueError('Unsupported type')
    a=amount_positive(amount); ids=[bank_id]+([to_bank_id] if to_bank_id else [])
    if kind=='transfer' and (not to_bank_id or to_bank_id==bank_id): raise ValueError('Choose two different banks')
    if kind!='transfer' and to_bank_id: raise ValueError('Unexpected destination bank')
    day=business_today()
    try:
        bs=lock_banks(ids)
        if len(bs)!=len(ids) or any(not b.active for b in bs): raise ValueError('Bank missing or inactive')
        # A bank marked cannot_transfer may receive funds but cannot send funds.
        # All other non-active states block new movements in either direction.
        for b in bs:
            is_source = b.id == bank_id
            if b.status == 'active':
                continue
            if b.status == 'cannot_transfer' and not is_source and kind == 'transfer':
                continue
            if b.status == 'cannot_transfer' and is_source and kind == 'deposit':
                continue
            raise ValueError(f'Bank {b.name} status is {BANK_STATUSES.get(b.status,b.status)}; this transaction is not permitted')
        check_open_day(day,ids)
        if Transaction.query.filter_by(reference=reference.strip()).first(): raise ValueError('Duplicate Transaction ID')
        delta=a if kind=='deposit' else -a
        if delta<0 and ledger_balance(bank_id)<a: raise ValueError('Insufficient balance (negative balances blocked for new debits)')
        t=Transaction(reference=reference.strip(),kind=kind,signed_amount=delta,customer_ref=customer_ref.strip() or None,remark=remark.strip(),created_by=user_id,business_date=day)
        db.session.add(t);db.session.flush()
        db.session.add(Entry(transaction_id=t.id,bank_id=bank_id,delta=delta))
        if kind=='transfer': db.session.add(Entry(transaction_id=t.id,bank_id=to_bank_id,delta=a))
        db.session.add(Audit(actor_id=user_id,action='CREATE_TRANSACTION',detail=f'{t.id} {kind} {reference}'))
        db.session.commit()
        return t
    except Exception:
        db.session.rollback();raise

@login_manager.user_loader
def load_user(uid): return db.session.get(User,uid)

def create_app(test_config=None):
    app=Flask(__name__)
    uri=os.getenv('DATABASE_URL','sqlite:///bankflow.db')
    if uri.startswith('postgres://'): uri='postgresql+psycopg://'+uri[len('postgres://'):]
    elif uri.startswith('postgresql://'): uri='postgresql+psycopg://'+uri[len('postgresql://'):]
    app.config.update(SECRET_KEY=os.getenv('SECRET_KEY','dev-only-change-me'),SQLALCHEMY_DATABASE_URI=uri,SQLALCHEMY_TRACK_MODIFICATIONS=False,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE','0')=='1')
    if test_config: app.config.update(test_config)
    if not app.config['TESTING'] and app.config['SECRET_KEY']=='dev-only-change-me': raise RuntimeError('Set SECRET_KEY before starting')
    db.init_app(app);login_manager.init_app(app);login_manager.login_view='login';csrf.init_app(app)
    app.jinja_env.filters['rm']=lambda v:f'{money(v):,.2f}'
    app.jinja_env.filters['mytime']=lambda d: d.replace(tzinfo=timezone.utc).astimezone(MYT).strftime('%Y-%m-%d %H:%M') if d else ''

    @app.cli.command('init-db')
    def init_db():
        db.create_all();print('Tables created. Run flask --app app seed-admin to create administrator.')

    @app.cli.command('seed-admin')
    def seed_admin():
        username=os.getenv('ADMIN_USER');password=os.getenv('ADMIN_PASS')
        if not username or not password or len(password)<12: raise RuntimeError('Set ADMIN_USER and ADMIN_PASS (12+ chars)')
        if User.query.filter_by(username=username).first(): raise RuntimeError('Admin already exists')
        db.session.add(User(username=username,password_hash=generate_password_hash(password),role='admin'))
        db.session.commit();print('Admin created')

    @app.cli.command('bootstrap')
    def bootstrap():
        """Idempotent first-run bootstrap; safe on repeated Railway deploys."""
        if not os.getenv('DATABASE_URL'):
            raise RuntimeError('Railway DATABASE_URL is required; refusing ephemeral SQLite')
        username=os.getenv('ADMIN_USER','').strip()
        password=os.getenv('ADMIN_PASS','')
        if not username or len(password)<12:
            raise RuntimeError('Set ADMIN_USER and ADMIN_PASS (at least 12 characters)')
        db.create_all()
        if not User.query.filter_by(role='admin').first():
            if User.query.filter_by(username=username).first():
                raise RuntimeError('ADMIN_USER already exists without admin role')
            db.session.add(User(username=username,password_hash=generate_password_hash(password),role='admin'))
            db.session.commit()
            print('Initial administrator created')
        else:
            print('Database ready; administrator already exists')

    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            u=User.query.filter_by(username=request.form.get('username','')).first()
            if u and u.active and check_password_hash(u.password_hash,request.form.get('password','')):
                login_user(u);return redirect(url_for('dashboard'))
            flash('Invalid credentials','danger')
        return render_template('login.html')

    @app.post('/logout')
    @login_required
    def logout(): logout_user();return redirect(url_for('login'))

    @app.get('/')
    @login_required
    def dashboard():
        bs=Bank.query.order_by(Bank.kind,Bank.name).all();bal=balances()
        today=business_today()
        rows=db.session.execute(select(Transaction.kind,func.count(Transaction.id),func.coalesce(func.sum(Transaction.signed_amount),0)).where(Transaction.business_date==today).group_by(Transaction.kind)).all()
        totals={k:{'count':n,'amount':money(v)} for k,n,v in rows}
        latest=Transaction.query.order_by(Transaction.created_at.desc()).limit(12).all()
        transaction_banks = {}
        latest_ids = [t.id for t in latest]
        if latest_ids:
            movements = db.session.execute(
                select(Entry.transaction_id, Entry.delta, Bank.name)
                .join(Bank, Entry.bank_id == Bank.id)
                .where(Entry.transaction_id.in_(latest_ids))
            ).all()
            grouped = {}
            for transaction_id, delta, bank_name in movements:
                grouped.setdefault(transaction_id, []).append((delta, bank_name))
            for t in latest:
                items = grouped.get(t.id, [])
                if t.kind == 'transfer':
                    source = next((name for delta, name in items if delta < 0), 'Unknown')
                    destination = next((name for delta, name in items if delta > 0), 'Unknown')
                    transaction_banks[t.id] = f'{source} → {destination}'
                else:
                    transaction_banks[t.id] = items[0][1] if items else 'Unknown'
        return render_template(
            'dashboard.html', banks=bs, balances=bal, totals=totals,
            latest=latest, transaction_banks=transaction_banks,
            grand=sum(bal.values(), Decimal('0'))
        )

    @app.route('/banks',methods=['GET','POST'])
    @login_required
    def banks():
        if request.method=='POST':
            if current_user.role!='admin': abort(403)
            try:
                name=request.form.get('name','').strip()
                kind=request.form.get('kind','')
                if not name or kind not in ('deposit','withdrawal','store','other'): raise ValueError('Invalid bank details')
                b=Bank(name=name,kind=kind,opening_balance=money(request.form.get('opening_balance','0')))
                db.session.add(b);db.session.flush()
                db.session.add(Audit(actor_id=current_user.id,action='CREATE_BANK',detail=f'{b.id} {name}'))
                db.session.commit();flash('Bank created','success')
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('banks'))
        return render_template('banks.html',banks=Bank.query.order_by(Bank.kind,Bank.name).all(),balances=balances(),
                               statuses=BANK_STATUSES, history=BankStatusHistory.query.order_by(BankStatusHistory.created_at.desc()).limit(30).all())

    @app.post('/banks/<bank_id>/status')
    @role_required('admin','staff')
    def update_bank_status(bank_id):
        new_status=request.form.get('status','').strip()
        remark=request.form.get('remark','').strip()
        if new_status not in BANK_STATUSES:
            flash('Invalid bank status','danger')
            return redirect(url_for('banks'))
        if len(remark)>2000:
            flash('Remark must be 2000 characters or fewer','danger')
            return redirect(url_for('banks'))
        if new_status!='active' and not remark:
            flash('A reason is required when reporting a bank issue','danger')
            return redirect(url_for('banks'))
        try:
            locked=lock_banks([bank_id])
            if len(locked)!=1: abort(404)
            bank=locked[0]
            old_status=bank.status
            if old_status==new_status and (bank.status_remark or '')==remark:
                db.session.rollback()
                flash('No changes to save','info')
                return redirect(url_for('banks'))
            bank.status=new_status
            bank.status_remark=remark or None
            bank.status_updated_at=now()
            bank.status_updated_by=current_user.id
            # Legacy active flag stays synchronized for older code paths.
            bank.active=(new_status!='closed')
            db.session.add(BankStatusHistory(bank_id=bank.id,actor_id=current_user.id,
                                             old_status=old_status,new_status=new_status,remark=remark or None))
            db.session.add(Audit(actor_id=current_user.id,action='UPDATE_BANK_STATUS',
                                 detail=f'{bank.id}: {old_status} -> {new_status}; reason={remark[:500]}'))
            db.session.commit()
            flash(f'{bank.name} status updated to {BANK_STATUSES[new_status]}','success')
        except Exception:
            db.session.rollback()
            raise
        return redirect(url_for('banks'))

    @app.route('/transactions/new',methods=['GET','POST'])
    @login_required
    def new_transaction():
        if request.method=='POST':
            try:
                record(request.form.get('kind',''),request.form.get('reference',''),request.form.get('amount',''),request.form.get('bank_id',''),current_user.id,request.form.get('customer_ref',''),request.form.get('remark',''),request.form.get('to_bank_id') or None)
                flash('Transaction confirmed and balance updated','success');return redirect(url_for('dashboard'))
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
        return render_template('transaction.html',banks=Bank.query.filter_by(active=True).order_by(Bank.name).all())

    @app.get('/transactions')
    @login_required
    def transactions():
        from sqlalchemy import or_
        q = Transaction.query
        search = request.args.get('search', '').strip()
        kind = request.args.get('kind', 'all').strip().lower()
        bank_id = request.args.get('bank_id', '').strip()

        if search:
            pattern = f'%{search}%'
            matching_bank_ids = select(Bank.id).where(Bank.name.ilike(pattern))
            matching_transaction_ids = select(Entry.transaction_id).where(
                Entry.bank_id.in_(matching_bank_ids)
            )
            q = q.filter(or_(
                Transaction.customer_ref.ilike(pattern),
                Transaction.reference.ilike(pattern),
                Transaction.id.in_(matching_transaction_ids)
            ))
        if kind in ('deposit', 'withdrawal', 'transfer'):
            q = q.filter(Transaction.kind == kind)
        if bank_id:
            q = q.filter(Transaction.id.in_(
                select(Entry.transaction_id).where(Entry.bank_id == bank_id)
            ))

        page = max(1, request.args.get('page', 1, type=int))
        records = q.order_by(Transaction.created_at.desc(), Transaction.id.desc()).paginate(
            page=page, per_page=30, error_out=False
        )
        ids = [t.id for t in records.items]
        transaction_banks = {}
        if ids:
            entries = db.session.execute(
                select(Entry.transaction_id, Entry.delta, Bank.name)
                .join(Bank, Entry.bank_id == Bank.id)
                .where(Entry.transaction_id.in_(ids))
            ).all()
            grouped = {}
            for transaction_id, delta, name in entries:
                grouped.setdefault(transaction_id, []).append((delta, name))
            for t in records.items:
                movements = grouped.get(t.id, [])
                if t.kind == 'transfer':
                    source = next((name for delta, name in movements if delta < 0), 'Unknown')
                    destination = next((name for delta, name in movements if delta > 0), 'Unknown')
                    transaction_banks[t.id] = f'{source} → {destination}'
                else:
                    transaction_banks[t.id] = movements[0][1] if movements else 'Unknown'

        return render_template('transactions.html', records=records,
                               transaction_banks=transaction_banks,
                               banks=Bank.query.order_by(Bank.name).all(),
                               search=search, kind=kind, bank_id=bank_id)

    @app.route('/closing',methods=['GET','POST'])
    @role_required('admin','finance')
    def closing():
        today=business_today()
        if request.method=='POST':
            try:
                bank_id=request.form.get('bank_id','');actual=money(request.form.get('actual',''))
                bs=lock_banks([bank_id]);
                if len(bs)!=1: raise ValueError('Bank not found')
                if Closing.query.filter_by(bank_id=bank_id,business_date=today).first(): raise ValueError('Already closed today')
                sysbal=ledger_balance(bank_id)
                close=Closing(bank_id=bank_id,business_date=today,system_balance=sysbal,actual_balance=actual,difference=actual-sysbal,remark=request.form.get('remark','').strip(),created_by=current_user.id)
                db.session.add(close);db.session.add(Audit(actor_id=current_user.id,action='CLOSE_BANK',detail=f'{bank_id} {today} difference={actual-sysbal}'))
                db.session.commit();flash('Bank closed for today','success')
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('closing'))
        closes=Closing.query.filter_by(business_date=today).all()
        return render_template('closing.html',banks=Bank.query.order_by(Bank.name).all(),balances=balances(),closes=closes,closed_ids={c.bank_id for c in closes})

    @app.get('/health')
    def health(): return {'status':'ok'}
    return app

app=create_app()
