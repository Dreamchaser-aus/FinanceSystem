"""Stage 14: pure accounting preparation for auto-closing (NO database writes).

Separates ledger totals, operating totals, and verified actual balances.
This module is deliberately not connected to the Stage 5 writer or scheduler.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

CENT = Decimal('0.01')


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Invalid money amount') from exc
    if not result.is_finite() or result.as_tuple().exponent < -2:
        raise ValueError('Money requires at most two decimal places')
    return result.quantize(CENT)


@dataclass(frozen=True)
class LedgerSnapshot:
    opening: Decimal
    incoming: Decimal
    outgoing: Decimal
    pending_unclaimed: Decimal
    ledger_closing: Decimal
    operating_closing: Decimal
    actual_bank_balance: None = None
    status: str = 'PENDING_VERIFICATION'


def prepare_snapshot(opening, incoming, outgoing, pending_unclaimed):
    opening, incoming, outgoing, pending = map(amount, (opening, incoming, outgoing, pending_unclaimed))
    if incoming < 0 or outgoing < 0 or pending < 0:
        raise ValueError('Incoming, outgoing and pending unclaimed cannot be negative')
    closing = opening + incoming - outgoing
    if pending > closing:
        raise ValueError('Pending unclaimed exceeds ledger closing; manual investigation required')
    return LedgerSnapshot(opening, incoming, outgoing, pending, closing, closing - pending)


def verify_snapshot(snapshot, actual_balance):
    if snapshot.actual_bank_balance is not None or snapshot.status != 'PENDING_VERIFICATION':
        raise ValueError('Only unverified snapshots can be verified')
    actual = amount(actual_balance)
    difference = actual - snapshot.ledger_closing
    return {'actual_bank_balance': actual, 'difference': difference,
            'status': 'VERIFIED' if difference == 0 else 'DISPUTED'}
