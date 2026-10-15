"""API /api/v1/ (этап 5): семестры, проверка, пожелания, закрепления, запуски расчёта, варианты.

Документация и «попробовать» — /api/v1/docs (нужен вход в админку). Тонкий слой: вся логика —
в ``timetable.services`` и ядре, те же функции, что у админки. Предназначен для интерфейса
диспетчера (этап 6) и MCP-сервера агента (этап 5б).
"""

import copy

from django.contrib.admin.models import ADDITION, DELETION
from django.contrib.admin.views.decorators import staff_member_required
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Query, Status
from ninja.errors import HttpError

from scheduler import __version__
from scheduler.compare import diff_schedules
from scheduler.criteria import LABELS
from scheduler.criteria import Crit as CritEnum
from scheduler.domain import Assignment, Schedule, Week
from scheduler.precheck import precheck
from scheduler.validator import Report, validate
from timetable import models as m
from timetable.services import views as sv
from timetable.services.problem import ProblemBuildError, build_problem
from timetable.services.runs import RunConflict, enqueue, request_cancel
from timetable.services.variants import ApproveError, approve_variant, default_preset, load_variant

from . import schemas as s
from .auth import StaffSession, TokenAuth, log_change, require

api = NinjaAPI(
    title="Расписание: API",
    version="1",
    description="Сервис составления расписания колледжа. Вход: токен (Authorization: Bearer rsp_…, "
    "выпускается в админке → «Токены API») или сессия админки (+ заголовок X-CSRFToken для изменений). "
    "Права — как у пользователя в админке.",
    urls_namespace="api-v1",
    auth=[TokenAuth(), StaffSession()],
    docs_decorator=staff_member_required,
)


@api.exception_handler(Http404)
def not_found(request: HttpRequest, exc: Http404):  # type: ignore[no-untyped-def]
    return api.create_response(request, {"detail": "Не найдено"}, status=404)


def _week(w: str) -> Week:
    return {"both": Week.BOTH, "odd": Week.ODD, "even": Week.EVEN}[w]


def _crits(breakdown: dict) -> list[s.Crit]:  # type: ignore[type-arg]
    out = []
    for k, n in breakdown.items():
        code = k.value if isinstance(k, CritEnum) else str(k)
        if n:
            label = LABELS.get(CritEnum(code), code) if code in CritEnum._value2member_map_ else code
            out.append(s.Crit(code=code, label=label, count=int(n)))
    return out


# ====================================================================== общее


@api.get("/me", response=s.Me, summary="Кто я и что мне можно")
def me(request: HttpRequest) -> s.Me:
    u = request.user
    perms = sorted(p for p in u.get_all_permissions() if p.startswith("timetable."))
    return s.Me(email=u.email, name=str(u), version=__version__, permissions=perms)


# ====================================================================== семестры


def _semester_out(sem: m.Semester) -> s.SemesterOut:
    active = next((r.pk for r in sem.runs.all() if r.is_active), None)
    approved = next((v.pk for v in sem.variants.all() if v.status == m.ScheduleVariant.Status.APPROVED), None)
    return s.SemesterOut(
        id=sem.pk,
        name=sem.name,
        start_date=sem.start_date,
        end_date=sem.end_date,
        status=sem.status,
        days=sem.days,
        pairs=sem.pair_numbers(),
        active_run_id=active,
        approved_variant_id=approved,
    )


def _semesters():  # type: ignore[no-untyped-def]
    return m.Semester.objects.prefetch_related(
        Prefetch("runs", m.SolveRun.objects.filter(status__in=m.SolveRun.ACTIVE)),
        Prefetch("variants", m.ScheduleVariant.objects.filter(status=m.ScheduleVariant.Status.APPROVED)),
    )


