#!/usr/bin/env python3
"""verify_install.py — кросс-платформенная проверка установки (ядро + веб).

    python verify_install.py           # все проверки, включая smoke-test решателя (~10–20 с)
    python verify_install.py --quick   # без smoke-test'а

Код выхода 0 — всё в порядке, 1 — есть провалы (см. docs/TROUBLESHOOTING.md).
GPU проекту не нужен: CP-SAT работает на CPU, поэтому проверяются ядра и память.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

EXPECTED = {"ortools": "9.15.6755", "django": "6.1.2", "psycopg": "3.3.6", "openpyxl": "3.1.5"}
PY_MIN, PY_MAX = (3, 12), (3, 14)

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if os.name == "nt" and _COLOR:
    os.system("")  # включает ANSI-последовательности в консоли Windows 10+
GREEN, RED, YELLOW, RESET = ("\033[32m", "\033[31m", "\033[33m", "\033[0m") if _COLOR else ("", "", "", "")
try:
    "✓✗".encode(sys.stdout.encoding or "ascii")
    MARK_OK, MARK_FAIL = "✓", "✗"
except (UnicodeEncodeError, LookupError):  # консоль cp1251/cp866
    MARK_OK, MARK_FAIL = "OK", "FAIL"


class Check:
    def __init__(self, name: str, fn: Callable[[], tuple[bool, str]], heavy: bool = False):
        self.name, self.fn, self.heavy = name, fn, heavy

    def run(self) -> bool:
        try:
            ok, msg = self.fn()
        except Exception as e:  # проверка не должна падать целиком
            ok, msg = False, f"{type(e).__name__}: {str(e)[:300]}"
        mark = f"{GREEN}{MARK_OK}{RESET}" if ok else f"{RED}{MARK_FAIL}{RESET}"
        print(f"  {mark} {self.name}: {msg}")
        return ok


def check_python() -> tuple[bool, str]:
    v = sys.version_info[:2]
    ok = PY_MIN <= v <= PY_MAX
    hint = "" if ok else " — нужен 3.12–3.14 (минимум задаёт Django 6.x этапа 4, максимум — колёса ortools)"
    return ok, f"{platform.python_version()} ({platform.system()} {platform.machine()}){hint}"


def check_package(name: str) -> Callable[[], tuple[bool, str]]:
    def fn() -> tuple[bool, str]:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "?")
        want = EXPECTED.get(name)
        if want and ver != want:
            return False, f"версия {ver}, ожидалась {want} (переустановите из requirements-lock.txt)"
        return True, f"версия {ver}"

    return fn


def check_protobuf() -> tuple[bool, str]:
    import google.protobuf

    v = google.protobuf.__version__
    major_minor = tuple(int(x) for x in v.split(".")[:2])
    ok = (6, 33) <= major_minor < (6, 34)  # требование ortools 9.15: protobuf>=6.33.1,<6.34
    return ok, f"protobuf {v}" + ("" if ok else " — ortools 9.15 требует >=6.33.1,<6.34")


def check_cpu() -> tuple[bool, str]:
    n = os.cpu_count() or 1
    note = "" if n >= 8 else " (меньше 8: решатель будет медленнее ориентиров)"
    return n >= 1, f"{n} логических ядер{note}"


def check_memory() -> tuple[bool, str]:
    total = None
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        total = None
    if total is None and os.name == "nt":
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
        total = st.ullTotalPhys
    if total is None:
        return True, "не удалось определить (не критично)"
    gb = total / 2**30
    return gb >= 2, f"{gb:.1f} ГБ" + ("" if gb >= 4 else " (мало: рекомендуется ≥ 4 ГБ, на сервере 16 ГБ)")


def check_project_import() -> tuple[bool, str]:
    import scheduler
    from scheduler.solver import CpSatSolver  # noqa: F401
    from scheduler.validator import validate  # noqa: F401

    return True, f"scheduler {scheduler.__version__}"


def check_django() -> tuple[bool, str]:
    """Системные проверки Django и подключение к БД (sqlite по умолчанию, postgres — DB_ENGINE)."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
    import io

    import django
    from django.core.management import call_command

    django.setup()
    from django.conf import settings
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    out = io.StringIO()
    call_command("check", stdout=out, stderr=out)
    connection.ensure_connection()
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    engine = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
    if pending:
        return False, f"БД {engine}: не применено миграций — {len(pending)} (python manage.py migrate)"
    return True, f"manage.py check без замечаний; БД {engine} подключена, миграции применены"


