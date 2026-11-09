# Размещение на сервере (для администратора)

Полное руководство с ожидаемыми результатами команд, отдельным тестовым стеком,
проверяемыми копиями и передачей сопровождения — [deployment/deployment-guide.md](deployment/deployment-guide.md).
Для восстановления используйте его раздел 19: необходимо останавливать и `web`, и `worker`.

Одна машина, четыре контейнера (`docker compose`):

| Контейнер | Что делает |
|---|---|
| `db` | PostgreSQL 16, данные в томе `pgdata` |
| `web` | приложение: Django, сервер gunicorn; в образе — собранный интерфейс диспетчера (`/app/`) |
| `worker` | обработчик расчётов: забирает запуски «Построить расписание» и считает решателем OR-Tools |
| `caddy` | принимает запросы из сети, сам получает и продлевает сертификат HTTPS, раздаёт статику |

## 1. Сервер

- **Где.** В расписании персональные данные (ФИО, почта преподавателей), поэтому по 152-ФЗ сервер
  должен быть в России. Подойдёт сервер колледжа или облако с площадкой в РФ: Yandex Cloud,
  VK Cloud, Selectel, Timeweb и т. п.
- **Размер.** 4 vCPU (решателю важны ядра и частота), 8 ГБ RAM, диск 20 ГБ. Хватит и 2 vCPU /
  4 ГБ, но расчёт будет идти дольше (см. README, раздел про время расчёта).
- **ОС.** Любой Linux с Docker Engine и плагином compose, например Ubuntu 24.04:
  https://docs.docker.com/engine/install/ubuntu/
- **Сеть.**
  - Для работы по домену с HTTPS: DNS-запись `A` домена (например, `raspisanie.college.ru`)
    указывает на IP сервера, входящие порты 80 и 443 открыты.
  - Для отправки кодов входа нужен исходящий порт 465 (или 587) до почтового сервера.

## 2. Установка

```bash
sudo mkdir -p /opt/raspisanie && cd /opt/raspisanie
# распакуйте сюда schedule_service_vX.Y.Z.zip (папка с docker-compose.yml)
cp .env.example .env
nano .env
```

Что заполнить в `.env`:

| Переменная | Пример | Зачем |
|---|---|---|
| `SITE_ADDRESS` | `raspisanie.college.ru` | адрес сайта; для пилота без домена — `:80` |
| `DJANGO_ALLOWED_HOSTS` | `raspisanie.college.ru` | то же имя (через запятую, если несколько); для пилота — IP сервера |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://raspisanie.college.ru` | адрес с протоколом; для пилота — `http://IP` |
| `DJANGO_SECRET_KEY` | результат команды ниже | секрет подписи сессий |
| `DB_PASSWORD` | длинная случайная строка | пароль PostgreSQL; задаётся один раз, потом не менять |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | `smtp.yandex.ru`, `465`, … | почта для кодов входа (пароль приложения, не от ящика) |
| `DJANGO_SECURE_COOKIES` | `0` | **только** для пилота без HTTPS (`SITE_ADDRESS=:80`) |

Секретный ключ: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.

```bash
docker compose up -d --build        # первый раз — несколько минут (скачиваются образы и пакеты)
docker compose ps                   # все три контейнера должны быть в состоянии running/healthy
docker compose exec web python manage.py createsuperuser   # первый администратор (почта + пароль)
docker compose exec web python manage.py test_email ваш@адрес   # проверка почты
```

Откройте `https://raspisanie.college.ru/admin/`. Номер версии виден в шапке.

Интерфейс диспетчера (React) собирается при `docker compose build` в отдельном этапе образа на
`node:22-slim` (другой образ — переменная `NODE_IMAGE` в `.env`); Node.js на сервере не нужен.

При старте контейнер `web` сам дожидается базы и выполняет миграции, создание ролей и сборку
статики. Отдельно их запускать не нужно.

## 3. Перенос данных с ноутбука

Проще всего загрузить справочники и нагрузку через админку теми же xlsx-файлами («Справочники ⇣» и
«Нагрузка ⇣» на ноутбуке → «⇡» на сервере). Это работает в любом направлении и между разными
версиями.

## 4. Расчёт расписания на сервере

Диспетчер запускает расчёт кнопкой **«Построить расписание»** в строке семестра; считает
контейнер `worker`. Его журнал: `docker compose logs -f worker`.

- **Ядра.** Решателю отдаются все ядра, кроме одного (сайт не тормозит). Изменить —
  `SOLVER_MAX_WORKERS` в `.env`, затем `docker compose up -d`.
- **Перезапуск во время расчёта** (`docker compose restart worker`, обновление, перезагрузка
  сервера) прерывает расчёт: через две минуты он помечается «не построено», его нужно запустить
  заново. Обновлять лучше, когда в «Запусках расчёта» ничего не считается.
- Запуск из консоли тоже работает и попадает в ту же историю:

```bash
docker compose exec worker python manage.py solve_semester <номер семестра> --time 300 --variants 3
```

## 5. Резервные копии

```bash
bash deploy/backup.sh                # копия в backups/schedule_ГГГГ-ММ-ДД_ЧЧММ.dump (хранятся последние 30)
bash deploy/restore.sh backups/<файл> # восстановление (спросит подтверждение, на время остановит сайт)
```

Ежедневная копия в 3:00 — строка в `crontab -e` пользователя, который может запускать docker:

```
0 3 * * * bash /opt/raspisanie/deploy/backup.sh >> /var/log/raspisanie-backup.log 2>&1
```

Папку `backups/` стоит копировать за пределы сервера, например в облачное хранилище или на сетевой
диск колледжа. Копия на том же сервере не спасёт, если пропадёт сам сервер.

## 6. Обновление до новой версии

```bash
cd /opt/raspisanie
bash deploy/backup.sh                # сначала копия
# распакуйте новый архив поверх (файл .env и папка backups/ не затрагиваются)
docker compose up -d --build
```

Миграции применятся сами при старте. Что изменилось — в `CHANGELOG.md`.

## 7. Если что-то не так

| Признак | Где смотреть |
|---|---|
| Сайт не открывается | `docker compose ps`, `docker compose logs caddy` — не получен сертификат? (DNS, порты 80/443) |
| «Bad Request (400)» | имя сайта не в `DJANGO_ALLOWED_HOSTS` |
| «CSRF verification failed» при входе | адрес не в `DJANGO_CSRF_TRUSTED_ORIGINS` (с `https://`) |
| Вход по коду: код не приходит | `docker compose logs web` — там ошибка SMTP; `manage.py test_email`; журнал «Коды входа» в админке |
| Пилот по `http://`: после ввода кода снова просит войти | `DJANGO_SECURE_COOKIES=0` в `.env`, затем `docker compose up -d` |
| «PostgreSQL недоступен» в логах web | `docker compose logs db`, пароль `DB_PASSWORD` менялся после первого запуска? |

Если Docker Hub недоступен из сети сервера, образы можно брать из зеркала. Базовый образ
приложения задаётся переменной `BASE_IMAGE` в `.env`, например `BASE_IMAGE=<зеркало>/python:3.13-slim`.
Образы `postgres:16-alpine` и `caddy:2` настраиваются через зеркало реестра в настройках Docker
(`registry-mirrors`).
