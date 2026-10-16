"""Командная строка ядра. Подробности — schedule_cli.py и README.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .criteria import LABELS, PRESETS, Crit
from .domain import CYCLE_WEEKS, DAY_NAMES, Problem, Schedule, Week, weeks_of
from .generator import GenParams, generate
from .precheck import has_errors, precheck
from .serialization import (
    load_json,
    problem_from_dict,
    problem_to_dict,
    save_json,
    schedule_from_dict,
    schedule_to_dict,
)
from .validator import validate

# ------------------------------------------------------------------ вывод

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def log(msg: str) -> None:
    print(_c("36", "[schedule]") + " " + msg, flush=True)


def ok(msg: str) -> None:
    print(_c("32", "[schedule] ✓ ") + msg, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "[schedule] ! ") + msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(_c("31", "[schedule] ✗ ") + msg, file=sys.stderr)
    print("            Подробнее: docs/TROUBLESHOOTING.md; запуск с --debug покажет трассировку.", file=sys.stderr)
    raise SystemExit(code)


def _load_problem(path: str) -> Problem:
    if not Path(path).is_file():
        die(f"Файл задачи не найден: {path}")
    try:
        return problem_from_dict(load_json(path))
    except (ValueError, KeyError, TypeError) as e:
        die(f"Не удалось прочитать задачу {path}: {e}")
        raise  # недостижимо, для mypy


def _load_schedule(path: str) -> Schedule:
    if not Path(path).is_file():
        die(f"Файл расписания не найден: {path}")
    return schedule_from_dict(load_json(path))


# ------------------------------------------------------------------ команды


def cmd_generate(a: argparse.Namespace) -> None:
    gp = GenParams(
        seed=a.seed,
        n_groups=a.groups,
        n_teachers=a.teachers,
        tightness=a.tightness,
        n_blocks=a.blocks,
        saturday=a.saturday,
    )
    g = generate(gp)
    save_json(problem_to_dict(g.problem), a.output)
    ok(
        f"Задача: {len(g.problem.groups)} групп, {len(g.problem.teachers)} преподавателей, "
        f"{len(g.problem.rooms)} аудиторий, {len(g.problem.items)} элементов нагрузки → {a.output}"
    )
    if a.planted:
        save_json(schedule_to_dict(g.planted), a.planted)
        ok(f"Посаженное (эталонное допустимое) расписание → {a.planted}")


def cmd_precheck(a: argparse.Namespace) -> None:
    p = _load_problem(a.problem)
    fs = precheck(p)
    for f in sorted(fs, key=lambda x: {"error": 0, "warning": 1}.get(x.level, 2)):
        if f.level == "error":
            print(_c("31", "[schedule] ✗ ") + f.message)
        elif f.level == "warning":
            warn(f.message)
        else:
            log("подсказка: " + f.message)
    if has_errors(fs):
        raise SystemExit(2)
    n_warn = sum(1 for f in fs if f.level == "warning")
    ok(f"Быстрые проверки пройдены (предупреждений: {n_warn}, подсказок: {len(fs) - n_warn})")


def cmd_solve(a: argparse.Namespace) -> None:
    from .solver import CpSatSolver, SolveParams, Status

    p = _load_problem(a.problem)
    if a.preset not in PRESETS:
        die(f"Неизвестный пресет весов {a.preset!r}; есть: {', '.join(PRESETS)}")
    params = SolveParams(
        time_limit_s=a.time,
        workers=a.workers,
        seed=a.seed,
        variants=a.variants,
        min_diff=a.min_diff,
        profile=PRESETS[a.preset],
        reference=_load_schedule(a.reference) if a.reference else None,
        log_search=a.debug,
    )
    log(f"Решатель CP-SAT: {a.variants} вар. × до {a.time:.0f} с, потоков {a.workers}, пресет «{params.profile.name}»")
    last = [0.0]

    def progress(pr):  # type: ignore[no-untyped-def]
        if time.monotonic() - last[0] < 2 and pr.solutions > 1:
            return
        last[0] = time.monotonic()
        gap = f", разрыв {pr.gap:.0%}" if pr.gap is not None else ""
        obj = f"штраф {pr.objective:.0f}" if pr.objective is not None else "найдено"
        log(f"  вариант {pr.variant} [{pr.stage}] {pr.elapsed_s:5.1f} с: решений {pr.solutions}, {obj}{gap}")

    t0 = time.monotonic()
    res = CpSatSolver().solve(p, params, progress)
    for f in res.precheck:
        if f.level == "warning" and a.verbose:
            warn(f.message)
    if res.status in (Status.PRECHECK_FAILED, Status.INFEASIBLE, Status.UNKNOWN):
        for line in res.diagnostics:
            print(_c("31", "  ") + line)
        die(f"Расписание не построено: {res.status.value}", 2)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    for i, v in enumerate(res.variants, start=1):
        path = out if len(res.variants) == 1 else out.with_name(f"{out.stem}_v{i}{out.suffix}")
        v.schedule.meta["breakdown"] = {c.value: n for c, n in v.breakdown.items() if n}
        v.schedule.meta["total"] = v.total
        save_json(schedule_to_dict(v.schedule), path)
        ok(f"Вариант {i}: штраф {v.total} ({v.status.value}, {v.wall_time_s:.0f} с) → {path}")
    if len(res.variants) > 1:
        _compare(res.variants)
    log(f"Готово за {time.monotonic() - t0:.0f} с")


def _compare(variants) -> None:  # type: ignore[no-untyped-def]
    crits = [c for c in Crit if any(v.breakdown.get(c) for v in variants)]
    head = f"{'Критерий':44s}" + "".join(f"{'В' + str(i + 1):>9s}" for i in range(len(variants)))
    print(head)
    for c in crits:
        print(f"{LABELS[c][:44]:44s}" + "".join(f"{v.breakdown.get(c, 0):9d}" for v in variants))
    print(f"{'Итого (взвешенно)':44s}" + "".join(f"{v.total:9d}" for v in variants))


def cmd_validate(a: argparse.Namespace) -> None:
    p = _load_problem(a.problem)
    s = _load_schedule(a.schedule)
    rep = validate(p, s, PRESETS[a.preset], _load_schedule(a.reference) if a.reference else None)
    print(rep.summary(limit=a.limit))
    if a.issues:
        for c in Crit:
            items = [i for i in rep.issues if i.crit is c]
            if items:
                print(f"\n{LABELS[c]} ({len(items)}):")
                for i in items[: a.limit]:
                    print(f"  • {i.message}")
    if a.json:
        Path(a.json).write_text(
            json.dumps(
                {
                    "ok": rep.ok,
                    "total": rep.total,
                    "hard": [v.__dict__ for v in rep.hard],
                    "warnings": [v.__dict__ for v in rep.warnings],
                    "breakdown": {c.value: n for c, n in rep.breakdown.items()},
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    if not rep.ok:
        raise SystemExit(3)


def cmd_show(a: argparse.Namespace) -> None:
    p = _load_problem(a.problem)
    s = _load_schedule(a.schedule)
    week = Week(a.week)
    key = a.who
    cells: dict[str, list[str]] = {}
    for x in s.assignments:
        if week not in weeks_of(x.week):
            continue
        it = p.item_by_id[x.item_id]
        groups = {p.group_by_id[g].name for g in p.item_groups(it)}
        teachers = {p.teacher_by_id[t].name for t in it.teacher_ids}
        room = p.room_by_id[x.room_id].name if x.room_id else "онлайн"
        if key not in groups and key not in teachers and key != room:
            continue
        mark = "" if x.week is Week.BOTH else "*"
        disc = p.discipline_by_id[it.discipline_id].name + (f" {it.label}" if it.label else "")
        cells.setdefault(x.slot_id, []).append(f"{disc[:18]}{mark} [{room}] {', '.join(sorted(teachers))[:16]}")
    if not cells:
        die(f"Ничего не найдено для «{key}» (укажите название группы, ФИО преподавателя или номер аудитории)")
    print(f"{key} — неделя {'нечётная' if week is Week.ODD else 'чётная'} (* — мигающая пара)")
    for d in p.grid.days:
        print(f"\n{DAY_NAMES[d]}")
        for sl in p.grid.day_slots(d):
            pt = next(x for x in p.grid.pairs if x.number == sl.pair)
            text = " | ".join(cells.get(sl.id, [])) or "—"
            print(f"  {sl.pair}. {pt.start}–{pt.end}  {text}")


def cmd_bench(a: argparse.Namespace) -> None:
    from .solver import CpSatSolver, SolveParams

    rows = []
    for seed in range(1, a.seeds + 1):
        for tight in a.tightness:
            g = generate(GenParams(seed=seed, tightness=tight))
            first = [None]

            def progress(pr, first=first):  # type: ignore[no-untyped-def]
                if first[0] is None:
                    first[0] = pr.elapsed_s

            t0 = time.monotonic()
            res = CpSatSolver().solve(
                g.problem, SolveParams(time_limit_s=a.time, workers=a.workers, seed=seed), progress
            )
            v = res.variants[0] if res.variants else None
            row = {
                "seed": seed,
                "tightness": tight,
                "items": len(g.problem.items),
                "status": res.status.value,
                "first_s": round(first[0], 1) if first[0] is not None else None,
                "total_s": round(time.monotonic() - t0, 1),
                "penalty": v.total if v else None,
                "planted_penalty": validate(g.problem, g.planted).total,
                "hard_violations": len(validate(g.problem, v.schedule).hard) if v else None,
            }
            rows.append(row)
            log(json.dumps(row, ensure_ascii=False))
    if a.output:
        Path(a.output).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        ok(f"Результаты бенчмарка → {a.output}")


# ------------------------------------------------------------------ разбор аргументов


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="schedule_cli.py", description="Ядро сервиса составления расписания")
    ap.add_argument("--debug", action="store_true", help="трассировки ошибок и лог поиска CP-SAT")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="сгенерировать синтетическую задачу")
    g.add_argument("-o", "--output", default="data/problem.json")
    g.add_argument("--planted", help="сохранить посаженное допустимое расписание")
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("--groups", type=int, default=25)
    g.add_argument("--teachers", type=int, default=45)
    g.add_argument("--tightness", type=float, default=0.5, help="теснота 0…1")
    g.add_argument("--blocks", type=int, default=2, help="число языковых синхронных блоков")
    g.add_argument("--saturday", action="store_true")
    g.set_defaults(func=cmd_generate)

    c = sub.add_parser("precheck", help="быстрые проверки разрешимости")
    c.add_argument("problem")
    c.set_defaults(func=cmd_precheck)

    s = sub.add_parser("solve", help="построить расписание")
    s.add_argument("problem")
    s.add_argument("-o", "--output", default="output/schedule.json")
    s.add_argument("--time", type=float, default=60, help="лимит времени на вариант, с")
    s.add_argument("--workers", type=int, default=os.cpu_count() or 8)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--variants", type=int, default=1)
    s.add_argument("--min-diff", type=int, default=10, help="варианты отличаются минимум на K назначений")
    s.add_argument("--preset", default="default", help=f"пресет весов: {', '.join(PRESETS)}")
    s.add_argument("--reference", help="текущее расписание: пересборка с минимальными изменениями")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_solve)

    v = sub.add_parser("validate", help="проверить расписание независимым валидатором")
    v.add_argument("problem")
    v.add_argument("schedule")
    v.add_argument("--preset", default="default")
    v.add_argument("--reference")
    v.add_argument("--issues", action="store_true", help="сводка проблем по критериям")
    v.add_argument("--limit", type=int, default=15)
    v.add_argument("--json", help="сохранить отчёт в JSON")
    v.set_defaults(func=cmd_validate)

    w = sub.add_parser("show", help="показать расписание группы/преподавателя/аудитории")
    w.add_argument("problem")
    w.add_argument("schedule")
    w.add_argument("who", help="название группы, ФИО преподавателя или номер аудитории")
    w.add_argument("--week", choices=[k.value for k in CYCLE_WEEKS], default="odd")
    w.set_defaults(func=cmd_show)

    b = sub.add_parser("bench", help="бенчмарк времени и качества на синтетике")
    b.add_argument("--seeds", type=int, default=3)
    b.add_argument("--tightness", type=float, nargs="+", default=[0.3, 0.7, 1.0])
    b.add_argument("--time", type=float, default=60)
    b.add_argument("--workers", type=int, default=os.cpu_count() or 8)
    b.add_argument("-o", "--output", help="сохранить результаты в JSON")
    b.set_defaults(func=cmd_bench)
    return ap


def _utf8_output() -> None:
    """Вывод в UTF-8. На Windows, когда вывод уходит в файл или другую программу, Python пишет в кодировке
    системы (cp1251/cp1252) и падает на кириллице и значках (UnicodeEncodeError). В консоли Windows
    вывод и так UTF-8 — там ничего не меняется."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        a.func(a)
    except KeyboardInterrupt:
        die("Прервано пользователем", 130)
    except BrokenPipeError:  # вывод обрезан (| head) — это не ошибка
        sys.stderr.close()
        return 0
    except SystemExit:
        raise
    except Exception as e:
        if a.debug:
            raise
        die(f"{type(e).__name__}: {e}")
    return 0
