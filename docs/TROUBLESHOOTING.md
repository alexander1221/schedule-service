# Типовые проблемы

## 1. Установка

**`No suitable Python` / `Python не найден`**
Причина: нет Python 3.12–3.14. Решение: Ubuntu 24.04+ — `sudo apt install python3.13 python3.13-venv`;
macOS — `brew install python@3.13`; Windows — `winget install -e --id Python.Python.3.13`.
Затем `bash setup.sh --python python3.13` / `setup.ps1 -PythonBin "py -3.13"`.

**`Нет модулей venv/ensurepip`**
Debian/Ubuntu ставит venv отдельным пакетом: `sudo apt install python3.13-venv`.

**`ERROR: Could not find a version that satisfies the requirement ortools==9.15.6755`**
1. Python вне 3.9–3.14 или 32-битный Python — колёс нет. Проверка: `python -c "import struct; print(struct.calcsize('P')*8)"` → должно быть 64.
2. Нет доступа к PyPI. Задайте зеркало: `export PIP_INDEX_URL=https://<зеркало>/simple` (Windows:
   `$env:PIP_INDEX_URL=...`) или установите офлайн из колёс: `bash setup.sh --wheels wheels`.

**`THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE`**
Зеркало отдало другой файл (пересобранное колесо) или скачивание оборвалось. Очистите кеш
`python -m pip cache purge` и повторите. Если зеркало действительно пересобирает пакеты —
сгенерируйте lock заново с его колёсами (см. REPRODUCIBILITY.md, «уровень 2»).

**`.venv создано для Python X, а выбран Y`**
Setup спросит, удалить ли `.venv`. Ответьте `y` или удалите вручную.

## 2. Импорт

**`ImportError: ... protobuf` / `Descriptors cannot be created directly`**
protobuf «уехал» за пределы `>=6.33.1,<6.34`, которых требует ortools 9.15. Переустановите из lock:
`.venv/bin/python -m pip install --require-hashes -r requirements-lock.txt`.

**Windows: `ImportError: DLL load failed while importing ...` для ortools**
Не установлен Microsoft Visual C++ Redistributable 2015–2022:
`winget install -e --id Microsoft.VCRedist.2015+.x64`.

**`ModuleNotFoundError: No module named 'scheduler'`**
Скрипты запускаются из корня проекта: `python schedule_cli.py ...`. Для своих скриптов добавьте
корень в `PYTHONPATH` или `pip install -e .`.

## 3. Веб-часть (Django)

**`RuntimeError: DJANGO_SECRET_KEY не задан, а DJANGO_DEBUG=0`**
В продакшене задайте ключ: `export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"`.

**`OperationalError: unable to open database file`** (SQLite)
Нет прав на каталог `data/` или задан `DB_NAME` с несуществующим путём. Проверьте `SCHEDULE_DATA_DIR`.

**`connection to server ... failed` / `password authentication failed`** (PostgreSQL)
Проверьте `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`; что сервер слушает этот адрес; что
версия PostgreSQL ≥ 15 (`psql -c "select version()"`) — младшие Django 6.1 не поддерживает.

**verify_install: «не применено миграций — N»**
`python manage.py migrate` (с теми же переменными `DB_*`, что и у сервиса).

**`createsuperuser` спрашивает почту, а не логин** — так задумано: вход по корпоративной почте.

**Импорт xlsx: «нет листа «Нагрузка»» / «нет обязательных колонок»**
Начните с выгрузки (`Нагрузка ⇣xlsx`) — у неё правильные листы и заголовки. Порядок колонок
можно менять, названия — нет (кроме «Пар за цикл» ⇄ «Пар за 2 недели»).

**Импорт xlsx: ошибки в строках, но ничего не записано** — так и задумано («всё или ничего»).
Исправьте все отмеченные строки и загрузите файл снова.

**Импорт xlsx: «несколько преподавателей с таким ФИО»** — укажите почту вместо ФИО.

## 4. Запуск решателя

**`Расписание не построено: precheck_failed`**
Быстрые проверки нашли противоречие — сообщение называет причину («у Петрова 16 пар за цикл,
доступно 12 слотов»). Исправьте данные: снимите часть запретов «−2», уменьшите нагрузку,
добавьте аудитории.

**`Расписание не построено: infeasible`** — далее список «Эти требования нельзя выполнить одновременно».
Это минимальный набор противоречащих требований: уберите или ослабьте любое из них.
Чаще всего — закрепления, жёсткие лимиты в день и синхронные блоки.

**`Расписание не построено: unknown` — «за отведённое время не найдено ни одного решения»**
Увеличьте `--time`; проверьте, что `--workers` не 1. На очень тесных задачах сделайте
жёсткие лимиты мягкими.

**`AssertionError: Валидатор нашёл жёсткие нарушения в решении CP-SAT`**
Это ошибка в коде (модель решателя и валидатор разошлись), а не в данных. Сохраните задачу
(`problem.json`) и параметры запуска и сообщите разработчикам.

**`RoomAssignmentError: Не удалось распределить аудитории в слоте ...`**
Обычно из-за закреплённых аудиторий, которые конфликтуют с другими закреплениями того же слота.
Проверьте закрепления этого слота.

## 5. Производительность

**Долго / плохое качество**
CP-SAT масштабируется по ядрам: на 2 ядрах в разы медленнее, чем на 8. Сравните с `bench`:
`python schedule_cli.py bench --seeds 2 --time 60`. Для итогового расписания давайте 3–5 минут
на вариант.

**Не хватает памяти**
Модель на синтетике §1 занимает ~0,5–1 ГБ на поток поиска. При 16 ГБ и 8 потоках запас есть; на
слабой машине уменьшите `--workers`.

## 6. Вывод в консоли

**Windows: `UnicodeEncodeError` или «кракозябры» при выводе в файл** (`python verify_install.py > отчёт.txt`)
С 0.13.1 программы проекта сами пишут в UTF-8 — ошибки больше нет; файл открывайте как UTF-8 (Блокнот
понимает сам). Если в PowerShell русский текст из трубы выглядит «кракозябрами», выполните
`[Console]::OutputEncoding = [Text.Encoding]::UTF8` перед командой.

**`BrokenPipeError` при `| head`** — безопасно, вывод просто обрезан (в 0.1.0 подавлено).

## Что-то ещё непонятно

1. `python verify_install.py` — что именно не проходит?
2. Запустите команду с `--debug`: появится трассировка и лог поиска CP-SAT.
3. `python schedule_cli.py precheck data/problem.json` — нет ли противоречий в данных.
4. Приложите к обращению: вывод пунктов 1–3, `requirements-frozen.txt`, ОС и версию Python.