def check_sample_data() -> tuple[bool, str]:
    p = ROOT / "data" / "problem.json"
    if not p.exists():
        return True, "data/problem.json нет (создаётся: python schedule_cli.py generate)"
    size = p.stat().st_size
    if size < 1024:
        return False, f"data/problem.json подозрительно мал ({size} байт) — удалите и сгенерируйте заново"
    from scheduler.serialization import load_json, problem_from_dict

    prob = problem_from_dict(load_json(p))
    return True, f"data/problem.json: {len(prob.groups)} групп, {len(prob.items)} элементов нагрузки"


def check_assignment_solver() -> tuple[bool, str]:
    from ortools.graph.python import linear_sum_assignment as lsa

    a = lsa.SimpleLinearSumAssignment()
    for i, j, c in [(0, 0, 5), (0, 1, 1), (1, 0, 1), (1, 1, 5)]:
        a.add_arc_with_cost(i, j, c)
    ok = a.solve() == a.OPTIMAL and a.optimal_cost() == 2
    return ok, "задача о назначениях решается" if ok else "неожиданный результат linear_sum_assignment"


def check_smoke_solve() -> tuple[bool, str]:
    """Маленькая синтетика целиком: генератор → CP-SAT → аудитории → валидатор."""
    from scheduler.generator import GenParams, generate
    from scheduler.solver import CpSatSolver, SolveParams
    from scheduler.validator import validate

    g = generate(GenParams(seed=1, n_groups=6, n_teachers=12, courses=2, n_blocks=1, n_merges=0))
    t0 = time.monotonic()
    res = CpSatSolver().solve(g.problem, SolveParams(time_limit_s=10, workers=os.cpu_count() or 2))
    dt = time.monotonic() - t0
    if not res.variants:
        return False, f"решение не найдено: {res.status.value}; {' '.join(res.diagnostics)[:200]}"
    rep = validate(g.problem, res.variants[0].schedule)
    if not rep.ok:
        return False, f"валидатор нашёл {len(rep.hard)} жёстких нарушений — это ошибка решателя"
    return True, f"6 групп, {len(g.problem.items)} элементов: решено за {dt:.1f} с, 0 нарушений, штраф {rep.total}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="пропустить smoke-test решателя")
    args = ap.parse_args()
    checks = [
        Check("Python", check_python),
        Check("OR-Tools", check_package("ortools")),
        Check("protobuf", check_protobuf),
        Check("numpy", check_package("numpy")),
        Check("Django", check_package("django")),
        Check("psycopg (PostgreSQL)", check_package("psycopg")),
        Check("openpyxl (xlsx)", check_package("openpyxl")),
        Check("Django: настройки и БД", check_django),
        Check("Процессор", check_cpu),
        Check("Память", check_memory),
        Check("Модули проекта", check_project_import),
        Check("Задача о назначениях", check_assignment_solver),
        Check("Пример данных", check_sample_data),
        Check("Smoke-test решателя", check_smoke_solve, heavy=True),
    ]
    print("Проверка установки сервиса расписания")
    results = []
    for c in checks:
        if c.heavy and args.quick:
            print(f"  {YELLOW}-{RESET} {c.name}: пропущено (--quick)")
            continue
        results.append(c.run())
    n_ok, n = sum(results), len(results)
    color = GREEN if n_ok == n else RED
    print(f"{color}Итого: {n_ok}/{n} проверок пройдено{RESET}")
    if n_ok != n:
        print("Решения типовых проблем: docs/TROUBLESHOOTING.md")
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