@api.get("/semesters", response=list[s.SemesterOut], summary="Семестры")
def semesters(request: HttpRequest, include_archived: bool = False) -> list[s.SemesterOut]:
    require(request, "timetable.view_semester")
    qs = _semesters()
    if not include_archived:
        qs = qs.exclude(status=m.Semester.Status.ARCHIVED)
    return [_semester_out(x) for x in qs]


@api.get("/semesters/{sem_id}", response=s.SemesterOut, summary="Семестр")
def semester(request: HttpRequest, sem_id: int) -> s.SemesterOut:
    require(request, "timetable.view_semester")
    return _semester_out(get_object_or_404(_semesters(), pk=sem_id))


@api.get("/semesters/{sem_id}/precheck", response=s.PrecheckOut, summary="Быстрая проверка задачи до расчёта")
def semester_precheck(request: HttpRequest, sem_id: int) -> s.PrecheckOut:
    require(request, "timetable.view_workloaditem")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    try:
        p = build_problem(sem)
    except ProblemBuildError as e:
        return s.PrecheckOut(ok=False, build_error=str(e), errors=[], warnings=[], hints=[], stats={})
    f = precheck(p)
    errors = [x.message for x in f if x.level == "error"]
    return s.PrecheckOut(
        ok=not errors,
        errors=errors,
        warnings=[x.message for x in f if x.level == "warning"],
        hints=[x.message for x in f if x.level == "info"],
        stats={
            "groups": len(p.groups),
            "teachers": len(p.teachers),
            "items": len(p.items),
            "pairs_per_cycle": sum(i.pairs_per_cycle for i in p.items),
            "rooms": len(p.active_rooms),
            "pins": len(p.pins),
        },
    )


@api.get("/semesters/{sem_id}/teachers", response=list[s.TeacherOut], summary="Преподаватели с нагрузкой в семестре")
def semester_teachers(request: HttpRequest, sem_id: int, q: str = "") -> list[s.TeacherOut]:
    require(request, "timetable.view_teacher")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    qs = (
        m.Teacher.objects.filter(items__semester=sem)
        .annotate(n=Count("items", filter=Q(items__semester=sem), distinct=True))
        .order_by("name")
        .distinct()
    )
    if q:
        qs = qs.filter(name__icontains=q)
    with_prefs = set(m.TeacherAvailability.objects.filter(semester=sem).values_list("teacher_id", flat=True))
    return [s.TeacherOut(id=t.pk, name=t.name, active=t.active, items=t.n, has_prefs=t.pk in with_prefs) for t in qs]


def _prefs_out(sem: m.Semester, t: m.Teacher, av: m.TeacherAvailability | None) -> s.PrefsOut:
    return s.PrefsOut(
        teacher=s.Ref(id=t.pk, name=t.name),
        semester_id=sem.pk,
        prefs=dict(av.prefs) if av else {},
        days=sem.days,
        pairs=sem.pair_numbers(),
        submitted_at=av.submitted_at if av else None,
    )


@api.get("/semesters/{sem_id}/teachers/{teacher_id}/prefs", response=s.PrefsOut, summary="Пожелания преподавателя")
def teacher_prefs(request: HttpRequest, sem_id: int, teacher_id: int) -> s.PrefsOut:
    require(request, "timetable.view_teacheravailability")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    t = get_object_or_404(m.Teacher, pk=teacher_id)
    return _prefs_out(sem, t, m.TeacherAvailability.objects.filter(semester=sem, teacher=t).first())


