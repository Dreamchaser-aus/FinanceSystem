import os, uuid
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, url_for, flash, request, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, select, UniqueConstraint, or_
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
    rows=db.session.execute(select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0)).join(Transaction, Entry.transaction_id==Transaction.id).where(Transaction.status=='confirmed').group_by(Entry.bank_id)).all()
    changes=dict(rows)
    return {b.id:money(b.opening_balance)+money(changes.get(b.id,0)) for b in Bank.query.all()}

def ledger_balance(bank_id,through=None):
    q=select(func.coalesce(func.sum(Entry.delta),0)).where(Entry.bank_id==bank_id).join(Transaction,Entry.transaction_id==Transaction.id).where(Transaction.status=='confirmed')
    if through is not None: q=q.where(Transaction.business_date<=through)
    bank=db.session.get(Bank,bank_id)
    return money(bank.opening_balance)+money(db.session.execute(q).scalar_one())

def business_today(): return datetime.now(MYT).date()

def selected_date():
    """Read-only reporting date. Future dates and malformed dates are rejected."""
    raw = request.args.get('date', '').strip()
    if not raw:
        return business_today()
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        abort(400, description='Invalid date. Expected YYYY-MM-DD')
    if day > business_today():
        abort(400, description='Future reporting dates are not supported')
    return day

def bank_status_on(bank, day, history_by_bank):
    """Reconstruct status at end of MYT day from append-only history.

    Historical states preceding the first history record use its old_status.
    Banks without history use their current status (legacy limitation).
    """
    changes = history_by_bank.get(bank.id, [])
    if not changes:
        return bank.status
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=MYT).astimezone(timezone.utc)
    for h in reversed(changes):
        if h.created_at.astimezone(timezone.utc) < day_end:
            return h.new_status
    return changes[0].old_status

