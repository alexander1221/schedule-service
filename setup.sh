#!/usr/bin/env bash
# setup.sh — установка ядра сервиса расписания (Linux / macOS / WSL2).
#
# Шаги:
#   1. Системные проверки (Python 3.12+, ядра CPU, память)
#   2. Создание виртуального окружения .venv
#   3. Зависимости: OR-Tools, Django, psycopg, openpyxl (из lock-файла с SHA256)
#   4. Инструменты разработки (pytest, ruff, mypy) — только с --dev
#   5. База данных (миграции, роли) и пример данных: синтетика в JSON и в БД
#   6. Проверка установки (verify_install.py)
#
# Параметры:
#   --python <путь>          интерпретатор (по умолчанию python3.13, затем python3)
#   --dev                    поставить также pytest/ruff/mypy
#   --no-download            не создавать пример данных на шаге 5 (миграции выполняются всегда)
#   --skip-venv              ставить в уже активное окружение
#   --wheels <каталог>       офлайн-установка из локальных колёс (без PyPI)
#   --offline-bundle <файл>  собрать архив для установки без интернета и выйти
#   --debug                  подробный вывод (set -x, pip -v)
#
# set -e намеренно НЕ используется: каждая важная команда проверяется явно,
# чтобы пользователь видел понятную причину, а не молчаливый выход.
set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

PYTHON=""
DEV=0
NO_DOWNLOAD=0
SKIP_VENV=0
WHEELS=""
BUNDLE=""
DEBUG=0
TOTAL=6

if [[ -t 1 ]]; then
    C_B=$'\033[36m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else
    C_B=""; C_G=""; C_Y=""; C_R=""; C_0=""
fi
log()  { echo "${C_B}[setup]${C_0} $*"; }
ok()   { echo "${C_G}[setup] ✓${C_0} $*"; }
warn() { echo "${C_Y}[setup] !${C_0} $*"; }
err()  { echo "${C_R}[setup] ✗${C_0} $*" >&2; }
die() {
    err "$*"
    err "Повторите с --debug для подробного вывода; типовые проблемы — docs/TROUBLESHOOTING.md"
    exit 1
}
confirm() {
    local ans
    read -r -p "$1 [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]]
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python) PYTHON="${2:-}"; shift 2 ;;
        --dev) DEV=1; shift ;;
        --no-download) NO_DOWNLOAD=1; shift ;;
        --skip-venv) SKIP_VENV=1; shift ;;
        --wheels) WHEELS="${2:-}"; shift 2 ;;
        --offline-bundle) BUNDLE="${2:-}"; shift 2 ;;
        --debug) DEBUG=1; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) die "Неизвестный параметр: $1 (см. --help)" ;;
    esac
done
[[ $DEBUG -eq 1 ]] && set -x
PIP_Q="-q"; [[ $DEBUG -eq 1 ]] && PIP_Q="-v"

# ------------------------------------------------------------------ шаг 1
log "Шаг 1/$TOTAL: системные проверки"
if [[ -z "$PYTHON" ]]; then
    for cand in python3.13 python3.12 python3.14 python3; do
        if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
    done
fi
[[ -n "$PYTHON" ]] || die "Python не найден. Установите Python 3.13: sudo apt install python3.13 python3.13-venv (Ubuntu 24.04+) или brew install python@3.13"
command -v "$PYTHON" >/dev/null 2>&1 || [[ -x "$PYTHON" ]] || die "Интерпретатор не найден: $PYTHON"

PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
    || die "Не удалось запустить $PYTHON"
PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER##*.}"
log "Python: $PYTHON ($PYVER)"
# 3.12 — минимум из-за Django 6.x (этап 4 проекта); 3.14 — последняя версия,
# для которой у ortools 9.15.6755 есть колёса на PyPI.
if [[ "$PYMAJ" -ne 3 || "$PYMIN" -lt 12 || "$PYMIN" -gt 14 ]]; then
    warn "Нужен Python 3.12–3.14 (рекомендуется 3.13), найден $PYVER."
    warn "  Ubuntu: sudo apt install python3.13 python3.13-venv;  macOS: brew install python@3.13"
    confirm "Продолжить всё равно?" || die "Остановлено: неподходящая версия Python"
