#!/usr/bin/env bash
# Восстановить базу из копии: bash deploy/restore.sh backups/schedule_2026-10-15_0300.dump
# ВНИМАНИЕ: текущие данные в базе будут заменены данными из копии.
cd "$(dirname "$0")/.." || exit 1
file="$1"
if [ -z "$file" ] || [ ! -s "$file" ]; then
    echo "Укажите файл копии, например: bash deploy/restore.sh backups/schedule_2026-10-15_0300.dump"
    ls -1t backups/schedule_*.dump 2>/dev/null | head -5
    exit 1
fi
read -r -p "Заменить текущую базу данными из $file? Напишите ДА: " answer
[ "$answer" = "ДА" ] || { echo "Отменено."; exit 1; }
echo "Останавливаю сайт…"
docker compose stop web
if docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$file"; then
    echo "База восстановлена."
else
    echo "pg_restore сообщил об ошибках (см. выше). Часть предупреждений при --clean бывает безвредной."
fi
docker compose start web
echo "Сайт запущен."
