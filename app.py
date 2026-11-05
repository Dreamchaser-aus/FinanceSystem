import os, uuid, json, hashlib, secrets, subprocess, tempfile, shutil
from datetime import datetime, timezone, date, timedelta, time
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo
from flask import Flask, render_template, redirect, url_for, flash, request, abort, g, session, send_file
from flask_sqlalchemy import SQLAlchemy
from urllib.parse import parse_qs
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func, select, UniqueConstraint, Index, or_, and_, case, text, delete
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken
from closing_engine import next_boundary, as_utc, effective_timezone, period_business_day
from period_reporting import utc_bank_overview, utc_ledger, day_bank_overview
from financial_integrity import verify_period

# Legacy name kept to limit the diff: this is the business display timezone, now Melbourne by default.
MYT=ZoneInfo(os.getenv('BUSINESS_TIMEZONE','Australia/Melbourne'))
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
    'close_bank':'Daily Reconciliation (Finance role only)',
    'view_bank_balances':'View all bank balances in assigned platform',
    'view_bank_movements':'View recent bank movements',
    'create_bank':'Create bank (zero opening balance) / exchange dealer',
    'view_bank_details':'View bank account details',
    'view_sensitive_bank_details':'View bank login credentials',
    'edit_bank_details':'Edit bank account details',
    'view_dashboard_customers':'Dashboard customer statistics',
    'view_dashboard_financials':'Dashboard financial summary',
    'view_dashboard_closing':'Dashboard closing details',
    'view_dashboard_dealers':'Dashboard dealer summary',
    'view_dashboard_transactions':'Dashboard recent transactions',
    'manage_unclaimed':'Log and review unclaimed deposit leads',
    'manage_bank_labels':'Create and assign bank labels',
    'view_audit_logs':'View transaction audit trail for authorized platforms',
}
DEFAULT_ROLE_PERMS={
    'staff':{'view_dashboard','view_bank_balances','view_bank_movements','create_transaction','view_transactions'},
    'finance':{'view_dashboard','view_banks','view_bank_balances','view_bank_movements','view_transactions','view_dealers','close_bank'},
    'manager':set(PERMISSIONS),
}

def permitted(permission):
    if current_user.role=='admin': return True
    if permission == 'close_bank' and current_user.role != 'finance':
        return False
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
    name=db.Column(db.String(120),nullable=False)
    kind=db.Column(db.String(20),nullable=False,default='deposit')
    opening_balance=db.Column(db.Numeric(18,2),nullable=False,default=0)
    active=db.Column(db.Boolean,nullable=False,default=True)
    status=db.Column(db.String(30),nullable=False,default='active')
    status_remark=db.Column(db.Text)
    status_updated_at=db.Column(db.DateTime(timezone=True))
    status_updated_by=db.Column(db.String(36),db.ForeignKey('user.id'))
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)
    __table_args__=(UniqueConstraint('platform_id','name',name='uq_bank_platform_name'),)

class BankDetail(db.Model):
    __tablename__='bank_detail'
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),primary_key=True)
    payid=db.Column(db.String(180))
    bsb_number=db.Column(db.String(12))
    account_number=db.Column(db.String(40))
    phone_number=db.Column(db.String(40))
    date_of_birth=db.Column(db.String(20))
    card_details=db.Column(db.String(120))  # Masked card identifier only, never full PAN
    expiry_date=db.Column(db.String(12))
    login_id=db.Column(db.String(180))
    login_password_cipher=db.Column(db.Text)
    otp_api_link=db.Column(db.Text)
    remark=db.Column(db.Text)
    updated_by=db.Column(db.String(36),db.ForeignKey('user.id'))
    updated_at=db.Column(db.DateTime(timezone=True),default=now)

class BankEncryptionKey(db.Model):
    """Persistent, single-purpose Fernet key stored in the Railway PostgreSQL database.

    Back up the PostgreSQL database securely: losing this row makes saved credentials unreadable.
    """
    __tablename__='bank_encryption_key'
    id=db.Column(db.Integer,primary_key=True)
    key_value=db.Column(db.String(128),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)


def initialize_bank_encryption_key():
    """Run during Railway bootstrap; insert exactly once even with concurrent deploys."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    configured=os.getenv('BANK_DETAILS_ENCRYPTION_KEY','').strip()
    if configured:
        try: Fernet(configured.encode('ascii'))
        except (ValueError,TypeError) as exc: raise RuntimeError('Invalid existing BANK_DETAILS_ENCRYPTION_KEY') from exc
    candidate=configured or Fernet.generate_key().decode('ascii')
    if db.engine.dialect.name=='postgresql':
        statement=pg_insert(BankEncryptionKey).values(id=1,key_value=candidate).on_conflict_do_nothing(index_elements=['id'])
        db.session.execute(statement)
        db.session.commit()
    else:
        if db.session.get(BankEncryptionKey,1) is None:
            db.session.add(BankEncryptionKey(id=1,key_value=candidate))
            db.session.commit()
    saved=db.session.get(BankEncryptionKey,1)
    if configured and saved.key_value!=configured:
        raise RuntimeError('Existing Railway key differs from stored key; stop to protect encrypted passwords')
    Fernet(saved.key_value.encode('ascii'))
    print('Bank details encryption key ready in persistent PostgreSQL storage')


def bank_cipher():
    saved=db.session.get(BankEncryptionKey,1)
    if saved is None:
        raise RuntimeError('Bank encryption key not initialized; run Railway bootstrap')
    return Fernet(saved.key_value.encode('ascii'))

BANK_DETAIL_FIELDS=('payid','bsb_number','account_number','phone_number','date_of_birth','card_details','expiry_date','login_id','otp_api_link','remark')

def save_bank_detail(bank,form):
    detail=db.session.get(BankDetail,bank.id)
    if detail is None:
        detail=BankDetail(bank_id=bank.id)
        db.session.add(detail)
    for field in BANK_DETAIL_FIELDS:
        if field in form:
            value=form.get(field,'').strip()
            limit=2000 if field in ('otp_api_link','remark') else 180
            if len(value)>limit: raise ValueError(f'{field} exceeds maximum length')
            if field=='card_details' and value:
                if not (value.isascii() and value.isdigit() and len(value)==4):
                    raise ValueError('Card Last 4 Digits must contain exactly four digits')
            setattr(detail,field,value or None)
    if 'login_password' in form and form.get('login_password'):
        detail.login_password_cipher=bank_cipher().encrypt(form['login_password'].encode()).decode()
    detail.updated_by=current_user.id
    detail.updated_at=now()
    return detail

class BankLabel(db.Model):
    """Optional platform-scoped, admin-managed labels; no ledger impact."""
    __tablename__='bank_label'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    name=db.Column(db.String(60),nullable=False)
    __table_args__=(UniqueConstraint('platform_id','name',name='uq_bank_label_platform_name'),)

class BankLabelAssignment(db.Model):
    __tablename__='bank_label_assignment'
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),primary_key=True)
    label_id=db.Column(db.String(36),db.ForeignKey('bank_label.id'),primary_key=True,index=True)

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
    __table_args__=(UniqueConstraint('bank_id','reference',name='uq_unclaimed_bank_reference'),
        Index('uq_unclaimed_transaction_link', 'transaction_id', unique=True, postgresql_where=db.text('transaction_id IS NOT NULL'), sqlite_where=db.text('transaction_id IS NOT NULL')))

class UnclaimAllocation(db.Model):
    """Immutable allocation event; no bank Entry is posted."""
    __tablename__='unclaim_allocation'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    original_transaction_id=db.Column(db.String(36),db.ForeignKey('transaction.id'),nullable=False,index=True)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    deposit_reference=db.Column(db.String(120),unique=True,nullable=False)
    customer_ref=db.Column(db.String(120),nullable=False)
    amount=db.Column(db.Numeric(18,2),nullable=False)
    reason=db.Column(db.Text,nullable=False)
    created_by=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now,index=True)

class OperatingClosingSnapshot(db.Model):
    """Immutable operating figures; separate from existing actual-ledger snapshot."""
    __tablename__='operating_closing_snapshot'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    closing_period_id=db.Column(db.String(36),db.ForeignKey('closing_period.id'),nullable=False,unique=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    actual_bank_closing=db.Column(db.Numeric(18,2),nullable=False)
    pending_unclaimed_closing=db.Column(db.Numeric(18,2),nullable=False)
    operating_closing=db.Column(db.Numeric(18,2),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)

class BlockedUnclaimedSettlement(db.Model):
    """Immutable point-in-time bank block snapshot; never posts ledger entries."""
    __tablename__='blocked_unclaimed_settlement'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    status_history_id=db.Column(db.String(36),db.ForeignKey('bank_status_history.id'),nullable=False,unique=True)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    ledger_balance=db.Column(db.Numeric(18,2),nullable=False)
    unclaimed_amount=db.Column(db.Numeric(18,2),nullable=False)
    unclaimed_count=db.Column(db.Integer,nullable=False)
    with_unclaimed=db.Column(db.Numeric(18,2),nullable=False)
    leads_json=db.Column(db.Text,nullable=False)
    remark=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    bank=db.relationship('Bank')
    actor=db.relationship('User')

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

class ClosingFinancialSnapshot(db.Model):
    """Write-once financial and unclaimed snapshot at the bank's UTC closing boundary."""
    __tablename__='closing_financial_snapshot'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    closing_period_id=db.Column(db.String(36),db.ForeignKey('closing_period.id'),nullable=False,unique=True)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    opening=db.Column(db.Numeric(18,2),nullable=False)
    incoming=db.Column(db.Numeric(18,2),nullable=False)
    outgoing=db.Column(db.Numeric(18,2),nullable=False)
    closing_balance=db.Column(db.Numeric(18,2),nullable=False)
    unclaimed_amount=db.Column(db.Numeric(18,2),nullable=False)
    unclaimed_count=db.Column(db.Integer,nullable=False)
    unclaimed_json=db.Column(db.Text,nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)

class BusinessTimezoneChange(db.Model):
    """Append-only timezone policy; timestamps before each change keep their prior GMT offset."""
    __tablename__='business_timezone_change'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    effective_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now,index=True)
    offset_minutes=db.Column(db.Integer,nullable=False)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)

class ClosingPolicy(db.Model):
    """Current policy for FUTURE boundaries only; historical periods remain immutable."""
    __tablename__='closing_policy'
    id=db.Column(db.Integer,primary_key=True)
    auto_timezone=db.Column(db.Boolean,nullable=False,default=True)
    manual_offset_minutes=db.Column(db.Integer,nullable=False,default=600)
    closing_hour=db.Column(db.Integer,nullable=False,default=0)
    closing_minute=db.Column(db.Integer,nullable=False,default=0)
    updated_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    updated_by=db.Column(db.String(36),db.ForeignKey('user.id'))

class ClosingPeriod(db.Model):
    """Explicit per-bank immutable UTC boundaries. Historical rows are NOT backfilled."""
    __tablename__='closing_period'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    bank_id=db.Column(db.String(36),db.ForeignKey('bank.id'),nullable=False,index=True)
    start_utc=db.Column(db.DateTime(timezone=True),nullable=False)
    end_utc=db.Column(db.DateTime(timezone=True),nullable=False)
    status=db.Column(db.String(20),nullable=False,default='closed')
    closing_id=db.Column(db.String(36),db.ForeignKey('closing.id'),unique=True)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    __table_args__=(UniqueConstraint('bank_id','start_utc',name='uq_closing_period_start'),UniqueConstraint('bank_id','end_utc',name='uq_closing_period_end'))

class Audit(db.Model):
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False)
    action=db.Column(db.String(60),nullable=False)
    detail=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),default=now,nullable=False)

class PlatformResetBackupProof(db.Model):
    """Shared, short-lived server-side proof of a completed pg_dump generation."""
    __tablename__='platform_reset_backup_proof'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    admin_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False,index=True)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    nonce_hash=db.Column(db.String(64),nullable=False)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now)
    expires_at=db.Column(db.DateTime(timezone=True),nullable=False)
    used=db.Column(db.Boolean,nullable=False,default=False)

class TransactionAudit(db.Model):
    """Append-only application audit; created in same DB transaction as ledger changes."""
    __tablename__='transaction_audit'
    id=db.Column(db.String(36),primary_key=True,default=idgen)
    platform_id=db.Column(db.String(36),db.ForeignKey('platform.id'),nullable=False,index=True)
    transaction_id=db.Column(db.String(36),db.ForeignKey('transaction.id'),nullable=False,index=True)
    actor_id=db.Column(db.String(36),db.ForeignKey('user.id'),nullable=False,index=True)
    action=db.Column(db.String(30),nullable=False,index=True)
    before_json=db.Column(db.Text)
    after_json=db.Column(db.Text)
    reason=db.Column(db.Text)
    created_at=db.Column(db.DateTime(timezone=True),nullable=False,default=now,index=True)
    actor=db.relationship('User')
    transaction=db.relationship('Transaction')


def transaction_snapshot(tx, entries=None):
    if entries is None:
        entries=Entry.query.filter_by(transaction_id=tx.id).order_by(Entry.bank_id,Entry.id).all()
    return {'id':tx.id,'platform_id':tx.platform_id,'reference':tx.reference,
            'kind':tx.kind,'signed_amount':str(tx.signed_amount),'status':tx.status,
            'customer_ref':tx.customer_ref,'remark':tx.remark,
            'business_date':tx.business_date.isoformat() if tx.business_date else None,
            'created_by':tx.created_by,
            'entries':[{'bank_id':e.bank_id,'delta':str(e.delta)} for e in entries]}


def write_transaction_audit(tx,actor_id,action,before=None,after=None,reason=None):
    db.session.add(TransactionAudit(platform_id=tx.platform_id,transaction_id=tx.id,
        actor_id=actor_id,action=action,
        before_json=json.dumps(before,ensure_ascii=False,sort_keys=True) if before is not None else None,
        after_json=json.dumps(after,ensure_ascii=False,sort_keys=True) if after is not None else None,
        reason=reason))

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

def ledger_balance_at_utc(bank_id, end_utc):
    """Historical ledger snapshot at an exclusive UTC boundary."""
    bank=db.session.get(Bank,bank_id)
    if bank is None: raise ValueError('Bank not found')
    return money(bank.opening_balance)+money(utc_ledger(db,Bank,Entry,Transaction,[bank_id],end_utc).get(bank_id,0))

def pending_unclaim_at_utc(bank_id, end_utc):
    """Cumulative posted Unclaims less allocations, as of an exclusive UTC instant."""
    credited=db.session.execute(select(func.coalesce(func.sum(Entry.delta),0))
        .join(Transaction,Entry.transaction_id==Transaction.id)
        .where(Entry.bank_id==bank_id,Transaction.kind=='unclaim',
               Transaction.status=='confirmed',Transaction.created_at<end_utc)).scalar()
    assigned=db.session.execute(select(func.coalesce(func.sum(UnclaimAllocation.amount),0))
        .where(UnclaimAllocation.bank_id==bank_id,UnclaimAllocation.created_at<end_utc)).scalar()
    return money(credited)-money(assigned)

