"""Настройки Django. Всё, что различается между машинами, берётся из переменных окружения.

Переменные (полный список — docs/WEB.md):
    DJANGO_SECRET_KEY      обязательно в продакшене
    DJANGO_DEBUG           1 / 0 (по умолчанию 1 — разработка)
    DJANGO_ALLOWED_HOSTS   через запятую
    DB_ENGINE              sqlite (по умолчанию, для разработки) | postgres (продакшен)
    DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT — для postgres
    SCHEDULE_DATA_DIR      каталог для SQLite и временных файлов (по умолчанию ./data)
    EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_SSL / EMAIL_USE_TLS,
    DEFAULT_FROM_EMAIL     почта для кодов входа; без EMAIL_HOST письма печатаются в консоль

Переменные можно не задавать в системе, а вписать в файл ``.env`` в корне проекта (образец —
``.env.example``): строки ``ИМЯ=значение``. Заданное в системе важнее файла.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    """Простой разбор .env без сторонних библиотек: ИМЯ=значение, # — комментарий."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(BASE_DIR / ".env")
DATA_DIR = Path(os.environ.get("SCHEDULE_DATA_DIR", BASE_DIR / "data"))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on", "да"}


DEBUG = _bool("DJANGO_DEBUG", True)
_DEV_KEY = "dev-insecure-key-do-not-use-in-production"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", _DEV_KEY)
if not DEBUG and SECRET_KEY == _DEV_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY не задан, а DJANGO_DEBUG=0. Задайте секретный ключ (docs/WEB.md).")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "timetable",
    "portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "webapp.urls"
WSGI_APPLICATION = "webapp.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------- база данных
DB_ENGINE = os.environ.get("DB_ENGINE", "sqlite").lower()
if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "schedule"),
            "USER": os.environ.get("DB_USER", "schedule"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
elif DB_ENGINE == "sqlite":
    if "DB_NAME" not in os.environ:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("DB_NAME", str(DATA_DIR / "dev.sqlite3")),
            # сайт и обработчик расчётов пишут в один файл: WAL не блокирует чтение на время
            # записи, timeout — ждать чужую запись, а не падать с «database is locked»
            "OPTIONS": {"timeout": 20, "init_command": "PRAGMA journal_mode=WAL;"},
        }
    }
else:
    raise RuntimeError(f"DB_ENGINE={DB_ENGINE!r}: поддерживаются sqlite и postgres")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"

# ---------------------------------------------------------------- почта (коды входа)
# Django 6.1: почтовые серверы описываются в MAILERS (старые EMAIL_* устарели). Переменные
# окружения и .env по-прежнему называются EMAIL_HOST и т. д. — так их проще заполнять.
EMAIL_HOST_ENV = os.environ.get("EMAIL_HOST", "")
if EMAIL_HOST_ENV:
    _port = int(os.environ.get("EMAIL_PORT", "465"))
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": EMAIL_HOST_ENV,
                "port": _port,
                "username": os.environ.get("EMAIL_HOST_USER", ""),
                "password": os.environ.get("EMAIL_HOST_PASSWORD", ""),
                "use_ssl": _bool("EMAIL_USE_SSL", _port == 465),
                "use_tls": _bool("EMAIL_USE_TLS", _port == 587),
                "timeout": 20,
            },
        }
    }
else:  # разработка: письмо с кодом печатается в окне, где запущен runserver
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", os.environ.get("EMAIL_HOST_USER", "") or "raspisanie@localhost"
)

# Расчёт расписания (manage.py solve_worker): больше этого числа ядер решателю не дадим, чтобы
# сайт на том же сервере не тормозил. По умолчанию — все ядра, кроме одного.
SOLVER_MAX_WORKERS = max(1, int(os.environ.get("SOLVER_MAX_WORKERS", "0")) or (os.cpu_count() or 2) - 1)

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))

# Загрузка xlsx: файл нагрузки колледжа — сотни строк, 10 МБ с запасом.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# Адреса, с которых открывают сайт (нужно для форм за HTTPS-прокси): https://raspisanie.college.ru
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
# За обратным прокси (Caddy в docker-compose): он сообщает, что исходный запрос был по HTTPS.
if _bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

if not DEBUG:
    # Без HTTPS (пилот в локальной сети по http://) задайте DJANGO_SECURE_COOKIES=0 — иначе
    # браузер не сохранит сессию и вход не сработает.
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = _bool("DJANGO_SECURE_COOKIES", True)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler", "stream": sys.stderr}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {"timetable": {"level": "INFO"}, "accounts": {"level": "INFO"}},
}
