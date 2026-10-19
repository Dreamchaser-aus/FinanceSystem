import os, uuid
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, url_for, flash, request, abort, g, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, select, UniqueConstraint, or_, and_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

MYT=ZoneInfo(os.getenv('BUSINESS_TIMEZONE','Asia/Kuala_Lumpur'))
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
    platform_access=db.relationship("UserPlatformAccess",cascade="all, delete-orphan",back_populates="user")
    permissions=db.relationship("UserPermission",cascade="all, delete-orphan")


class Platform(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    name=db.Column(db.String(120),nullable=False,unique=True)
    active=db.Column(db.Boolean,nullable=False,default=True)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)

class UserPlatformAccess(db.Model):
    __tablename__='user_platform_access'
    user_id=db.Column(db.String(36),db.ForeignKey('user.id'),primary_key=True)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),primary_key=True)
    user=db.relationship('User',back_populates='platform_access')

class UserPermission(db.Model):
    __tablename__='user_permission'
    user_id=db.Column(db.String(36),db.ForeignKey('user.id'),primary_key=True)
    permission=db.Column(db.String(60),primary_key=True)

PERMISSIONS={
    'view_dashboard':'View dashboard',
    'view_banks':'View banks',
    'create_transaction':'Create transactions',
    'view_transactions':'View transaction history',
    'edit_transaction':'Edit / void transactions',
    'update_bank_status':'Update bank status',
    'view_dealers':'View dealer ledger',
    'close_bank':'Daily closing',
    'view_bank_balances':'View all bank balances in assigned platform',
    'view_bank_movements':'View recent bank movements',
    'create_bank':'Create bank with zero opening balance',
    'view_dashboard_customers':'Dashboard customer statistics',
    'view_dashboard_financials':'Dashboard financial summary',
    'view_dashboard_closing':'Dashboard closing details',
    'view_dashboard_dealers':'Dashboard dealer summary',
    'view_dashboard_transactions':'Dashboard recent transactions',
    'manage_unclaimed':'Log and review unclaimed deposit leads',
}
DEFAULT_ROLE_PERMS={
    'staff':{'view_dashboard','view_bank_balances','view_bank_movements','create_transaction','view_transactions'},
    'finance':{'view_dashboard','view_banks','view_bank_balances','view_bank_movements','view_transactions','view_dealers','close_bank'},
    'manager':set(PERMISSIONS),
}

def permitted(permission):
    if current_user.role=='admin': return True
    return db.session.get(UserPermission,(current_user.id,permission)) is not None

def require_permission(permission):
    if not permitted(permission): abort(403)

def permitted_platform_ids():
    if current_user.role=='admin':
        return set(db.session.execute(select(Platform.id).where(Platform.active.is_(True))).scalars().all())
    return set(db.session.execute(select(UserPlatformAccess.platform_id).join(Platform,Platform.id==UserPlatformAccess.platform_id).where(UserPlatformAccess.user_id==current_user.id,Platform.active.is_(True))).scalars().all())

def selected_platform_id():
    """Fail closed for staff. Admin may choose All Platforms only for read-only reports."""
    requested=request.args.get('platform_id') or request.form.get('platform_id') or session.get('platform_id')
    allowed=permitted_platform_ids()
    if current_user.role=='admin' and requested=='all':return None
    if requested and requested in allowed: return requested
    if requested and requested not in allowed: abort(403)
    if current_user.role=='admin' and not requested:return None
    if len(allowed)==1:return next(iter(allowed))
    # Multi-platform staff must select a specific authorized platform.
    abort(400,description='Select an authorized platform from the platform switcher')

def platform_scope(model):
    pid=selected_platform_id()
    return (model.platform_id==pid) if pid else True

def require_platform(pid):
    if not pid or pid not in permitted_platform_ids():abort(403)
    chosen=selected_platform_id()
    if chosen is not None and pid!=chosen:abort(403)

def require_single_platform():
    pid=selected_platform_id()
    if pid is None: abort(400,description='Select one platform before making changes')
    require_platform(pid)
    return pid

def bank_for_platform(bank_id,pid):
    b=db.session.get(Bank,bank_id)
    if not b or b.platform_id!=pid:abort(403)
    return b

class Bank(db.Model):
    platform_id=db.Column(db.String(36),db.ForeignKey("platform.id"),nullable=False,index=True)
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
    platform_id=db.Column(db.String(36),db.ForeignKey("platform.id"),nullable=False,index=True)
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

