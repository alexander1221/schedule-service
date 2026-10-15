# Воспроизводимость

Цель: через 5–10 лет поднять то же окружение и получить то же расписание из того же снимка задачи.

## Зафиксированные версии (2026-10-13)

| Компонент | Версия | Почему именно она |
|---|---|---|
| ОС разработки | Linux x86_64 (контейнер) | — |
| Python | 3.13.16 (допустимо 3.12–3.14) | 3.12 — минимум Django 6.x; 3.14 — последний с колёсами ortools 9.15.6755 |
| ortools | 9.15.6755 | последняя на PyPI на дату сборки |
| protobuf | 6.33.6 | ortools 9.15 требует `>=6.33.1,<6.34` |
| numpy | 2.5.4 | транзитивно от ortools (`>=2.0.2`) |
| pandas | 3.0.6 | транзитивно от ortools (`>=2.0.0`), ядром не используется |
| absl-py | 2.5.1 | транзитивно от ortools |
| Django | 6.1.2 | веб-часть; задаёт минимум Python 3.12 |
| psycopg (+binary) | 3.3.6 | драйвер PostgreSQL, рекомендованный Django 6.1 |
| openpyxl | 3.1.5 | импорт/экспорт xlsx |
| asgiref / sqlparse | 3.12.1 / 0.6.0 | транзитивно от Django |
| PostgreSQL (сервер) | 15+ (проверено на 16.15) | минимум Django 6.1 |
| pytest / pytest-django / ruff / mypy | 9.1.1 / 4.14.0 / 0.17.0 / 2.4.0 | только разработка |
| uv | 0.11.32 | генерация lock-файлов |

Полные списки с SHA256 — `requirements-lock.txt`, `requirements-dev-lock.txt`, `uv.lock`.
Фактическое окружение после установки — `requirements-frozen.txt` (пишет `setup.sh`).

## Что делает расписание воспроизводимым

* **Снимок задачи.** `problem.json` (формат `schema_version = 1`) — то же, что пойдёт в JSONB
  варианта. В `meta` расписания — seed, число потоков, лимит времени, режим и веса.
* **Детерминизм CP-SAT.** При `num_workers > 1` поиск недетерминирован по времени: тот же seed
  на другом железе может дать другой (но не хуже по смыслу) вариант. Для побитового повтора
  запускайте с `--workers 1` и тем же `--seed` — медленнее, зато детерминированно для одной версии
  ortools. Независимо от этого валидатор по снимку задачи всегда пересчитает те же штрафы.
* **Синтетика** детерминирована по `--seed` (`random.Random(seed)`).

## Четыре уровня установки

### Уровень 1. Обычный путь
```bash
bash setup.sh            # Windows: windows_first_run.bat
```

### Уровень 2. Строго по хэшам
```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt
```
Если колесо на зеркале пересобрано и хэш не совпал — это сигнал, а не помеха: проверьте зеркало.
Перегенерация lock (осознанно, с записью в CHANGELOG):
```bash
uv pip compile pyproject.toml --generate-hashes --python-version 3.13 --universal -o requirements-lock.txt
uv pip compile pyproject.toml --group dev --generate-hashes --python-version 3.13 --universal -o requirements-dev-lock.txt
uv lock
```

### Уровень 3. Офлайн из локальных колёс
На машине с интернетом той же ОС/архитектуры и версии Python:
```bash
python3.13 -m pip download --require-hashes -r requirements-lock.txt -d wheels
```
На целевой машине: `bash setup.sh --wheels wheels` (Windows: `setup.ps1 -Wheels .\wheels`).

### Уровень 4. Полный архив
```bash
bash setup.sh --offline-bundle schedule_offline.tar.xz      # исходники + колёса, ~55 МБ для Linux x86_64
```
Распаковать на целевой машине и выполнить `bash setup.sh --wheels wheels`.

## Если внешние источники пропадут

| Источник | Риск | Что делать |
|---|---|---|
| PyPI | недоступен из РФ / удалён пакет | внутреннее зеркало (devpi, Nexus, Artifactory) + `PIP_INDEX_URL`; уровни 3–4 |
| GitHub google/or-tools | закрытие доступа, удаление релиза | держать форк исходников OR-Tools (требование ТЗ §11) и архив колёс; лицензия Apache 2.0 позволяет хранить и пересобирать |
| Docker Hub (этап 8) | недоступен | собственный реестр образов, базовый образ хранить там же |

## Чек-лист архивации (раз в семестр, после утверждения расписания)

- [ ] дамп БД (`pg_dump -Fc schedule > schedule_YYYYMMDD.dump`) — справочники, нагрузка, все варианты
- [ ] `problem.json` — снимок входных данных утверждённого варианта (он же лежит в БД, в варианте)
- [ ] утверждённое расписание (`schedule_*.json`) и отчёт валидатора (`validate ... --json`)
- [ ] `requirements-frozen.txt` с сервера
- [ ] архив колёс (уровень 4) для текущей версии Python сервера
- [ ] git-тег версии кода

## Что вероятно сломается через 3–5 лет

* **OR-Tools API Python.** Между 9.x уже менялись имена (`NewBoolVar` → `new_bool_var`), а
  `linear_sum_assignment` решает только квадратную задачу и роняет процесс при `right_mate()` после
  неуспешного решения (обойдено в `solver/rooms.py`).
  При обновлении ortools прогоните `pytest --slow`.
* **protobuf.** Узкий диапазон у ortools; любое обновление соседнего пакета может его сдвинуть.
* **Python.** 3.13 перестанет получать исправления безопасности в 2029 г.; колёс ortools 9.15 для
  будущих версий Python не будет — придётся обновлять ortools вместе с Python.
* **numpy 2.x / pandas 3.x** — транзитивные; ядро их не использует напрямую, риск низкий.
* **Django** выпускает LTS раз в два года; 6.1 не LTS — до конца её поддержки (≈ весна 2028)
  перейти на следующую LTS. Миграции БД переносятся вместе с кодом.

## Команды «для будущего вас»

```bash
python verify_install.py                       # всё ли на месте
python -c "import ortools; print(ortools.__version__)"
python schedule_cli.py validate problem.json schedule.json   # штрафы пересчитываются из снимка
pytest --slow                                  # полный прогон, включая решатель на синтетике
python schedule_cli.py bench --seeds 2 --time 60   # не деградировала ли производительность
```
