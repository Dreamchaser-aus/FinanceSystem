"""V7.9.6.43 stage 3: read-only database readiness / due-period inspection.

No database writes, no scheduler. Run with DATABASE_URL set.
"""
import os
from datetime import datetime, timezone
from auto_closing_planner import due_periods


def inspect_database(database_url=None, instant=None):
    from sqlalchemy import create_engine, text
    url = database_url or os.environ['DATABASE_URL']
    engine = create_engine(url, pool_pre_ping=True)
    instant = instant or datetime.now(timezone.utc)
    output = []
    with engine.connect() as conn:
        policies = conn.execute(text('SELECT id, auto_timezone, manual_offset_minutes, closing_hour, closing_minute FROM closing_policy ORDER BY id')).mappings().all()
        platforms = conn.execute(text('SELECT id, name FROM platform WHERE active = true ORDER BY name')).mappings().all()
        banks = conn.execute(text('SELECT id, platform_id, name, status FROM bank ORDER BY platform_id, name')).mappings().all()
        anchors = conn.execute(text('SELECT DISTINCT ON (bank_id) bank_id, end_utc FROM closing_period ORDER BY bank_id, end_utc DESC')).mappings().all()
    policy = policies[0] if len(policies) == 1 else None
    latest = {r['bank_id']: r['end_utc'] for r in anchors}
    for platform in platforms:
        for bank in (b for b in banks if b['platform_id'] == platform['id']):
            row = {'platform': platform['name'], 'bank': bank['name'], 'bank_id': bank['id'], 'status': 'BLOCKED', 'due': []}
            if bank['status'] == 'closed':
                row['reason'] = 'Bank is closed'
            elif policy is None:
                row['reason'] = 'Missing or ambiguous global closing policy'
            elif bank['id'] not in latest:
                row['reason'] = 'No verified UTC closing anchor'
            else:
                try:
                    periods = due_periods(platform['id'], bank['id'], latest[bank['id']], instant,
                        hour=policy['closing_hour'], minute=policy['closing_minute'],
                        auto_timezone=policy['auto_timezone'], manual_offset_minutes=policy['manual_offset_minutes'])
                    row.update(status='READY', reason='Read-only preview; global policy may not match platform',
                               due=[{'start_utc': p.start_utc.isoformat(), 'end_utc': p.end_utc.isoformat()} for p in periods])
                except ValueError as exc:
                    row['reason'] = str(exc)
            output.append(row)
    engine.dispose()
    return output

if __name__ == '__main__':
    import json
    print(json.dumps(inspect_database(), indent=2, ensure_ascii=False))
