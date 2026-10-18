"""One-time, explicit provisioning after V6 migration. No credentials hardcoded."""
import os
from werkzeug.security import generate_password_hash
from app import create_app, db, User, Platform, UserPlatformAccess, UserPermission

USERNAME = 'staff_01'
PLATFORM = 'Platform A'
PERMS = {'view_dashboard', 'create_transaction', 'view_transactions'}

app = create_app()
with app.app_context():
    password = os.environ.get('STAFF_01_INITIAL_PASSWORD', '')
    if len(password) < 12:
        raise SystemExit('Set STAFF_01_INITIAL_PASSWORD to a strong password (12+ characters).')
    p = Platform.query.filter_by(name=PLATFORM).one_or_none()
    if p is None:
        p = Platform(name=PLATFORM)
        db.session.add(p)
        db.session.flush()
    if not p.active:
        raise SystemExit('Platform A is inactive. No account changes made.')
    u = User.query.filter_by(username=USERNAME).one_or_none()
    if u is not None:
        raise SystemExit('staff_01 already exists. Configure it in Admin > Staff; refusing to overwrite.')
    u = User(username=USERNAME, password_hash=generate_password_hash(password), role='staff', active=True)
    db.session.add(u)
    db.session.flush()
    db.session.add(UserPlatformAccess(user_id=u.id, platform_id=p.id))
    for permission in sorted(PERMS):
        db.session.add(UserPermission(user_id=u.id, permission=permission))
    db.session.commit()
    print('Created staff_01; role=staff; platform=Platform A; permissions=' + ', '.join(sorted(PERMS)))