def balances_through(day):
    q = (select(Entry.bank_id, func.coalesce(func.sum(Entry.delta), 0))
         .join(Transaction, Entry.transaction_id == Transaction.id)
         .where(Transaction.status == 'confirmed', Transaction.business_date <= day)
         .group_by(Entry.bank_id))
    changes = dict(db.session.execute(q).all())
    return {b.id: money(b.opening_balance) + money(changes.get(b.id, 0))
            for b in Bank.query.all()}


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
        day = selected_date()
        bank_page = max(1, request.args.get('bank_page', 1, type=int))
        close_page = max(1, request.args.get('close_page', 1, type=int))
        all_banks = Bank.query.order_by(Bank.kind, Bank.name).all()
        history = BankStatusHistory.query.order_by(BankStatusHistory.created_at, BankStatusHistory.id).all()
        history_by_bank = {}
        for h in history:
            history_by_bank.setdefault(h.bank_id, []).append(h)
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=MYT).astimezone(timezone.utc)
        existing = [b for b in all_banks if b.created_at is None or b.created_at.astimezone(timezone.utc) < day_end]
        statuses = {b.id: bank_status_on(b, day, history_by_bank) for b in existing}
        bal = balances_through(day)
        # "Bank Block" means status=closed; other issues remain in the closing total.
        blocked = [b for b in existing if statuses[b.id] == 'closed']
        active_banks = [b for b in existing if statuses[b.id] == 'active']
        closing_total = sum((bal[b.id] for b in existing if statuses[b.id] != 'closed'), Decimal('0'))
        active_total = sum((bal[b.id] for b in active_banks), Decimal('0'))
        start = (bank_page - 1) * 10
        bank_items = active_banks[start:start + 10]
        bank_pages = max(1, (len(active_banks) + 9) // 10)
        bank_page = min(bank_page, bank_pages)
        bank_items = active_banks[(bank_page-1)*10:bank_page*10]
        rows = db.session.execute(
            select(Transaction.kind, func.count(Transaction.id), func.coalesce(func.sum(Transaction.signed_amount), 0))
            .where(Transaction.business_date == day, Transaction.status == 'confirmed')
            .group_by(Transaction.kind)).all()
        totals = {k: {'count': n, 'amount': money(v)} for k, n, v in rows}
        latest = (Transaction.query.filter_by(status='confirmed', business_date=day)
                  .order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(20).all())
        transaction_banks = {}
        ids = [t.id for t in latest]
        if ids:
            movements = db.session.execute(
                select(Entry.transaction_id, Entry.delta, Bank.name)
                .join(Bank, Entry.bank_id == Bank.id)
                .where(Entry.transaction_id.in_(ids))).all()
            grouped = {}
            for tid, delta, name in movements:
                grouped.setdefault(tid, []).append((delta, name))
            for t in latest:
                items = grouped.get(t.id, [])
                if t.kind == 'transfer':
                    source = next((name for delta, name in items if delta < 0), 'Unknown')
                    dest = next((name for delta, name in items if delta > 0), 'Unknown')
                    transaction_banks[t.id] = f'{source} → {dest}'
                else:
                    transaction_banks[t.id] = items[0][1] if items else 'Unknown'
        closed_query = (Closing.query.filter_by(business_date=day)
                        .order_by(Closing.created_at.desc(), Closing.id.desc()))
        closed_page = closed_query.paginate(page=close_page, per_page=10, error_out=False)
        return render_template(
            'dashboard.html', selected_day=day, banks=bank_items, balances=bal,
            totals=totals, latest=latest, transaction_banks=transaction_banks,
            grand=active_total, page_total=sum((bal[b.id] for b in bank_items), Decimal('0')),
            bank_page=bank_page, bank_pages=bank_pages, active_count=len(active_banks),
            block_count=len(blocked), block_banks=blocked, closing_total=closing_total,
            closed_banks=closed_page.items, closed_page=closed_page,
            closed_count=closed_page.total, statuses=statuses)

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
        day=selected_date()
        bank_page=max(1,request.args.get('bank_page',1,type=int))
        history_page=max(1,request.args.get('history_page',1,type=int))
        bank_pagination=Bank.query.order_by(Bank.kind,Bank.name).paginate(page=bank_page,per_page=15,error_out=False)
        end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=MYT).astimezone(timezone.utc)
        history_pagination=BankStatusHistory.query.filter(BankStatusHistory.created_at < end).order_by(BankStatusHistory.created_at.desc(),BankStatusHistory.id.desc()).paginate(page=history_page,per_page=15,error_out=False)
        return render_template('banks.html',banks=bank_pagination.items,balances=balances(),
                               statuses=BANK_STATUSES,history=history_pagination.items,bank_pagination=bank_pagination,history_pagination=history_pagination,selected_day=day)

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
        day = selected_date()
        q = Transaction.query.filter(Transaction.business_date == day)
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
            page=page, per_page=20, error_out=False
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
                               search=search, kind=kind, bank_id=bank_id, selected_day=day)

    @app.route('/transactions/<transaction_id>/edit', methods=['GET','POST'])
    @role_required('admin','staff')
    def edit_transaction(transaction_id):
        t=db.session.get(Transaction,transaction_id)
        if not t: abort(404)
        if request.method=='GET':
            if t.status!='confirmed':
                flash('Voided transactions cannot be edited','warning')
                return redirect(url_for('transactions'))
            entries=Entry.query.filter_by(transaction_id=t.id).all()
            source=next((e for e in entries if e.delta<0),None) if t.kind=='transfer' else (entries[0] if entries else None)
            destination=next((e for e in entries if e.delta>0),None) if t.kind=='transfer' else None
            return render_template('transaction_edit.html',t=t,banks=Bank.query.order_by(Bank.name).all(),source_id=source.bank_id if source else '',destination_id=destination.bank_id if destination else '')
        try:
            # Serialize all edits/deletes of the same transaction.
            t=db.session.execute(select(Transaction).where(Transaction.id==transaction_id).with_for_update()).scalar_one_or_none()
            if not t: abort(404)
            if t.status!='confirmed': raise ValueError('Transaction already voided')
            if t.business_date!=business_today(): raise ValueError('Only current-day transactions can be edited')
            kind=request.form.get('kind','').strip()
            if kind not in ('deposit','withdrawal','transfer'): raise ValueError('Invalid type')
            reference=request.form.get('reference','').strip()
            if not reference: raise ValueError('Transaction ID required')
            amount=amount_positive(request.form.get('amount',''))
            bank_id=request.form.get('bank_id','').strip()
            to_bank_id=request.form.get('to_bank_id','').strip() if kind=='transfer' else ''
            if kind=='transfer' and (not to_bank_id or to_bank_id==bank_id): raise ValueError('Choose two different banks')
            old_entries=Entry.query.filter_by(transaction_id=t.id).all()
            old_ids={e.bank_id for e in old_entries}
            new_ids={bank_id}|({to_bank_id} if to_bank_id else set())
            affected=old_ids|new_ids
            locked=lock_banks(affected)
            if len(locked)!=len(affected): raise ValueError('Bank not found')
            if Closing.query.filter(Closing.bank_id.in_(affected),Closing.business_date==t.business_date).first():
                raise ValueError('Cannot edit: an affected bank has been closed for this business date')
            banks_by_id={b.id:b for b in locked}
            for bid in new_ids:
                b=banks_by_id[bid]
                if not b.active: raise ValueError(f'{b.name} is inactive')
                if b.status=='active': continue
                if b.status=='cannot_transfer' and ((kind=='deposit' and bid==bank_id) or (kind=='transfer' and bid==to_bank_id)): continue
                raise ValueError(f'{b.name} status does not permit this transaction')
            duplicate=Transaction.query.filter(Transaction.reference==reference,Transaction.id!=t.id).first()
            if duplicate: raise ValueError('Duplicate Transaction ID')
            new_deltas={bank_id:amount if kind=='deposit' else -amount}
            if kind=='transfer': new_deltas[to_bank_id]=amount
            old_deltas={}
            for e in old_entries: old_deltas[e.bank_id]=old_deltas.get(e.bank_id,Decimal('0'))+money(e.delta)
            for bid in affected:
                projected=ledger_balance(bid)-old_deltas.get(bid,Decimal('0'))+new_deltas.get(bid,Decimal('0'))
                if projected<0: raise ValueError(f'Insufficient balance after edit: {banks_by_id[bid].name}')
            before=f'{t.reference} {t.kind} {t.signed_amount} entries={[(e.bank_id,str(e.delta)) for e in old_entries]}'
            for e in old_entries: db.session.delete(e)
            db.session.flush()
            for bid,delta in new_deltas.items(): db.session.add(Entry(transaction_id=t.id,bank_id=bid,delta=delta))
            t.reference=reference;t.kind=kind;t.signed_amount=amount if kind=='deposit' else -amount
            t.customer_ref=request.form.get('customer_ref','').strip()[:120] or None
            t.remark=request.form.get('remark','').strip()[:2000]
            db.session.add(Audit(actor_id=current_user.id,action='EDIT_TRANSACTION',detail=f'{t.id} BEFORE {before} AFTER {reference} {kind} {amount} entries={[(k,str(v)) for k,v in new_deltas.items()]}'))
            db.session.commit()
            flash('Transaction updated and balances recalculated','success')
            return redirect(url_for('transactions'))
        except (ValueError,IntegrityError) as e:
            db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('edit_transaction',transaction_id=transaction_id))

    @app.post('/transactions/<transaction_id>/delete')
    @role_required('admin','staff')
    def delete_transaction(transaction_id):
        reason=request.form.get('reason','').strip()
        if len(reason)<5: flash('Deletion reason must contain at least 5 characters','danger');return redirect(url_for('transactions'))
        try:
            t=db.session.execute(select(Transaction).where(Transaction.id==transaction_id).with_for_update()).scalar_one_or_none()
            if not t: abort(404)
            if t.status!='confirmed': raise ValueError('Transaction already voided')
            if t.business_date!=business_today(): raise ValueError('Only current-day transactions can be voided')
            entries=Entry.query.filter_by(transaction_id=t.id).all()
            affected={e.bank_id for e in entries}
            banks=lock_banks(affected)
            if Closing.query.filter(Closing.bank_id.in_(affected),Closing.business_date==t.business_date).first():
                raise ValueError('Cannot delete: an affected bank has been closed for this business date')
            # Removing a credit must not create a negative bank balance.
            by_bank={}
            for e in entries: by_bank[e.bank_id]=by_bank.get(e.bank_id,Decimal('0'))+money(e.delta)
            for b in banks:
                if ledger_balance(b.id)-by_bank.get(b.id,Decimal('0'))<0:
                    raise ValueError(f'Cannot void: {b.name} would have a negative balance')
            t.status='voided'
            db.session.add(Audit(actor_id=current_user.id,action='VOID_TRANSACTION',detail=f'{t.id} {t.reference}; reason={reason[:500]}'))
            db.session.commit()
            flash('Transaction voided; balances recalculated. History preserved.','success')
        except (ValueError,IntegrityError) as e:
            db.session.rollback();flash(str(e),'danger')
        return redirect(url_for('transactions'))

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
        day=selected_date()
        closes_all=Closing.query.filter_by(business_date=day).all()
        close_page=max(1,request.args.get('page',1,type=int))
        close_pagination=Closing.query.filter_by(business_date=day).order_by(Closing.created_at.desc(),Closing.id.desc()).paginate(page=close_page,per_page=15,error_out=False)
        return render_template('closing.html',banks=Bank.query.order_by(Bank.name).all(),balances=balances(),closes=close_pagination.items,close_pagination=close_pagination,closed_ids={c.bank_id for c in closes_all},selected_day=day,can_close=(day==today))

    @app.context_processor
    def inject_report_date():
        return {'report_date': request.args.get('date', business_today().isoformat()), 'today_myt': business_today().isoformat()}

    @app.get('/health')
    def health(): return {'status':'ok'}
    return app

app=create_app()
