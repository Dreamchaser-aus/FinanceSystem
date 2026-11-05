"""Stage 7 experimental admin UI for Stage 4-6 verification records.

Requires Stage 4 migration. Does not schedule or generate closings.
Writes require BANKFLOW_STAGE6_VERIFY=YES_ISOLATED_TEST_DATABASE.
"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import text
from auto_closing_stage6 import verify

bp = Blueprint('auto_closing_stage7', __name__, url_prefix='/admin/auto-closing')

# Read-only Store-balance label for the New Transaction page (see store_balance.py).
from store_balance import bp as _store_balance_bp  # noqa: E402
bp.register_blueprint(_store_balance_bp)


def admin_only():
    if not current_user.is_authenticated or current_user.role != 'admin':
        abort(403)


@bp.get('/')
@login_required
def index():
    admin_only()
    from app import db
    try:
        rows = db.session.execute(text('''
            SELECT v.closing_period_id, v.platform_id, p.name AS platform_name,
                   b.name AS bank_name, v.status, v.system_balance,
                   v.actual_balance, v.difference, cp.start_utc, cp.end_utc
              FROM auto_closing_verification v
              JOIN platform p ON p.id=v.platform_id
              JOIN bank b ON b.id=v.bank_id AND b.platform_id=v.platform_id
              JOIN closing_period cp ON cp.id=v.closing_period_id AND cp.bank_id=v.bank_id
             ORDER BY cp.end_utc DESC LIMIT 200
        ''')).mappings().all()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Stage 7 verification query failed')
        return render_template('auto_closing_stage7.html', rows=[], unavailable=True), 503
    return render_template('auto_closing_stage7.html', rows=rows, unavailable=False)


@bp.post('/verify/<period_id>')
@login_required
def verify_period(period_id):
    admin_only()
    if not current_app.config.get('TESTING') and not current_app.config.get('STAGE7_EXPERIMENTAL_WRITE', False):
        abort(403, 'Experimental writes disabled')
    if request.form.get('confirm') != 'VERIFY':
        flash('Enter VERIFY to confirm.', 'danger')
        return redirect(url_for('auto_closing_stage7.index'))
    from app import db
    try:
        result = verify(db.engine.url.render_as_string(hide_password=False), period_id, str(current_user.id),
                        request.form.get('actual_balance'),
                        remark=request.form.get('remark', '')[:500], dry_run=False)
        flash('Verification saved: ' + result['status'], 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('auto_closing_stage7.index'))
