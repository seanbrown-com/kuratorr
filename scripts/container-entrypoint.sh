#!/bin/sh
set -eu
if [ "${SKIP_MIGRATIONS:-false}" != "true" ]; then
  python manage.py migrate --noinput
  python manage.py collectstatic --noinput
fi
exec "$@"
