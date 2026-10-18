"""Apply migrations/001_bank_status.sql transactionally on PostgreSQL before Flask bootstrap."""
import os
from pathlib import Path
from sqlalchemy import create_engine

def main():
    url=os.environ.get('DATABASE_URL','')
    if url.startswith('postgres://'):
        url='postgresql+psycopg://'+url[len('postgres://'):]
    elif url.startswith('postgresql://'):
        url='postgresql+psycopg://'+url[len('postgresql://'):]
    if not url.startswith('postgresql+psycopg://'):
        raise RuntimeError('PostgreSQL DATABASE_URL is required for migration')
    sql=(Path(__file__).parent/'migrations'/'001_bank_status.sql').read_text()
    statements=[s.strip() for s in sql.split(';') if s.strip()]
    engine=create_engine(url)
    with engine.begin() as connection:
        for stmt in statements:
            stmt='\n'.join(line for line in stmt.splitlines() if not line.lstrip().startswith('--')).strip()
            if stmt.upper() in ('BEGIN','COMMIT') or not stmt:
                continue
            connection.exec_driver_sql(stmt)
    engine.dispose()
    print('Bank status migration completed successfully',flush=True)

if __name__=='__main__': main()