fi
if [[ -n "${CONDA_DEFAULT_ENV:-}" && "${CONDA_DEFAULT_ENV}" != "base" ]] || [[ -n "${CONDA_PREFIX:-}" ]]; then
    warn "Активна Conda (${CONDA_DEFAULT_ENV:-$CONDA_PREFIX}). venv поверх Conda иногда тянет её библиотеки."
    confirm "Продолжить? (рекомендуется: conda deactivate и повторить)" || die "Остановлено из-за активной Conda"
fi
"$PYTHON" -c "import venv, ensurepip" 2>/dev/null \
    || die "Нет модулей venv/ensurepip. Ubuntu/Debian: sudo apt install python${PYVER}-venv"
NCPU="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
log "Ядер CPU: $NCPU (CP-SAT использует все; рекомендуется ≥ 8)"
[[ "$NCPU" -lt 4 ]] && warn "Мало ядер: решатель будет заметно медленнее ориентиров из README"
ok "Шаг 1/$TOTAL пройден"

# ------------------------------------------------------------------ офлайн-архив
if [[ -n "$BUNDLE" ]]; then
    log "Сборка офлайн-архива: колёса для текущей платформы + исходники проекта"
    TMPD="$(mktemp -d)" || die "mktemp не сработал"
    "$PYTHON" -m pip download $PIP_Q --require-hashes -r requirements-lock.txt -d "$TMPD/wheels" \
        || die "pip download не смог скачать колёса (проверьте сеть или PIP_INDEX_URL)"
    if [[ $DEV -eq 1 ]]; then
        "$PYTHON" -m pip download $PIP_Q --require-hashes -r requirements-dev-lock.txt -d "$TMPD/wheels" \
            || die "pip download (dev) не сработал"
    fi
    NWHL="$(find "$TMPD/wheels" -name '*.whl' | wc -l)"
    [[ "$NWHL" -ge 5 ]] || die "Скачано подозрительно мало колёс: $NWHL"
    mkdir -p "$TMPD/schedule_service" || die "mkdir"
    tar --exclude=.venv --exclude=__pycache__ --exclude=output --exclude=data --exclude=.git \
        --exclude=wheels --exclude=requirements-frozen.txt --exclude="*.tar.xz" --exclude=staticfiles -cf - . \
        | tar -xf - -C "$TMPD/schedule_service" || die "Не удалось скопировать исходники"
    mv "$TMPD/wheels" "$TMPD/schedule_service/wheels" || die "mv"
    tar -C "$TMPD" -cJf "$BUNDLE" schedule_service || die "Не удалось упаковать $BUNDLE"
    SZ="$(stat -c%s "$BUNDLE" 2>/dev/null || stat -f%z "$BUNDLE")"
    [[ "$SZ" -gt 1048576 ]] || die "Архив $BUNDLE подозрительно мал ($SZ байт)"
    rm -rf "$TMPD"
    ok "Офлайн-архив: $BUNDLE ($((SZ / 1048576)) МБ, колёс: $NWHL). На целевой машине: bash setup.sh --wheels wheels"
    exit 0
fi

# ------------------------------------------------------------------ шаг 2
log "Шаг 2/$TOTAL: виртуальное окружение"
if [[ $SKIP_VENV -eq 1 ]]; then
    PY="$PYTHON"
    warn "--skip-venv: пакеты будут поставлены в окружение $("$PY" -c 'import sys; print(sys.prefix)')"
else
    if [[ -d .venv ]]; then
        VENV_VER="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "битое")"
        if [[ "$VENV_VER" != "$PYVER" ]]; then
            warn ".venv создано для Python $VENV_VER, а выбран $PYVER"
            if confirm "Удалить .venv и создать заново?"; then
                rm -rf .venv || die "Не удалось удалить .venv"
            else
                die "Остановлено: .venv от другой версии Python"
            fi
        else
            log ".venv уже есть (Python $VENV_VER) — используем его"
        fi
    fi
    if [[ ! -d .venv ]]; then
        "$PYTHON" -m venv .venv || die "Не удалось создать .venv"
    fi
    PY="$ROOT/.venv/bin/python"
    [[ -x "$PY" ]] || die "В .venv нет bin/python — окружение повреждено, удалите .venv"
