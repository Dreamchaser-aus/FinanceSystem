# BankFlow V1 — Railway direct deployment

This is a **development starter**, not an audited financial production application.

## Deploy via GitHub to Railway
1. Create a **private** GitHub repository. Upload the **contents of this ZIP to the repository root** (`app.py`, `railway.json`, `requirements.txt`, `start.sh`, `templates/`). Do not upload the ZIP as one file into the repository.
2. Railway > New Project > Deploy from GitHub repo. Select the private repository.
3. Railway > Add Service > Database > PostgreSQL (same project).
4. In the **web app service** Variables add:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}` (use Railway's **Add Reference** picker to select the database's DATABASE_URL; database service name may differ)
   - `SECRET_KEY=` long random secret (generate locally using `python -c "import secrets; print(secrets.token_urlsafe(48))"`)
   - `ADMIN_USER=` administrator username
   - `ADMIN_PASS=` strong administrator password (at least 12 characters)
   - `COOKIE_SECURE=1`
5. Deploy or redeploy the app. `start.sh` checks variables, creates tables and initial administrator if needed, and starts Gunicorn on Railway's `$PORT`.
6. In Railway web service Settings > Networking > Generate Domain. Open the HTTPS domain and log in.

## Important notes
- **Do not add** `.env`, the real bank workbook, personal information, or passwords to the repository.
- The bootstrap only creates the initial admin **if no admin exists**. Changing `ADMIN_PASS` later will **not** reset the existing admin password.
- If PostgreSQL is missing or unreachable, the web service fails to start intentionally instead of writing money data to ephemeral SQLite.
- The app currently supports manual single transaction entry and confirmed internal transfers; it does not yet implement import, full employee administration, reversal approval, or audited financial controls.
- Before real funds are recorded, add MFA/rate limiting, migration management, backups, stronger audit controls, reconciliation tests and restore drills.
- On Railway, you can use `Procfile` only if you explicitly select it; `railway.json` takes care of the start command.
