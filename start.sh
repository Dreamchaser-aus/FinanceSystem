#!/bin/sh
set -eu
: "${DATABASE_URL:?Set DATABASE_URL to Railway PostgreSQL connection URL}"
: "${SECRET_KEY:?Set SECRET_KEY}"
: "${ADMIN_USER:?Set ADMIN_USER}"
: "${ADMIN_PASS:?Set ADMIN_PASS}"
export COOKIE_SECURE=1
flask --app app bootstrap
exec gunicorn app:app --bind "0.0.0.0:${PORT:-8080}" --workers 2 --timeout 60 --access-logfile -