class Dealer(db.Model):
    platform_id=db.Column(db.String(36),db.ForeignKey("platform.id"),nullable=False,index=True)
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    name=db.Column(db.String(120),unique=True,nullable=False)
    active=db.Column(db.Boolean,nullable=False,default=True)
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)

class DealerTrade(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    transaction_id=db.Column(db.String(36),db.ForeignKey('transaction.id'),unique=True,nullable=False,index=True)
    dealer_id=db.Column(db.String(36),db.ForeignKey('dealer.id'),nullable=False,index=True)
    aud_amount=db.Column(db.Numeric(18,2),nullable=False)
    rate=db.Column(db.Numeric(18,6),nullable=False)
    myr_amount=db.Column(db.Numeric(18,2),nullable=False)
    # positive: dealer owes us MYR; negative: we owe dealer MYR
    myr_delta=db.Column(db.Numeric(18,2),nullable=False)
    flow=db.Column(db.String(30),nullable=False)
    dealer=db.relationship('Dealer')
    transaction=db.relationship('Transaction')

class UnclaimedDeposit(db.Model):
    __tablename__='unclaimed_deposit'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    amount=db.Column(db.Numeric(18,2),nullable=False)
    reference=db.Column(db.String(120),nullable=False)
    remark=db.Column(db.Text)
    status=db.Column(db.String(20),nullable=False,default='unclaimed')
    transaction_id=db.Column(db.String(36),db.ForeignKey('transaction.id'))
    created_by=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    bank=db.relationship('Bank')
    __table_args__=(UniqueConstraint('bank_id','reference',name='uq_unclaimed_bank_reference'),)

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

def selected_range():
    """Dashboard only: inclusive MYT date range, max 366 days."""
    today = business_today()
    raw_start = request.args.get('start_date', '').strip()
    raw_end = request.args.get('end_date', '').strip()
    if not raw_start and not raw_end:
        single = selected_date()
        return single, single
    try:
        start = date.fromisoformat(raw_start or raw_end)
        end = date.fromisoformat(raw_end or raw_start)
    except ValueError:
        abort(400, description='Invalid start/end date')
    if start > end or end > today or (end - start).days > 365:
        abort(400, description='Date range must be ordered, not future, and at most 366 days')
    return start, end

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

def record(kind,reference,amount,bank_id,user_id,customer_ref='',remark='',to_bank_id=None,platform_id=None):
    if not reference.strip(): raise ValueError('Transaction ID required')
    if kind not in ('deposit','withdrawal','transfer'): raise ValueError('Unsupported type')
    if not platform_id: raise ValueError("Select platform")
    a=amount_positive(amount); ids=[bank_id]+([to_bank_id] if to_bank_id else [])
    if kind=='transfer' and (not to_bank_id or to_bank_id==bank_id): raise ValueError('Choose two different banks')
    if kind!='transfer' and to_bank_id: raise ValueError('Unexpected destination bank')
    day=business_today()
    try:
        bs=lock_banks(ids)
        if len(bs)!=len(ids) or any(not b.active or b.platform_id!=platform_id for b in bs): raise ValueError('Bank missing, inactive or belongs to another platform')
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
        t=Transaction(platform_id=platform_id,reference=reference.strip(),kind=kind,signed_amount=delta,customer_ref=customer_ref.strip() or None,remark=remark.strip(),created_by=user_id,business_date=day)
        db.session.add(t);db.session.flush()
        db.session.add(Entry(transaction_id=t.id,bank_id=bank_id,delta=delta))
        if kind=='transfer': db.session.add(Entry(transaction_id=t.id,bank_id=to_bank_id,delta=a))
        db.session.add(Audit(actor_id=user_id,action='CREATE_TRANSACTION',detail=f'{t.id} {kind} {reference}'))
        db.session.commit()
        return t
    except Exception:
        db.session.rollback();raise

def record_dealer(kind, reference, amount, bank_id, user_id, dealer_id, rate, customer_ref='', remark='',platform_id=None):
    if not platform_id: raise ValueError('Select platform')
    if kind not in ('dealer_sell','dealer_buy','dealer_payout'):
        raise ValueError('Unsupported dealer transaction')
    reference=reference.strip()
    if not reference: raise ValueError('Transaction ID required')
    aud=amount_positive(amount)
    try: fx=Decimal(str(rate))
    except (InvalidOperation,TypeError): raise ValueError('Invalid exchange rate')
    if not fx.is_finite() or fx<=0 or fx>Decimal('1000') or fx.as_tuple().exponent < -6:
        raise ValueError('Exchange rate must be positive with at most 6 decimals')
    myr=money(aud*fx)
    if kind=='dealer_payout' and not customer_ref.strip(): raise ValueError('Customer ID required')
    if kind=='dealer_payout' and bank_id: raise ValueError('Dealer direct payout must not select a bank')
    if kind!='dealer_payout' and not bank_id: raise ValueError('Select a real bank')
    day=business_today()
    try:
        dealer=db.session.execute(select(Dealer).where(Dealer.id==dealer_id).with_for_update()).scalar_one_or_none()
        if not dealer or not dealer.active or dealer.platform_id!=platform_id: raise ValueError('Dealer missing, inactive or belongs to another platform')
        if Transaction.query.filter_by(reference=reference).first(): raise ValueError('Duplicate Transaction ID')
        delta=Decimal('0')
        if bank_id:
            banks=lock_banks([bank_id])
            if len(banks)!=1 or not banks[0].active or banks[0].platform_id!=platform_id: raise ValueError('Bank missing, inactive or belongs to another platform')
            bank=banks[0]
            if bank.status!='active' and not (kind=='dealer_buy' and bank.status=='cannot_transfer'):
                raise ValueError(f'Bank {bank.name} status does not permit this movement')
            check_open_day(day,[bank_id])
            delta=-aud if kind=='dealer_sell' else aud
            if delta<0 and ledger_balance(bank_id)<aud: raise ValueError('Insufficient bank balance')
        # dealer payout counts as customer withdrawal, without any bank Entry
        t=Transaction(platform_id=platform_id,reference=reference,signed_amount=(-aud if kind!='dealer_buy' else aud),
                      customer_ref=customer_ref.strip() or None,remark=remark.strip()[:2000],created_by=user_id,business_date=day)
        db.session.add(t);db.session.flush()
        if bank_id: db.session.add(Entry(transaction_id=t.id,bank_id=bank_id,delta=delta))
        db.session.add(DealerTrade(transaction_id=t.id,dealer_id=dealer_id,aud_amount=aud,rate=fx,
                        myr_amount=myr,myr_delta=myr if kind=='dealer_sell' else -myr,flow=kind))
        db.session.add(Audit(actor_id=user_id,action='CREATE_DEALER_TRANSACTION',detail=f'{t.id} {kind} {reference} AUD={aud} rate={fx} MYR={myr}'))
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

    @app.before_request
    def enforce_platform_permissions():
        if not current_user.is_authenticated:return
        endpoint=request.endpoint or ''
        permission_routes={
            'dashboard':'view_dashboard',
            'new_transaction':'create_transaction','transactions':'view_transactions',
            'edit_transaction':'edit_transaction','delete_transaction':'edit_transaction',
            'update_bank_status':'update_bank_status','dealers':'view_dealers',
            'closing':'close_bank',
            'workspace':'view_bank_balances',
            'unclaimed_deposits':'manage_unclaimed',
        }
        if endpoint in permission_routes:require_permission(permission_routes[endpoint])

    @app.post('/switch-platform')
    @login_required
    def switch_platform():
        pid=request.form.get('platform_id','')
        if pid=='all' and current_user.role=='admin':pass
        elif pid not in permitted_platform_ids():abort(403)
        session['platform_id']=pid
        return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for('new_transaction') if permitted('create_transaction') and current_user.role!='admin' else url_for('dashboard'))

    @app.route('/admin/platforms',methods=['GET','POST'])
    @role_required('admin')
    def manage_platforms():
        if request.method=='POST':
            name=request.form.get('name','').strip()[:120]
            if not name:flash('Platform name is required','danger')
            else:
                try:
                    obj=Platform(name=name);db.session.add(obj)
                    db.session.flush()
                    db.session.add(Audit(actor_id=current_user.id,action='CREATE_PLATFORM',detail=f'{obj.id} {name}'))
                    db.session.commit();flash('Platform created','success')
                except IntegrityError:db.session.rollback();flash('Platform already exists','danger')
            return redirect(url_for('manage_platforms'))
        return render_template('platforms.html',platforms=Platform.query.order_by(Platform.name).all())

    @app.post('/admin/platforms/<pid>/update')
    @role_required('admin')
    def update_platform(pid):
        obj=db.session.get(Platform,pid)
        if not obj: abort(404)
        name=request.form.get('name','').strip()[:120]
        active=request.form.get('active')=='1'
        if not name:
            flash('Platform name is required','danger')
            return redirect(url_for('manage_platforms'))
        try:
            previous=f'{obj.name} active={obj.active}'
            obj.name=name;obj.active=active
            db.session.add(Audit(actor_id=current_user.id,action='UPDATE_PLATFORM',detail=f'{obj.id} {previous} -> {name} active={active}'))
            db.session.commit()
            if not active and session.get('platform_id')==pid:session['platform_id']='all'
            flash('Platform updated','success')
        except IntegrityError:
            db.session.rollback();flash('Platform name already exists','danger')
        return redirect(url_for('manage_platforms'))

    @app.route('/admin/staff',methods=['GET','POST'])
    @role_required('admin')
    def manage_staff():
        if request.method=='POST':
            try:
                username=request.form.get('username','').strip()
                password=request.form.get('password','')
                role=request.form.get('role','staff')
                selected=set(request.form.getlist('platform_ids'))
                allowed=set(db.session.execute(select(Platform.id)).scalars().all())
                perms=set(request.form.getlist('permissions'))
                if not username or len(username)>80 or len(password)<12:raise ValueError('Username required; password must be 12+ characters')
                if role not in DEFAULT_ROLE_PERMS:raise ValueError('Invalid role')
                if not selected or not selected.issubset(allowed):raise ValueError('Choose valid platforms')
                if not perms.issubset(PERMISSIONS):raise ValueError('Invalid permissions')
                u=User(username=username,password_hash=generate_password_hash(password),role=role)
                db.session.add(u);db.session.flush()
                for pid in selected:db.session.add(UserPlatformAccess(user_id=u.id,platform_id=pid))
                for permission in perms:db.session.add(UserPermission(user_id=u.id,permission=permission))
                db.session.add(Audit(actor_id=current_user.id,action='CREATE_STAFF',detail=f'{u.id} {username} role={role}'))
                db.session.commit();flash('Staff created','success')
            except (ValueError,IntegrityError) as exc:db.session.rollback();flash(str(exc),'danger')
            return redirect(url_for('manage_staff'))
        users=User.query.order_by(User.username).all()
        return render_template('staff.html',users=users,platforms=Platform.query.order_by(Platform.name).all(),permissions=PERMISSIONS,defaults=DEFAULT_ROLE_PERMS,user_permission_map={u.id:{p.permission for p in u.permissions} for u in users})

    @app.post('/admin/staff/<uid>/update')
    @role_required('admin')
    def update_staff(uid):
        u=db.session.get(User,uid)
        if not u or u.role=='admin':abort(403)
        selected=set(request.form.getlist('platform_ids'))
        allowed=set(db.session.execute(select(Platform.id)).scalars().all())
        perms=set(request.form.getlist('permissions'))
        role=request.form.get('role','staff')
        if not selected or not selected.issubset(allowed) or not perms.issubset(PERMISSIONS) or role not in DEFAULT_ROLE_PERMS:abort(400)
        u.role=role;u.active=request.form.get('active')=='1'
        UserPlatformAccess.query.filter_by(user_id=uid).delete()
        UserPermission.query.filter_by(user_id=uid).delete()
        db.session.flush()
        for pid in selected:db.session.add(UserPlatformAccess(user_id=uid,platform_id=pid))
        for perm in perms:db.session.add(UserPermission(user_id=uid,permission=perm))
        db.session.add(Audit(actor_id=current_user.id,action='UPDATE_STAFF',detail=f'{uid} role={role} active={u.active}'))
        db.session.commit();flash('Staff permissions updated','success')
        return redirect(url_for('manage_staff'))

    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            u=User.query.filter_by(username=request.form.get('username','')).first()
            if u and u.active and check_password_hash(u.password_hash,request.form.get('password','')):
                login_user(u)
                ids=permitted_platform_ids()
                session['platform_id']='all' if u.role=='admin' else (sorted(ids)[0] if ids else '')
                return redirect(url_for('dashboard') if u.role=='admin' else url_for('new_transaction') if permitted('create_transaction') else url_for('workspace') if permitted('view_bank_balances') else url_for('transactions') if permitted('view_transactions') else url_for('dashboard'))
            flash('Invalid credentials','danger')
        return render_template('login.html')

    @app.post('/logout')
    @login_required
    def logout(): logout_user();session.pop('platform_id',None);return redirect(url_for('login'))

    @app.get('/')
    @login_required
    def dashboard():
        if not any(permitted(p) for p in ('view_dashboard_customers','view_dashboard_financials','view_dashboard_closing','view_dashboard_dealers','view_dashboard_transactions','view_bank_balances')) and current_user.role!='admin':
            return render_template('dashboard_limited.html')
        start_day, day = selected_range()
        bank_page = max(1, request.args.get('bank_page', 1, type=int))
        close_page = max(1, request.args.get('close_page', 1, type=int))
        all_banks = Bank.query.filter(platform_scope(Bank)).order_by(Bank.kind, Bank.name).all()
        history = BankStatusHistory.query.filter(BankStatusHistory.bank_id.in_([b.id for b in all_banks])).order_by(BankStatusHistory.created_at, BankStatusHistory.id).all()
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
            .where(Transaction.business_date.between(start_day, day), Transaction.status == 'confirmed',platform_scope(Transaction))
            .group_by(Transaction.kind)).all()
        totals = {k: {'count': n, 'amount': money(v)} for k, n, v in rows}
        latest = (Transaction.query.filter(Transaction.status=='confirmed', Transaction.business_date.between(start_day, day),platform_scope(Transaction))
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
                    transaction_banks[t.id] = items[0][1] if items else ('Dealer → Customer' if t.kind=='dealer_payout' else 'Unknown')
        closed_query = (Closing.query.filter(Closing.business_date.between(start_day, day),Closing.bank_id.in_([b.id for b in all_banks]))
                        .order_by(Closing.created_at.desc(), Closing.id.desc()))
        closed_page = closed_query.paginate(page=close_page, per_page=10, error_out=False)
        # A customer is identified by normalized customer_ref (customer ID).
        # Active = distinct IDs with confirmed deposit/withdrawal in selected range.
        # New = active IDs whose first ever confirmed deposit/withdrawal is in range.
        activity_filter = (platform_scope(Transaction),Transaction.status == 'confirmed',
                           Transaction.kind.in_(['deposit', 'withdrawal', 'dealer_payout']),
                           Transaction.customer_ref.isnot(None),
                           func.length(func.trim(Transaction.customer_ref)) > 0)
        normalized_id = func.lower(func.trim(Transaction.customer_ref))
        active_ids = set(db.session.execute(
            select(normalized_id).where(*activity_filter,
                Transaction.business_date.between(start_day, day)).distinct()
        ).scalars().all())
        first_seen = db.session.execute(
            select(normalized_id, func.min(Transaction.business_date))
            .where(*activity_filter).group_by(normalized_id)
        ).all()
        first_dates = dict(first_seen)
        new_count = sum(1 for customer_id in active_ids
                        if start_day <= first_dates[customer_id] <= day)
        dealer_items = Dealer.query.filter(platform_scope(Dealer)).order_by(Dealer.name).all()
        dealer_rows = db.session.execute(
            select(DealerTrade.dealer_id, DealerTrade.flow,
                   func.sum(DealerTrade.aud_amount), func.sum(DealerTrade.myr_delta))
            .join(Transaction, Transaction.id == DealerTrade.transaction_id)
            .where(Transaction.status == 'confirmed',platform_scope(Transaction))
            .group_by(DealerTrade.dealer_id, DealerTrade.flow)
        ).all()
        dealer_totals = {}
        for did, flow, aud, myr_delta in dealer_rows:
            item = dealer_totals.setdefault(did, {
                'dealer_sell': Decimal('0'), 'dealer_buy': Decimal('0'),
                'dealer_payout': Decimal('0'), 'myr': Decimal('0')})
            item[flow] = money(aud)
            item['myr'] += money(myr_delta)
        return render_template(
            'dashboard.html', selected_day=day, start_day=start_day,
            dealer_items=dealer_items, dealer_totals=dealer_totals,
            active_customers=len(active_ids), new_customers=new_count,
            banks=bank_items, balances=bal,
            totals=totals, latest=latest, transaction_banks=transaction_banks,
            grand=active_total, page_total=sum((bal[b.id] for b in bank_items), Decimal('0')),
            bank_page=bank_page, bank_pages=bank_pages, active_count=len(active_banks),
            block_count=len(blocked), block_banks=blocked, closing_total=closing_total,
            closed_banks=closed_page.items, closed_page=closed_page,
            closed_count=closed_page.total, statuses=statuses)

    @app.route('/banks',methods=['GET','POST'])
    @login_required
    def banks():
        if not (permitted('view_banks') or permitted('create_bank')):abort(403)
        if request.method=='POST':
            require_permission('create_bank')
            try:
                name=request.form.get('name','').strip()
                kind=request.form.get('kind','')
                if not name or kind not in ('deposit','withdrawal','store','other'): raise ValueError('Invalid bank details')
                pid=require_single_platform()
                b=Bank(platform_id=pid,name=name,kind=kind,opening_balance=money(request.form.get('opening_balance','0')) if current_user.role=='admin' else Decimal('0'))
                db.session.add(b);db.session.flush()
                db.session.add(Audit(actor_id=current_user.id,action='CREATE_BANK',detail=f'{b.id} {name}'))
                db.session.commit();flash('Bank created','success')
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('banks'))
        day=selected_date()
        bank_page=max(1,request.args.get('bank_page',1,type=int))
        history_page=max(1,request.args.get('history_page',1,type=int))
        bank_pagination=Bank.query.filter(platform_scope(Bank)).order_by(Bank.kind,Bank.name).paginate(page=bank_page,per_page=15,error_out=False)
        end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=MYT).astimezone(timezone.utc)
        history_pagination=BankStatusHistory.query.filter(BankStatusHistory.created_at < end,BankStatusHistory.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).order_by(BankStatusHistory.created_at.desc(),BankStatusHistory.id.desc()).paginate(page=history_page,per_page=15,error_out=False)
        return render_template('banks.html',banks=bank_pagination.items,balances=balances() if permitted('view_bank_balances') else {},
                               statuses=BANK_STATUSES,history=history_pagination.items,bank_pagination=bank_pagination,history_pagination=history_pagination,selected_day=day)

    @app.post('/banks/<bank_id>/status')
    @role_required('admin','staff')
    def update_bank_status(bank_id):
        bank_for_platform(bank_id,require_single_platform())
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

    @app.route('/dealers',methods=['GET','POST'])
    @login_required
    def dealers():
        if request.method=='POST':
            require_permission('create_bank')
            pid=require_single_platform()
            name=request.form.get('name','').strip()
            if not name: flash('Dealer name required','danger')
            else:
                try:
                    db.session.add(Dealer(name=name,platform_id=pid));db.session.commit();flash('Dealer added','success')
                except IntegrityError:
                    db.session.rollback();flash('Dealer already exists','danger')
            return redirect(url_for('banks') + '#add-dealer')
        items=Dealer.query.filter(platform_scope(Dealer)).order_by(Dealer.name).all()
        rows=db.session.execute(select(DealerTrade.dealer_id,DealerTrade.flow,func.sum(DealerTrade.aud_amount),func.sum(DealerTrade.myr_delta)).join(Transaction,Transaction.id==DealerTrade.transaction_id).where(Transaction.status=='confirmed',platform_scope(Transaction)).group_by(DealerTrade.dealer_id,DealerTrade.flow)).all()
        totals={}
        for did,flow,aud,myr in rows:
            d=totals.setdefault(did,{'dealer_sell':Decimal('0'),'dealer_buy':Decimal('0'),'dealer_payout':Decimal('0'),'myr':Decimal('0')})
            d[flow]+=aud;d['myr']+=myr
        return render_template('dealers.html',dealers=items,totals=totals)

    @app.route('/transactions/new',methods=['GET','POST'])
    @login_required
    def new_transaction():
        if request.method=='POST':
            pid=require_single_platform()
            try:
                kind=request.form.get('kind','')
                if kind in ('dealer_sell','dealer_buy','dealer_payout'):
                    record_dealer(kind,request.form.get('reference',''),request.form.get('amount',''),
                                  request.form.get('bank_id','') if kind!='dealer_payout' else None,
                                  current_user.id,request.form.get('dealer_id',''),request.form.get('rate',''),
                                  request.form.get('customer_ref',''),request.form.get('remark',''),platform_id=pid)
                else:
                    record(kind,request.form.get('reference',''),request.form.get('amount',''),request.form.get('bank_id',''),current_user.id,request.form.get('customer_ref',''),request.form.get('remark',''),request.form.get('to_bank_id') or None,platform_id=pid)
                flash('Transaction confirmed and balance updated','success');return redirect(url_for('new_transaction'))
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
        
        balance_banks=[];balance_values={};recent=[];recent_pagination=None
        if permitted('view_bank_balances'):
            balance_banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
            balance_values={b.id:ledger_balance(b.id) for b in balance_banks}
            bank_ids=[b.id for b in balance_banks]
            if permitted('view_bank_movements') and bank_ids:
                movement_query=(select(Entry,Transaction,Bank.name)
                    .join(Transaction,Transaction.id==Entry.transaction_id)
                    .join(Bank,Bank.id==Entry.bank_id)
                    .where(Entry.bank_id.in_(bank_ids),Transaction.status=='confirmed',platform_scope(Transaction))
                    .order_by(Transaction.created_at.desc(),Entry.id.desc()))
                movement_count=db.session.scalar(select(func.count()).select_from(movement_query.order_by(None).subquery())) or 0
                page=max(1,request.args.get('recent_page',1,type=int))
                per_page=10
                total_pages=max(1,(movement_count+per_page-1)//per_page)
                page=min(page,total_pages)
                recent=db.session.execute(movement_query.offset((page-1)*per_page).limit(per_page)).all()
                recent_pagination={'page':page,'pages':total_pages,'total':movement_count,'has_prev':page>1,'has_next':page<total_pages}
        return render_template('transaction.html',banks=Bank.query.filter(platform_scope(Bank),Bank.active==True).order_by(Bank.name).all(),dealers=Dealer.query.filter(platform_scope(Dealer),Dealer.active==True).order_by(Dealer.name).all(),balance_banks=balance_banks,balance_values=balance_values,recent=recent,recent_pagination=recent_pagination)

    @app.get('/workspace')
    @login_required
    def workspace():
        require_permission('view_bank_balances')
        banks_list=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
        bank_ids=[b.id for b in banks_list]
        current_balances={b.id:ledger_balance(b.id) for b in banks_list}
        recent=[]
        if permitted('view_bank_movements') and bank_ids:
            recent=db.session.execute(select(Entry,Transaction,Bank.name)
                .join(Transaction,Transaction.id==Entry.transaction_id)
                .join(Bank,Bank.id==Entry.bank_id)
                .where(Entry.bank_id.in_(bank_ids),Transaction.status=='confirmed',platform_scope(Transaction))
                .order_by(Transaction.created_at.desc()).limit(40)).all()
        pending={}
        if permitted('manage_unclaimed') and bank_ids:
            rows=db.session.execute(select(UnclaimedDeposit.bank_id,func.count(UnclaimedDeposit.id))
                .where(UnclaimedDeposit.bank_id.in_(bank_ids),UnclaimedDeposit.status=='unclaimed',platform_scope(UnclaimedDeposit))
                .group_by(UnclaimedDeposit.bank_id)).all()
            pending=dict(rows)
        return render_template('workspace.html',banks=banks_list,balances=current_balances,
                               recent=recent,pending=pending)

    @app.route('/unclaimed',methods=['GET','POST'])
    @login_required
    def unclaimed_deposits():
        require_permission('manage_unclaimed')
        if request.method=='POST':
            pid=require_single_platform()
            try:
                bank_id=request.form.get('bank_id','')
                bank_for_platform(bank_id,pid)
                amount=amount_positive(request.form.get('amount',''))
                reference=request.form.get('reference','').strip()[:120]
                if not reference:raise ValueError('Bank reference is required')
                # Leads do NOT post a ledger entry or alter calculated bank balances.
                obj=UnclaimedDeposit(platform_id=pid,bank_id=bank_id,amount=amount,
                    reference=reference,remark=request.form.get('remark','').strip()[:2000],created_by=current_user.id)
                db.session.add(obj);db.session.flush()
                db.session.add(Audit(actor_id=current_user.id,action='LOG_UNCLAIMED',detail=f'{obj.id} bank={bank_id}'))
                db.session.commit();flash('Unclaimed deposit lead recorded; bank balance unchanged','success')
            except (ValueError,IntegrityError) as exc:
                db.session.rollback();flash(str(exc),'danger')
            return redirect(url_for('unclaimed_deposits'))
        banks_list=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
        rows=UnclaimedDeposit.query.filter(platform_scope(UnclaimedDeposit)).order_by(UnclaimedDeposit.created_at.desc()).limit(100).all()
        return render_template('unclaimed.html',banks=banks_list,items=rows)

    @app.post('/unclaimed/<lead_id>/claim')
    @login_required
    def claim_unclaimed(lead_id):
        require_permission('manage_unclaimed')
        lead=db.session.get(UnclaimedDeposit,lead_id)
        if not lead:abort(404)
        require_platform(lead.platform_id)
        transaction_ref=request.form.get('transaction_reference','').strip()
        if not transaction_ref:abort(400,description='Provide a confirmed deposit transaction reference')
        tx=Transaction.query.filter_by(reference=transaction_ref,platform_id=lead.platform_id,
                                      kind='deposit',status='confirmed').first()
        if not tx:abort(400,description='Confirmed customer deposit not found in this platform')
        entry=Entry.query.filter_by(transaction_id=tx.id,bank_id=lead.bank_id).first()
        if not entry or money(entry.delta)!=money(lead.amount):
            abort(400,description='Bank or amount does not match confirmed deposit')
        if lead.status!='unclaimed':abort(409)
        lead.status='claimed';lead.transaction_id=tx.id
        db.session.add(Audit(actor_id=current_user.id,action='CLAIM_UNCLAIMED',detail=f'{lead.id} transaction={tx.id}'))
        db.session.commit();flash('Linked to confirmed deposit; no additional bank movement posted','success')
        return redirect(url_for('unclaimed_deposits'))

    @app.get('/transactions')
    @login_required
    def transactions():
        from sqlalchemy import or_
        day = selected_date()
        q = Transaction.query.filter(Transaction.business_date == day,platform_scope(Transaction))
        search = request.args.get('search', '').strip()
        kinds = [v for v in request.args.getlist('kind')
                 if v in ('deposit', 'withdrawal', 'transfer','dealer_sell','dealer_buy','dealer_payout')]
        kinds = list(dict.fromkeys(kinds))
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
        if kinds:
            q = q.filter(Transaction.kind.in_(kinds))
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
                    transaction_banks[t.id] = movements[0][1] if movements else ('Dealer → Customer' if t.kind=='dealer_payout' else 'Unknown')

        return render_template('transactions.html', records=records,
                               transaction_banks=transaction_banks,
                               banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all(),
                               search=search, kinds=kinds, bank_id=bank_id, selected_day=day)

    @app.route('/transactions/<transaction_id>/edit', methods=['GET','POST'])
    @role_required('admin','staff')
    def edit_transaction(transaction_id):
        t=db.session.get(Transaction,transaction_id)
        if not t: abort(404)
        require_platform(t.platform_id)
        if request.method=='POST' and require_single_platform()!=t.platform_id:abort(403)
        if request.method=='GET':
            if t.kind.startswith('dealer_'):
                flash('Dealer transactions require a controlled reversal; editing is disabled','warning')
                return redirect(url_for('transactions'))
            if t.status!='confirmed':
                flash('Voided transactions cannot be edited','warning')
                return redirect(url_for('transactions'))
            entries=Entry.query.filter_by(transaction_id=t.id).all()
            source=next((e for e in entries if e.delta<0),None) if t.kind=='transfer' else (entries[0] if entries else None)
            destination=next((e for e in entries if e.delta>0),None) if t.kind=='transfer' else None
            return render_template('transaction_edit.html',t=t,banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all(),source_id=source.bank_id if source else '',destination_id=destination.bank_id if destination else '')
        try:
            # Serialize all edits/deletes of the same transaction.
            t=db.session.execute(select(Transaction).where(Transaction.id==transaction_id).with_for_update()).scalar_one_or_none()
            if not t: abort(404)
            require_platform(t.platform_id)
            if require_single_platform()!=t.platform_id:abort(403)
            if t.kind.startswith('dealer_'): raise ValueError('Dealer transactions require an audited reversal; edit/void is disabled')
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
            if any(b.platform_id!=t.platform_id for b in locked):abort(403)
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
            require_platform(t.platform_id)
            if require_single_platform()!=t.platform_id:abort(403)
            if t.kind.startswith('dealer_'): raise ValueError('Dealer transactions require an audited reversal; edit/void is disabled')
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
                pid=require_single_platform()
                bank_id=request.form.get('bank_id','');bank_for_platform(bank_id,pid);actual=money(request.form.get('actual',''))
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
        closes_all=Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).all()
        close_page=max(1,request.args.get('page',1,type=int))
        close_pagination=Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).order_by(Closing.created_at.desc(),Closing.id.desc()).paginate(page=close_page,per_page=15,error_out=False)
        return render_template('closing.html',banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all(),balances=balances(),closes=close_pagination.items,close_pagination=close_pagination,closed_ids={c.bank_id for c in closes_all},selected_day=day,can_close=(day==today))

    @app.context_processor
    def inject_report_date():
        return {'report_date': request.args.get('date', business_today().isoformat()), 'today_myt': business_today().isoformat(), 'platform_options': Platform.query.filter(Platform.id.in_(permitted_platform_ids()),Platform.active.is_(True)).order_by(Platform.name).all() if current_user.is_authenticated else [], 'active_platform_id': session.get('platform_id','all' if current_user.is_authenticated and current_user.role=='admin' else ''), 'can': permitted if current_user.is_authenticated else lambda _:False}

    @app.get('/health')
    def health(): return {'status':'ok'}
    return app

app=create_app()
