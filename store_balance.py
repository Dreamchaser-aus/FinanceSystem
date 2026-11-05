"""Read-only dealer balances for the New Transaction page.

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
def inject_store_balances():
    if request.endpoint != 'new_transaction' or not current_user.is_authenticated:
        return {}
    try:
        from sqlalchemy import select, func, case
        from app import db, Dealer, DealerTrade, Transaction, platform_scope, permitted, money
        if not permitted('view_bank_balances'):
            return {}
        # Per dealer: MYR balance (same as the Dealers page) and AUD balance. AUD is signed like
        # myr_delta: dealer_sell is +, dealer_buy / dealer_payout are -.
        aud_delta = case((DealerTrade.flow == 'dealer_sell', DealerTrade.aud_amount),
                         else_=-DealerTrade.aud_amount)
        rows = {r[0]: (r[1], r[2], r[3]) for r in db.session.execute(
            select(DealerTrade.dealer_id, func.sum(DealerTrade.myr_delta),
                   func.sum(aud_delta), func.count(DealerTrade.id))
            .join(Transaction, Transaction.id == DealerTrade.transaction_id)
            .where(Transaction.status == 'confirmed', platform_scope(Transaction))
            .group_by(DealerTrade.dealer_id)).all()}
        dealers = (Dealer.query.filter(platform_scope(Dealer), Dealer.active.is_(True))
                   .order_by(Dealer.name).all())
        out = []
        for d in dealers:
            myr, aud, n = rows.get(d.id, (0, 0, 0))
            if 'store' in d.name.lower():      # Store runs at rate 1: keep showing RM
                out.append((d.name, money(myr), 'RM'))
            elif n:                            # other dealers: AUD amount, only once they have trades
                out.append((d.name, money(aud), 'AUD'))
        return {'store_balances': out}
    except Exception:
        current_app.logger.exception('Store balance label failed')
        return {}
