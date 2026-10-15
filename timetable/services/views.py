"""Представления готового варианта: сетка группы / преподавателя / аудитории, поиск свободных
аудиторий. Работают по снимку задачи варианта, поэтому не зависят от позднейших правок справочников.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scheduler.domain import CYCLE_WEEKS, DAY_NAMES, Format, Problem, Schedule, Week, weeks_of

BY_CHOICES = {"group": "группа", "teacher": "преподаватель", "room": "аудитория"}


@dataclass
class Entry:
    discipline: str
    teachers: str
    groups: str
    room: str
    marks: list[str] = field(default_factory=list)  # «мигающая», «поток», «онлайн», «бронь», …
    #: (элемент, неделя назначения) — что именно стоит в клетке; нужно интерфейсу для переноса.
    #: В сравнение записей не входит (выгрузки сравнивают нечётную и чётную недели по содержанию).
    lessons: list[tuple[str, str]] = field(default_factory=list, compare=False)


@dataclass
class GridRow:
    pair: int
    time: str
    cells: list[list[Entry]]


def entities(p: Problem, by: str) -> list[tuple[str, str]]:
    if by == "group":
        return sorted(((g.id, g.name) for g in p.groups), key=lambda x: x[1])
    if by == "teacher":
        return sorted(((t.id, t.name) for t in p.teachers), key=lambda x: x[1])
    return sorted(((r.id, r.name) for r in p.rooms), key=lambda x: x[1])


def _matches(p: Problem, item_id: str, room_id: str | None, by: str, key: str) -> bool:
    it = p.item_by_id[item_id]
    if by == "group":
        return key in p.item_groups(it)
    if by == "teacher":
        return key in it.teacher_ids
    return room_id == key


def grid(p: Problem, s: Schedule, by: str, key: str, week: Week) -> list[GridRow]:
    cells: dict[str, list[Entry]] = {}
    for a in s.assignments:
        if week not in weeks_of(a.week) or not _matches(p, a.item_id, a.room_id, by, key):
            continue
        it = p.item_by_id[a.item_id]
        marks = []
        if a.week is not Week.BOTH:
            marks.append("через неделю" if it.is_alt_week else "мигающая")
        if len(it.group_ids) > 1 or it.id in p.merge_of_item:
            marks.append("поток")
        if it.format is Format.ONLINE:
            marks.append("онлайн, бронь" if it.teacher_in_room else "онлайн")
        if it.id in p.block_of_item:
            marks.append(it.label or "блок")
        if a.teacher_remote:
            marks.append("преподаватель удалённо")
        rm = p.room_by_id.get(a.room_id) if a.room_id else None
        if rm and rm.closes_min is not None and p.grid.slot_by_id[a.slot_id].end_min > rm.closes_min:
            hh, mm = divmod(rm.closes_min, 60)
            marks.append(f"до {hh}:{mm:02d}")  # «можно закончить раньше»: площадка закрывается
        room = (
            p.room_by_id[a.room_id].name if a.room_id and a.room_id in p.room_by_id else ("—" if it.needs_room else "")
        )
        groups = ", ".join(sorted(p.group_by_id[g].name for g in p.item_groups(it)))
        e = Entry(
            p.discipline_by_id[it.discipline_id].name,
            ", ".join(p.teacher_by_id[t].name for t in it.teacher_ids if t in p.teacher_by_id),
            groups,
            room,
            marks,
            [(a.item_id, a.week.value)],
        )
        lst = cells.setdefault(a.slot_id, [])
        # объединённые элементы в одной аудитории — одна запись с перечислением групп
        same = next(
            (x for x in lst if x.discipline == e.discipline and x.room == e.room and x.teachers == e.teachers), None
        )
        if same:
            same.lessons += e.lessons
            if same.groups != e.groups:
                same.groups = ", ".join(sorted(set(same.groups.split(", ")) | set(e.groups.split(", "))))
                if "поток" not in same.marks:
                    same.marks.append("поток")
        else:
            lst.append(e)
    rows = []
    for pt in sorted(p.grid.pairs, key=lambda x: x.start_min):
        rows.append(
            GridRow(pt.number, f"{pt.start}–{pt.end}", [cells.get(f"{d}-{pt.number}", []) for d in sorted(p.grid.days)])
        )
    return rows


def day_headers(p: Problem) -> list[str]:
    return [DAY_NAMES[d] for d in sorted(p.grid.days)]


@dataclass
class FreeRoom:
    room_id: str
    name: str
    capacity: int
    type_name: str
    equipment: str
    free_pairs: list[int]  # свободные пары в запрошенном диапазоне
    fully_free: bool


def free_rooms(
    p: Problem,
    s: Schedule,
    day: int,
    pair_from: int,
    pair_to: int,
    weeks: tuple[Week, ...],
    min_capacity: int = 0,
    room_type: str | None = None,
    equipment: set[str] | None = None,
    include_partial: bool = True,
) -> list[FreeRoom]:
    """Аудитории, свободные в день ``day`` на парах ``pair_from..pair_to`` в указанные недели.

    Занятость — назначения варианта плюс слоты «занята не нами»; неактивные аудитории не
    показываются. Сначала полностью свободные, затем частично (с перечнем свободных пар).
    """
    pairs = [x.number for x in sorted(p.grid.pairs, key=lambda x: x.start_min) if pair_from <= x.number <= pair_to]
    busy: set[tuple[str, str, Week]] = set()
    for a in s.assignments:
        if a.room_id:
            for k in weeks_of(a.week):
                busy.add((a.room_id, a.slot_id, k))
    types = {t.id: t.name for t in p.room_types}
    out = []
    for r in p.active_rooms:
        if r.capacity < min_capacity or (room_type and r.type_id != room_type):
            continue
        if equipment and not equipment <= set(r.equipment):
            continue
        free = [
            n for n in pairs if p.room_free(r, f"{day}-{n}") and all((r.id, f"{day}-{n}", k) not in busy for k in weeks)
        ]
        if not free or (not include_partial and len(free) < len(pairs)):
            continue
        out.append(
            FreeRoom(
                r.id,
                r.name,
                r.capacity,
                types.get(r.type_id, r.type_id),
                ", ".join(sorted(r.equipment)),
                free,
                len(free) == len(pairs),
            )
        )
    return sorted(out, key=lambda x: (not x.fully_free, -len(x.free_pairs), x.capacity, x.name))


WEEK_CHOICES = {"odd": (Week.ODD,), "even": (Week.EVEN,), "both": CYCLE_WEEKS}
