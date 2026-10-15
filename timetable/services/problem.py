"""Мост БД ↔ ядро: сборка `scheduler.domain.Problem` из семестра и обратная загрузка.

Идентификаторы в задаче — первичные ключи БД, приведённые к строке
(для типов аудиторий — код). Поэтому назначения варианта однозначно
ссылаются обратно на строки БД, а снимок задачи (JSONB варианта) читаем сам по себе.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.db import transaction

from scheduler import domain as d
from scheduler.defaults import course_limits, shift_prefs

from .. import models as m


class ProblemBuildError(ValueError):
    """Данные семестра нельзя превратить в задачу (например, нет курсов)."""


def _grid(sem: m.Semester) -> d.TimeGrid:
    pairs = [d.PairTime(p.number, f"{p.start:%H:%M}", f"{p.end:%H:%M}") for p in sem.pairs.order_by("number")] or list(
        d.DEFAULT_PAIRS
    )
    return d.TimeGrid(days=sem.days, pairs=pairs)


def build_problem(sem: m.Semester) -> d.Problem:
    """Собрать задачу расписания из семестра. Запросов к БД — константа, не O(строк)."""
    grid = _grid(sem)
    semester = d.Semester(name=sem.name, start_date=sem.start_date, first_week=d.Week(sem.first_week), grid=grid)

    courses = [
        d.Course(
            number=c.number,
            level=d.Level(c.level),
            limits=d.DayLimits(
                max_per_day=c.max_per_day,
                max_per_day_hard=c.max_per_day_hard,
                min_per_day=c.min_per_day,
                min_per_day_hard=c.min_per_day_hard,
                max_consecutive=c.max_consecutive,
                max_consecutive_hard=c.max_consecutive_hard,
            ),
            prefs=dict(c.prefs or {}),
        )
        for c in sem.courses.all()
    ]
    if not courses:
        raise ProblemBuildError(f"В семестре «{sem}» нет курсов")

    groups_qs = m.Group.objects.filter(course__semester=sem).select_related("course")
    groups = [d.Group(str(g.pk), g.name, g.course.number, g.size, dict(g.prefs_override or {})) for g in groups_qs]
    subgroups = [
        d.Subgroup(str(s.pk), str(s.group_id), s.name, s.size, s.partition)
        for s in m.Subgroup.objects.filter(group__course__semester=sem)
    ]

    manual = manual_items_status(sem)
    skip_items = {pk for pk, full in manual.items() if not full}
    items_qs = (
        m.WorkloadItem.objects.filter(semester=sem)
        .exclude(pk__in=skip_items)
        .select_related("discipline", "room_type")
        .prefetch_related("teachers", "groups", "subgroups", "required_equipment")
        .order_by("id")
    )
    items: list[d.WorkloadItem] = []
    used_teachers: set[int] = set()
    used_disciplines: set[int] = set()
    for it in items_qs:
        tids = [t.pk for t in it.teachers.all()]
        used_teachers |= set(tids)
        used_disciplines.add(it.discipline_id)
        items.append(
            d.WorkloadItem(
                id=str(it.pk),
                discipline_id=str(it.discipline_id),
                teacher_ids=[str(t) for t in tids],
                pairs_per_cycle=it.pairs_per_cycle,
                kind=d.LessonKind(it.kind),
                group_ids=[str(g.pk) for g in it.groups.all()],
                subgroup_ids=[str(s.pk) for s in it.subgroups.all()],
                format=d.Format(it.format),
                room_type_id=it.room_type.code if it.room_type else None,
                headcount=it.headcount,
                double=it.double,
                priority=it.priority,
                label=it.label,
                week_pattern=d.WeekPattern(it.week_pattern),
                teacher_in_room=it.teacher_in_room,
                required_equipment=sorted(e.code for e in it.required_equipment.all()),
                may_end_early=it.may_end_early,
            )
        )

    avail = {a.teacher_id: a.prefs for a in m.TeacherAvailability.objects.filter(semester=sem)}
    blocked = {a.room_id: a.blocked for a in m.RoomAvailability.objects.filter(semester=sem)}
    teachers = [
        d.Teacher(
            str(t.pk),
            t.name,
            t.email or "",
            dict(avail.get(t.pk) or {}),
            t.max_per_day,
            t.max_per_day_hard,
            required_equipment=sorted(e.code for e in t.required_equipment.all()),
            preferred_rooms=sorted(str(r.pk) for r in t.preferred_rooms.all()),
        )
        for t in m.Teacher.objects.filter(pk__in=used_teachers)
        .prefetch_related("required_equipment", "preferred_rooms")
        .order_by("pk")
    ]
    disciplines = [
        d.Discipline(str(x.pk), x.name, x.difficulty) for x in m.Discipline.objects.filter(pk__in=used_disciplines)
    ]
    room_types = [d.RoomType(t.code, t.name) for t in m.RoomType.objects.all()]
    rooms = [
        d.Room(
            str(r.pk),
            r.name,
            r.capacity,
            r.room_type.code,
            r.building,
            r.active,
            equipment=sorted(e.code for e in r.equipment.all()),
            blocked=sorted(blocked.get(r.pk, [])),
            closes_min=(r.site.closes_at.hour * 60 + r.site.closes_at.minute) if r.site and r.site.closes_at else None,
        )
        for r in m.Room.objects.select_related("room_type", "site").prefetch_related("equipment")
    ]

    merges = []
    for mg in sem.merges.prefetch_related("items"):
        ids = [str(i.pk) for i in mg.items.all() if i.pk not in skip_items]
        if ids:
            merges.append(d.Merge(str(mg.pk), ids))
    blocks = []
    for b in sem.blocks.prefetch_related("items", "groups"):
        ids = [str(i.pk) for i in b.items.all() if i.pk not in skip_items]
        if ids:
            blocks.append(d.SyncBlock(str(b.pk), b.name, [str(g.pk) for g in b.groups.all()], ids))
    pins = [
        d.Pin(str(p.item_id), d.Week(p.week), d.slot_id(p.day, p.pair), str(p.room_id) if p.room_id else None)
        for p in m.Pin.objects.filter(item__semester=sem).exclude(item_id__in=skip_items)
    ]
    policy = d.Policy(
        format_gap_min=sem.format_gap_min,
        discipline_per_day=sem.discipline_per_day,
        heavy_threshold=sem.heavy_threshold,
        heavy_max_consecutive=sem.heavy_max_consecutive,
        teacher_max_per_day=sem.teacher_max_per_day,
        teacher_max_per_day_hard=sem.teacher_max_per_day_hard,
        teacher_min_per_day=sem.teacher_min_per_day,
        hours_per_pair=sem.hours_per_pair,
        check_capacity=sem.check_capacity,
    )
    return d.Problem(
        semester=semester,
        courses=courses,
        groups=groups,
        teachers=teachers,
        disciplines=disciplines,
        room_types=room_types,
        rooms=rooms,
        items=items,
        subgroups=subgroups,
        merges=merges,
        blocks=blocks,
        pins=pins,
        policy=policy,
    )


def manual_items_status(sem: m.Semester) -> dict[int, bool]:
    """Элементы «вручную» (элективы, кружки) → закреплены ли все их пары.

    Полностью закреплённые попадают в задачу как неподвижные (решатель их учитывает
    и обходит); остальные решатель пропускает — диспетчер поставит их сам.
    """
    out: dict[int, bool] = {}
    qs = m.WorkloadItem.objects.filter(semester=sem, planning=m.WorkloadItem.Planning.MANUAL).prefetch_related("pins")
    for it in qs:
        pinned = sum(2 if p.week == m.Week.BOTH else 1 for p in it.pins.all())
        out[it.pk] = pinned == it.pairs_per_cycle
    return out


# ====================================================================== семестр по умолчанию


@transaction.atomic
def create_semester(
    name: str, start_date: dt.date, courses: int = 4, saturday: bool = False, first_week: str = "odd"
) -> m.Semester:
    """Семестр с сеткой пар и курсами по умолчанию (значения [уточнить] из scheduler.defaults)."""
    sem = m.Semester.objects.create(name=name, start_date=start_date, saturday=saturday, first_week=first_week)
    sem.ensure_default_pairs()
    add_default_courses(sem, courses)
    return sem


def add_default_courses(sem: m.Semester, courses: int = 4) -> None:
    """Курсы 1..N с лимитами и сетками смен по умолчанию (нужны до загрузки групп)."""
    grid = _grid(sem)
    for n in range(1, courses + 1):
        lim = course_limits(n)
        m.Course.objects.create(
            semester=sem,
            number=n,
            max_per_day=lim.max_per_day,
            max_per_day_hard=lim.max_per_day_hard,
            min_per_day=lim.min_per_day,
            min_per_day_hard=lim.min_per_day_hard,
            max_consecutive=lim.max_consecutive,
            max_consecutive_hard=lim.max_consecutive_hard,
            prefs=shift_prefs(n, grid),
        )


# ====================================================================== загрузка задачи в БД


@dataclass
class IdMap:
    """Соответствие идентификаторов задачи → первичных ключей БД (для пересчёта расписаний)."""

    items: dict[str, int] = field(default_factory=dict)
    rooms: dict[str, int] = field(default_factory=dict)
    teachers: dict[str, int] = field(default_factory=dict)
    groups: dict[str, int] = field(default_factory=dict)

    def schedule(self, s: d.Schedule) -> d.Schedule:
        return d.Schedule(
            [
                d.Assignment(
                    str(self.items[a.item_id]),
                    a.week,
                    a.slot_id,
                    str(self.rooms[a.room_id]) if a.room_id else None,
                    a.teacher_remote,
                )
                for a in s.assignments
            ],
            dict(s.meta),
        )


@transaction.atomic
def import_problem(p: d.Problem, semester_name: str) -> tuple[m.Semester, IdMap]:
    """Записать задачу (например, синтетическую) в БД как новый семестр.

    Справочники организации (преподаватели, дисциплины, аудитории, типы) ищутся по
    естественному ключу и переиспользуются, если уже есть.
    """
    ids = IdMap()
    sem = m.Semester.objects.create(
        name=semester_name,
        start_date=p.semester.start_date,
        first_week=p.semester.first_week.value,
        saturday=p.grid.saturday,
        format_gap_min=p.policy.format_gap_min,
        discipline_per_day=p.policy.discipline_per_day,
        heavy_threshold=p.policy.heavy_threshold,
        heavy_max_consecutive=p.policy.heavy_max_consecutive,
        teacher_max_per_day=p.policy.teacher_max_per_day,
        teacher_max_per_day_hard=p.policy.teacher_max_per_day_hard,
        teacher_min_per_day=p.policy.teacher_min_per_day,
        hours_per_pair=p.policy.hours_per_pair,
        check_capacity=p.policy.check_capacity,
    )
    m.PairTime.objects.bulk_create(
        [
            m.PairTime(
                semester=sem,
                number=x.number,
                start=dt.time(x.start_min // 60, x.start_min % 60),
                end=dt.time(x.end_min // 60, x.end_min % 60),
            )
            for x in p.grid.pairs
        ]
    )
    courses = {}
    for c in p.courses:
        lim = c.limits
        courses[c.number] = m.Course.objects.create(
            semester=sem,
            number=c.number,
            level=c.level.value,
            max_per_day=lim.max_per_day,
            max_per_day_hard=lim.max_per_day_hard,
            min_per_day=lim.min_per_day,
            min_per_day_hard=lim.min_per_day_hard,
            max_consecutive=lim.max_consecutive,
            max_consecutive_hard=lim.max_consecutive_hard,
            prefs=dict(c.prefs),
        )
    rtypes = {}
    for t in p.room_types:
        rtypes[t.id], _ = m.RoomType.objects.get_or_create(code=t.id, defaults={"name": t.name})
    for r in p.rooms:
        obj, _ = m.Room.objects.get_or_create(
            name=r.name,
            defaults={
                "capacity": r.capacity,
                "room_type": rtypes[r.type_id],
                "building": r.building,
                "active": r.active,
            },
        )
        obj.equipment.add(*[_equipment(c) for c in r.equipment])
        if r.closes_min is not None and obj.site_id is None:
            hh, mm = divmod(r.closes_min, 60)
            obj.site, _ = m.Site.objects.get_or_create(
                name=f"Площадка до {hh}:{mm:02d}", defaults={"closes_at": dt.time(hh, mm)}
            )
            obj.save(update_fields=["site"])
        if r.blocked:
            m.RoomAvailability.objects.update_or_create(
                room=obj, semester=sem, defaults={"blocked": sorted(r.blocked), "reason": "синтетика"}
            )
        ids.rooms[r.id] = obj.pk
    groups = {}
    for g in p.groups:
        groups[g.id] = m.Group.objects.create(
            course=courses[g.course], name=g.name, size=g.size, prefs_override=dict(g.prefs_override)
        )
        ids.groups[g.id] = groups[g.id].pk
    subgroups = {
        s.id: m.Subgroup.objects.create(group=groups[s.group_id], name=s.name, size=s.size, partition=s.partition)
        for s in p.subgroups
    }
    teachers = {}
    for t in p.teachers:
        if t.email:
            obj, _ = m.Teacher.objects.get_or_create(email=t.email.lower(), defaults={"name": t.name})
        else:
            obj = m.Teacher.objects.create(name=t.name)
        obj.max_per_day, obj.max_per_day_hard = t.max_per_day, t.max_per_day_hard
        obj.save()
        obj.required_equipment.set([_equipment(c) for c in t.required_equipment])
        obj.preferred_rooms.set([ids.rooms[r] for r in t.preferred_rooms if r in ids.rooms])
        teachers[t.id] = obj
        ids.teachers[t.id] = obj.pk
        m.TeacherAvailability.objects.create(teacher=obj, semester=sem, prefs=dict(t.prefs))
    disciplines = {}
    for x in p.disciplines:
        disciplines[x.id], _ = m.Discipline.objects.get_or_create(name=x.name, defaults={"difficulty": x.difficulty})
    blocks = {}
    for b in p.blocks:
        blk = m.SyncBlock.objects.create(semester=sem, name=b.name)
        blk.groups.set([groups[g] for g in b.group_ids])
        for iid in b.item_ids:
            blocks[iid] = blk
    merges = {}
    for mg in p.merges:
        obj = m.MergeGroup.objects.create(semester=sem, name=f"Объединение {mg.id}")
        for iid in mg.item_ids:
            merges[iid] = obj
    for it in p.items:
        obj = m.WorkloadItem.objects.create(
            semester=sem,
            discipline=disciplines[it.discipline_id],
            kind=it.kind.value,
            pairs_per_cycle=it.pairs_per_cycle,
            format=it.format.value,
            room_type=rtypes[it.room_type_id] if it.room_type_id else None,
            headcount=it.headcount,
            double=it.double,
            priority=it.priority,
            label=it.label,
            week_pattern=it.week_pattern.value,
            teacher_in_room=it.teacher_in_room,
            may_end_early=it.may_end_early,
            sync_block=blocks.get(it.id),
            merge_group=merges.get(it.id),
        )
        obj.teachers.set([teachers[t] for t in it.teacher_ids])
        obj.groups.set([groups[g] for g in it.group_ids])
        obj.subgroups.set([subgroups[s] for s in it.subgroup_ids])
        obj.required_equipment.set([_equipment(c) for c in it.required_equipment])
        ids.items[it.id] = obj.pk
    for pin in p.pins:
        sl = p.grid.slot_by_id[pin.slot_id]
        m.Pin.objects.create(
            item_id=ids.items[pin.item_id],
            week=pin.week.value,
            day=sl.day,
            pair=sl.pair,
            room_id=ids.rooms.get(pin.room_id) if pin.room_id else None,
        )
    return sem, ids


EQUIPMENT_NAMES = {
    "projector": "Проектор",
    "pc": "Компьютеры",
    "pc_rtx": "Компьютеры с RTX",
    "java_sw": "ПО: Java",
}


def _equipment(code: str) -> m.Equipment:
    obj, _ = m.Equipment.objects.get_or_create(code=code, defaults={"name": EQUIPMENT_NAMES.get(code, code)})
    return obj
