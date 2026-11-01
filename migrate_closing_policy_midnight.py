"""One-time: set the global closing policy to Melbourne 00:00 (auto timezone).

Only changes a policy still at the old 07:00 default; never touches closed periods.
Run once against the live DB:  python migrate_closing_policy_midnight.py
"""
import os
from sqlalchemy import create_engine, text

engine = create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True)
with engine.begin() as conn:
    n = conn.execute(text('''UPDATE closing_policy
        SET closing_hour=0, closing_minute=0, auto_timezone=true, updated_at=now()
        WHERE id=1 AND closing_hour=7 AND closing_minute=0''')).rowcount
    row = conn.execute(text('SELECT auto_timezone, closing_hour, closing_minute FROM closing_policy WHERE id=1')).one_or_none()
print('updated rows:', n, '| current policy:', tuple(row) if row else None)
engine.dispose()
