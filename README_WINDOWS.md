# Установка на Windows

## TL;DR

1. Поставьте Python 3.13: `winget install -e --id Python.Python.3.13`
2. Поставьте Visual C++ Redistributable (нужен ortools): `winget install -e --id Microsoft.VCRedist.2015+.x64`
3. Распакуйте проект в путь **без кириллицы и пробелов**, например `C:\work\schedule_service`
4. Двойной щелчок по `windows_first_run.bat` — он найдёт Python и запустит `setup.ps1`
5. В конце должно быть `Итого: 10/10 проверок пройдено`
6. Запуск: `.venv\Scripts\activate`, затем `python schedule_cli.py solve data\problem.json -o output\schedule.json --time 120`

## Подробно

### 1. Python

```powershell
winget install -e --id Python.Python.3.13
py -0p          # список установленных версий; должна быть 3.13 (или 3.12 / 3.14)
```

Используется **py launcher** (`py -3.13`), а не `python` из PATH: так не важно, какой Python
стоит «по умолчанию», и не мешает заглушка Microsoft Store.

### 2. Установка

Двойной щелчок по `windows_first_run.bat` или вручную:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1 -PythonBin "py -3.13"
```

Параметры `setup.ps1`: `-Dev` (pytest, ruff, mypy), `-NoDownload` (без примера данных),
`-SkipVenv`, `-Wheels <папка>` (офлайн), `-OfflineBundle <папка>` (скачать колёса и выйти),
`-DebugMode`. Справка: `Get-Help .\setup.ps1 -Full`.

### 3. Работа

```powershell
.venv\Scripts\activate
python manage.py createsuperuser      # вход по почте
python manage.py runserver            # http://127.0.0.1:8000/admin/
python manage.py solve_semester 1 --time 120 --variants 3
python schedule_cli.py precheck data\problem.json
python schedule_cli.py solve data\problem.json -o output\schedule.json --time 120 --variants 3
python schedule_cli.py show data\problem.json output\schedule_v1.json ИС-11 --week odd
```

## Особенности Windows

**ExecutionPolicy.** По умолчанию PowerShell не запускает скрипты. `windows_first_run.bat`
обходит это только для одного запуска (`-ExecutionPolicy Bypass`), политику системы не меняет.
Если запускаете `setup.ps1` сами — тоже через `powershell -ExecutionPolicy Bypass -File ...`.

**Почему `setup.ps1` на английском.** Windows PowerShell 5.1 читает `.ps1` в системной кодировке
(Windows-1251 в русской локали), и кириллица ломает разбор скрипта. Поэтому скрипт — чистый ASCII,
а документация — на русском.

**Кириллица в путях.** Сам проект с ней работает, но часть инструментов (старые сборки pip,
антивирусы при распаковке) — нет. Держите проект в пути из латиницы без пробелов.

**Кириллица в консоли.** Python печатает русский текст корректно в консоли Windows 10/11. При
перенаправлении вывода в файл задайте `$env:PYTHONIOENCODING="utf-8"` (setup делает это сам).

**PowerShell 5.1 и 7+.** Скрипт рассчитан на 5.1 (есть в любой Windows 10/11) и работает в 7+.
Отличие, которое может встретиться: в 5.1 `Out-File` по умолчанию пишет UTF-16 — поэтому
`requirements-frozen.txt` пишется явно в ASCII.

**Long Path Support** не нужен: самые длинные пути проекта короче 80 символов.

**Антивирус** иногда замедляет первую установку numpy/ortools (проверка .pyd). Это не ошибка —
подождите.

**Нет доступа к PyPI.** Задайте внутреннее зеркало перед запуском:
`$env:PIP_INDEX_URL="https://<зеркало>/simple"`, либо установите из заранее скачанных колёс:
`setup.ps1 -Wheels .\wheels` (колёса готовятся на машине с интернетом командой
`setup.ps1 -OfflineBundle .\wheels`; Python и платформа должны совпадать).

Проблемы — [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