@api.patch(
    "/semesters/{sem_id}/teachers/{teacher_id}/prefs",
    response={200: s.PrefsOut, 400: s.Error},
    summary="Изменить пожелания (только указанные слоты)",
)
def teacher_prefs_patch(request: HttpRequest, sem_id: int, teacher_id: int, body: s.PrefsPatch) -> s.PrefsOut:
    require(request, "timetable.change_teacheravailability")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    t = get_object_or_404(m.Teacher, pk=teacher_id)
    valid = {f"{d}-{p}" for d in sem.days for p in sem.pair_numbers()}
    bad = [k for k in [*body.set, *body.reset] if k not in valid]
    if bad:
        raise HttpError(400, f"Нет таких слотов в сетке семестра: {', '.join(bad)} (формат «день-пара», день 1–6)")
    bad_v = [f"{k}={v}" for k, v in body.set.items() if not -2 <= v <= 2]
    if bad_v:
        raise HttpError(400, f"Значения вне шкалы −2…+2: {', '.join(bad_v)}")
    with transaction.atomic():
        av, _ = m.TeacherAvailability.objects.select_for_update().get_or_create(semester=sem, teacher=t)
        prefs = dict(av.prefs or {})
        for k in body.reset:
            prefs.pop(k, None)
        for k, v in body.set.items():
            if v:
                prefs[k] = v
            else:
                prefs.pop(k, None)
        av.prefs = prefs
        av.save(update_fields=["prefs"])
        changes = [f"{k}: {v:+d}" for k, v in body.set.items()] + [f"{k}: сброс" for k in body.reset]
        log_change(request, av, "Пожелания " + ", ".join(changes))
    return _prefs_out(sem, t, av)


def _pin_out(p: m.Pin) -> s.PinOut:
    return s.PinOut(
        id=p.pk,
        item_id=p.item_id,
        week=p.week,
        day=p.day,
        pair=p.pair,
        room_id=p.room_id,
        room=p.room.name if p.room else "",
    )


@api.get("/semesters/{sem_id}/items", response=list[s.ItemOut], summary="Нагрузка семестра (с закреплениями)")
def semester_items(
    request: HttpRequest, sem_id: int, teacher_id: int | None = None, group_id: int | None = None
) -> list[s.ItemOut]:
    require(request, "timetable.view_workloaditem")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    qs = (
        m.WorkloadItem.objects.filter(semester=sem)
        .select_related("discipline")
        .prefetch_related("teachers", "groups", Prefetch("pins", m.Pin.objects.select_related("room")))
        .order_by("pk")
    )
    if teacher_id:
        qs = qs.filter(teachers=teacher_id)
    if group_id:
        qs = qs.filter(groups=group_id)
    return [
        s.ItemOut(
            id=it.pk,
            discipline=it.discipline.name,
            kind=it.kind,
            format=it.format,
            planning=it.planning,
            pairs_per_cycle=it.pairs_per_cycle,
            teachers=[s.Ref(id=x.pk, name=x.name) for x in it.teachers.all()],
            groups=[s.Ref(id=x.pk, name=x.name) for x in it.groups.all()],
            pins=[_pin_out(p) for p in it.pins.all()],
        )
        for it in qs.distinct()
    ]


@api.post(
    "/semesters/{sem_id}/pins",
    response={201: s.PinOut, 400: s.Error, 409: s.Error},
    summary="Закрепить пару (решатель поставит её ровно сюда)",
)
def pin_create(request: HttpRequest, sem_id: int, body: s.PinIn):  # type: ignore[no-untyped-def]
    require(request, "timetable.add_pin")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    item = get_object_or_404(m.WorkloadItem, pk=body.item_id, semester=sem)
    if body.day not in sem.days or body.pair not in sem.pair_numbers():
        raise HttpError(400, f"Нет такого слота в сетке семестра: день {body.day}, пара {body.pair}")
    room = get_object_or_404(m.Room, pk=body.room_id) if body.room_id else None
    try:
        with transaction.atomic():
            pin = m.Pin.objects.create(item=item, week=body.week, day=body.day, pair=body.pair, room=room)
    except IntegrityError as e:
        raise HttpError(409, "Такое закрепление уже есть") from e
    log_change(request, pin, "Закрепление добавлено", ADDITION)
    return Status(201, _pin_out(pin))


