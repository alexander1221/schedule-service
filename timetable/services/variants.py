"""Запуск решателя для семестра и хранение вариантов в БД.

``run_and_save`` вызывают обработчик фоновых расчётов и ``manage.py solve_semester``
(через ``services.runs.execute``); прогресс приходит в ``progress``.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from scheduler.criteria import PRESETS, Crit, Mode, WeightProfile
from scheduler.domain import Assignment, Problem, Schedule, Week
from scheduler.serialization import problem_from_dict, problem_to_dict
from scheduler.solver import CpSatSolver, ProgressCallback, SolveParams, SolveResult
from scheduler.validator import Report, validate

from .. import models as m
from .problem import build_problem


def profile_from_preset(preset: m.WeightPreset | None) -> WeightProfile:
    """Пресет БД → профиль ядра. Отсутствующие веса берутся по умолчанию."""
    if preset is None:
        return PRESETS["default"]
    base = WeightProfile()
    weights = dict(base.weights)
    for k, v in (preset.weights or {}).items():
        try:
            weights[Crit(k)] = int(v)
        except ValueError:
            continue  # неизвестный критерий из старой версии — пропускаем
    pref_cost = dict(base.pref_cost)
    for k, v in (preset.pref_cost or {}).items():
        pref_cost[int(k)] = int(v)
    levels = [[Crit(c) for c in lv if c in Crit._value2member_map_] for lv in (preset.lex_levels or [])]
    return WeightProfile(
        name=preset.name,
        mode=Mode(preset.mode),
        weights=weights,
        pref_cost=pref_cost,
        lex_levels=levels or base.lex_levels,
    )


def default_preset() -> m.WeightPreset | None:
    return m.WeightPreset.objects.filter(is_default=True).first()


@transaction.atomic
def save_variant(
    sem: m.Semester,
    problem: Problem,
    schedule: Schedule,
    name: str,
    *,
    user: Any = None,
    params: dict[str, Any] | None = None,
    report: Report | None = None,
    solver_status: str = "",
    objective: float | None = None,
    wall_time_s: float | None = None,
    profile: WeightProfile | None = None,
    run: m.SolveRun | None = None,
) -> m.ScheduleVariant:
    rep = report or validate(problem, schedule, profile)
    v = m.ScheduleVariant.objects.create(
        semester=sem,
        name=name,
        created_by=user if getattr(user, "pk", None) else None,
        solver=str(schedule.meta.get("solver", "")),
        solver_status=solver_status,
        params=params or {},
        problem_snapshot=problem_to_dict(problem),
        breakdown={c.value: n for c, n in rep.breakdown.items() if n},
        total=rep.total,
        objective=objective,
        wall_time_s=wall_time_s,
        run=run,
    )
    existing_items = set(m.WorkloadItem.objects.filter(semester=sem).values_list("pk", flat=True))
    existing_rooms = set(m.Room.objects.values_list("pk", flat=True))
    rows = []
    for a in schedule.assignments:
        sl = problem.grid.slot_by_id[a.slot_id]
        item_pk = int(a.item_id) if a.item_id.isdigit() and int(a.item_id) in existing_items else None
        room_pk = int(a.room_id) if a.room_id and a.room_id.isdigit() and int(a.room_id) in existing_rooms else None
        rows.append(
            m.ScheduleAssignment(
                variant=v,
                item_id=item_pk,
                item_key=a.item_id,
                week=a.week.value,
                day=sl.day,
                pair=sl.pair,
                room_id=room_pk,
                room_key=a.room_id or "",
                teacher_remote=a.teacher_remote,
            )
        )
    m.ScheduleAssignment.objects.bulk_create(rows, batch_size=1000)
    return v


def load_variant(v: m.ScheduleVariant) -> tuple[Problem, Schedule]:
    """Вариант → (задача из снимка, расписание). Не зависит от текущего состояния справочников."""
    problem = problem_from_dict(v.problem_snapshot)
    sched = Schedule(
        [
            Assignment(a.item_key, Week(a.week), f"{a.day}-{a.pair}", a.room_key or None, a.teacher_remote)
            for a in v.assignments.all()
        ],
        meta={"variant_id": v.pk},
    )
    return problem, sched


class ApproveError(Exception):
    pass


def approve_variant(v: m.ScheduleVariant) -> None:
    """Утвердить вариант: остальные варианты семестра — в черновики. Вариант с жёсткими
    нарушениями (по независимому валидатору) утвердить нельзя."""
    problem, sched = load_variant(v)
    rep = validate(problem, sched)
    if not rep.ok:
        raise ApproveError(f"Вариант нельзя утвердить: {len(rep.hard)} жёстких нарушений")
    with transaction.atomic():
        m.ScheduleVariant.objects.filter(semester=v.semester, status=m.ScheduleVariant.Status.APPROVED).exclude(
            pk=v.pk
        ).update(status=m.ScheduleVariant.Status.DRAFT)
        v.status = m.ScheduleVariant.Status.APPROVED
        v.save(update_fields=["status"])


def run_and_save(
    sem: m.Semester,
    params: SolveParams,
    *,
    name_prefix: str = "Вариант",
    user: Any = None,
    progress: ProgressCallback | None = None,
    run: m.SolveRun | None = None,
) -> tuple[SolveResult, list[m.ScheduleVariant]]:
    problem = build_problem(sem)
    result = CpSatSolver().solve(problem, params, progress)
    saved = []
    for i, var in enumerate(result.variants, start=1):
        saved.append(
            save_variant(
                sem,
                problem,
                var.schedule,
                f"{name_prefix} {i}",
                user=user,
                params={
                    "time_limit_s": params.time_limit_s,
                    "workers": params.workers,
                    "seed": params.seed,
                    "variants": params.variants,
                    "min_diff": params.min_diff,
                    "profile": params.profile.name,
                    "mode": params.profile.mode.value,
                    "reference": bool(params.reference),
                    "stopped": result.stopped,
                    # пересборка: сколько назначений отличается от эталона (в штраф варианта не входит)
                    **({"changes": var.breakdown.get(Crit.CHANGES, 0)} if params.reference else {}),
                },
                solver_status=var.status.value,
                objective=var.objective,
                wall_time_s=var.wall_time_s,
                profile=params.profile,
                run=run,
            )
        )
    return result, saved