def pending_unclaim_through_day(bank_id, day, event_end_utc):
    """Pending Unclaim as of the end of business day `day`.

    Posted Unclaim credits follow their business_date (like every other ledger entry);
    allocations are real-time events, so they count when created before event_end_utc.
    """
    credited=db.session.execute(select(func.coalesce(func.sum(Entry.delta),0))
        .join(Transaction,Entry.transaction_id==Transaction.id)
        .where(Entry.bank_id==bank_id,Transaction.kind=='unclaim',
               Transaction.status=='confirmed',Transaction.business_date<=day)).scalar()
    assigned=db.session.execute(select(func.coalesce(func.sum(UnclaimAllocation.amount),0))
        .where(UnclaimAllocation.bank_id==bank_id,UnclaimAllocation.created_at<event_end_utc)).scalar()
    return money(credited)-money(assigned)

def operating_balance_at_utc(bank_id,end_utc):
    return ledger_balance_at_utc(bank_id,end_utc)-pending_unclaim_at_utc(bank_id,end_utc)

def utc_report_bounds(day):
    """Wall-clock policy closing boundaries for the selected local report day."""
    policy=db.session.get(ClosingPolicy,1)
    if policy is None: raise ValueError('Configure Business Timezone first')
    tz=effective_timezone(policy.auto_timezone,policy.manual_offset_minutes)
    target=datetime.combine(day,datetime.min.time(),tzinfo=tz).astimezone(timezone.utc)
    start=next_boundary(target-timedelta(days=3),policy.closing_hour,policy.closing_minute,policy.auto_timezone,policy.manual_offset_minutes)
    while start.astimezone(tz).date()<day:
        start=next_boundary(start,policy.closing_hour,policy.closing_minute,policy.auto_timezone,policy.manual_offset_minutes)
    if start.astimezone(tz).date()!=day: raise ValueError('Report date has no closing boundary')
    end=next_boundary(start,policy.closing_hour,policy.closing_minute,policy.auto_timezone,policy.manual_offset_minutes)
    return start,end

def ledger_balance(bank_id,through=None):
    q=select(func.coalesce(func.sum(Entry.delta),0)).where(Entry.bank_id==bank_id).join(Transaction,Entry.transaction_id==Transaction.id).where(Transaction.status=='confirmed')
    if through is not None: q=q.where(Transaction.business_date<=through)
    bank=db.session.get(Bank,bank_id)
    return money(bank.opening_balance)+money(db.session.execute(q).scalar_one())

def timezone_at(instant=None):
    """Resolve offset from append-only policy; earlier timestamps never change display."""
    instant=instant or now()
    if instant.tzinfo is None:instant=instant.replace(tzinfo=timezone.utc)
    try:
        row=(BusinessTimezoneChange.query.filter(BusinessTimezoneChange.effective_at<=instant)
             .order_by(BusinessTimezoneChange.effective_at.desc()).first())
        if row:return timezone(timedelta(minutes=row.offset_minutes))
    except Exception:
        # Used before the initial migration in startup paths only.
        pass
    return MYT

def timezone_for_report_day(day):
    """Offset active at the end of the selected calendar day; previous days retain their policy."""
    end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)
    return timezone_at(end)

def business_today(): return now().astimezone(timezone_at()).date()

def format_business_time(d):
    if not d:return ''
    if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone_at(d)).strftime('%Y-%m-%d %H:%M')


def validate_entry_business_day(value):
    """Only MYT today/yesterday are permitted; enforced server-side."""
    today=business_today()
    if value is None or value=='': return today
    try: chosen=date.fromisoformat(str(value))
    except (TypeError,ValueError): raise ValueError('Invalid business date')
    if chosen not in (today,today-timedelta(days=1)):
        raise ValueError('Transaction date must be today or yesterday (Melbourne time)')
    return chosen


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
    day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone_for_report_day(day)).astimezone(timezone.utc)
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


def unclaimed_balance_components(bank_ids, day=None):
    """Pending unclaimed leads are bank-reported credits, not posted ledger entries.

    Only current open leads are included. Historical snapshots cannot reconstruct
    subsequently claimed/dismissed leads without a status history table.
    """
    if not bank_ids: return {}
    q=UnclaimedDeposit.query.filter(UnclaimedDeposit.bank_id.in_(bank_ids),
                                    UnclaimedDeposit.status=='unclaimed')
    leads=q.all()
    result={bid:Decimal('0.00') for bid in bank_ids}
    for lead in leads:
        created=lead.created_at
        if day is not None and created is not None:
            if created.tzinfo is None: created=created.replace(tzinfo=timezone.utc)
            if created.astimezone(timezone_at(created)).date()>day: continue
        result[lead.bank_id]+=money(lead.amount)
    return result

def bank_daily_overview(bank_list, day=None):
    """Per-bank posted movements for the MYT business day; no manual balance input.

    Opening is the calculated ledger balance immediately before day; Current is
    the all-time ledger balance (including later posted transactions, if any).
    The displayed daily balance is the as-of-day close and is independent of
    the current balance when historical dates are selected.
    """
    if day is None: day=business_today()
    ids=[b.id for b in bank_list]
    if not ids:return {}
    base=(select(Entry.bank_id,
                 func.coalesce(func.sum(Entry.delta),0).label('delta'),
                 func.count(Entry.id).label('entries'),
                 func.coalesce(func.sum(case((Entry.delta>0,1),else_=0)),0).label('in_count'),
                 func.coalesce(func.sum(case((Entry.delta<0,1),else_=0)),0).label('out_count'),
                 func.coalesce(func.sum(case((Entry.delta>0,Entry.delta),else_=0)),0).label('in_amount'),
                 func.coalesce(func.sum(case((Entry.delta<0,-Entry.delta),else_=0)),0).label('out_amount'))
          .join(Transaction,Entry.transaction_id==Transaction.id)
          .where(Entry.bank_id.in_(ids),Transaction.status=='confirmed',Transaction.business_date==day)
          .group_by(Entry.bank_id))
    daily={r.bank_id:r for r in db.session.execute(base).all()}
    before=dict(db.session.execute(
        select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0))
        .join(Transaction,Entry.transaction_id==Transaction.id)
        .where(Entry.bank_id.in_(ids),Transaction.status=='confirmed',Transaction.business_date<day)
        .group_by(Entry.bank_id)).all())
    total=dict(db.session.execute(
        select(Entry.bank_id,func.coalesce(func.sum(Entry.delta),0))
        .join(Transaction,Entry.transaction_id==Transaction.id)
        .where(Entry.bank_id.in_(ids),Transaction.status=='confirmed')
        .group_by(Entry.bank_id)).all())
    pending=unclaimed_balance_components(ids,day)
    out={}
    for b in bank_list:
        r=daily.get(b.id)
        opening=money(b.opening_balance)+money(before.get(b.id,0))
        change=money(r.delta) if r else Decimal('0.00')
        out[b.id]={'opening':opening,'daily':change,'day_close':opening+change,
                   'current':money(b.opening_balance)+money(total.get(b.id,0)),
                   'unclaimed':pending.get(b.id,Decimal('0.00')),
                   'in_count':int(r.in_count) if r else 0,'out_count':int(r.out_count) if r else 0,
                   'in_amount':money(r.in_amount) if r else Decimal('0.00'),
                   'out_amount':money(r.out_amount) if r else Decimal('0.00')}
    return out

def check_open_day(day,bank_ids):
    # Accept only today/yesterday in MYT, including direct internal calls.
    day=validate_entry_business_day(day.isoformat() if isinstance(day,date) else day)
    if Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(bank_ids)).first():
        raise ValueError('Bank has already been reconciled/closed for the selected business date')

def lock_banks(ids):
    # Postgres row locks serialize balance checks and updates; SQLite is for local demo only.
    return db.session.execute(select(Bank).where(Bank.id.in_(sorted(set(ids)))).order_by(Bank.id).with_for_update()).scalars().all()

def record(kind,reference,amount,bank_id,user_id,customer_ref='',remark='',to_bank_id=None,platform_id=None,business_day=None):
    # Legacy internal transfers use the same double-entry ledger as ordinary bank transfers.
    if kind=='internal_transfer': kind='transfer'
    reference=(reference or '').strip() if kind in ('deposit','withdrawal') else 'BF-'+uuid.uuid4().hex[:20].upper()
    if not reference: raise ValueError('Customer transaction ID required')
    if kind not in ('deposit','withdrawal','transfer','cash_in','cash_out','unclaim','internal_transfer'): raise ValueError('Unsupported type')
    if not platform_id: raise ValueError("Select platform")
    a=amount_positive(amount); ids=[bank_id]+([to_bank_id] if to_bank_id else [])
    if kind in ('transfer','internal_transfer') and (not to_bank_id or to_bank_id==bank_id): raise ValueError('Choose two different banks')
    if kind not in ('transfer','internal_transfer') and to_bank_id: raise ValueError('Unexpected destination bank')
    day=validate_entry_business_day(business_day)
    try:
        bs=lock_banks(ids)
        if len(bs)!=len(ids) or any(not b.active or b.platform_id!=platform_id for b in bs): raise ValueError('Bank missing, inactive or belongs to another platform')
        # A bank marked cannot_transfer may receive funds but cannot send funds.
        # All other non-active states block new movements in either direction.
        for b in bs:
            is_source = b.id == bank_id
            if b.status == 'active':
                continue
            if b.status == 'cannot_transfer' and not is_source and kind in ('transfer','internal_transfer'):
                continue
            if b.status == 'cannot_transfer' and is_source and kind in ('deposit','cash_in','unclaim'):
                continue
            raise ValueError(f'Bank {b.name} status is {BANK_STATUSES.get(b.status,b.status)}; this transaction is not permitted')
        check_open_day(day,ids)
        for bid in ids:
            last=ClosingPeriod.query.filter_by(bank_id=bid,status='closed').order_by(ClosingPeriod.end_utc.desc()).first()
            if last and now() < (last.end_utc if last.end_utc.tzinfo else last.end_utc.replace(tzinfo=timezone.utc)):
                raise ValueError('Transaction timestamp precedes last finalized closing boundary')
        if Transaction.query.filter_by(reference=reference.strip()).first(): raise ValueError('Duplicate Transaction ID')
        delta=a if kind in ('deposit','cash_in','unclaim') else -a
        if delta<0 and ledger_balance(bank_id)<a: raise ValueError('Insufficient balance (negative balances blocked for new debits)')
        t=Transaction(platform_id=platform_id,reference=reference.strip(),kind=kind,signed_amount=delta,customer_ref=customer_ref.strip() or None,remark=remark.strip(),created_by=user_id,business_date=day)
        db.session.add(t);db.session.flush()
        db.session.add(Entry(transaction_id=t.id,bank_id=bank_id,delta=delta))
        if kind in ('transfer','internal_transfer'): db.session.add(Entry(transaction_id=t.id,bank_id=to_bank_id,delta=a))
        db.session.flush()
        write_transaction_audit(t,user_id,'CREATE',after=transaction_snapshot(t))
        db.session.add(Audit(actor_id=user_id,action='CREATE_TRANSACTION',detail=f'{t.id} {kind} {reference}'))
        db.session.commit()
        return t
    except Exception:
        db.session.rollback();raise

def record_deposit_back(withdraw_reference, deposit_reference, amount, user_id,
                        customer_ref='', remark='', platform_id=None, business_day=None):
    """Two counted customer movements, one atomic posting, zero net bank balance."""
    if not platform_id: raise ValueError('Select platform')
    wr=(withdraw_reference or '').strip(); dr=(deposit_reference or '').strip()
    if not wr or not dr: raise ValueError('Both Withdrawal and Deposit Transaction IDs are required')
    if wr==dr: raise ValueError('Withdrawal and Deposit Transaction IDs must be different')
    if len(wr)>120 or len(dr)>120: raise ValueError('Transaction ID too long')
    a=amount_positive(amount); day=validate_entry_business_day(business_day)
    try:
        # Internal re-deposit never touches a real bank or the Closing Engine ledger.
        # Business-day validation still applies; no bank can be selected or debited.
        if db.session.execute(select(Transaction.id).where(Transaction.reference.in_([wr,dr])).limit(1)).first():
            raise ValueError('Duplicate Transaction ID')
        group='DEPOSIT_BACK:'+uuid.uuid4().hex.upper()
        note=(remark or '').strip()[:1500]
        pair=[]
        for kind,ref,delta in (('withdrawal',wr,-a),('deposit',dr,a)):
            tx=Transaction(platform_id=platform_id,reference=ref,kind=kind,signed_amount=delta,
                customer_ref=(customer_ref or '').strip() or None,
                remark=f'[{group}] Internal re-deposit (no external bank movement). {note}',
                created_by=user_id,business_date=day)
            db.session.add(tx);db.session.flush()
            write_transaction_audit(tx,user_id,'CREATE',after=transaction_snapshot(tx))
            db.session.add(Audit(actor_id=user_id,action='CREATE_DEPOSIT_BACK',detail=f'{group} {tx.id} {kind} {ref} AUD={a}'))
            pair.append(tx)
        db.session.commit()
        return pair
    except Exception:
        db.session.rollback();raise

