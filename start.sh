
#!/bin/sh
set -eu

echo "Starting BankFlow V1..."

: "${DATABASE_URL:?DATABASE_URL is missing}"
: "${SECRET_KEY:?SECRET_KEY is missing}"
: "${ADMIN_USER:?ADMIN_USER is missing}"
: "${ADMIN_PASS:?ADMIN_PASS is missing}"

export COOKIE_SECURE=1

echo "Initializing PostgreSQL database..."
flask --app app bootstrap

echo "Database initialized successfully."

echo "Starting Gunicorn..."
exec gunicorn app:app \
  --bind "0.0.0.0:${PORT:-8080}" \
  --workers 2 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