fi
"$PY" -m pip install $PIP_Q --upgrade "pip>=24" || warn "Не удалось обновить pip — продолжаем со встроенным"
ok "Шаг 2/$TOTAL пройден"

# ------------------------------------------------------------------ шаги 3–4
PIP_SRC=()
if [[ -n "$WHEELS" ]]; then
    [[ -d "$WHEELS" ]] || die "Каталог колёс не найден: $WHEELS"
    PIP_SRC=(--no-index --find-links "$WHEELS")
    log "Офлайн-режим: пакеты берутся только из $WHEELS"
fi

log "Шаг 3/$TOTAL: зависимости — OR-Tools, Django, psycopg, openpyxl (requirements-lock.txt, проверка SHA256)"
"$PY" -m pip install $PIP_Q ${PIP_SRC[@]+"${PIP_SRC[@]}"} --require-hashes -r requirements-lock.txt \
    || die "Установка зависимостей не удалась. Если нет доступа к PyPI — задайте PIP_INDEX_URL на внутреннее зеркало или используйте --wheels"
ORV="$("$PY" -c 'import ortools; print(ortools.__version__)' 2>/dev/null || echo "нет")"
[[ "$ORV" == "9.15.6755" ]] || die "После установки ortools версии '$ORV', ожидалась 9.15.6755"
ok "Шаг 3/$TOTAL пройден (ortools $ORV)"

log "Шаг 4/$TOTAL: инструменты разработки"
if [[ $DEV -eq 1 ]]; then
    "$PY" -m pip install $PIP_Q ${PIP_SRC[@]+"${PIP_SRC[@]}"} --require-hashes -r requirements-dev-lock.txt \
        || die "Не удалось поставить pytest/ruff/mypy"
    ok "Шаг 4/$TOTAL пройден (pytest, ruff, mypy)"
else
    log "  пропущен (нужен только для разработки: bash setup.sh --dev)"
fi

# ------------------------------------------------------------------ шаг 5
log "Шаг 5/$TOTAL: база данных и пример данных (внешних загрузок у проекта нет)"
log "  миграции БД (${DB_ENGINE:-sqlite}; переменные — docs/WEB.md)"
"$PY" manage.py migrate --noinput -v 0 || die "Миграции не применились. Для PostgreSQL проверьте DB_HOST/DB_USER/DB_PASSWORD"
"$PY" manage.py init_roles >/dev/null || die "Не удалось создать роли"
if [[ $NO_DOWNLOAD -eq 1 ]]; then
    log "  пример данных пропущен (--no-download)"
else
    if [[ -f data/problem.json ]]; then
        log "  data/problem.json уже есть — не перезаписываем"
    else
        "$PY" schedule_cli.py generate -o data/problem.json --planted data/planted.json \
            || die "Не удалось сгенерировать пример данных"
        SZ="$(stat -c%s data/problem.json 2>/dev/null || stat -f%z data/problem.json)"
        [[ "$SZ" -gt 1024 ]] || die "data/problem.json подозрительно мал ($SZ байт)"
    fi
    "$PY" manage.py seed_synthetic --if-empty || die "Не удалось загрузить синтетический семестр в БД"
fi
ok "Шаг 5/$TOTAL пройден"

# ------------------------------------------------------------------ шаг 6
log "Шаг 6/$TOTAL: проверка установки"
"$PY" verify_install.py || die "verify_install.py нашёл проблемы (см. вывод выше)"
"$PY" -m pip freeze > requirements-frozen.txt 2>/dev/null \
    && log "Фактический состав окружения сохранён в requirements-frozen.txt"
ok "Шаг 6/$TOTAL пройден"

echo
ok "Готово. Дальше:"
echo "    source .venv/bin/activate"
echo "    python manage.py createsuperuser          # вход в админку по почте"
echo "    python manage.py runserver                # http://127.0.0.1:8000/admin/"
echo "    python manage.py solve_semester 1 --time 120 --variants 3"
