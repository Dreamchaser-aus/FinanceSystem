"""Stage 11 fail-closed accounting safety gate for experimental auto closing.

The Stage 5/6 write paths are deliberately blocked until actual-vs-ledger
schema, period attribution and PostgreSQL integration are verified.
"""
from auto_closing_stage10 import audit_source


def require_verified_accounting():
    report = audit_source()
    blockers = [issue['code'] for issue in report['issues'] if issue['severity'] == 'BLOCKER']
    if not report['database_integration_tested'] or blockers:
        raise RuntimeError('Stage 11 safety gate: auto-closing writes disabled; '
                           'unverified accounting/schema: ' + ', '.join(blockers or ['POSTGRES_INTEGRATION']))
    return True

if __name__ == '__main__':
    import json
    print(json.dumps(audit_source(), indent=2))
