"""Read-only Store (dealer) balances for the New Transaction page.

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
        from sqlalchemy import select, func
        from app import db, Dealer, DealerTrade, Transaction, platform_scope, permitted, money
        if not permitted('view_bank_balances'):
            return {}
        totals = dict(db.session.execute(
            select(DealerTrade.dealer_id, func.sum(DealerTrade.myr_delta))
            .join(Transaction, Transaction.id == DealerTrade.transaction_id)
            .where(Transaction.status == 'confirmed', platform_scope(Transaction))
            .group_by(DealerTrade.dealer_id)).all())
        dealers = (Dealer.query.filter(platform_scope(Dealer), Dealer.active.is_(True))
                   .order_by(Dealer.name).all())
        return {'store_balances': [(d.name, money(totals.get(d.id, 0)))
                                   for d in dealers if 'store' in d.name.lower()]}
    except Exception:
        current_app.logger.exception('Store balance label failed')
        return {}