@api.delete("/pins/{pin_id}", response={204: None}, summary="Снять закрепление")
def pin_delete(request: HttpRequest, pin_id: int):  # type: ignore[no-untyped-def]
    require(request, "timetable.delete_pin")
    pin = get_object_or_404(m.Pin, pk=pin_id)
    log_change(request, pin, f"Закрепление снято: {pin}", DELETION)
    pin.delete()
    return Status(204, None)


# ====================================================================== запуски расчёта


def _run_out(r: m.SolveRun, log: bool = False) -> s.RunOut:
    return s.RunOut(
        id=r.pk,
        semester_id=r.semester_id,
        status=r.status,
        status_label=r.get_status_display(),
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
        elapsed_s=r.elapsed_s,
        time_limit_s=r.time_limit_s,
        variants_requested=r.variants_requested,
        workers=r.workers,
        preset_id=r.preset_id,
        reference_variant_id=r.reference_id,
        cancel_requested=r.cancel_requested,
        progress=r.progress or {},
        message=r.message,
        solver_status=r.solver_status,
        variant_ids=[v.pk for v in r.variants.all()],
        log=r.log if log else None,
    )


@api.get("/semesters/{sem_id}/runs", response=list[s.RunOut], summary="Запуски расчёта семестра (новые сверху)")
def runs_list(request: HttpRequest, sem_id: int, limit: int = Query(20, ge=1, le=200)) -> list[s.RunOut]:
    require(request, "timetable.view_solverun")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    return [_run_out(r) for r in sem.runs.prefetch_related("variants")[:limit]]


@api.post(
    "/semesters/{sem_id}/runs",
    response={201: s.RunOut, 400: s.Error, 409: s.Conflict},
    summary="Построить расписание (поставить расчёт в очередь)",
)
def run_create(request: HttpRequest, sem_id: int, body: s.RunIn):  # type: ignore[no-untyped-def]
    require(request, "timetable.add_solverun")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    preset = get_object_or_404(m.WeightPreset, pk=body.preset_id) if body.preset_id else default_preset()
    reference = None
    if body.reference_variant_id:
        reference = get_object_or_404(m.ScheduleVariant, pk=body.reference_variant_id, semester=sem)
    try:
        run = enqueue(
            sem,
            time_limit_s=body.time_limit_s,
            variants=body.variants,
            workers=body.workers,
            seed=body.seed,
            min_diff=body.min_diff,
            preset=preset,
            reference=reference,
            name_prefix=body.name_prefix,
            user=request.user,
        )
    except RunConflict as e:
        return Status(409, s.Conflict(detail=str(e), active_run_id=e.active.pk if e.active else None))
    log_change(request, run, "Расчёт поставлен в очередь", ADDITION)
    return Status(201, _run_out(run))


@api.get("/runs/{run_id}", response=s.RunOut, summary="Ход и итог расчёта")
def run_detail(request: HttpRequest, run_id: int, log: bool = False) -> s.RunOut:
    require(request, "timetable.view_solverun")
    return _run_out(get_object_or_404(m.SolveRun.objects.prefetch_related("variants"), pk=run_id), log=log)


@api.post("/runs/{run_id}/cancel", response=s.RunOut, summary="Остановить (найденное сохранится) / снять с очереди")
def run_cancel(request: HttpRequest, run_id: int) -> s.RunOut:
    require(request, "timetable.change_solverun")
    run = request_cancel(get_object_or_404(m.SolveRun, pk=run_id))
    log_change(request, run, "Остановка расчёта")
    return _run_out(run)


# ====================================================================== варианты


def _variant_out(v: m.ScheduleVariant) -> dict:  # type: ignore[type-arg]
    return dict(
        id=v.pk,
        semester_id=v.semester_id,
        name=v.name,
        status=v.status,
        total=v.total,
        solver_status=v.solver_status,
        created_at=v.created_at,
        wall_time_s=v.wall_time_s,
        run_id=v.run_id,
        changes=(v.params or {}).get("changes"),
        room_changes=(v.params or {}).get("room_changes"),
    )


