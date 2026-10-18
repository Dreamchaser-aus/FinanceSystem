# BankFlow V1 — Single Entry Finance Backend

## Scope
Functional starter project: admin login, bank registry, single transaction entry (deposit, withdrawal, **confirmed internal** transfer), balance calculation, transaction history, and per-bank daily closing. Does **not** import the Excel automatically, handle pending/external transfers, create staff through UI, generate Excel reports, provide adjustment/reversal UI, or reconcile legacy daily difference formula. Do not use as a production accounting system until those controls and tests are completed.

## Local setup (Python 3.10+)
```powershell
cd bankflow_v1
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```
Edit `.env` to set a secure SECRET_KEY and ADMIN_PASS. Flask CLI uses `.env` if `python-dotenv` is installed.
```powershell
flask --app app init-db
flask --app app seed-admin
flask --app app run --debug
```
Visit http://127.0.0.1:5000. Add banks with opening balances, then record transactions. `--debug` is for local development only.

## Railway deployment
1. Push project to a private GitHub repository and deploy it as a Railway service.
2. Attach a Railway PostgreSQL database; set `DATABASE_URL` to its connection string.
3. Set `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASS` and `COOKIE_SECURE=1` in Railway environment variables.
4. Run `flask --app app init-db` and `flask --app app seed-admin` once against the Railway database (via Railway CLI or a one-off job). Do not run `seed-admin` repeatedly.
5. Configure Railway to start via the included `Procfile` or `gunicorn 'app:app' --bind 0.0.0.0:$PORT`.
6. Configure backups, restricted access, monitoring and restore testing before storing real data.

## Key accounting rules
- Bank balance = opening balance + sum(ledger entries). The balance is not editable by staff.
- Deposit adds amount; withdrawal subtracts amount; confirmed internal transfer posts two equal and opposite entries.
- All posts are atomic DB transactions. PostgreSQL locks bank rows during posting.
- Unique transaction reference rejects duplicate IDs. Avoid reusing identifiers even across banks.
- New debits cannot take balance below zero, but imported opening balances may be negative.
- Each transaction is recorded against the current Malaysia business date; closing prevents further posting for that bank on the same date.
- `Closing` is per-bank, not a full day/global closing.
- Legacy Excel `TOPUP` signed values and `REDEPOSIT` require a separate migration mapping before importing historical records.

## Security / known V1 limitations
- No staff administration UI, reset password, MFA, rate-limited login, or granular bank-level permissions yet.
- No reversal/adjustment approval workflow, export, full reconciliation reports, import wizard, or financial audit report UI yet.
- SQLite is for local demo only; PostgreSQL is required for concurrent production use.
- Database schema uses `db.create_all()` only; adopt Alembic migrations for production schema changes.
- Do not upload the supplied sensitive Excel to a public repo.
- Historical workbook values should not be imported without validating sheet dates, cached formulas, bank aliases and negative TOPUP semantics.