def record_dealer(kind, reference, amount, bank_id, user_id, dealer_id, rate, customer_ref='', remark='',platform_id=None,business_day=None):
    if not platform_id: raise ValueError('Select platform')
    if kind not in ('dealer_sell','dealer_buy','dealer_payout'):
        raise ValueError('Unsupported dealer transaction')
    reference=(reference or '').strip() if kind=='dealer_payout' else 'BF-'+uuid.uuid4().hex[:20].upper()
    if not reference: raise ValueError('Customer transaction ID required')
    aud=amount_positive(amount)
    try: fx=Decimal(str(rate))
    except (InvalidOperation,TypeError): raise ValueError('Invalid exchange rate')
    if not fx.is_finite() or fx<=0 or fx>Decimal('1000') or fx.as_tuple().exponent < -6:
        raise ValueError('Exchange rate must be positive with at most 6 decimals')
    myr=money(aud*fx)
    if kind=='dealer_payout' and not customer_ref.strip(): raise ValueError('Customer ID required')
    if kind=='dealer_payout' and bank_id: raise ValueError('Dealer direct payout must not select a bank')
    if kind!='dealer_payout' and not bank_id: raise ValueError('Select a real bank')
    day=validate_entry_business_day(business_day)
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
        t=Transaction(platform_id=platform_id,kind=kind,reference=reference,signed_amount=(-aud if kind!='dealer_buy' else aud),
                      customer_ref=customer_ref.strip() or None,remark=remark.strip()[:2000],created_by=user_id,business_date=day)
        db.session.add(t);db.session.flush()
        if bank_id: db.session.add(Entry(transaction_id=t.id,bank_id=bank_id,delta=delta))
        db.session.add(DealerTrade(transaction_id=t.id,dealer_id=dealer_id,aud_amount=aud,rate=fx,
                        myr_amount=myr,myr_delta=myr if kind=='dealer_sell' else -myr,flow=kind))
        db.session.flush()
        write_transaction_audit(t,user_id,'CREATE',after=transaction_snapshot(t))
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
    app.jinja_env.filters['mytime']=format_business_time

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
        # Fail closed if PostgreSQL cannot install immutable financial-record guards.
        from release_guards import install_postgres_guards
        install_postgres_guards(db)
        initialize_bank_encryption_key()
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
            'update_bank_status':'update_bank_status','dealers':('create_bank' if request.method == 'POST' else 'view_dealers'),
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
        page = request.form.get('return_page', '')
        parent_pages = {
            'reveal_bank_password': 'banks', 'bank_details': 'banks', 'blocked_settlements': 'banks',
            'bank_investigation': 'workspace', 'edit_transaction': 'transactions',
            'transaction_audit_detail': 'transaction_audit_list',
        }
        page = parent_pages.get(page, page)
        pages = {
            'dashboard', 'banks', 'dealers', 'new_transaction', 'workspace',
            'unclaimed_deposits', 'transactions', 'transaction_audit_list',
            'closing', 'utc_reconciliation', 'closing_verification', 'change_password',
            'manage_staff', 'manage_platforms', 'manage_timezone', 'clear_platform_settings',
        }
        if page not in pages:
            page = 'new_transaction' if current_user.role != 'admin' and permitted('create_transaction') else 'dashboard'
        filters = {
            key: values for key, values in parse_qs(request.form.get('return_filters', '')).items()
            if key in ('date', 'start_date', 'end_date', 'from_date', 'to_date',
                       'date_from', 'date_to', 'all_dates', 'kind', 'category',
                       'bank_group', 'label', 'status', 'search', 'movement_search',
                       'reference', 'action', 'actor')
        }
        return redirect(url_for(page, **filters))

    @app.route('/admin/timezone',methods=['GET','POST'])
    @role_required('admin')
    def manage_timezone():
        policy=db.session.get(ClosingPolicy,1)
        if not policy:
            policy=ClosingPolicy(id=1,auto_timezone=True,manual_offset_minutes=600,closing_hour=0,closing_minute=0)
            db.session.add(policy);db.session.commit()
        if request.method=='POST':
            try:
                mode=request.form.get('auto_timezone','active')
                if mode not in ('active','inactive'):raise ValueError('Invalid timezone mode')
                minutes=int(request.form.get('offset_minutes',str(policy.manual_offset_minutes)))
                hour=int(request.form.get('closing_hour','0'))
                minute=int(request.form.get('closing_minute','0'))
                effective_timezone(mode=='active',minutes)
                if not 0<=hour<24 or not 0<=minute<60:raise ValueError('Invalid closing time')
                old=f'auto={policy.auto_timezone},offset={policy.manual_offset_minutes},clock={policy.closing_hour:02d}:{policy.closing_minute:02d}'
                policy.auto_timezone=(mode=='active');policy.manual_offset_minutes=minutes
                policy.closing_hour=hour;policy.closing_minute=minute
                policy.updated_at=now();policy.updated_by=current_user.id
                db.session.add(Audit(actor_id=current_user.id,action='UPDATE_CLOSING_POLICY',detail=f'{old} -> auto={policy.auto_timezone},offset={minutes},clock={hour:02d}:{minute:02d}; existing periods unchanged'))
                db.session.commit();flash('Future closing policy updated. Existing closed periods are unchanged.','success')
            except (ValueError,TypeError) as e:
                db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('manage_timezone'))
        history=BusinessTimezoneChange.query.order_by(BusinessTimezoneChange.effective_at.desc()).limit(30).all()
        eff_now=now().astimezone(effective_timezone(policy.auto_timezone,policy.manual_offset_minutes))
        off_min=int(eff_now.utcoffset().total_seconds()//60)
        live_label=f"{eff_now.tzname() if policy.auto_timezone else 'Manual'} GMT{'+' if off_min>=0 else '-'}{abs(off_min)//60:02d}:{abs(off_min)%60:02d}"
        return render_template('timezone_settings.html',offset_minutes=policy.manual_offset_minutes,history=history,policy=policy,live_label=live_label)

    @app.get('/admin/clear-platform')
    @role_required('admin')
    def clear_platform_settings():
        platforms=Platform.query.order_by(Platform.name).all()
        proof=(PlatformResetBackupProof.query.filter_by(admin_id=current_user.id,used=False)
            .filter(PlatformResetBackupProof.expires_at>now())
            .order_by(PlatformResetBackupProof.created_at.desc()).first())
        token=request.cookies.get('bankflow_reset_token','')
        if not (proof and token and secrets.compare_digest(
                proof.nonce_hash,hashlib.sha256(token.encode()).hexdigest())):
            proof=None
        return render_template('clear_platform.html',platforms=platforms,
            backup_ready=bool(proof),backup_proof=proof)

    @app.post('/admin/clear-platform/backup')
    @role_required('admin')
    def clear_platform_backup():
        # Never accept 'all', and never infer a platform from a stale browser selection.
        pid=request.form.get('platform_id','')
        platform=db.session.get(Platform,pid)
        if not platform or pid=='all': abort(400)
        if db.engine.dialect.name!='postgresql':
            flash('Backup requires PostgreSQL; reset disabled.','danger')
            return redirect(url_for('clear_platform_settings'))
        binary=shutil.which('pg_dump')
        if not binary:
            flash('pg_dump is not installed on this server. Create a PostgreSQL backup externally before enabling reset.','danger')
            return redirect(url_for('clear_platform_settings'))
        # A full database backup is intentional: foreign-key relationships and global settings matter.
        # Never log credentials or return pg_dump stderr (which could include connection details).
        fd,path=tempfile.mkstemp(prefix='bankflow_backup_',suffix='.dump')
        os.close(fd)
        try:
            url=db.engine.url
            env=os.environ.copy()
            if url.password: env['PGPASSWORD']=url.password
            cmd=[binary,'--format=custom','--no-owner','--no-acl','--file',path,
                '--host',url.host or 'localhost','--port',str(url.port or 5432),
                '--username',url.username or '',url.database or '']
            result=subprocess.run(cmd,env=env,capture_output=True,timeout=180,check=False)
            if result.returncode!=0 or os.path.getsize(path)<100:
                raise RuntimeError('PostgreSQL backup failed; nothing was cleared')
            with open(path,'rb') as f:
                if f.read(5)!=b'PGDMP':raise RuntimeError('Invalid PostgreSQL custom-format backup')
            # Persist proof in PostgreSQL so all Gunicorn workers see the same state.
            # This confirms successful generation, NOT that the browser saved or restored it.
            token=secrets.token_urlsafe(32)
            db.session.execute(delete(PlatformResetBackupProof).where(
                PlatformResetBackupProof.admin_id==current_user.id))
            proof=PlatformResetBackupProof(admin_id=current_user.id,platform_id=pid,
                nonce_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now()+timedelta(minutes=15))
            db.session.add(proof)
            db.session.commit()
            response=send_file(path,as_attachment=True,download_name='bankflow_full_backup_'+now().strftime('%Y%m%d_%H%M%S')+'.dump',mimetype='application/octet-stream')
            # Set an HttpOnly cookie on the download response; no cross-worker memory state.
            response.set_cookie('bankflow_reset_token',token,max_age=900,httponly=True,
                secure=app.config['SESSION_COOKIE_SECURE'],samesite='Lax',path='/admin/clear-platform')
            @response.call_on_close
            def cleanup():
                try:os.remove(path)
                except OSError:pass
            return response
        except (OSError,RuntimeError,subprocess.TimeoutExpired):
            try:os.remove(path)
            except OSError:pass
            db.session.rollback()
            flash('Database backup could not be generated. Reset remains disabled.','danger')
            return redirect(url_for('clear_platform_settings'))

    @app.post('/admin/clear-platform/execute')
    @role_required('admin')
    def clear_platform_execute():
        pid=request.form.get('platform_id','')
        platform=db.session.get(Platform,pid)
        if not platform or pid=='all':abort(400)
        token=request.cookies.get('bankflow_reset_token','')
        proof=(PlatformResetBackupProof.query.filter_by(admin_id=current_user.id,
            platform_id=pid,used=False).filter(PlatformResetBackupProof.expires_at>now())
            .order_by(PlatformResetBackupProof.created_at.desc()).first())
        if (not token or not proof or
                not secrets.compare_digest(proof.nonce_hash,hashlib.sha256(token.encode()).hexdigest()) or
                request.form.get('backup_proof_id','')!=proof.id):
            abort(400,description='Generate a fresh backup for this platform first')
        if request.form.get('confirm_text','')!='CLEAR ALL DATA':
            flash('Confirmation text must be exactly CLEAR ALL DATA','danger')
            return redirect(url_for('clear_platform_settings'))
        if not check_password_hash(current_user.password_hash,request.form.get('password','')):
            flash('Incorrect administrator password.','danger')
            return redirect(url_for('clear_platform_settings'))
        if request.form.get('backup_saved')!='yes':
            flash('Confirm that the downloaded backup is saved before clearing.','danger')
            return redirect(url_for('clear_platform_settings'))
        # The reset is a single transaction; all deletes are scoped to explicit platform IDs.
        # Database-level immutable financial triggers recognize the reset transaction flag.
        try:
            with db.session.begin_nested():
                if db.engine.dialect.name=='postgresql':
                    db.session.execute(text("SELECT set_config('bankflow.allow_platform_reset', 'true', true)"))
                bank_ids=select(Bank.id).where(Bank.platform_id==pid)
                tx_ids=select(Transaction.id).where(Transaction.platform_id==pid)
                dealer_ids=select(Dealer.id).where(Dealer.platform_id==pid)
                period_ids=select(ClosingPeriod.id).where(ClosingPeriod.bank_id.in_(bank_ids))
                status_ids=select(BankStatusHistory.id).where(BankStatusHistory.bank_id.in_(bank_ids))
                # Delete leaf rows before their parents; never touch global users/configuration.
                db.session.execute(delete(TransactionAudit).where(TransactionAudit.platform_id==pid))
                db.session.execute(delete(OperatingClosingSnapshot).where(OperatingClosingSnapshot.closing_period_id.in_(period_ids)))
                db.session.execute(delete(ClosingFinancialSnapshot).where(ClosingFinancialSnapshot.closing_period_id.in_(period_ids)))
                db.session.execute(delete(ClosingPeriod).where(ClosingPeriod.bank_id.in_(bank_ids)))
                db.session.execute(delete(Closing).where(Closing.bank_id.in_(bank_ids)))
                db.session.execute(delete(BlockedUnclaimedSettlement).where(BlockedUnclaimedSettlement.platform_id==pid))
                db.session.execute(delete(BankStatusHistory).where(BankStatusHistory.bank_id.in_(bank_ids)))
                db.session.execute(delete(UnclaimAllocation).where(UnclaimAllocation.platform_id==pid))
                db.session.execute(delete(UnclaimedDeposit).where(UnclaimedDeposit.platform_id==pid))
                db.session.execute(delete(DealerTrade).where(DealerTrade.transaction_id.in_(tx_ids)))
                db.session.execute(delete(Entry).where(Entry.transaction_id.in_(tx_ids)))
                db.session.execute(delete(Transaction).where(Transaction.platform_id==pid))
                db.session.execute(delete(BankLabelAssignment).where(BankLabelAssignment.bank_id.in_(bank_ids)))
                db.session.execute(delete(BankDetail).where(BankDetail.bank_id.in_(bank_ids)))
                db.session.execute(delete(Bank).where(Bank.platform_id==pid))
                db.session.execute(delete(BankLabel).where(BankLabel.platform_id==pid))
                db.session.execute(delete(Dealer).where(Dealer.platform_id==pid))
                proof.used=True
                db.session.add(Audit(actor_id=current_user.id,action='CLEAR_PLATFORM_DATA',
                    detail=f'platform_id={pid} platform_name={platform.name} reset performed after pg_dump generation and administrator backup confirmation'))
            db.session.commit()
            flash(f'Platform {platform.name}: business data cleared. Platform, accounts and settings retained.','success')
        except Exception:
            db.session.rollback()
            flash('Reset failed; database transaction rolled back. No partial reset was committed.','danger')
        response=redirect(url_for('clear_platform_settings'))
        if not db.session.get(PlatformResetBackupProof,proof.id) or db.session.get(PlatformResetBackupProof,proof.id).used:
            response.delete_cookie('bankflow_reset_token',path='/admin/clear-platform')
        return response

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

    @app.route('/account/password', methods=['GET', 'POST'])
    @login_required
    def change_password():
        if request.method == 'POST':
            old = request.form.get('current_password', '')
            new = request.form.get('new_password', '')
            previous_hash = current_user.password_hash
            if not check_password_hash(previous_hash, old):
                flash('Current password is incorrect.', 'danger')
            elif len(new) < 6:
                flash('Use at least 6 characters for your new password.', 'danger')
            elif new != request.form.get('confirm_password'):
                flash('New passwords do not match.', 'danger')
            elif new == old:
                flash('Choose a different password.', 'danger')
            else:
                uid = current_user.id
                changed = User.query.filter_by(id=uid, password_hash=previous_hash).update(
                    {'password_hash': generate_password_hash(new)}, synchronize_session=False)
                if changed:
                    db.session.add(Audit(actor_id=uid, action='CHANGE_PASSWORD', detail='Self-service password change'))
                    db.session.commit()
                    logout_user()
                    session.clear()
                    flash('Password changed. Sign in with your new password.', 'success')
                    return redirect(url_for('login'))
                db.session.rollback()
                flash('Password was changed in another request. Enter your current password again.', 'danger')
        return render_template('change_password.html')

    @app.post('/logout')
    @login_required
    def logout(): logout_user();session.pop('platform_id',None);return redirect(url_for('login'))

    def dashboard_utc_window(start_day, end_day):
        """Business-day labels refer to the local interval starting at closing time.

        E.g. Melbourne 00:00 on Oct 20 labels the interval Oct 20 00:00
        through Oct 21 00:00. Current-day reports stop at the current instant.
        Historical closing snapshots are only matched on their exact UTC boundary.
        """
        policy = db.session.get(ClosingPolicy, 1)
        auto = policy.auto_timezone if policy else True
        offset = policy.manual_offset_minutes if policy else 600
        hour = policy.closing_hour if policy else 0
        minute = policy.closing_minute if policy else 0
        zone = effective_timezone(auto, offset)

        def boundary(d):
            # Choose the first valid occurrence on the specified local date.
            # For a DST gap, use the first real time after the gap.
            candidates = []
            for fold in (0, 1):
                local = datetime.combine(d, time(hour, minute)).replace(tzinfo=zone, fold=fold)
                instant = local.astimezone(timezone.utc)
                if instant.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
                    candidates.append(instant)
            if candidates:
                return min(candidates)
            # A nonexistent clock time on DST spring-forward day: use the
            # earliest next valid boundary (not an imaginary local instant).
            probe = datetime.combine(d, time(0, 0), tzinfo=zone).astimezone(timezone.utc) - timedelta(seconds=1)
            return next_boundary(probe, hour, minute, auto, offset)

        start = boundary(start_day)
        end = boundary(end_day + timedelta(days=1))
        # When today's configured closing hour has not yet arrived, the
        # currently open business period began at yesterday's closing.
        if start_day == end_day == business_today() and start > now():
            start = boundary(start_day - timedelta(days=1))
            end = boundary(start_day)
        return start, end

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
        range_start_utc, scheduled_end_utc = dashboard_utc_window(start_day, day)
        # Never include future transactions in a live (unfinished) period.
        range_end_utc = min(scheduled_end_utc, now())
        if range_end_utc <= range_start_utc:
            # The selected business-day period has not started yet.
            range_end_utc = range_start_utc
        day_end = range_end_utc
        existing = [b for b in all_banks if b.created_at is None or _utc_db(b.created_at) < day_end]
        statuses = {b.id: (b.status if day == business_today() else bank_status_on(b, day, history_by_bank)) for b in existing}
        # Amounts follow business_date so the dashboard equals the closing figures.
        period_stats = day_bank_overview(db, Bank, Entry, Transaction, existing, start_day, day) if existing else {}
        if not period_stats:
            period_stats = {b.id: dict(opening=money(b.opening_balance),in_amount=Decimal('0'),out_amount=Decimal('0'),in_count=0,out_count=0,day_close=money(b.opening_balance)) for b in existing}
        # Use immutable snapshots only for a matching single completed period.
        # A snapshot cannot safely be reused for a multi-day range or different UTC bounds.
        historical_snapshot_ids = set()
        historical_unclaimed_missing = set()
        if start_day == day and scheduled_end_utc <= now() and existing:
            periods = db.session.execute(select(ClosingPeriod, ClosingFinancialSnapshot)
                .join(ClosingFinancialSnapshot, ClosingFinancialSnapshot.closing_period_id == ClosingPeriod.id)
                .where(ClosingPeriod.bank_id.in_([b.id for b in existing]), ClosingPeriod.status == 'closed',
                       ClosingPeriod.start_utc == range_start_utc, ClosingPeriod.end_utc == scheduled_end_utc)).all()
            for period, snapshot in periods:
                period_stats[period.bank_id] = dict(
                    opening=money(snapshot.opening), in_amount=money(snapshot.incoming),
                    out_amount=money(snapshot.outgoing),
                    in_count=period_stats[period.bank_id]['in_count'],
                    out_count=period_stats[period.bank_id]['out_count'],
                    day_close=money(snapshot.closing_balance))
                historical_snapshot_ids.add(period.bank_id)
        # V7.9.6.35: Posted Unclaim is a distinct dashboard category, not IN.
        # The ledger entry remains a genuine credit; presentation reclassifies
        # that credit rather than posting it a second time.
        posted_unclaim = {b.id: Decimal('0.00') for b in existing}
        if existing:
            unclaim_rows = db.session.execute(
                select(Entry.bank_id, func.coalesce(func.sum(Entry.delta), 0), func.count(Entry.id))
                .join(Transaction, Entry.transaction_id == Transaction.id)
                .where(Entry.bank_id.in_([b.id for b in existing]),
                       Transaction.kind == 'unclaim', Transaction.status == 'confirmed',
                       Transaction.business_date >= start_day,
                       Transaction.business_date <= day)
                .group_by(Entry.bank_id)).all()
            for bank_id, amount, count in unclaim_rows:
                posted_unclaim[bank_id] = money(amount)
                period_stats[bank_id]['in_amount'] = money(period_stats[bank_id]['in_amount']) - money(amount)
                period_stats[bank_id]['in_count'] = max(0, int(period_stats[bank_id]['in_count']) - int(count))
        # Operating opening excludes cumulative unallocated Unclaim from prior days.
        pending_open={b.id:pending_unclaim_through_day(b.id,start_day-timedelta(days=1),range_start_utc) for b in existing}
        pending_close={b.id:pending_unclaim_through_day(b.id,day,range_end_utc) for b in existing}
        for bank in existing:
            stats=period_stats[bank.id]
            stats['opening']=money(stats['opening'])-pending_open[bank.id]
            stats['day_close']=money(stats['day_close'])-pending_close[bank.id]
        allocation_rows=db.session.execute(select(UnclaimAllocation.bank_id,
            func.coalesce(func.sum(UnclaimAllocation.amount),0),func.count(UnclaimAllocation.id))
            .where(UnclaimAllocation.bank_id.in_([b.id for b in existing]),
                   UnclaimAllocation.created_at>=range_start_utc,
                   UnclaimAllocation.created_at<range_end_utc)
            .group_by(UnclaimAllocation.bank_id)).all() if existing else []
        for bank_id, amount, count in allocation_rows:
            period_stats[bank_id]['in_amount']=money(period_stats[bank_id]['in_amount'])+money(amount)
            period_stats[bank_id]['in_count']+=count
        bal = {b.id: period_stats[b.id]['day_close'] for b in existing}
        unclaimed_components = unclaimed_balance_components([b.id for b in existing], day)
        if day < business_today():
            # Current mutable lead statuses must not masquerade as historical balances.
            unclaimed_components = {b.id: Decimal('0') for b in existing}
            historical_unclaimed_missing = {b.id for b in existing}
            if start_day == day and historical_snapshot_ids:
                snapshot_rows = db.session.execute(select(ClosingFinancialSnapshot)
                    .join(ClosingPeriod, ClosingFinancialSnapshot.closing_period_id == ClosingPeriod.id)
                    .where(ClosingPeriod.bank_id.in_(list(historical_snapshot_ids)),
                           ClosingPeriod.start_utc == range_start_utc,
                           ClosingPeriod.end_utc == scheduled_end_utc)).scalars().all()
                for snap in snapshot_rows:
                    unclaimed_components[snap.bank_id] = money(snap.unclaimed_amount)
                    historical_unclaimed_missing.discard(snap.bank_id)
        # Dashboard columns: Before Unclaimed + Unclaimed = With Unclaimed.
        # Only the selected period's posted Unclaim is moved out of IN and
        # into Unclaimed; earlier posted Unclaims remain in the opening balance.
        before_unclaim = {b.id: bal[b.id] for b in existing}
        unclaimed_components = {b.id: pending_close[b.id] for b in existing}
        display_bal = {b.id: before_unclaim[b.id] + unclaimed_components[b.id] for b in existing}
        # "Bank Block" means status=closed; other issues remain in the closing total.
        blocked = [b for b in existing if statuses[b.id] == 'closed']
        active_banks = [b for b in existing if statuses[b.id] == 'active']
        # Bank balance tabs use existing bank groups and statuses only.
        category_labels = []
        bank_category_ids = {b.id: set() for b in existing}
        available_bank_groups = sorted({b.kind for b in existing if b.kind})
        selected_bank_groups = [g for g in available_bank_groups if g in request.args.getlist('bank_group')]
        selected_category = request.args.get('category', 'active')
        allowed_categories = {'active', 'all', 'problem', 'group:deposit', 'group:withdrawal', 'group:store', 'group:other'}
        if selected_category not in allowed_categories:
            selected_category = 'active'
        problem_statuses = set(BANK_STATUSES) - {'active', 'closed'}
        if selected_category == 'active':
            category_banks = active_banks
        elif selected_category == 'all':
            category_banks = existing
        elif selected_category == 'problem':
            # Operational problem queue always reflects CURRENT status, even when viewing historical balances.
            category_banks = [b for b in existing if b.status not in ('active', 'closed')]
        elif selected_category.startswith('group:'):
            category_banks = [b for b in existing if b.kind == selected_category.split(':', 1)[1]]
        else:
            category_banks = active_banks
        filtered_active_banks = ([b for b in category_banks if b.kind in selected_bank_groups]
                                 if selected_bank_groups else category_banks)
        dashboard_bank_stats = {b.id: type('PeriodStats', (), period_stats[b.id])() for b in existing}
        closing_total = sum((bal[b.id] for b in existing if statuses[b.id] != 'closed'), Decimal('0'))
        active_total = sum((bal[b.id] for b in active_banks), Decimal('0'))
        bank_pages = max(1, (len(filtered_active_banks) + 9) // 10)
        bank_page = min(bank_page, bank_pages)
        bank_items = filtered_active_banks[(bank_page-1)*10:bank_page*10]
        rows = db.session.execute(
            select(Transaction.kind, func.count(Transaction.id), func.coalesce(func.sum(Transaction.signed_amount), 0))
            .where(Transaction.business_date >= start_day, Transaction.business_date <= day, Transaction.status == 'confirmed',platform_scope(Transaction))
            .group_by(Transaction.kind)).all()
        totals = {k: {'count': n, 'amount': money(v)} for k, n, v in rows}
        # Allocations count as Customer Deposits on the allocation date, without a bank entry.
        allocation_count=sum(int(r[2]) for r in allocation_rows)
        allocation_amount=sum((money(r[1]) for r in allocation_rows),Decimal('0'))
        if allocation_count:
            deposit_total=totals.setdefault('deposit',{'count':0,'amount':Decimal('0')})
            deposit_total['count']+=allocation_count
            deposit_total['amount']+=allocation_amount

        # Dashboard retains its historical report-date filtering (V7.9.6.11 behavior).
        latest = (Transaction.query.filter(Transaction.business_date >= start_day,
                                           Transaction.business_date <= day, platform_scope(Transaction))
                  .order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(20).all())
        platform_names = {p.id:p.name for p in Platform.query.filter(Platform.id.in_({t.platform_id for t in latest})).all()} if latest else {}
        void_audits = (TransactionAudit.query.filter(TransactionAudit.transaction_id.in_([t.id for t in latest]),TransactionAudit.action=='VOID').order_by(TransactionAudit.created_at.desc()).all() if latest else [])
        void_actor_ids={a.actor_id for a in void_audits}
        void_actor_names={u.id:u.username for u in User.query.filter(User.id.in_(void_actor_ids)).all()} if void_actor_ids else {}
        deleted_by={}
        for audit in void_audits:
            deleted_by.setdefault(audit.transaction_id,void_actor_names.get(audit.actor_id,'Unknown user'))
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
                if t.kind in ('transfer','internal_transfer'):
                    source = next((name for delta, name in items if delta < 0), 'Unknown')
                    dest = next((name for delta, name in items if delta > 0), 'Unknown')
                    transaction_banks[t.id] = f'{source} → {dest}'
                else:
                    transaction_banks[t.id] = items[0][1] if items else ('Internal · Deposit Back' if (t.remark or '').startswith('[DEPOSIT_BACK:') else 'Dealer → Customer' if t.kind=='dealer_payout' else 'Unknown')
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
                Transaction.business_date >= start_day, Transaction.business_date <= day).distinct()
        ).scalars().all())
        first_seen = db.session.execute(
            select(normalized_id, func.min(Transaction.business_date))
            .where(*activity_filter).group_by(normalized_id)
        ).all()
        first_dates = dict(first_seen)
        def _as_day(v): return date.fromisoformat(str(v)[:10]) if isinstance(v,str) else (v.date() if isinstance(v,datetime) else v)
        new_count = sum(1 for customer_id in active_ids
                        if start_day <= _as_day(first_dates[customer_id]) <= day)
        dealer_items = Dealer.query.filter(platform_scope(Dealer)).order_by(Dealer.name).all()
        dealer_rows = db.session.execute(
            select(DealerTrade.dealer_id, DealerTrade.flow,
                   func.sum(DealerTrade.aud_amount), func.sum(DealerTrade.myr_delta))
            .join(Transaction, Transaction.id == DealerTrade.transaction_id)
            .where(Transaction.status == 'confirmed',Transaction.business_date >= start_day, Transaction.business_date <= day,platform_scope(Transaction))
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
            banks=bank_items, balances=display_bal, dashboard_bank_stats=dashboard_bank_stats, dashboard_unclaimed=unclaimed_components,
            totals=totals, latest=latest, transaction_banks=transaction_banks, platform_names=platform_names, deleted_by=deleted_by,
            grand=active_total, page_total=sum((bal[b.id] for b in bank_items), Decimal('0')),
            page_with_unclaimed=sum((display_bal[b.id] for b in bank_items), Decimal('0')),
            active_with_unclaimed=sum((display_bal[b.id] for b in active_banks), Decimal('0')),
            total_unclaimed=sum((unclaimed_components.get(b.id, Decimal('0')) for b in active_banks), Decimal('0')),
            ledger_balances=before_unclaim,
            historical_unclaimed_missing=historical_unclaimed_missing,
            bank_page=bank_page, bank_pages=bank_pages, active_count=len(active_banks),
            available_bank_groups=available_bank_groups, selected_bank_groups=selected_bank_groups,
            category_labels=category_labels, selected_category=selected_category, bank_category_ids=bank_category_ids,
            bank_status_options=BANK_STATUSES, problem_statuses=problem_statuses,
            filtered_bank_count=len(filtered_active_banks),
            filtered_ledger_total=sum((before_unclaim[b.id] for b in filtered_active_banks), Decimal('0')),
            filtered_unclaimed_total=sum((unclaimed_components.get(b.id, Decimal('0')) for b in filtered_active_banks), Decimal('0')),
            filtered_with_unclaimed_total=sum((display_bal[b.id] for b in filtered_active_banks), Decimal('0')),
            block_count=len(blocked), block_banks=blocked, closing_total=closing_total,
            closed_banks=closed_page.items, closed_page=closed_page,
            closed_count=closed_page.total, statuses=statuses, range_start_utc=range_start_utc, range_end_utc=range_end_utc)

    @app.post('/bank-categories/<label_id>/rename')
    @login_required
    def rename_bank_category(label_id):
        require_permission('manage_bank_labels')
        label=db.session.get(BankLabel,label_id)
        if not label or label.platform_id!=require_single_platform(): abort(404)
        name=request.form.get('name','').strip()[:60]
        if not name: abort(400)
        old=label.name
        try:
            label.name=name
            db.session.add(Audit(actor_id=current_user.id,action='RENAME_BANK_CATEGORY',detail=f'{label.id}: {old} -> {name}'))
            db.session.commit();flash('Category renamed','success')
        except IntegrityError:
            db.session.rollback();flash('Category name already exists','warning')
        return redirect(url_for('dashboard',category=label.id))

    @app.post('/bank-categories/<label_id>/delete')
    @login_required
    def delete_bank_category(label_id):
        require_permission('manage_bank_labels')
        label=db.session.get(BankLabel,label_id)
        if not label or label.platform_id!=require_single_platform(): abort(404)
        if BankLabelAssignment.query.filter_by(label_id=label.id).first():
            flash('Category is assigned to banks. Unassign banks before deleting.','warning')
            return redirect(url_for('dashboard',category=label.id))
        db.session.add(Audit(actor_id=current_user.id,action='DELETE_BANK_CATEGORY',detail=f'{label.id} {label.name}'))
        db.session.delete(label);db.session.commit();flash('Category deleted','success')
        return redirect(url_for('dashboard'))

    @app.post('/banks/<bank_id>/categories')
    @login_required
    def update_bank_categories(bank_id):
        require_permission('manage_bank_labels')
        pid=require_single_platform()
        bank_for_platform(bank_id,pid)
        chosen=set(request.form.getlist('label_ids'))
        allowed={x.id for x in BankLabel.query.filter_by(platform_id=pid).all()}
        if not chosen.issubset(allowed): abort(400)
        current={x.label_id for x in BankLabelAssignment.query.filter_by(bank_id=bank_id).all()}
        for removed in current-chosen:
            db.session.delete(db.session.get(BankLabelAssignment,(bank_id,removed)))
        for added in chosen-current:
            db.session.add(BankLabelAssignment(bank_id=bank_id,label_id=added))
        db.session.add(Audit(actor_id=current_user.id,action='UPDATE_BANK_CATEGORIES',detail=f'{bank_id}: {sorted(current)} -> {sorted(chosen)}'))
        db.session.commit();flash('Bank categories saved','success')
        return redirect(url_for('dashboard',category=request.form.get('return_category','active')))

    @app.post('/bank-labels')
    @login_required
    def create_bank_label():
        require_permission('manage_bank_labels')
        pid=require_single_platform()
        name=request.form.get('name','').strip()[:60]
        if not name:abort(400,description='Label name required')
        try:
            label=BankLabel(platform_id=pid,name=name)
            db.session.add(label);db.session.flush()
            db.session.add(Audit(actor_id=current_user.id,action='CREATE_BANK_LABEL',detail=f'{pid} {label.id} {name}'))
            db.session.commit();flash('Bank tag created','success')
        except IntegrityError:
            db.session.rollback();flash('This tag already exists on this platform','warning')
        return redirect(url_for('dashboard') if request.form.get('return_to')=='dashboard' else url_for('banks'))

    @app.post('/banks/<bank_id>/label')
    @login_required
    def assign_bank_label(bank_id):
        require_permission('manage_bank_labels')
        pid=require_single_platform()
        bank_for_platform(bank_id,pid)
        label_id=request.form.get('label_id','').strip()
        label=db.session.get(BankLabel,label_id) if label_id else None
        if label_id and (not label or label.platform_id!=pid):abort(403)
        existing_assignments=BankLabelAssignment.query.filter_by(bank_id=bank_id).all()
        for assignment in existing_assignments: db.session.delete(assignment)
        db.session.flush()
        if label:db.session.add(BankLabelAssignment(bank_id=bank_id,label_id=label.id))
        db.session.add(Audit(actor_id=current_user.id,action='ASSIGN_BANK_LABEL',detail=f'{bank_id} label={label_id or "none"}'))
        db.session.commit();flash('Bank tag updated','success')
        return redirect(url_for('banks'))

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
                existing_bank=Bank.query.filter(Bank.platform_id==pid,func.lower(Bank.name)==name.lower()).first()
                if existing_bank: raise ValueError(f'Bank \"{name}\" already exists in this platform.')
                b=Bank(platform_id=pid,name=name,kind=kind,opening_balance=money(request.form.get('opening_balance','0')) if current_user.role=='admin' else Decimal('0'))
                db.session.add(b);db.session.flush()
                save_bank_detail(b,request.form)
                db.session.add(Audit(actor_id=current_user.id,action='CREATE_BANK',detail=f'{b.id} {name}'))
                db.session.commit();flash('Bank created','success')
            except ValueError as e:
                db.session.rollback();flash(str(e),'danger')
            except IntegrityError:
                db.session.rollback();flash('Bank name already exists in this platform or conflicts with another record.','danger')
            return redirect(url_for('banks'))
        day=selected_date()
        bank_page=max(1,request.args.get('bank_page',1,type=int))
        history_page=max(1,request.args.get('history_page',1,type=int))
        label_filter=request.args.get('label','').strip()[:36]
        status_filter=request.args.get('status','').strip()[:30]
        bank_query=Bank.query.filter(platform_scope(Bank))
        if label_filter:
            bank_query=bank_query.join(BankLabelAssignment,BankLabelAssignment.bank_id==Bank.id).join(BankLabel,BankLabel.id==BankLabelAssignment.label_id).filter(BankLabel.id==label_filter, BankLabel.platform_id==Bank.platform_id)
        if status_filter in BANK_STATUSES:
            bank_query=bank_query.filter(Bank.status==status_filter)
        else:
            # Default operational list excludes closed banks. They remain
            # discoverable with explicit status=closed and in historical reports.
            bank_query=bank_query.filter(Bank.status!='closed')
        bank_pagination=bank_query.order_by(Bank.kind,Bank.name).paginate(page=bank_page,per_page=15,error_out=False)
        end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=timezone_for_report_day(day)).astimezone(timezone.utc)
        history_pagination=BankStatusHistory.query.filter(BankStatusHistory.created_at < end,BankStatusHistory.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).order_by(BankStatusHistory.created_at.desc(),BankStatusHistory.id.desc()).paginate(page=history_page,per_page=15,error_out=False)
        return render_template('banks.html',banks=bank_pagination.items,bank_stats=bank_daily_overview(bank_pagination.items,day) if permitted('view_bank_balances') else {},balances={b.id:ledger_balance(b.id,day)+unclaimed_balance_components([b.id],day).get(b.id,Decimal('0.00')) for b in bank_pagination.items} if permitted('view_bank_balances') else {},
                               statuses=BANK_STATUSES,history=history_pagination.items,bank_pagination=bank_pagination,history_pagination=history_pagination,selected_day=day,
                               labels=BankLabel.query.filter(platform_scope(BankLabel)).order_by(BankLabel.name).all(),
                               assigned_labels={a.bank_id:a.label_id for a in db.session.execute(select(BankLabelAssignment).join(Bank,Bank.id==BankLabelAssignment.bank_id).where(platform_scope(Bank))).scalars().all()},
                               label_filter=label_filter,status_filter=status_filter)

    @app.route('/banks/<bank_id>/details',methods=['GET','POST'])
    @login_required
    def bank_details(bank_id):
        bank=bank_for_platform(bank_id,require_single_platform())
        require_permission('edit_bank_details' if request.method=='POST' else 'view_bank_details')
        detail=db.session.get(BankDetail,bank.id)
        if request.method=='POST':
            try:
                save_bank_detail(bank,request.form)
                db.session.add(Audit(actor_id=current_user.id,action='EDIT_BANK_DETAILS',detail=f'bank={bank.id}; fields={",".join(k for k in BANK_DETAIL_FIELDS if k in request.form)}; password_changed={bool(request.form.get("login_password"))}'))
                db.session.commit();flash('Bank details updated','success')
            except ValueError as e:
                db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('bank_details',bank_id=bank.id))
        return render_template('bank_details.html',bank=bank,detail=detail,can_sensitive=permitted('view_sensitive_bank_details'))

    @app.post('/banks/<bank_id>/details/reveal')
    @login_required
    def reveal_bank_password(bank_id):
        bank=bank_for_platform(bank_id,require_single_platform())
        require_permission('view_sensitive_bank_details')
        detail=db.session.get(BankDetail,bank.id)
        password=None
        if detail and detail.login_password_cipher:
            try: password=bank_cipher().decrypt(detail.login_password_cipher.encode()).decode()
            except InvalidToken: abort(500,'Unable to decrypt stored credential')
        db.session.add(Audit(actor_id=current_user.id,action='REVEAL_BANK_PASSWORD',detail=f'bank={bank.id}'))
        db.session.commit()
        if request.accept_mimetypes.best == 'application/json':
            response=app.json.response({'password':password or ''})
        else:
            response=app.make_response(render_template('bank_password_reveal.html',bank=bank,password=password))
        response.headers['Cache-Control']='no-store, private'
        return response

    @app.post('/banks/<bank_id>/status')
    @role_required('admin','staff')
    def update_bank_status(bank_id):
        bank_for_platform(bank_id,require_single_platform())
        destination = request.form.get('return_to') if request.form.get('return_to') in ('dashboard','new_transaction') else 'banks'
        def status_return():
            if destination == 'new_transaction':
                return redirect(url_for('new_transaction') + '#newtx-bank-balances')
            if destination == 'dashboard':
                return redirect(url_for('dashboard',start_date=request.form.get('start_date'),end_date=request.form.get('end_date'),category=request.form.get('category','active'),bank_group=request.form.getlist('bank_group')) + '#bank-balances')
            return redirect(url_for('banks'))
        new_status=request.form.get('status','').strip()
        remark=request.form.get('remark','').strip()
        if new_status not in BANK_STATUSES:
            flash('Invalid bank status','danger')
            return status_return()
        if len(remark)>2000:
            flash('Remark must be 2000 characters or fewer','danger')
            return status_return()
        if new_status!='active' and not remark:
            flash('A reason is required when reporting a bank issue','danger')
            return status_return()
        try:
            locked=lock_banks([bank_id])
            if len(locked)!=1: abort(404)
            bank=locked[0]
            old_status=bank.status
            # Closed is terminal for ordinary status updates. A future reopen must
            # be a separate, audited financial workflow with an explicit new anchor.
            if old_status=='closed' and new_status!='closed':
                db.session.rollback()
                flash('Closed banks cannot be reactivated via Status. A separately verified reopen workflow is required.','danger')
                return status_return()
            if old_status==new_status and (bank.status_remark or '')==remark:
                db.session.rollback()
                flash('No changes to save','info')
                return status_return()
            bank.status=new_status
            bank.status_remark=remark or None
            bank.status_updated_at=now()
            bank.status_updated_by=current_user.id
            # Legacy active flag stays synchronized for older code paths.
            bank.active=(new_status!='closed')
            status_event=BankStatusHistory(bank_id=bank.id,actor_id=current_user.id,
                                           old_status=old_status,new_status=new_status,remark=remark or None)
            db.session.add(status_event)
            db.session.flush()  # assign status history ID before snapshot FK
            if new_status=='closed' and old_status!='closed':
                # Lock pending leads so their status cannot change during the snapshot.
                leads=(UnclaimedDeposit.query.filter_by(bank_id=bank.id,platform_id=bank.platform_id,status='unclaimed')
                       .order_by(UnclaimedDeposit.id).with_for_update().all())
                ledger=ledger_balance(bank.id)
                # Posted Unclaim is already in the actual ledger; pending is
                # outstanding posted Unclaim less allocations, not investigation leads.
                pending=pending_unclaim_at_utc(bank.id,now())
                snapshot=[{'id':x.id,'reference':x.reference,'amount':str(money(x.amount)),
                           'created_at':x.created_at.isoformat() if x.created_at else None} for x in leads]
                db.session.add(BlockedUnclaimedSettlement(
                    platform_id=bank.platform_id,bank_id=bank.id,status_history_id=status_event.id,
                    actor_id=current_user.id,ledger_balance=ledger,unclaimed_amount=pending,
                    unclaimed_count=len(leads),with_unclaimed=ledger,
                    leads_json=json.dumps(snapshot,ensure_ascii=False),remark=remark or None))
                db.session.add(Audit(actor_id=current_user.id,action='BLOCKED_UNCLAIMED_SNAPSHOT',
                    detail=f'bank={bank.id} leads={len(leads)} unclaimed={pending} ledger={ledger}'))
            db.session.add(Audit(actor_id=current_user.id,action='UPDATE_BANK_STATUS',
                                 detail=f'{bank.id}: {old_status} -> {new_status}; reason={remark[:500]}'))
            db.session.commit()
            flash(f'{bank.name} status updated to {BANK_STATUSES[new_status]}','success')
        except Exception:
            db.session.rollback()
            raise
        return status_return()

    @app.get('/banks/<bank_id>/blocked-settlements')
    @login_required
    def blocked_settlements(bank_id):
        require_permission('view_bank_balances')
        bank=bank_for_platform(bank_id,require_single_platform())
        records=(BlockedUnclaimedSettlement.query.filter_by(bank_id=bank.id,platform_id=bank.platform_id)
                 .order_by(BlockedUnclaimedSettlement.created_at.desc()).all())
        return render_template('blocked_settlements.html',bank=bank,records=records,json=json)

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
            return redirect(url_for('banks'))
        items=Dealer.query.filter(platform_scope(Dealer)).order_by(Dealer.name).all()
        rows=db.session.execute(select(DealerTrade.dealer_id,DealerTrade.flow,func.sum(DealerTrade.aud_amount),func.sum(DealerTrade.myr_delta)).join(Transaction,Transaction.id==DealerTrade.transaction_id).where(Transaction.status=='confirmed',platform_scope(Transaction)).group_by(DealerTrade.dealer_id,DealerTrade.flow)).all()
        totals={}
        for did,flow,aud,myr in rows:
            d=totals.setdefault(did,{'dealer_sell':Decimal('0'),'dealer_buy':Decimal('0'),'dealer_payout':Decimal('0'),'myr':Decimal('0')})
            d[flow]+=aud;d['myr']+=myr
        return render_template('dealers.html',dealers=items,totals=totals)

    @app.post('/transactions/check-reference')
    @login_required
    def check_transaction_reference():
        # This is a preflight UX check only. The final POST still enforces uniqueness.
        require_permission('create_transaction')
        pid=require_single_platform()
        kind=(request.form.get('kind') or '').strip()
        reference=(request.form.get('reference') or '').strip()
        if kind not in ('deposit','withdrawal','dealer_payout'):
            return app.json.response({'ok':True,'duplicate':False})
        if not reference:
            response=app.json.response({'ok':False,'duplicate':False,'error':'Platform Transaction ID is required.'})
            response.status_code=400
            response.headers['Cache-Control']='no-store'
            return response
        # Match the existing global uniqueness rule used by record/record_dealer.
        exists=db.session.execute(select(Transaction.id).where(Transaction.reference==reference).limit(1)).first() is not None
        response=app.json.response({'ok':not exists,'duplicate':exists,'error':'Duplicate Transaction ID' if exists else ''})
        response.headers['Cache-Control']='no-store'
        return response

    @app.route('/transactions/new',methods=['GET','POST'])
    @login_required
    def new_transaction():
        if request.method=='POST':
            pid=require_single_platform()
            try:
                chosen_day=validate_entry_business_day(request.form.get('business_date'))
                kind=request.form.get('kind','')
                require_permission('create_transaction')
                if kind == 'deposit_back':
                    record_deposit_back(request.form.get('withdraw_reference',''),request.form.get('deposit_reference',''),request.form.get('amount',''),current_user.id,request.form.get('customer_ref',''),request.form.get('remark',''),platform_id=pid,business_day=chosen_day)
                elif kind in ('dealer_sell','dealer_buy','dealer_payout'):
                    record_dealer(kind,request.form.get('reference',''),request.form.get('amount',''),
                                  request.form.get('bank_id','') if kind!='dealer_payout' else None,
                                  current_user.id,request.form.get('dealer_id',''),request.form.get('rate',''),
                                  request.form.get('customer_ref',''),request.form.get('remark',''),platform_id=pid,business_day=chosen_day)
                else:
                    record(kind,request.form.get('reference',''),request.form.get('amount',''),request.form.get('bank_id') or None,current_user.id,request.form.get('customer_ref',''),request.form.get('remark',''),request.form.get('to_bank_id') or None,platform_id=pid,business_day=chosen_day)
                if request.accept_mimetypes.best == 'application/json':
                    response=app.json.response({'ok':True,'redirect':url_for('new_transaction')})
                    response.headers['Cache-Control']='no-store'
                    return response
                flash('Transaction confirmed and balance updated','success');return redirect(url_for('new_transaction'))
            except (ValueError,IntegrityError) as e:
                db.session.rollback()
                if request.accept_mimetypes.best == 'application/json':
                    message=str(e)
                    duplicate=('duplicate' in message.lower() or ('unique' in message.lower() and 'reference' in message.lower()))
                    response=app.json.response({'ok':False,'field':'reference' if duplicate else None,'error':'This platform Transaction ID already exists. Please enter a different ID.' if duplicate else message})
                    response.status_code=409 if duplicate else 400
                    response.headers['Cache-Control']='no-store'
                    return response
                message=str(e)
                duplicate=('duplicate' in message.lower() or ('unique' in message.lower() and 'reference' in message.lower()))
                if duplicate and (request.form.get('kind') or '') in ('deposit','withdrawal','dealer_payout'):
                    # No redirect/flash: re-render below, restoring all inputs and opening the entry modal.
                    g.duplicate_transaction_form_state={k:v for k,v in request.form.items() if k!='csrf_token'}
                else:
                    flash(message,'danger')
        
        today = business_today()
        staff_totals = {}
        if current_user.role == 'staff':
            rows = db.session.execute(
                select(Transaction.kind, func.count(Transaction.id), func.sum(Transaction.signed_amount))
                .where(platform_scope(Transaction), Transaction.business_date == today,
                       Transaction.status == 'confirmed',
                       Transaction.kind.in_(('deposit', 'withdrawal', 'dealer_payout')))
                .group_by(Transaction.kind))
            totals = {kind: (count, money(amount)) for kind, count, amount in rows}
            start, end = dashboard_utc_window(today, today)
            claims, claimed_amount = db.session.execute(
                select(func.count(UnclaimAllocation.id), func.coalesce(func.sum(UnclaimAllocation.amount), 0))
                .where(platform_scope(UnclaimAllocation), UnclaimAllocation.created_at >= start,
                       UnclaimAllocation.created_at < min(end, now()))).one()
            deposits, received = totals.get('deposit', (0, 0))
            withdrawals, paid = totals.get('withdrawal', (0, 0))
            payouts, dealer_paid = totals.get('dealer_payout', (0, 0))
            staff_totals = {
                'Deposit': (deposits + claims, received + claimed_amount),
                'Withdrawal': (withdrawals + payouts, abs(paid + dealer_paid)),
            }

        balance_banks=[];balance_values={};bank_stats={};recent=[];recent_pagination=None
        if permitted('view_bank_balances'):
            balance_banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
            bank_stats=bank_daily_overview(balance_banks)
            balance_values={b.id:bank_stats[b.id]['current'] for b in balance_banks}
            bank_ids=[b.id for b in balance_banks if b.status=='active' and b.active]
            if permitted('view_bank_movements') and bank_ids:
                movement_search=request.args.get('movement_search','').strip()[:120]
                movement_bank=request.args.get('movement_bank','').strip()[:36]
                movement_query=(select(Entry,Transaction,Bank.name)
                    .join(Transaction,Transaction.id==Entry.transaction_id)
                    .join(Bank,Bank.id==Entry.bank_id)
                    .where(Entry.bank_id.in_(bank_ids),Transaction.status.in_(['confirmed','voided']),platform_scope(Transaction))
                    .order_by(Transaction.created_at.desc(),Entry.id.desc()))
                if movement_bank in bank_ids:
                    movement_query=movement_query.where(Entry.bank_id==movement_bank)
                if movement_search:
                    pattern=f'%{movement_search}%'
                    movement_query=movement_query.where(db.or_(Transaction.reference.ilike(pattern),Transaction.customer_ref.ilike(pattern),Bank.name.ilike(pattern)))
                # New Transaction sidebar: only the current closing business day, latest 20 movements.
                today_start_utc, today_scheduled_end_utc = dashboard_utc_window(business_today(), business_today())
                movement_query = movement_query.where(
                    Transaction.created_at >= today_start_utc,
                    Transaction.created_at < min(today_scheduled_end_utc, now()))
                recent=list(db.session.execute(movement_query.limit(20)).all())
                if not movement_bank:
                    from types import SimpleNamespace
                    internal_query=Transaction.query.filter(
                        platform_scope(Transaction), Transaction.status.in_(['confirmed','voided']),
                        Transaction.remark.like('[DEPOSIT_BACK:%'),
                        Transaction.created_at >= today_start_utc,
                        Transaction.created_at < min(today_scheduled_end_utc, now()))
                    if movement_search:
                        internal_query=internal_query.filter(db.or_(
                            Transaction.reference.ilike(pattern), Transaction.customer_ref.ilike(pattern)))
                    internal=internal_query.order_by(Transaction.created_at.desc()).limit(20).all()
                    recent.extend((SimpleNamespace(delta=t.signed_amount),t,'Internal · Deposit Back') for t in internal)
                    recent.sort(key=lambda row: (row[1].created_at,row[1].id),reverse=True)
                    recent=recent[:20]
                recent_pagination=None
        return render_template('transaction.html',staff_totals=staff_totals,entry_today=today.isoformat(),entry_yesterday=(business_today()-timedelta(days=1)).isoformat(),banks=Bank.query.filter(platform_scope(Bank),Bank.active==True).order_by(Bank.name).all(),dealers=Dealer.query.filter(platform_scope(Dealer),Dealer.active==True).order_by(Dealer.name).all(),balance_banks=balance_banks,balance_values=balance_values,bank_stats=bank_stats,recent=recent,recent_pagination=recent_pagination,bank_status_options=BANK_STATUSES,bank_details_map={d.bank_id:d for d in BankDetail.query.filter(BankDetail.bank_id.in_([b.id for b in balance_banks])).all()} if permitted('view_bank_details') and balance_banks else {},movement_search=request.args.get('movement_search','').strip()[:120],movement_bank=request.args.get('movement_bank','').strip()[:36],duplicate_form_state=getattr(g,'duplicate_transaction_form_state',None))

    @app.get('/banks/<bank_id>/investigation')
    @login_required
    def bank_investigation(bank_id):
        # An explicit bank-scoped endpoint; never trust a bank ID without platform authorization.
        require_permission('view_bank_movements')
        pid=require_single_platform()
        bank=bank_for_platform(bank_id,pid)
        direction=request.args.get('direction','all')
        if direction not in ('all','in','out'): abort(400,description='Invalid direction')
        search=request.args.get('q','').strip()[:120]
        raw_start=request.args.get('from',request.args.get('from_date','')).strip()
        raw_end=request.args.get('to',request.args.get('to_date','')).strip()
        try:
            start=date.fromisoformat(raw_start) if raw_start else None
            end=date.fromisoformat(raw_end) if raw_end else None
        except ValueError: abort(400,description='Invalid date')
        if start and end and start>end: abort(400,description='Invalid date range')
        query=(select(Entry,Transaction)
            .join(Transaction,Transaction.id==Entry.transaction_id)
            .where(Entry.bank_id==bank.id,Transaction.platform_id==pid,
                   Transaction.status=='confirmed'))
        if direction=='in':query=query.where(Entry.delta>0)
        if direction=='out':query=query.where(Entry.delta<0)
        if start:query=query.where(Transaction.business_date>=start)
        if end:query=query.where(Transaction.business_date<=end)
        if search:
            pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            query=query.where(or_(Transaction.reference.ilike(pattern,escape='\\'),
                                  Transaction.customer_ref.ilike(pattern,escape='\\'),
                                  Transaction.remark.ilike(pattern,escape='\\')))
        sub=query.order_by(None).subquery()
        summary=db.session.execute(select(
            func.count(sub.c.id),
            func.coalesce(func.sum(case((sub.c.delta>0,sub.c.delta),else_=0)),0),
            func.coalesce(func.sum(case((sub.c.delta<0,-sub.c.delta),else_=0)),0),
            func.coalesce(func.sum(case((sub.c.delta>0,1),else_=0)),0),
            func.coalesce(func.sum(case((sub.c.delta<0,1),else_=0)),0)
        )).one()
        count=int(summary[0]); per_page=25
        pages=max(1,(count+per_page-1)//per_page)
        page=min(max(1,request.args.get('page',1,type=int)),pages)
        movements=db.session.execute(query.order_by(Transaction.created_at.desc(),Entry.id.desc())
            .limit(per_page).offset((page-1)*per_page)).all()
        pending=0
        if permitted('manage_unclaimed'):
            pending=db.session.scalar(select(func.count(UnclaimedDeposit.id)).where(
                UnclaimedDeposit.bank_id==bank.id,UnclaimedDeposit.platform_id==pid,
                UnclaimedDeposit.status=='unclaimed')) or 0
        return render_template('bank_investigation.html',bank=bank,movements=movements,
            summary=summary,pending=pending,count=count,page=page,pages=pages,
            direction=direction,search=search,raw_start=raw_start,raw_end=raw_end,
            calculated_balance=ledger_balance(bank.id)+unclaimed_balance_components([bank.id]).get(bank.id,Decimal('0.00')),myt=timezone_at())

    @app.get('/workspace')
    @login_required
    def workspace():
        require_permission('view_bank_balances')
        banks_list=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
        bank_ids=[b.id for b in banks_list]
        pending_balances=unclaimed_balance_components([b.id for b in banks_list])
        current_balances={b.id:ledger_balance(b.id)+pending_balances.get(b.id,Decimal('0.00')) for b in banks_list}
        recent=[]
        if permitted('view_bank_movements') and bank_ids:
            recent=db.session.execute(select(Entry,Transaction,Bank.name)
                .join(Transaction,Transaction.id==Entry.transaction_id)
                .join(Bank,Bank.id==Entry.bank_id)
                .where(Entry.bank_id.in_(bank_ids),Transaction.status.in_(['confirmed','voided']),platform_scope(Transaction))
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
                lead_bank=bank_for_platform(bank_id,pid)
                if lead_bank.status=='closed':raise ValueError('Blocked bank: cannot record new unclaimed leads')
                amount=amount_positive(request.form.get('amount',''))
                reference=request.form.get('reference','').strip()[:120]
                if not reference:raise ValueError('Bank reference is required')
                # Leads do NOT post a ledger entry or alter calculated bank balances.
                obj=UnclaimedDeposit(platform_id=pid,bank_id=bank_id,amount=amount,
                    reference=reference,remark=request.form.get('remark','').strip()[:2000],created_by=current_user.id)
                db.session.add(obj);db.session.flush()
                db.session.add(Audit(actor_id=current_user.id,action='LOG_UNCLAIMED',detail=f'{obj.id} bank={bank_id}'))
                db.session.commit();flash('Unclaimed recorded for selected bank; confirmed ledger unchanged','success')
            except (ValueError,IntegrityError) as exc:
                db.session.rollback();flash(str(exc),'danger')
            return redirect(url_for('unclaimed_deposits'))
        banks_list=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
        lead_search=request.args.get('search','').strip()[:120]
        lead_status=request.args.get('status','all').strip()
        lead_bank=request.args.get('bank_id','').strip()
        # Date filters use the same MYT calendar boundaries for confirmed transactions
        # and investigation leads; invalid ranges are rejected rather than ignored.
        raw_from=request.args.get('date_from','').strip()
        raw_to=request.args.get('date_to','').strip()
        try:
            date_from=date.fromisoformat(raw_from) if raw_from else None
            date_to=date.fromisoformat(raw_to) if raw_to else None
            if date_from and date_to and date_from>date_to:
                raise ValueError('From date cannot be after To date')
        except ValueError:
            flash('Invalid date range. Use YYYY-MM-DD and ensure From is not after To.','danger')
            date_from=date_to=None
            raw_from=raw_to=''
        start_utc=datetime.combine(date_from,time.min,MYT).astimezone(timezone.utc) if date_from else None
        end_utc=datetime.combine(date_to+timedelta(days=1),time.min,MYT).astimezone(timezone.utc) if date_to else None
        valid_bank_ids={b.id for b in banks_list}
        if lead_bank not in valid_bank_ids:lead_bank=''

        q=UnclaimedDeposit.query.filter(platform_scope(UnclaimedDeposit))
        if lead_status in ('unclaimed','claimed','dismissed'):q=q.filter(UnclaimedDeposit.status==lead_status)
        if lead_bank:q=q.filter(UnclaimedDeposit.bank_id==lead_bank)
        if lead_search:q=q.filter(or_(UnclaimedDeposit.reference.ilike(f'%{lead_search}%'),UnclaimedDeposit.remark.ilike(f'%{lead_search}%')))
        if start_utc:q=q.filter(UnclaimedDeposit.created_at>=start_utc)
        if end_utc:q=q.filter(UnclaimedDeposit.created_at<end_utc)
        lead_page=max(1,request.args.get('lead_page',1,type=int))
        pagination=q.order_by(UnclaimedDeposit.created_at.desc(),UnclaimedDeposit.id.desc()).paginate(page=lead_page,per_page=20,error_out=False)

        # Confirmed Unclaim transactions are separate from investigation leads.
        # Filter by bank through Entry so the list reflects the actual credited bank.
        tq=Transaction.query.filter(platform_scope(Transaction),Transaction.kind=='unclaim')
        if lead_bank:
            tq=tq.filter(Transaction.id.in_(select(Entry.transaction_id).where(Entry.bank_id==lead_bank)))
        if lead_search:
            tq=tq.filter(or_(Transaction.reference.ilike(f'%{lead_search}%'),Transaction.remark.ilike(f'%{lead_search}%'),Transaction.customer_ref.ilike(f'%{lead_search}%')))
        if start_utc:tq=tq.filter(Transaction.created_at>=start_utc)
        if end_utc:tq=tq.filter(Transaction.created_at<end_utc)
        tx_page=max(1,request.args.get('tx_page',1,type=int))
        tx_pagination=tq.order_by(Transaction.created_at.desc(),Transaction.id.desc()).paginate(page=tx_page,per_page=20,error_out=False)
        allocation_totals=dict(db.session.execute(select(UnclaimAllocation.original_transaction_id,
            func.coalesce(func.sum(UnclaimAllocation.amount),0)).group_by(UnclaimAllocation.original_transaction_id)).all())
        tx_ids=[t.id for t in tx_pagination.items]
        entries=(Entry.query.filter(Entry.transaction_id.in_(tx_ids)).all() if tx_ids else [])
        bank_names={b.id:b.name for b in banks_list}
        tx_bank_names={e.transaction_id:bank_names.get(e.bank_id,'—') for e in entries}
        def display_myt(value):
            if not value:return ''
            if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
            return value.astimezone(MYT).strftime('%Y-%m-%d %H:%M')
        return render_template('unclaimed.html',banks=banks_list,items=pagination.items,
            pagination=pagination,lead_search=lead_search,lead_status=lead_status,
            lead_bank=lead_bank,date_from=raw_from,date_to=raw_to,
            tx_items=tx_pagination.items,tx_pagination=tx_pagination,
            tx_bank_names=tx_bank_names,display_myt=display_myt,current_business_day=business_today(),allocation_totals=allocation_totals)

    @app.post('/unclaimed/transactions/<transaction_id>/assign')
    @role_required('admin','staff')
    def assign_confirmed_unclaim(transaction_id):
        require_permission('manage_unclaimed')
        require_permission('edit_transaction')
        customer_ref=request.form.get('customer_ref','').strip()
        deposit_ref=request.form.get('deposit_reference','').strip()
        reason=request.form.get('reason','').strip()
        try:
            if not customer_ref or len(customer_ref)>120: raise ValueError('Customer ID required (max 120)')
            if not deposit_ref or len(deposit_ref)>120: raise ValueError('New Deposit Transaction ID required (max 120)')
            if len(reason)<5: raise ValueError('Reason must contain at least 5 characters')
            identity=db.session.execute(select(Transaction.platform_id).where(Transaction.id==transaction_id)).scalar_one_or_none()
            if not identity: abort(404)
            require_platform(identity)
            if require_single_platform()!=identity: abort(403)
            entries=Entry.query.filter_by(transaction_id=transaction_id).all()
            if len(entries)!=1 or money(entries[0].delta)<=0: raise ValueError('Invalid Unclaim bank credit')
            bank_id=entries[0].bank_id
            lock_banks([bank_id])
            tx=db.session.execute(select(Transaction).where(Transaction.id==transaction_id).with_for_update()).scalar_one_or_none()
            if not tx or tx.kind!='unclaim' or tx.status!='confirmed': raise ValueError('Original Unclaim is not confirmed')
            if Transaction.query.filter_by(reference=deposit_ref).first() or UnclaimAllocation.query.filter_by(deposit_reference=deposit_ref).first():
                raise ValueError('Duplicate Deposit Transaction ID')
            total=money(entries[0].delta)
            allocated=money(db.session.execute(select(func.coalesce(func.sum(UnclaimAllocation.amount),0))
                .where(UnclaimAllocation.original_transaction_id==tx.id)).scalar())
            amount=amount_positive(request.form.get('amount',''))
            if amount>total-allocated: raise ValueError('Assign exceeds remaining Unclaim amount')
            # An allocation is a new current-time classification event, not a rewrite of old ledger/closing.
            # The bank row lock serializes this with the closing engine and other allocations.
            last=ClosingPeriod.query.filter_by(bank_id=bank_id,status='closed').order_by(ClosingPeriod.end_utc.desc()).first()
            if last and _utc_db(last.end_utc)>now(): raise ValueError('Cannot allocate inside a finalized period')
            db.session.add(UnclaimAllocation(original_transaction_id=tx.id,platform_id=tx.platform_id,
                bank_id=bank_id,deposit_reference=deposit_ref,customer_ref=customer_ref,
                amount=amount,reason=reason[:500],created_by=current_user.id))
            db.session.add(Audit(actor_id=current_user.id,action='ALLOCATE_UNCLAIM',
                detail=f'original={tx.id} deposit={deposit_ref} customer={customer_ref} amount={amount}; {reason[:400]}'))
            db.session.commit()
            flash('Unclaim allocated as Customer Deposit; bank ledger unchanged.','success')
        except (ValueError,IntegrityError) as exc:
            db.session.rollback();flash(str(exc),'danger')
        return redirect(url_for('unclaimed_deposits'))

    @app.post('/unclaimed/<lead_id>/dismiss')
    @login_required
    def dismiss_unclaimed(lead_id):
        require_permission('manage_unclaimed')
        # Lock order matches Closing: bank first, then lead. Prevent a claim from
        # changing the observed Unclaimed state while Closing takes its snapshot.
        lead_identity=db.session.execute(select(UnclaimedDeposit.bank_id,UnclaimedDeposit.platform_id).where(UnclaimedDeposit.id==lead_id)).first()
        if not lead_identity:abort(404)
        require_platform(lead_identity.platform_id)
        lock_banks([lead_identity.bank_id])
        lead=db.session.execute(select(UnclaimedDeposit).where(UnclaimedDeposit.id==lead_id).with_for_update()).scalar_one_or_none()
        if not lead:abort(404)
        if lead.bank_id!=lead_identity.bank_id:abort(409,description='Unclaimed bank changed during request')
        if lead.bank.status=='closed':abort(409,description='Blocked bank: unclaimed lead frozen for settlement review')
        if lead.status!='unclaimed':abort(409,description='Only open leads can be dismissed')
        reason=request.form.get('reason','').strip()[:500]
        if not reason:abort(400,description='Dismissal reason required')
        lead.status='dismissed'
        lead.remark=((lead.remark or '')+'\nDismissed: '+reason).strip()[:2000]
        db.session.add(Audit(actor_id=current_user.id,action='DISMISS_UNCLAIMED',detail=f'{lead.id} reason={reason}'))
        db.session.commit();flash('Investigation dismissed; ledger unchanged','success')
        return redirect(url_for('unclaimed_deposits'))

    @app.post('/unclaimed/<lead_id>/claim')
    @login_required
    def claim_unclaimed(lead_id):
        require_permission('manage_unclaimed')
        # Lock order matches Closing: bank first, then lead. Prevent a claim from
        # changing the observed Unclaimed state while Closing takes its snapshot.
        lead_identity=db.session.execute(select(UnclaimedDeposit.bank_id,UnclaimedDeposit.platform_id).where(UnclaimedDeposit.id==lead_id)).first()
        if not lead_identity:abort(404)
        require_platform(lead_identity.platform_id)
        lock_banks([lead_identity.bank_id])
        lead=db.session.execute(select(UnclaimedDeposit).where(UnclaimedDeposit.id==lead_id).with_for_update()).scalar_one_or_none()
        if not lead:abort(404)
        if lead.bank_id!=lead_identity.bank_id:abort(409,description='Unclaimed bank changed during request')
        if lead.bank.status=='closed':abort(409,description='Blocked bank: unclaimed lead frozen for settlement review')
        transaction_ref=request.form.get('transaction_reference','').strip()
        if not transaction_ref:abort(400,description='Provide a confirmed deposit transaction reference')
        tx=Transaction.query.filter_by(reference=transaction_ref,platform_id=lead.platform_id,
                                      kind='deposit',status='confirmed').first()
        if not tx:abort(400,description='Confirmed customer deposit not found in this platform')
        entry=Entry.query.filter_by(transaction_id=tx.id,bank_id=lead.bank_id).first()
        if not entry or money(entry.delta)!=money(lead.amount):
            abort(400,description='Bank or amount does not match confirmed deposit')
        if lead.status!='unclaimed':abort(409)
        if db.session.execute(select(UnclaimedDeposit.id).where(UnclaimedDeposit.transaction_id==tx.id,UnclaimedDeposit.id!=lead.id)).first():
            abort(409,description='This deposit is already linked to another unclaimed lead')
        lead.status='claimed';lead.transaction_id=tx.id
        db.session.add(Audit(actor_id=current_user.id,action='CLAIM_UNCLAIMED',detail=f'{lead.id} transaction={tx.id}'))
        db.session.commit();flash('Linked to confirmed deposit; no additional bank movement posted','success')
        return redirect(url_for('unclaimed_deposits'))

    @app.get('/transactions')
    @login_required
    def transactions():
        from sqlalchemy import or_
        day = selected_date()
        all_dates = request.args.get('all_dates') == '1'
        q = Transaction.query.filter(platform_scope(Transaction))
        if not all_dates:
            q = q.filter(Transaction.business_date == day)
        search = request.args.get('search', '').strip()
        kinds = [v for v in request.args.getlist('kind')
                 if v in ('deposit', 'withdrawal', 'transfer','dealer_sell','dealer_buy','dealer_payout','cash_in','cash_out','internal_transfer')]
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
                if t.kind in ('transfer','internal_transfer'):
                    source = next((name for delta, name in movements if delta < 0), 'Unknown')
                    destination = next((name for delta, name in movements if delta > 0), 'Unknown')
                    transaction_banks[t.id] = f'{source} → {destination}'
                else:
                    transaction_banks[t.id] = movements[0][1] if movements else ('Internal · Deposit Back' if (t.remark or '').startswith('[DEPOSIT_BACK:') else 'Dealer → Customer' if t.kind=='dealer_payout' else 'Unknown')

        return render_template('transactions.html', records=records,
                               transaction_banks=transaction_banks,
                               banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all(),
                               search=search, kinds=kinds, bank_id=bank_id, selected_day=day, all_dates=all_dates)

    @app.get('/audit/transactions')
    @login_required
    def transaction_audit_list():
        require_permission('view_audit_logs')
        # Admin may view all; staff always restricted to currently authorized platform(s).
        q=TransactionAudit.query.join(Transaction,Transaction.id==TransactionAudit.transaction_id)
        if current_user.role!='admin':
            allowed=permitted_platform_ids()
            if not allowed:abort(403)
            q=q.filter(TransactionAudit.platform_id.in_(allowed))
            chosen=selected_platform_id()
            if chosen:q=q.filter(TransactionAudit.platform_id==chosen)
        else:
            chosen=selected_platform_id()
            if chosen:q=q.filter(TransactionAudit.platform_id==chosen)
        reference=request.args.get('reference','').strip()[:120]
        action=request.args.get('action','').strip().upper()
        actor=request.args.get('actor','').strip()[:80]
        if reference:q=q.filter(Transaction.reference.ilike(f'%{reference}%'))
        if action in ('CREATE','EDIT','VOID'):q=q.filter(TransactionAudit.action==action)
        if actor:q=q.join(User,User.id==TransactionAudit.actor_id).filter(User.username.ilike(f'%{actor}%'))
        # Audit filters use Melbourne-local calendar dates, independent of Closing business dates.
        from_date=request.args.get('from_date','').strip()
        to_date=request.args.get('to_date','').strip()
        try:
            start_day=date.fromisoformat(from_date) if from_date else None
            end_day=date.fromisoformat(to_date) if to_date else None
            if start_day and end_day and start_day>end_day: raise ValueError('Date range reversed')
        except ValueError:
            abort(400, description='Invalid audit date range')
        start_utc=datetime.combine(start_day,time.min,MYT).astimezone(timezone.utc) if start_day else None
        end_utc=datetime.combine(end_day+timedelta(days=1),time.min,MYT).astimezone(timezone.utc) if end_day else None
        def audit_date_filter(query, column):
            if start_utc is not None: query=query.filter(column>=start_utc)
            if end_utc is not None: query=query.filter(column<end_utc)
            return query
        q=audit_date_filter(q,TransactionAudit.created_at)
        page=max(1,request.args.get('page',1,type=int))
        logs=q.order_by(TransactionAudit.created_at.desc(),TransactionAudit.id.desc()).paginate(page=page,per_page=25,error_out=False)
        category=request.args.get('category','transactions').strip()
        bank_logs=None
        staff_logs=None
        if category=='bank_status':
            bank_q=BankStatusHistory.query.join(Bank,Bank.id==BankStatusHistory.bank_id).filter(platform_scope(Bank))
            if actor:bank_q=bank_q.join(User,User.id==BankStatusHistory.actor_id).filter(User.username.ilike(f'%{actor}%'))
            bank_q=audit_date_filter(bank_q,BankStatusHistory.created_at)
            bank_logs=bank_q.order_by(BankStatusHistory.created_at.desc(),BankStatusHistory.id.desc()).paginate(page=page,per_page=25,error_out=False)
        elif category=='staff':
            # Account management events are separate from bank status and transaction history.
            # General Audit has no platform_id: restrict these records to administrators.
            if current_user.role!='admin':abort(403)
            staff_q=Audit.query.filter(db.or_(Audit.action.ilike('%STAFF%'),Audit.action.ilike('%USER%'),Audit.action.ilike('%PERMISSION%')))
            staff_q=audit_date_filter(staff_q,Audit.created_at)
            staff_logs=staff_q.order_by(Audit.created_at.desc(),Audit.id.desc()).paginate(page=page,per_page=25,error_out=False)
        elif category!='transactions':
            category='transactions'
        return render_template('transaction_audit.html',logs=logs,reference=reference,action=action,actor=actor,category=category,bank_logs=bank_logs,staff_logs=staff_logs,statuses=BANK_STATUSES,from_date=from_date,to_date=to_date)

    @app.get('/audit/transactions/<audit_id>')
    @login_required
    def transaction_audit_detail(audit_id):
        require_permission('view_audit_logs')
        item=db.session.get(TransactionAudit,audit_id)
        if not item:abort(404)
        if current_user.role!='admin':
            if item.platform_id not in permitted_platform_ids():abort(403)
            if selected_platform_id()!=item.platform_id:abort(403)
        else:
            chosen=selected_platform_id()
            if chosen and chosen!=item.platform_id:abort(403)
        before=json.loads(item.before_json) if item.before_json else None
        after=json.loads(item.after_json) if item.after_json else None
        snapshots=[v for v in (before,after) if isinstance(v,dict)]
        bank_ids={e.get('bank_id') for v in snapshots for e in v.get('entries',[]) if e.get('bank_id')}
        user_ids={v.get('created_by') for v in snapshots if v.get('created_by')}
        bank_names={b.id:b.name for b in Bank.query.filter(Bank.id.in_(bank_ids),Bank.platform_id==item.platform_id).all()} if bank_ids else {}
        user_names={u.id:u.username for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        return render_template('transaction_audit_detail.html',item=item,before=before,after=after,
            bank_names=bank_names,user_names=user_names,platform_name=(db.session.get(Platform,item.platform_id).name if db.session.get(Platform,item.platform_id) else 'Unknown Platform'))

    @app.route('/transactions/<transaction_id>/edit', methods=['GET','POST'])
    @role_required('admin','staff')
    def edit_transaction(transaction_id):
        t=db.session.get(Transaction,transaction_id)
        if not t: abort(404)
        require_platform(t.platform_id)
        if request.method=='POST' and require_single_platform()!=t.platform_id:abort(403)
        if (t.remark or '').startswith('[DEPOSIT_BACK:'):
            flash('Deposit Back linked records cannot be edited individually','warning')
            return redirect(url_for('transactions'))
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
            if (t.remark or '').startswith('[DEPOSIT_BACK:'): raise ValueError('Deposit Back linked records cannot be voided individually')
            if t.kind.startswith('dealer_'): raise ValueError('Dealer transactions require an audited reversal; edit/void is disabled')
            if t.kind=='unclaim' and UnclaimAllocation.query.filter_by(original_transaction_id=t.id).first(): raise ValueError('Allocated Unclaim cannot be voided; use an audited current-day adjustment')
            if t.status!='confirmed': raise ValueError('Transaction already voided')
            if t.business_date!=business_today(): raise ValueError('Only current-day transactions can be edited')
            kind=request.form.get('kind','').strip()
            if kind not in ('deposit','withdrawal','transfer'): raise ValueError('Invalid type')
            reference=request.form.get('reference','').strip()
            if not reference: raise ValueError('Transaction ID required')
            amount=amount_positive(request.form.get('amount',''))
            bank_id=request.form.get('bank_id','').strip()
            to_bank_id=request.form.get('to_bank_id','').strip() if kind=='transfer' else ''
            if kind in ('transfer','internal_transfer') and (not to_bank_id or to_bank_id==bank_id): raise ValueError('Choose two different banks')
            old_entries=Entry.query.filter_by(transaction_id=t.id).all()
            old_ids={e.bank_id for e in old_entries}
            new_ids={bank_id}|({to_bank_id} if to_bank_id else set())
            affected=old_ids|new_ids
            locked=lock_banks(affected)
            if len(locked)!=len(affected): raise ValueError('Bank not found')
            _reject_closed_period_edits(t,affected)
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
            before_snapshot=transaction_snapshot(t,old_entries)
            before=f'{t.reference} {t.kind} {t.signed_amount} entries={[(e.bank_id,str(e.delta)) for e in old_entries]}'
            for e in old_entries: db.session.delete(e)
            db.session.flush()
            for bid,delta in new_deltas.items(): db.session.add(Entry(transaction_id=t.id,bank_id=bid,delta=delta))
            t.reference=reference;t.kind=kind;t.signed_amount=amount if kind=='deposit' else -amount
            t.customer_ref=request.form.get('customer_ref','').strip()[:120] or None
            t.remark=request.form.get('remark','').strip()[:2000]
            db.session.flush()
            write_transaction_audit(t,current_user.id,'EDIT',before=before_snapshot,after=transaction_snapshot(t))
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
            if (t.remark or '').startswith('[DEPOSIT_BACK:'): raise ValueError('Deposit Back linked records cannot be voided individually')
            if t.kind.startswith('dealer_'): raise ValueError('Dealer transactions require an audited reversal; edit/void is disabled')
            if t.status!='confirmed': raise ValueError('Transaction already voided')
            if t.business_date!=business_today(): raise ValueError('Only current-day transactions can be voided')
            entries=Entry.query.filter_by(transaction_id=t.id).all()
            affected={e.bank_id for e in entries}
            banks=lock_banks(affected)
            _reject_closed_period_edits(t,affected)
            if Closing.query.filter(Closing.bank_id.in_(affected),Closing.business_date==t.business_date).first():
                raise ValueError('Cannot delete: an affected bank has been closed for this business date')
            # Removing a credit must not create a negative bank balance.
            by_bank={}
            for e in entries: by_bank[e.bank_id]=by_bank.get(e.bank_id,Decimal('0'))+money(e.delta)
            for b in banks:
                if ledger_balance(b.id)-by_bank.get(b.id,Decimal('0'))<0:
                    raise ValueError(f'Cannot void: {b.name} would have a negative balance')
            before_snapshot=transaction_snapshot(t,entries)
            t.status='voided'
            write_transaction_audit(t,current_user.id,'VOID',before=before_snapshot,after=transaction_snapshot(t,entries),reason=reason[:500])
            db.session.add(Audit(actor_id=current_user.id,action='VOID_TRANSACTION',detail=f'{t.id} {t.reference}; reason={reason[:500]}'))
            db.session.commit()
            flash('Transaction voided; balances recalculated. History preserved.','success')
        except (ValueError,IntegrityError) as e:
            db.session.rollback();flash(str(e),'danger')
        destination=request.form.get('return_to','')
        if destination=='unclaimed':
            return redirect(url_for('unclaimed_deposits'))
        if destination=='new_transaction':
            return redirect(url_for('new_transaction')+'#latest-transactions')
        if destination=='dashboard':
            start=request.form.get('start_date','')
            end=request.form.get('end_date','')
            try:
                a=date.fromisoformat(start);b=date.fromisoformat(end)
                if a<=b<=business_today() and (b-a).days<=365:
                    return redirect(url_for('dashboard',start_date=a.isoformat(),end_date=b.isoformat()))
            except (ValueError,TypeError):pass
            return redirect(url_for('dashboard'))
        return redirect(url_for('transactions'))

    def _utc_db(dt):
        """Postgres aware datetimes; SQLite may return naive UTC for tests."""
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    def _closing_anchor(bank_id):
        return (ClosingPeriod.query.filter_by(bank_id=bank_id)
                .order_by(ClosingPeriod.end_utc.desc()).first())

    def _reject_closed_period_edits(transaction, bank_ids):
        """Protect historical closed periods even if their business_date differs."""
        instant=_utc_db(transaction.created_at)
        for bank_id in bank_ids:
            for period in ClosingPeriod.query.filter_by(bank_id=bank_id,status='closed').all():
                if _utc_db(period.start_utc) <= instant < _utc_db(period.end_utc):
                    raise ValueError('Transaction is inside a finalized UTC closing period; use an audited adjustment')

    @app.post('/closing/initialize')
    @role_required('admin')
    def initialize_closing_boundary():
        """Explicit one-time bank anchor, never inferred from historical business_date."""
        try:
            pid=require_single_platform()
            bank_id=request.form.get('bank_id','').strip()
            bank_for_platform(bank_id,pid)
            lock_banks([bank_id])
            if _closing_anchor(bank_id): raise ValueError('This bank already has a closing period')
            if Closing.query.filter_by(bank_id=bank_id).first():
                raise ValueError('Historical closings exist: first reconcile and migrate them before initializing; no automatic guess allowed')
            raw=request.form.get('anchor_utc','').strip()
            anchor=datetime.fromisoformat(raw.replace('Z','+00:00'))
            if anchor.tzinfo is None: raise ValueError('Anchor must include UTC offset')
            anchor=as_utc(anchor)
            if anchor>now(): raise ValueError('Anchor cannot be in the future')
            # Anchor is represented as a zero-duration marker with separate status,
            # never counted as a finalized closing period.
            period=ClosingPeriod(bank_id=bank_id,start_utc=anchor,end_utc=anchor,status='anchor')
            db.session.add(period)
            db.session.add(Audit(actor_id=current_user.id,action='INITIALIZE_CLOSING_BOUNDARY',detail=f'{bank_id} UTC={anchor.isoformat()}'))
            db.session.commit();flash('Closing boundary initialized. Next closing uses this exact UTC instant.','success')
        except (ValueError,TypeError,IntegrityError) as exc:
            db.session.rollback();flash(str(exc),'danger')
        return redirect(url_for('closing'))

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
                if bs[0].status=='closed': raise ValueError('Closed banks cannot be reconciled or rolled into the next opening')
                previous=_closing_anchor(bank_id)
                if previous is None:
                    raise ValueError('Closing is blocked: Admin must initialize a verified UTC boundary for this bank first')
                start=_utc_db(previous.end_utc)
                policy=db.session.get(ClosingPolicy,1)
                if policy is None: raise ValueError('Configure Business Timezone closing policy first')
                # Closing amounts follow business_date (Melbourne calendar day), which is only
                # coherent when the business day is Melbourne 00:00-00:00 with automatic DST.
                if not policy.auto_timezone or policy.closing_hour!=0 or policy.closing_minute!=0:
                    raise ValueError('Closing requires Auto Timezone (Australia/Melbourne) and a 00:00 closing time')
                end=next_boundary(start,policy.closing_hour,policy.closing_minute,policy.auto_timezone,policy.manual_offset_minutes)
                if end>now(): raise ValueError('Scheduled closing time has not arrived yet')
                # The period ending at Melbourne 00:00 on 2 Nov closes the books of 1 Nov.
                period_day=period_business_day(end,policy.auto_timezone,policy.manual_offset_minutes)
                if Closing.query.filter_by(bank_id=bank_id,business_date=period_day).first(): raise ValueError(f'Business date {period_day.isoformat()} is already closed for this bank')
                # No silent skip: if the earliest due period is older than one day,
                # require reconciliation instead of closing multiple days as one.
                if now()-end>timedelta(hours=26):
                    raise ValueError('Closing boundary is overdue; reconcile missed periods before proceeding')
                overview=day_bank_overview(db,Bank,Entry,Transaction,bs,period_day,period_day)[bank_id]
                sysbal=ledger_balance(bank_id,through=period_day)
                verify_period(overview['opening'],overview['in_amount'],overview['out_amount'],sysbal,start,end,previous_end=start)
                close=Closing(bank_id=bank_id,business_date=period_day,system_balance=sysbal,actual_balance=actual,difference=actual-sysbal,remark=request.form.get('remark','').strip(),created_by=current_user.id)
                db.session.add(close)
                db.session.flush()
                period=ClosingPeriod(bank_id=bank_id,start_utc=start,end_utc=end,status='closed',closing_id=close.id)
                db.session.add(period)
                db.session.flush()
                # Snapshot the state as it was OBSERVED at closing. Do not recalculate
                # historical unclaimed values from mutable live lead statuses.
                leads=db.session.execute(select(UnclaimedDeposit).where(
                    UnclaimedDeposit.bank_id==bank_id,
                    UnclaimedDeposit.status=='unclaimed',
                    UnclaimedDeposit.created_at<end).order_by(UnclaimedDeposit.id).with_for_update()).scalars().all()
                lead_items=[{'id':lead.id,'amount':str(money(lead.amount)),
                             'reference':lead.reference} for lead in leads]
                db.session.add(ClosingFinancialSnapshot(
                    closing_period_id=period.id,bank_id=bank_id,
                    opening=overview['opening'],incoming=overview['in_amount'],
                    outgoing=overview['out_amount'],closing_balance=sysbal,
                    unclaimed_amount=sum((money(lead.amount) for lead in leads),Decimal('0')),
                    unclaimed_count=len(leads),
                    unclaimed_json=json.dumps(lead_items,sort_keys=True)))
                pending_close=pending_unclaim_through_day(bank_id,period_day,end)
                if pending_close<0: raise ValueError('Pending Unclaimed cannot be negative')
                db.session.add(OperatingClosingSnapshot(closing_period_id=period.id,bank_id=bank_id,
                    actual_bank_closing=sysbal,pending_unclaimed_closing=pending_close,
                    operating_closing=sysbal-pending_close))
                db.session.add(Audit(actor_id=current_user.id,action='CLOSE_BANK',detail=f'{bank_id} UTC=[{start.isoformat()},{end.isoformat()}) difference={actual-sysbal}'))
                db.session.commit();flash('Bank closing saved with immutable UTC period','success')
            except (ValueError,IntegrityError) as e: db.session.rollback();flash(str(e),'danger')
            return redirect(url_for('closing'))
        day=selected_date()
        closes_all=Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).all()
        close_page=max(1,request.args.get('page',1,type=int))
        close_pagination=Closing.query.filter(Closing.business_date==day,Closing.bank_id.in_(select(Bank.id).where(platform_scope(Bank)))).order_by(Closing.created_at.desc(),Closing.id.desc()).paginate(page=close_page,per_page=15,error_out=False)
        close_ids=[c.id for c in close_pagination.items]
        operating_closes={}
        if close_ids:
            snap_rows=db.session.execute(select(ClosingPeriod.closing_id,OperatingClosingSnapshot)
                .join(OperatingClosingSnapshot,OperatingClosingSnapshot.closing_period_id==ClosingPeriod.id)
                .where(ClosingPeriod.closing_id.in_(close_ids))).all()
            operating_closes=dict(snap_rows)
        all_banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
        operational_banks=[b for b in all_banks if b.status!='closed']
        # Daily status transitions are immutable events. Capture the balance at
        # the moment of closure from its immutable blocked-bank snapshot.
        day_start=datetime.combine(day,datetime.min.time(),tzinfo=timezone_for_report_day(day)).astimezone(timezone.utc)
        day_end=datetime.combine(day+timedelta(days=1),datetime.min.time(),tzinfo=timezone_for_report_day(day)).astimezone(timezone.utc)
        closed_events=(db.session.execute(select(BankStatusHistory, Bank, BlockedUnclaimedSettlement)
            .join(Bank,Bank.id==BankStatusHistory.bank_id)
            .outerjoin(BlockedUnclaimedSettlement,BlockedUnclaimedSettlement.status_history_id==BankStatusHistory.id)
            .where(platform_scope(Bank),BankStatusHistory.new_status=='closed',
                   BankStatusHistory.old_status!='closed',BankStatusHistory.created_at>=day_start,
                   BankStatusHistory.created_at<day_end)
            .order_by(BankStatusHistory.created_at,BankStatusHistory.id)).all())
        closed_details=[]
        for event,bank,snap in closed_events:
            closed_details.append({'bank':bank,'at':_utc_db(event.created_at).astimezone(timezone_for_report_day(day)).strftime('%Y-%m-%d %H:%M:%S'),'actor_id':event.actor_id,
                'actual':money(snap.ledger_balance) if snap else None,
                'pending':money(snap.unclaimed_amount) if snap else None,
                'operating':money(snap.ledger_balance)-money(snap.unclaimed_amount) if snap else None})
        # Overview of the chosen date; historical completed reconciliation uses
        # immutable financial snapshots rather than live ledger recalculation.
        all_day_closes={c.bank_id:c for c in closes_all}
        day_snapshots={}
        if all_day_closes:
            for closing_id,snapshot in db.session.execute(select(ClosingPeriod.closing_id,OperatingClosingSnapshot)
                .join(OperatingClosingSnapshot,OperatingClosingSnapshot.closing_period_id==ClosingPeriod.id)
                .where(ClosingPeriod.closing_id.in_([c.id for c in closes_all]))):
                day_snapshots[closing_id]=snapshot
        total_actual=sum((money(x.actual_bank_closing) for x in day_snapshots.values()),Decimal('0'))
        total_pending=sum((money(x.pending_unclaimed_closing) for x in day_snapshots.values()),Decimal('0'))
        total_operating=sum((money(x.operating_closing) for x in day_snapshots.values()),Decimal('0'))
        total_difference=sum((money(c.difference) for c in closes_all),Decimal('0'))
        overview={'actual':total_actual,'pending':total_pending,'operating':total_operating,
                  'difference':total_difference,'reconciled':len(closes_all),
                  'active':len(operational_banks)}
        return render_template('closing.html',banks=operational_banks,balances=balances(),closes=close_pagination.items,close_pagination=close_pagination,closed_ids={c.bank_id for c in closes_all},selected_day=day,can_close=(day==today),operating_closes=operating_closes,closed_details=closed_details,closed_count=len({x['bank'].id for x in closed_details}),closed_balance_total=sum((x['actual'] for x in closed_details if x['actual'] is not None),Decimal('0')),overview=overview,all_banks=all_banks)

    @app.context_processor
    def inject_report_date():
        return {'report_date': request.args.get('date', business_today().isoformat()), 'today_myt': business_today().isoformat(), 'platform_options': Platform.query.filter(Platform.id.in_(permitted_platform_ids()),Platform.active.is_(True)).order_by(Platform.name).all() if current_user.is_authenticated else [], 'active_platform_id': session.get('platform_id','all' if current_user.is_authenticated and current_user.role=='admin' else ''), 'can': permitted if current_user.is_authenticated else lambda _:False}

    @app.get('/reports/utc-reconciliation')
    @role_required('admin','finance')
    def utc_reconciliation():
        day=selected_date()
        try:
            start,end=utc_report_bounds(day)
            banks=Bank.query.filter(platform_scope(Bank)).order_by(Bank.name).all()
            rows=utc_bank_overview(db,Bank,Entry,Transaction,banks,start,end)
        except ValueError as exc:
            flash(str(exc),'danger')
            return redirect(url_for('closing'))
        return render_template('utc_reconciliation.html',day=day,start=start,end=end,banks=banks,rows=rows)

    @app.get('/reports/closing-verification')
    @role_required('admin','finance')
    def closing_verification():
        from financial_integrity import verify_period
        periods=(db.session.execute(select(ClosingPeriod)
                 .where(ClosingPeriod.status=='closed',ClosingPeriod.bank_id.in_(
                     select(Bank.id).where(platform_scope(Bank))))
                 .order_by(ClosingPeriod.end_utc.desc()).limit(200)).scalars().all())
        result=[]
        for period in periods:
            snap=ClosingFinancialSnapshot.query.filter_by(closing_period_id=period.id).first()
            close=db.session.get(Closing,period.closing_id) if period.closing_id else None
            if snap is None or close is None:
                state='MISSING_SNAPSHOT'
            else:
                try:
                    verify_period(snap.opening,snap.incoming,snap.outgoing,
                                  snap.closing_balance,period.start_utc,period.end_utc)
                    if money(snap.closing_balance)!=money(close.system_balance):
                        raise ValueError('Closing system balance differs from snapshot')
                    state='MATCH'
                except ValueError:
                    state='MISMATCH'
            result.append({'bank_id':period.bank_id,'start':period.start_utc.isoformat(),
                           'end':period.end_utc.isoformat(),'status':state})
        return {'periods':result,'checked':len(result),
                'mismatches':sum(row['status']!='MATCH' for row in result)}

    # Stage 7: experimental Admin verification UI (no automatic scheduler).
    from auto_closing_stage7 import bp as auto_closing_stage7_bp
    app.register_blueprint(auto_closing_stage7_bp)

    # JSON-aware HTTP errors: fetch() callers send Accept: application/json and must
    # never receive Flask's HTML error page (it breaks response.json() in the browser).
    from werkzeug.exceptions import HTTPException as _HTTPException
    @app.errorhandler(_HTTPException)
    def _json_http_error(e):
        if request.accept_mimetypes.best!='application/json': return e
        response=app.json.response({'ok':False,'duplicate':False,'error':e.description or e.name})
        response.status_code=e.code or 500
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/health')
    def health(): return {'status':'ok'}
    return app

app=create_app()