@api.get("/semesters/{sem_id}/variants", response=list[s.VariantOut], summary="Варианты расписания семестра")
def variants_list(request: HttpRequest, sem_id: int) -> list[s.VariantOut]:
    require(request, "timetable.view_schedulevariant")
    sem = get_object_or_404(m.Semester, pk=sem_id)
    qs = sem.variants.defer("problem_snapshot")
    return [s.VariantOut(**_variant_out(v)) for v in qs]


def _variant(variant_id: int) -> m.ScheduleVariant:
    return get_object_or_404(m.ScheduleVariant, pk=variant_id)


@api.get("/variants/{variant_id}", response=s.VariantDetail, summary="Вариант: штраф по критериям")
def variant_detail(request: HttpRequest, variant_id: int) -> s.VariantDetail:
    require(request, "timetable.view_schedulevariant")
    v = _variant(variant_id)
    return s.VariantDetail(**_variant_out(v), breakdown=_crits(v.breakdown or {}), params=v.params or {}, note=v.note)


@api.post("/variants/{variant_id}/approve", response={200: s.VariantOut, 400: s.Error}, summary="Утвердить вариант")
def variant_approve(request: HttpRequest, variant_id: int):  # type: ignore[no-untyped-def]
    require(request, "timetable.change_schedulevariant")
    v = _variant(variant_id)
    try:
        approve_variant(v)
    except ApproveError as e:
        raise HttpError(400, str(e)) from e
    log_change(request, v, "Вариант утверждён")
    return s.VariantOut(**_variant_out(v))


def _report(rep: Report) -> dict:  # type: ignore[type-arg]
    return dict(
        ok=rep.ok,
        hard=[s.Violation(code=x.code, message=x.message) for x in rep.hard],
        warnings=[s.Violation(code=x.code, message=x.message) for x in rep.warnings],
        total=rep.total,
        breakdown=_crits(rep.breakdown),
    )


@api.post(
    "/variants/{variant_id}/check",
    response={200: s.CheckOut, 400: s.Error},
    summary="Проверить вариант независимым валидатором — как есть или с переносами пар",
)
def variant_check(request: HttpRequest, variant_id: int, body: s.CheckIn):  # type: ignore[no-untyped-def]
    require(request, "timetable.view_schedulevariant")
    v = _variant(variant_id)
    problem, sched = load_variant(v)
    base = validate(problem, sched)
    assignments = copy.copy(sched.assignments)
    for mv in body.moves:
        key = (str(mv.item_id), _week(mv.week), f"{mv.day}-{mv.pair}")
        idx = next((i for i, a in enumerate(assignments) if (a.item_id, a.week, a.slot_id) == key), None)
        if idx is None:
            raise HttpError(
                400, f"В варианте нет занятия: элемент {mv.item_id}, {mv.week}, день {mv.day}, пара {mv.pair}"
            )
        a = assignments[idx]
        room = str(mv.to_room_id) if mv.to_room_id else a.room_id
        assignments[idx] = Assignment(a.item_id, a.week, f"{mv.to_day}-{mv.to_pair}", room, a.teacher_remote)
    rep = validate(problem, Schedule(assignments, meta=sched.meta)) if body.moves else base
    return s.CheckOut(**_report(rep), base_total=base.total, delta=rep.total - base.total)


@api.get(
    "/variants/{variant_id}/entities", response=list[s.Entity], summary="Группы / преподаватели / аудитории варианта"
)
def variant_entities(request: HttpRequest, variant_id: int, by: str = "group") -> list[s.Entity]:
    require(request, "timetable.view_schedulevariant")
    if by not in sv.BY_CHOICES:
        raise HttpError(400, "by: group | teacher | room")
    p, _ = load_variant(_variant(variant_id))
    return [s.Entity(id=k, name=n) for k, n in sv.entities(p, by)]


