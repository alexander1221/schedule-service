# Размещение за nginx: HTTPS на порту 8443

Для выбранного сервера 185.11.247.226 предусмотрен путь:

`браузер → nginx:8443 (TLS) → 127.0.0.1:18000 → Caddy:80 → web:8000`.

Nginx и сайт оборудования на 443 сохраняются. Caddy нужен внутри стека для статики
и проксирования; его внешние порты 80/443 заменяются одним localhost-портом.
Требуется Compose **2.24.4+**, поддерживающий `!override` для полной замены портов.

В этом профиле web и worker используют один образ `schedule-service:work`.
По умолчанию Gunicorn запускает один веб-процесс; `WEB_WORKERS` позволяет изменить
число на более мощной машине. Это отдельная настройка от `SOLVER_MAX_WORKERS`.

На Linux в `/opt/raspisanie/.env`:

```dotenv
COMPOSE_PROJECT_NAME=raspisanie
COMPOSE_FILE=docker-compose.yml:deploy/compose.nginx.yml
DB_NAME=schedule
DB_USER=schedule
DB_PASSWORD=REPLACE_WITH_RANDOM_PASSWORD
DJANGO_SECRET_KEY=REPLACE_WITH_ANOTHER_RANDOM_SECRET
DJANGO_ALLOWED_HOSTS=185.11.247.226
DJANGO_CSRF_TRUSTED_ORIGINS=https://185.11.247.226:8443
DJANGO_SECURE_COOKIES=1
SOLVER_MAX_WORKERS=3
```

Заменить обе секретные строки; существующий `.env` не перезаписывать. Двоеточие
в `COMPOSE_FILE` рассчитано на Linux. После создания файла обычные команды
`docker compose` из каталога проекта учитывают оба YAML. Не передавайте только
`-f docker-compose.yml`: это вернёт самостоятельный Caddy на занятые 80/443.

Перед сборкой проверить `docker compose version` и `docker compose config -q`.
В разрешённой конфигурации Caddy имеет только `127.0.0.1:18000:80`; PostgreSQL
и web не публикуют порты. Не удалять `!override` ради обхода ошибки старого Compose.

Сначала сборка и база, затем одноразовая инициализация:

```bash
docker compose build
docker compose up -d db
docker compose run --rm --no-deps web python manage.py check
docker compose up -d
docker compose exec web python manage.py createsuperuser
```

После каждой ошибки остановиться. Полная инструкция: `docs/deployment/deployment-guide.md`.
Публичный URL — `https://185.11.247.226:8443/`; срок и доверенность существующего
сертификата nginx проверяются отдельно. Успешная проверка синтаксиса не проверяет TLS.

Проверка с сервера (HTTP остаётся только на loopback):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: 185.11.247.226:8443' http://127.0.0.1:18000/login/
curl --cacert /etc/nginx/ssl/equipment-bot/server.crt --resolve 185.11.247.226:8443:127.0.0.1 https://185.11.247.226:8443/login/ -o /dev/null -w '%{http_code}\n'
```

Ожидается 200. Внешнюю доступность 8443 проверить с другого компьютера отдельно.
Вход и загрузку `/static/` проверить в браузере; при ошибке CSRF не отключать защиту,
а проверить порт в origin и передаваемые nginx заголовки Host/X-Forwarded-Host.

## Сервер с 1 ГБ RAM и одним ядром

По выводу заказчика на сервере 961 MiB RAM, 1 GiB swap и одно ядро.
Для первого запуска оставить `WEB_WORKERS=1` и `SOLVER_MAX_WORKERS=1`.
Память для полного расчёта колледжа ещё не измерена: успешный вход в интерфейс
не доказывает, что задача решателя помещается в эту конфигурацию.

Сборку Node/Python выполнять на компьютере сопровождающего с Docker Desktop
(Linux containers), затем передавать готовый образ. Для x86_64 сервера:

```powershell
docker build --platform linux/amd64 -t schedule-service:work .
docker image save -o output/schedule-service-image.tar schedule-service:work
scp output/schedule-service-image.tar root@185.11.247.226:/root/
```

Перед загрузкой проверить архитектуру сервера через `uname -m`; для этого образа
ожидается `x86_64`. Для другой архитектуры образ собирается заново под неё.
На сервере сначала обновить профиль Compose из актуальной ветки `work`, затем:

```bash
docker image load -i /root/schedule-service-image.tar
docker compose pull db caddy
docker compose up -d --no-build db
docker compose run --rm --no-deps --no-build web python manage.py check
docker compose up -d --no-build
```

При ошибке загрузки/миграций не продолжать. Для запуска готового образа не добавлять
`--build`: иначе сборка снова будет выполняться на малом сервере.
Образ не содержит `.env`; секреты остаются на сервере. Git bundle и загруженный
образ должны быть получены из одного состояния кода.
