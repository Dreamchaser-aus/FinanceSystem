"""Stage 8 safe, read-only, platform-scoped scheduler readiness check.

This is NOT a live scheduler and NEVER writes closing records.
"""
import argparse
import json
import os
from datetime import datetime, timezone

from auto_closing_planner import due_periods


def inspect_due(database_url, now=None, max_periods=7):
    from sqlalchemy import create_engine, text
    if not 1 <= max_periods <= 31:
        raise ValueError('max_periods must be 1..31')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    engine = create_engine(database_url, pool_pre_ping=True)
    results = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(text('''SELECT p.platform_id, p.auto_timezone, p.manual_offset_minutes,
                p.closing_hour, p.closing_minute, p.enabled, b.id AS bank_id, b.name AS bank_name,
                b.status AS bank_status, b.active AS bank_active
                FROM platform_closing_policy p JOIN bank b ON b.platform_id=p.platform_id
                ORDER BY p.platform_id, b.name''')).mappings().all()
            for row in rows:
                result = {'platform_id': str(row['platform_id']), 'bank_id': str(row['bank_id']),
                          'bank_name': row['bank_name'], 'status': 'BLOCKED', 'due': []}
                if not row['enabled']:
                    result['reason'] = 'Platform auto closing disabled'
                elif not row['bank_active'] or row['bank_status'] == 'closed':
                    result['reason'] = 'Bank inactive or closed'
                else:
                    anchor = conn.execute(text('''SELECT end_utc FROM closing_period
                        WHERE bank_id=:bank_id ORDER BY end_utc DESC LIMIT 1'''),
                        {'bank_id': row['bank_id']}).scalar_one_or_none()
                    if anchor is None:
                        result['reason'] = 'Missing verified UTC anchor; no historical inference'
                    else:
                        try:
                            periods = due_periods(str(row['platform_id']), str(row['bank_id']), anchor, now,
                                hour=row['closing_hour'], minute=row['closing_minute'],
                                auto_timezone=row['auto_timezone'],
                                manual_offset_minutes=row['manual_offset_minutes'], limit=max_periods)
                            result['status'] = 'DUE' if periods else 'UP_TO_DATE'
                            result['reason'] = 'Read-only; no closing records written'
                            result['due'] = [{'start_utc': p.start_utc.isoformat(),
                                              'end_utc': p.end_utc.isoformat()} for p in periods]
                        except (ValueError, TypeError) as exc:
                            result['reason'] = str(exc)
                results.append(result)
    finally:
        engine.dispose()
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Stage 8 read-only auto closing readiness')
    parser.add_argument('--max-periods', type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(inspect_due(os.environ['DATABASE_URL'], max_periods=args.max_periods),
                     indent=2, ensure_ascii=False))
