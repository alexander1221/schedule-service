#!/bin/sh
# Запуск контейнеров web и worker: дождаться БД, применить миграции и роли, собрать статику
# (worker пропускает это: SKIP_MIGRATE=1, SKIP_COLLECTSTATIC=1), стартовать.
set -e

if [ "${DB_ENGINE:-sqlite}" = "postgres" ]; then
  echo "[entrypoint] жду PostgreSQL ${DB_HOST:-db}:${DB_PORT:-5432}…"
  i=0
  until python - <<'PY'
import os, sys
import psycopg
try:
    psycopg.connect(
        host=os.environ.get("DB_HOST", "db"), port=os.environ.get("DB_PORT", "5432"),
        user=os.environ.get("DB_USER", "schedule"), password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ.get("DB_NAME", "schedule"), connect_timeout=3,
    ).close()
except Exception as e:
    print(e, file=sys.stderr); sys.exit(1)
PY
  do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then echo "[entrypoint] PostgreSQL недоступен — проверьте DB_* в .env"; exit 1; fi
    sleep 2
  done
fi

if [ "${SKIP_MIGRATE:-0}" != "1" ]; then
  python manage.py migrate --noinput
  python manage.py init_roles
fi
if [ "${SKIP_COLLECTSTATIC:-0}" != "1" ]; then
  python manage.py collectstatic --noinput --verbosity 0
fi
exec "$@"