@api.get(
    "/variants/{variant_id}/grid", response=s.GridOut, summary="Сетка: расписание группы / преподавателя / аудитории"
)
def variant_grid(request: HttpRequest, variant_id: int, key: str, by: str = "group", week: str = "odd") -> s.GridOut:
    require(request, "timetable.view_schedulevariant")
    if by not in sv.BY_CHOICES or week not in ("odd", "even"):
        raise HttpError(400, "by: group | teacher | room; week: odd | even")
    p, sched = load_variant(_variant(variant_id))
    names = dict(sv.entities(p, by))
    if key not in names:
        raise HttpError(400, f"Нет такого {sv.BY_CHOICES[by]}: {key} (список — /entities?by={by})")
    rows = sv.grid(p, sched, by, key, Week(week))
    return s.GridOut(
        by=by,
        key=key,
        name=names[key],
        week=week,
        days=sv.day_headers(p),
        rows=[
            s.GridRow(pair=r.pair, time=r.time, cells=[[s.GridEntry(**vars(e)) for e in c] for c in r.cells])
            for r in rows
        ],
    )


@api.get("/variants/{variant_id}/free-rooms", response=list[s.FreeRoomOut], summary="Свободные аудитории")
def variant_free_rooms(
    request: HttpRequest,
    variant_id: int,
    day: int,
    pair_from: int,
    pair_to: int | None = None,
    week: str = "both",
    min_capacity: int = 0,
    include_partial: bool = True,
) -> list[s.FreeRoomOut]:
    require(request, "timetable.view_schedulevariant")
    if week not in sv.WEEK_CHOICES:
        raise HttpError(400, "week: odd | even | both")
    p, sched = load_variant(_variant(variant_id))
    rooms = sv.free_rooms(
        p,
        sched,
        day,
        pair_from,
        pair_to or pair_from,
        sv.WEEK_CHOICES[week],
        min_capacity=min_capacity,
        include_partial=include_partial,
    )
    return [s.FreeRoomOut(**vars(r)) for r in rooms]


@api.get(
    "/variants/{variant_id}/diff",
    response={200: s.DiffOut, 400: s.Error},
    summary="Что изменилось по сравнению с другим вариантом",
    description="against — id варианта для сравнения; по умолчанию эталон пересборки, иначе утверждённый вариант.",
)
def variant_diff(request: HttpRequest, variant_id: int, against: int | None = None):  # type: ignore[no-untyped-def]
    require(request, "timetable.view_schedulevariant")
    v = get_object_or_404(m.ScheduleVariant.objects.select_related("run"), pk=variant_id)
    if against is None:
        if v.run and v.run.reference_id:
            against = v.run.reference_id
        else:
            approved = v.semester.variants.filter(status=m.ScheduleVariant.Status.APPROVED).exclude(pk=v.pk).first()
            if approved is None:
                raise HttpError(400, "С чем сравнивать? Укажите against — id другого варианта")
            against = approved.pk
    other = get_object_or_404(m.ScheduleVariant, pk=against)
    if other.semester_id != v.semester_id:
        raise HttpError(400, "Сравнивать можно только варианты одного семестра")
    op, os_ = load_variant(other)
    np, ns = load_variant(v)
    d = diff_schedules(op, os_, np, ns)

    def pl(x):  # type: ignore[no-untyped-def]
        return s.PlacementOut(week=x.week, day=x.day, pair=x.pair, room=x.room, label=x.label())

    return s.DiffOut(
        variant_id=v.pk,
        against_variant_id=other.pk,
        moved=d.moved,
        room_changed=d.room_changed,
        affected_groups=d.affected_groups,
        affected_teachers=d.affected_teachers,
        affected_rooms=d.affected_rooms,
        changes=[
            s.ChangeOut(
                item_id=c.item_id,
                discipline=c.discipline,
                groups=c.groups,
                teachers=c.teachers,
                kind=c.kind,
                before=[pl(x) for x in c.before],
                after=[pl(x) for x in c.after],
            )
            for c in d.changes
        ],
    )
