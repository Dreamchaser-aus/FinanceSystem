"""Pure financial integrity checks for a single closed UTC period."""
from decimal import Decimal

def cents(value):
    return Decimal(str(value)).quantize(Decimal('0.01'))

def verify_period(opening, incoming, outgoing, closing, start, end, previous_end=None):
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('Closing boundaries must be aware and strictly increasing')
    if previous_end is not None and start != previous_end:
        raise ValueError('Closing periods are not contiguous')
    expected = cents(opening) + cents(incoming) - cents(outgoing)
    if expected != cents(closing):
        raise ValueError('Closing balance does not reconcile')
    return expected
