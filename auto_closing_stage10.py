"""Stage 10 safety gate for experimental auto-closing SQL (read-only).

This gate intentionally refuses to approve live writes until accounting and
schema invariants have been resolved and verified against a disposable PostgreSQL.
"""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent


def audit_source(root=ROOT):
    writer = (root / 'auto_closing_stage5.py').read_text(encoding='utf-8')
    verifier = (root / 'auto_closing_stage6.py').read_text(encoding='utf-8')
    issues = []
    # Do not equate an unverified ledger balance with the actual bank balance.
    if 'actual_bank_closing,pending_unclaimed_closing,operating_closing' in writer and "'system':system" in writer:
        issues.append({'code':'ACTUAL_IS_LEDGER','severity':'BLOCKER',
                       'detail':'Stage 5 writes computed ledger balance into actual_bank_closing before Admin verification.'})
    if 'u.actual_bank_closing AS operating_system' in verifier:
        issues.append({'code':'VERIFIER_USES_ACTUAL_AS_LEDGER','severity':'BLOCKER',
                       'detail':'Stage 6 compares an actual-bank field as if it were a system ledger field.'})
    if 't.created_at<:end' in writer and 'business_date' not in writer:
        issues.append({'code':'CREATED_AT_ATTRIBUTION','severity':'BLOCKER',
                       'detail':'Snapshot selection is based on transaction created_at; backdated/effective-date semantics unverified.'})
    if 'unclaim_allocation' in writer and 'a.created_at<:end' in writer:
        issues.append({'code':'UNCLAIM_TIMING','severity':'REVIEW',
                       'detail':'Unclaim allocation timing and partial allocation semantics need database fixtures.'})
    if 'INSERT INTO closing_period' in writer and 'ON CONFLICT' not in writer:
        issues.append({'code':'IDEMPOTENCY_CONSTRAINT','severity':'REVIEW',
                       'detail':'Confirm a unique constraint exists for bank_id/end_utc; advisory locks alone do not establish durable idempotency.'})
    return {'safe_to_enable_auto_closing':False, 'database_integration_tested':False,
            'issues':issues,
            'next_actions':['Resolve blockers in ledger/actual schema and verification',
                            'Run Stage 4 migration in disposable PostgreSQL',
                            'Exercise Stage 5 with sample transactions, unclaims and boundary times',
                            'Verify unique constraints, rollback and retry, then add scheduler']}


if __name__ == '__main__':
    print(json.dumps(audit_source(), ensure_ascii=False, indent=2))
