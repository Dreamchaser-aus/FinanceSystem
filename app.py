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
        return render_template('banks.html',banks=Bank.query.order_by(Bank.kind,Bank.name).all(),balances=balances())

    @app.route('/transactions/new',methods=['GET','POST'])
    @login_required
    def new_transaction():
        if request.method=='POST':
