#!/usr/bin/env bash
# Резервная копия базы расписания (docker compose). Запуск из папки проекта или по cron:
#   0 3 * * *  bash /opt/raspisanie/deploy/backup.sh >> /var/log/raspisanie-backup.log 2>&1
# Копии: backups/schedule_ГГГГ-ММ-ДД_ЧЧММ.dump; хранятся последние KEEP (по умолчанию 30).
# Восстановление: bash deploy/restore.sh backups/<файл>.dump
cd "$(dirname "$0")/.." || exit 1
KEEP="${KEEP:-30}"
mkdir -p backups
name="backups/schedule_$(date +%Y-%m-%d_%H%M).dump"
if ! docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' > "$name.part"; then
    echo "$(date '+%F %T') ОШИБКА: pg_dump не удался, копия не сделана" >&2
    rm -f "$name.part"
    exit 1
fi
if [ ! -s "$name.part" ]; then
    echo "$(date '+%F %T') ОШИБКА: пустая копия — проверьте, запущен ли контейнер db" >&2
    rm -f "$name.part"
    exit 1
fi
mv "$name.part" "$name"
echo "$(date '+%F %T') копия: $name ($(du -h "$name" | cut -f1))"
# старые копии
ls -1t backups/schedule_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done
