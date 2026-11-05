"""Read-only dealer balances for the New Transaction page ("Dealers" tab).

Each platform's Store is an ordinary Dealer whose name contains "Store" (rate 1).
Its balance is the sum of DealerTrade.myr_delta over confirmed transactions, the
same figure the Dealers page shows: negative = we owe the Store, positive = the
Store owes us. Nothing is written; failures only hide the label.

Registered as a nested blueprint of auto_closing_stage7 so that app.py stays
untouched; it can be moved into app.py later.
"""
from flask import Blueprint, current_app, request
from flask_login import current_user

bp = Blueprint('store_balance', __name__)


@bp.app_context_processor
def inject_dealer_rows():
    """Rows for the "Dealers" tab under Active Bank Balances on the New Transaction page."""
    if request.endpoint != 'new_transaction' or not current_user.is_authenticated:
        return {}
    try:
        from sqlalchemy import select, func
        from app import db, Dealer, DealerTrade, Platform, Transaction, platform_scope, permitted, money
        if not permitted('view_bank_balances'):
            return {}
        # Confirmed totals per dealer and flow: AUD moved and the MYR effect (same source as the Dealers page).
        totals = {}
        for dealer_id, flow, aud, myr in db.session.execute(
                select(DealerTrade.dealer_id, DealerTrade.flow,
                       func.sum(DealerTrade.aud_amount), func.sum(DealerTrade.myr_delta))
                .join(Transaction, Transaction.id == DealerTrade.transaction_id)
                .where(Transaction.status == 'confirmed', platform_scope(Transaction))
                .group_by(DealerTrade.dealer_id, DealerTrade.flow)).all():
            t = totals.setdefault(dealer_id, {'sell': 0, 'buy': 0, 'payout': 0, 'myr': 0})
            t[flow.replace('dealer_', '')] = money(aud)
            t['myr'] += money(myr)
        rows = db.session.execute(
            select(Dealer, Platform.name).join(Platform, Platform.id == Dealer.platform_id)
            .where(platform_scope(Dealer)).order_by(Platform.name, Dealer.name)).all()
        out = []
        for d, _platform_name in rows:
            t = totals.get(d.id, {'sell': 0, 'buy': 0, 'payout': 0, 'myr': 0})
            # Every dealer (Store included) is shown in AUD; sign: sell +, buy / payout -.
            balance = money(t['sell']) - money(t['buy']) - money(t['payout'])
            out.append({'platform_id': d.platform_id, 'name': d.name, 'active': d.active,
                        'balance': balance,
                        'sell': t['sell'], 'buy': t['buy'], 'payout': t['payout']})
        return {'dealer_rows': out}
    except Exception:
        current_app.logger.exception('Dealer rows failed')
        return {}
