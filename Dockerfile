# Образ сервиса расписания: Django + решатель OR-Tools, сервер gunicorn.
# Сборка: docker compose build   (см. docs/DEPLOY.md)
ARG BASE_IMAGE=python:3.13-slim
ARG NODE_IMAGE=node:22-slim

# --- 1. Интерфейс диспетчера (React): собирается здесь, в образ попадает только готовая статика.
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2. Приложение
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    STATIC_ROOT=/srv/static \
    SCHEDULE_DATA_DIR=/srv/data

WORKDIR /app
RUN python3 -m venv /opt/venv

# Зависимости — отдельным слоем: при правке кода не переустанавливаются.
# --require-hashes: ставится ровно то, что в lock-файле, с проверкой SHA256.
COPY requirements-lock.txt ./
RUN pip install --require-hashes -r requirements-lock.txt

COPY . .
COPY --from=frontend /src/frontend/dist /app/frontend/dist
RUN useradd --system --home /app app \
 && mkdir -p /srv/static /srv/data \
 && chown -R app /srv/static /srv/data \
 && chmod +x deploy/entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["deploy/entrypoint.sh"]
# Решатель считает отдельный сервис worker (manage.py solve_worker), не запрос к сайту,
# поэтому таймаут gunicorn обычный.
CMD ["gunicorn", "webapp.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--access-logfile", "-", "--no-control-socket"]
