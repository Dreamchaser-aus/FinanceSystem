#!/bin/sh
set -eu
: "${DATABASE_URL:?Set DATABASE_URL}"
: "${SECRET_KEY:?Set SECRET_KEY}"
: "${ADMIN_USER:?Set ADMIN_USER}"
: "${ADMIN_PASS:?Set ADMIN_PASS}"
export COOKIE_SECURE=1
echo "Starting BankFlow V1..."
echo "Initializing PostgreSQL database..."
flask --app app bootstrap
echo "Applying bank status migration..."
python migrate_bank_status.py
echo "Database initialized successfully."
echo "Starting Gunicorn..."
exec gunicorn app:app --bind "0.0.0.0:${PORT:-8080}" --workers 2 --timeout 60 --access-logfile - --error-logfile -
