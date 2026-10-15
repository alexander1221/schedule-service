"""Импорт/экспорт справочников в одну книгу xlsx.

Листы организации: «Преподаватели», «Оборудование», «Дисциплины», «Типы аудиторий», «Площадки», «Аудитории».
Листы семестра: «Группы», «Подгруппы», «Доступность» (сетка −2…+2 по слотам).

Импорт только добавляет и обновляет (по естественному ключу: почта или ФИО,
название, код, номер) и никогда не удаляет — удаление справочника затрагивает
нагрузку и делается осознанно в админке. Всё или ничего, есть пробный прогон.
Листы можно присылать не все: отсутствующий лист просто пропускается.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections.abc import Callable
from typing import Any, BinaryIO

from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from scheduler.domain import DAY_NAMES

from .. import models as m
from .common import (
    ImportReport,
    RowError,
    cell_str,
    data_rows,
    finish_sheet,
    header_map,
    norm,
    parse_bool,
    parse_int,
    split_list,
    suggest,
    write_header,
)

T_TEACHERS, T_DISC, T_RTYPES, T_ROOMS = "Преподаватели", "Дисциплины", "Типы аудиторий", "Аудитории"
T_GROUPS, T_SUBGROUPS, T_AVAIL = "Группы", "Подгруппы", "Доступность"
T_EQUIP, T_ROOM_BUSY = "Оборудование", "Занятость аудиторий"
T_SITES = "Площадки"
BUSY_MARKS = {"x", "х", "да", "1", "+", "занята", "занято"}


def _slot_titles(sem: m.Semester) -> list[tuple[str, str]]:
    """[(заголовок «Пн 1», slot_id «1-1»), …] по сетке семестра."""
    return [(f"{DAY_NAMES[d]} {p}", f"{d}-{p}") for d in sem.days for p in sem.pair_numbers()]


# ====================================================================== экспорт


def export_references(sem: m.Semester | None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = T_TEACHERS
    write_header(
        ws,
        [
            "ФИО",
            "Почта",
            "Работает",
            "Макс. пар в день",
            "Лимит жёсткий",
            "Нужно оборудование",
            "Предпочитает аудитории",
        ],
        [32, 30, 10, 12, 12, 28, 28],
    )
    for t in m.Teacher.objects.prefetch_related("required_equipment", "preferred_rooms").order_by("name"):
        ws.append(
            [
                t.name,
                t.email or "",
                "да" if t.active else "нет",
                t.max_per_day,
                "да" if t.max_per_day_hard else "нет",
                "; ".join(e.name for e in t.required_equipment.all()),
                "; ".join(r.name for r in t.preferred_rooms.all()),
            ]
        )
    finish_sheet(ws)

    ws = wb.create_sheet(T_EQUIP)
    write_header(ws, ["Код", "Название"], [16, 30])
    for e in m.Equipment.objects.order_by("name"):
        ws.append([e.code, e.name])
    finish_sheet(ws)

    ws = wb.create_sheet(T_DISC)
    write_header(ws, ["Название", "Сложность", "Кафедра"], [40, 10, 24])
    for x in m.Discipline.objects.select_related("department").order_by("name"):
        ws.append([x.name, x.difficulty, x.department.name if x.department else ""])
    finish_sheet(ws)

    ws = wb.create_sheet(T_RTYPES)
    write_header(ws, ["Код", "Название"], [16, 30])
    for rt in m.RoomType.objects.order_by("name"):
        ws.append([rt.code, rt.name])
    finish_sheet(ws)

    ws = wb.create_sheet(T_SITES)
    write_header(ws, ["Название", "Закрывается в", "Примечание"], [20, 14, 40])
    for site in m.Site.objects.order_by("name"):
        ws.append([site.name, site.closes_at.strftime("%H:%M") if site.closes_at else None, site.note])
    finish_sheet(ws)

    ws = wb.create_sheet(T_ROOMS)
    write_header(
        ws,
        ["Номер", "Корпус", "Вместимость", "Тип", "Используется", "Оборудование", "Примечание", "Площадка"],
        [10, 14, 12, 22, 12, 30, 30, 14],
    )
    for r in m.Room.objects.select_related("room_type", "site").prefetch_related("equipment").order_by("name"):
        ws.append(
            [
                r.name,
                r.building,
                r.capacity,
                r.room_type.name,
                "да" if r.active else "нет",
                "; ".join(e.name for e in r.equipment.all()),
                r.note,
                r.site.name if r.site else None,
            ]
        )
    finish_sheet(ws)

    if sem is not None:
        ws = wb.create_sheet(T_GROUPS)
        write_header(ws, ["Название", "Курс", "Численность"], [16, 8, 12])
        for g in m.Group.objects.filter(course__semester=sem).select_related("course"):
            ws.append([g.name, g.course.number, g.size])
        finish_sheet(ws)

        ws = wb.create_sheet(T_SUBGROUPS)
        write_header(ws, ["Группа", "Подгруппа", "Численность", "Деление"], [16, 16, 12, 14])
        for s in m.Subgroup.objects.filter(group__course__semester=sem).select_related("group"):
            ws.append([s.group.name, s.name, s.size, s.partition])
        finish_sheet(ws)

        ws = wb.create_sheet(T_AVAIL)
        slots = _slot_titles(sem)
        write_header(ws, ["ФИО", "Почта", *[t for t, _ in slots]], [30, 26, *[6] * len(slots)])
        avail = {a.teacher_id: a.prefs for a in sem.availability.all()}
        for t in m.Teacher.objects.filter(active=True).order_by("name"):
            prefs = avail.get(t.pk) or {}
            ws.append([t.name, t.email or "", *[prefs.get(sid, 0) or None for _, sid in slots]])
        finish_sheet(ws)

        ws = wb.create_sheet(T_ROOM_BUSY)
        write_header(ws, ["Номер", "Кем занята", *[t for t, _ in slots]], [12, 24, *[6] * len(slots)])
        busy = {a.room_id: a for a in sem.room_availability.all()}
        for r in m.Room.objects.order_by("name"):
            a = busy.get(r.pk)
            if a and a.blocked:
                ws.append([r.name, a.reason, *["x" if sid in a.blocked else None for _, sid in slots]])
        finish_sheet(ws)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ====================================================================== импорт


def import_references(sem: m.Semester | None, file: BinaryIO | bytes, *, dry_run: bool = True) -> ImportReport:
    rep = ImportReport()
    try:
        wb = load_workbook(io.BytesIO(file) if isinstance(file, bytes) else file, data_only=True)
    except Exception as e:
        rep.errors.append(RowError("", 0, "", f"не удалось открыть файл как xlsx: {e}"))
        return rep
    steps: list[tuple[str, Callable[[Worksheet, ImportReport], None]]] = [
        (T_EQUIP, _equipment),
        (T_RTYPES, _room_types),
        (T_SITES, _sites),
        (T_ROOMS, _rooms),
        (T_DISC, _disciplines),
        (T_TEACHERS, _teachers),
    ]
    if sem is not None:
        steps += [
            (T_GROUPS, lambda ws, r: _groups(sem, ws, r)),
            (T_SUBGROUPS, lambda ws, r: _subgroups(sem, ws, r)),
            (T_AVAIL, lambda ws, r: _availability(sem, ws, r)),
            (T_ROOM_BUSY, lambda ws, r: _room_busy(sem, ws, r)),
        ]
    elif any(s in wb.sheetnames for s in (T_GROUPS, T_SUBGROUPS, T_AVAIL, T_ROOM_BUSY)):
        rep.warnings.append("Листы «Группы», «Подгруппы», «Доступность» пропущены: не выбран семестр")
    known = {s for s, _ in steps} | {T_GROUPS, T_SUBGROUPS, T_AVAIL, T_ROOM_BUSY}
    for s in wb.sheetnames:
        if s not in known:
            rep.warnings.append(f"Лист «{s}» не распознан и пропущен")
    with transaction.atomic():
        for sheet, fn in steps:
            if sheet in wb.sheetnames:
                fn(wb[sheet], rep)
        if rep.errors or dry_run:
            transaction.set_rollback(True)
        else:
            rep.applied = True
    return rep


def _cols(ws: Worksheet, rep: ImportReport, spec: dict[str, str], required: list[str]) -> dict[str, int] | None:
    cols, unknown = header_map(ws, {norm(k): v for k, v in spec.items()})
    if unknown:
        rep.warnings.append(f"«{ws.title}»: колонки пропущены — {', '.join(unknown)}")
    miss = [k for k, v in spec.items() if v in required and v not in cols]
    if miss:
        rep.errors.append(RowError(ws.title, 1, "", "нет обязательных колонок: " + ", ".join(miss)))
        return None
    return cols


def _getter(row: tuple[Any, ...], cols: dict[str, int]) -> Callable[[str], Any]:
    return lambda k: row[cols[k] - 1] if k in cols and cols[k] - 1 < len(row) else None


def _upsert(rep: ImportReport, what: str, obj: Any, created: bool, changed: bool) -> None:
    if created:
        rep.bump(what)
    elif changed:
        rep.bump(what, "updated")


def _set(obj: Any, values: dict[str, Any]) -> bool:
    changed = False
    for k, v in values.items():
        if getattr(obj, k) != v:
            setattr(obj, k, v)
            changed = True
    return changed


def _room_types(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(ws, rep, {"Код": "code", "Название": "name"}, ["code", "name"])
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        code, name = cell_str(g("code")), cell_str(g("name"))
        if not code or not name:
            rep.errors.append(RowError(ws.title, rown, "", "нужны и код, и название"))
            continue
        obj = m.RoomType.objects.filter(code=code).first()
        created = obj is None
        obj = obj or m.RoomType(code=code)
        changed = _set(obj, {"name": name})
        obj.save()
        _upsert(rep, "типы аудиторий", obj, created, changed)


def _parse_time(v: Any) -> dt.time | None:
    """«21:00», «21.00», 21 или время Excel → time; пусто → None; иначе ValueError."""
    if v is None or cell_str(v) == "":
        return None
    if isinstance(v, dt.datetime):
        return v.time().replace(second=0, microsecond=0)
    if isinstance(v, dt.time):
        return v.replace(second=0, microsecond=0)
    mt = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*", cell_str(v))
    if not mt or int(mt.group(1)) > 23 or int(mt.group(2) or 0) > 59:
        raise ValueError(f"«{cell_str(v)}» — ожидается время вида 21:00")
    return dt.time(int(mt.group(1)), int(mt.group(2) or 0))


def _sites(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(ws, rep, {"Название": "name", "Закрывается в": "closes", "Примечание": "note"}, ["name"])
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        name = cell_str(g("name"))
        if not name:
            rep.errors.append(RowError(ws.title, rown, "Название", "пусто"))
            continue
        try:
            closes = _parse_time(g("closes"))
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "Закрывается в", str(e)))
            continue
        obj = m.Site.objects.filter(name=name).first()
        created = obj is None
        obj = obj or m.Site(name=name)
        values: dict[str, Any] = {"closes_at": closes}
        if "note" in cols:
            values["note"] = cell_str(g("note"))[:200]
        changed = _set(obj, values)
        obj.save()
        _upsert(rep, "площадки", obj, created, changed)


def _rooms(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(
        ws,
        rep,
        {
            "Номер": "name",
            "Площадка": "site",
            "Корпус": "building",
            "Вместимость": "capacity",
            "Тип": "type",
            "Используется": "active",
            "Оборудование": "equipment",
            "Примечание": "note",
        },
        ["name", "capacity", "type"],
    )
    if cols is None:
        return
    types = {norm(t.name): t for t in m.RoomType.objects.all()} | {norm(t.code): t for t in m.RoomType.objects.all()}
    sites = {norm(x.name): x for x in m.Site.objects.all()}
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        name = cell_str(g("name"))
        if not name:
            rep.errors.append(RowError(ws.title, rown, "Номер", "пусто"))
            continue
        try:
            cap = parse_int(g("capacity"), lo=1, hi=2000, required=True)
            active = parse_bool(g("active"), default=True)
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "", str(e)))
            continue
        rt = types.get(norm(g("type")))
        if rt is None:
            rep.errors.append(
                RowError(
                    ws.title,
                    rown,
                    "Тип",
                    f"тип «{cell_str(g('type'))}» не найден."
                    + suggest(cell_str(g("type")), [t.name for t in types.values()]),
                )
            )
            continue
        equipment = _resolve_equipment(g("equipment"), ws.title, rown, rep) if "equipment" in cols else None
        if equipment is False:
            continue
        site: m.Site | None = None
        site_name = cell_str(g("site")) if "site" in cols else ""
        if site_name:
            site = sites.get(norm(site_name))
            if site is None:
                rep.errors.append(
                    RowError(
                        ws.title,
                        rown,
                        "Площадка",
                        f"площадки «{site_name}» нет — добавьте её на лист «{T_SITES}»."
                        + suggest(site_name, [x.name for x in sites.values()]),
                    )
                )
                continue
        obj = m.Room.objects.filter(name=name).first()
        created = obj is None
        obj = obj or m.Room(name=name)
        values: dict[str, Any] = {
            "building": cell_str(g("building")),
            "capacity": cap,
            "room_type_id": rt.pk,
            "active": active,
        }
        if "note" in cols:
            values["note"] = cell_str(g("note"))[:200]
        if "site" in cols:
            values["site_id"] = site.pk if site else None
        changed = _set(obj, values)
        obj.save()
        if isinstance(equipment, list) and {e.pk for e in obj.equipment.all()} != {e.pk for e in equipment}:
            obj.equipment.set(equipment)
            changed = True
        _upsert(rep, "аудитории", obj, created, changed)


def _disciplines(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(ws, rep, {"Название": "name", "Сложность": "difficulty", "Кафедра": "department"}, ["name"])
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        name = cell_str(g("name"))
        if not name:
            rep.errors.append(RowError(ws.title, rown, "Название", "пусто"))
            continue
        try:
            diff = parse_int(g("difficulty"), lo=0, hi=10) or 0
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "Сложность", str(e)))
            continue
        obj = m.Discipline.objects.filter(name=name).first()
        created = obj is None
        obj = obj or m.Discipline(name=name)
        values: dict[str, Any] = {"difficulty": diff}
        if "department" in cols:
            dep_name = cell_str(g("department"))
            dep = None
            if dep_name:
                dep = m.Department.objects.filter(name__iexact=dep_name).first()
                if dep is None:
                    dep = m.Department.objects.create(name=dep_name)
                    rep.bump("кафедры")
            values["department_id"] = dep.pk if dep else None
        changed = _set(obj, values)
        obj.save()
        _upsert(rep, "дисциплины", obj, created, changed)


def _teachers(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(
        ws,
        rep,
        {
            "ФИО": "name",
            "Почта": "email",
            "Работает": "active",
            "Макс. пар в день": "max",
            "Лимит жёсткий": "hard",
            "Нужно оборудование": "equipment",
            "Предпочитает аудитории": "rooms",
        },
        ["name"],
    )
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        name, email = cell_str(g("name")), cell_str(g("email")).lower() or None
        try:
            active = parse_bool(g("active"), default=True)
            mx = parse_int(g("max"), lo=1, hi=12)
            hard = parse_bool(g("hard"))
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "", str(e)))
            continue
        if not name:
            rep.errors.append(RowError(ws.title, rown, "ФИО", "пусто"))
            continue
        obj = m.Teacher.objects.filter(email=email).first() if email else None
        if obj is None:
            same = list(m.Teacher.objects.filter(name=name))
            if len(same) > 1:
                rep.errors.append(
                    RowError(ws.title, rown, "Почта", "несколько преподавателей с таким ФИО — укажите почту")
                )
                continue
            obj = same[0] if same else None
        created = obj is None
        obj = obj or m.Teacher()
        equipment = _resolve_equipment(g("equipment"), ws.title, rown, rep) if "equipment" in cols else None
        rooms = _resolve_rooms(g("rooms"), ws.title, rown, rep) if "rooms" in cols else None
        if equipment is False or rooms is False:
            continue
        changed = _set(
            obj, {"name": name, "email": email, "active": active, "max_per_day": mx, "max_per_day_hard": hard}
        )
        obj.save()
        if isinstance(equipment, list) and {e.pk for e in obj.required_equipment.all()} != {e.pk for e in equipment}:
            obj.required_equipment.set(equipment)
            changed = True
        if isinstance(rooms, list) and {r.pk for r in obj.preferred_rooms.all()} != {r.pk for r in rooms}:
            obj.preferred_rooms.set(rooms)
            changed = True
        _upsert(rep, "преподаватели", obj, created, changed)


def _groups(sem: m.Semester, ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(ws, rep, {"Название": "name", "Курс": "course", "Численность": "size"}, ["name", "course", "size"])
    if cols is None:
        return
    courses = {c.number: c for c in sem.courses.all()}
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        name = cell_str(g("name"))
        if not name:
            rep.errors.append(RowError(ws.title, rown, "Название", "пусто"))
            continue
        try:
            num = parse_int(g("course"), lo=1, hi=6, required=True)
            size = parse_int(g("size"), lo=1, hi=500, required=True)
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "", str(e)))
            continue
        course = courses.get(num or 0)
        if course is None:
            rep.errors.append(RowError(ws.title, rown, "Курс", f"в семестре нет {num} курса (добавьте его в семестр)"))
            continue
        obj = m.Group.objects.filter(course__semester=sem, name=name).first()
        created = obj is None
        obj = obj or m.Group(name=name)
        changed = _set(obj, {"course_id": course.pk, "size": size})
        obj.save()
        _upsert(rep, "группы", obj, created, changed)


def _subgroups(sem: m.Semester, ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(
        ws,
        rep,
        {"Группа": "group", "Подгруппа": "name", "Численность": "size", "Деление": "partition"},
        ["group", "name", "size"],
    )
    if cols is None:
        return
    groups = {norm(g.name): g for g in m.Group.objects.filter(course__semester=sem)}
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        grp = groups.get(norm(g("group")))
        if grp is None:
            rep.errors.append(
                RowError(
                    ws.title,
                    rown,
                    "Группа",
                    f"группа «{cell_str(g('group'))}» не найдена."
                    + suggest(cell_str(g("group")), [x.name for x in groups.values()]),
                )
            )
            continue
        try:
            size = parse_int(g("size"), lo=1, hi=500, required=True)
        except ValueError as e:
            rep.errors.append(RowError(ws.title, rown, "Численность", str(e)))
            continue
        name, part = cell_str(g("name")), cell_str(g("partition")) or "lab"
        obj = m.Subgroup.objects.filter(group=grp, partition=part, name=name).first()
        created = obj is None
        obj = obj or m.Subgroup(group=grp, partition=part, name=name)
        changed = _set(obj, {"size": size})
        obj.save()
        _upsert(rep, "подгруппы", obj, created, changed)


def _availability(sem: m.Semester, ws: Worksheet, rep: ImportReport) -> None:
    slots = _slot_titles(sem)
    spec = {"ФИО": "name", "Почта": "email", **{t: sid for t, sid in slots}}
    cols = _cols(ws, rep, spec, ["name"])
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        email = cell_str(g("email")).lower()
        teacher = m.Teacher.objects.filter(email=email).first() if email else None
        if teacher is None:
            same = list(m.Teacher.objects.filter(name=cell_str(g("name"))))
            if len(same) != 1:
                rep.errors.append(
                    RowError(
                        ws.title,
                        rown,
                        "ФИО",
                        "преподаватель не найден" if not same else "ФИО неоднозначно — укажите почту",
                    )
                )
                continue
            teacher = same[0]
        prefs: dict[str, int] = {}
        bad = False
        for title, sid in slots:
            if sid not in cols:
                continue
            try:
                v = parse_int(g(sid), lo=-2, hi=2)
            except ValueError as e:
                rep.errors.append(RowError(ws.title, rown, title, str(e)))
                bad = True
                continue
            if v:
                prefs[sid] = v
        if bad:
            continue
        obj, created = m.TeacherAvailability.objects.get_or_create(teacher=teacher, semester=sem)
        changed = obj.prefs != prefs
        obj.prefs = prefs
        obj.save()
        _upsert(rep, "сетки доступности", obj, created, changed)


def _equipment(ws: Worksheet, rep: ImportReport) -> None:
    cols = _cols(ws, rep, {"Код": "code", "Название": "name"}, ["code", "name"])
    if cols is None:
        return
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        code, name = cell_str(g("code")), cell_str(g("name"))
        if not code or not name:
            rep.errors.append(RowError(ws.title, rown, "", "нужны и код, и название"))
            continue
        obj = m.Equipment.objects.filter(code=code).first()
        created = obj is None
        obj = obj or m.Equipment(code=code)
        changed = _set(obj, {"name": name})
        obj.save()
        _upsert(rep, "оборудование", obj, created, changed)


def _resolve_equipment(value: Any, sheet: str, rown: int, rep: ImportReport) -> list[m.Equipment] | bool:
    """Список оборудования по названиям или кодам; False — была ошибка (строка пропускается)."""
    known = {norm(e.name): e for e in m.Equipment.objects.all()} | {norm(e.code): e for e in m.Equipment.objects.all()}
    out = []
    for x in split_list(value):
        e = known.get(norm(x))
        if e is None:
            rep.errors.append(
                RowError(
                    sheet,
                    rown,
                    "Оборудование",
                    f"«{x}» нет в справочнике оборудования." + suggest(x, [e.name for e in known.values()]),
                )
            )
            return False
        out.append(e)
    return out


def _resolve_rooms(value: Any, sheet: str, rown: int, rep: ImportReport) -> list[m.Room] | bool:
    known = {norm(r.name): r for r in m.Room.objects.all()}
    out = []
    for x in split_list(value):
        r = known.get(norm(x))
        if r is None:
            rep.errors.append(
                RowError(
                    sheet,
                    rown,
                    "Предпочитает аудитории",
                    f"аудитории «{x}» нет." + suggest(x, [r.name for r in known.values()]),
                )
            )
            return False
        out.append(r)
    return out


def _room_busy(sem: m.Semester, ws: Worksheet, rep: ImportReport) -> None:
    slots = _slot_titles(sem)
    cols = _cols(ws, rep, {"Номер": "name", "Кем занята": "reason", **{t: sid for t, sid in slots}}, ["name"])
    if cols is None:
        return
    rooms = {norm(r.name): r for r in m.Room.objects.all()}
    for rown, row in data_rows(ws):
        g = _getter(row, cols)
        room = rooms.get(norm(g("name")))
        if room is None:
            rep.errors.append(
                RowError(
                    ws.title,
                    rown,
                    "Номер",
                    f"аудитории «{cell_str(g('name'))}» нет."
                    + suggest(cell_str(g("name")), [r.name for r in rooms.values()]),
                )
            )
            continue
        blocked = sorted(sid for _, sid in slots if sid in cols and norm(g(sid)) in BUSY_MARKS)
        obj, created = m.RoomAvailability.objects.get_or_create(room=room, semester=sem)
        changed = _set(obj, {"blocked": blocked, "reason": cell_str(g("reason"))[:200]})
        obj.save()
        _upsert(rep, "занятость аудиторий", obj, created, changed)
