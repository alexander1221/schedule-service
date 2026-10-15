"""Сравнение двух расписаний: что изменилось и кого это затронуло.

Нужно диспетчеру после пересборки («сдвинулись 3 пары у 2КП1, аудитория та же») и агенту
этапа 5б. Элементы сопоставляются по id (в БД — первичный ключ элемента нагрузки), поэтому
сравнивать имеет смысл варианты одного семестра. Каждая задача — из снимка своего варианта:
названия берутся из нового снимка, а для исчезнувших элементов — из старого.

Мигающие пары разворачиваются по неделям цикла: «каждую неделю» = нечётная + чётная.
Перенос — неделя и слот занятия у элемента были, а стали другими; смена аудитории — тот же
слот и неделя, но другая аудитория.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .domain import DAY_NAMES, Problem, Schedule, Week, weeks_of

#: (неделя, слот, аудитория или None)
_Key = tuple[Week, str, str | None]


@dataclass(frozen=True)
class Placement:
    week: str  # odd | even
    day: int
    pair: int
    room: str  # название аудитории, «» — без аудитории

    def label(self) -> str:
        w = "нечёт" if self.week == Week.ODD.value else "чёт"
        return f"{DAY_NAMES.get(self.day, self.day)} {self.pair} пара ({w})" + (f", {self.room}" if self.room else "")


@dataclass
class ItemChange:
    item_id: str
    discipline: str
    groups: list[str]
    teachers: list[str]
    #: «перенос» | «аудитория» | «добавлено» | «убрано»
    kind: str
    before: list[Placement]
    after: list[Placement]


@dataclass
class ScheduleDiff:
    changes: list[ItemChange] = field(default_factory=list)
    #: занятий (по неделям цикла), которые стоят в другое время
    moved: int = 0
    #: занятий в то же время, но в другой аудитории
    room_changed: int = 0
    affected_groups: list[str] = field(default_factory=list)
    affected_teachers: list[str] = field(default_factory=list)
    affected_rooms: list[str] = field(default_factory=list)

    @property
    def same(self) -> bool:
        return not self.changes


def _expand(s: Schedule) -> dict[str, Counter[_Key]]:
    out: dict[str, Counter[_Key]] = {}
    for a in s.assignments:
        c = out.setdefault(a.item_id, Counter())
        for k in weeks_of(a.week):
            c[(k, a.slot_id, a.room_id)] += 1
    return out


def _rooms_at(c: Counter[_Key], week: Week, sid: str) -> Counter[str | None]:
    return Counter({r: n for (k, s, r), n in c.items() if (k, s) == (week, sid)})


def _placements(p: Problem, keys: Counter[_Key]) -> list[Placement]:
    res = []
    for (k, sid, rid), n in keys.items():
        sl = p.grid.slot_by_id.get(sid)
        if sl:
            day, pair = sl.day, sl.pair
        else:  # слота нет в сетке снимка — берём из id «день-пара»
            d_, _, p_ = sid.partition("-")
            day, pair = (int(d_), int(p_)) if d_.isdigit() and p_.isdigit() else (0, 0)
        room = p.room_by_id[rid].name if rid and rid in p.room_by_id else (rid or "")
        res += [Placement(k.value, day, pair, room)] * n
    return sorted(res, key=lambda x: (x.week, x.day, x.pair, x.room))


def diff_schedules(old_p: Problem, old_s: Schedule, new_p: Problem, new_s: Schedule) -> ScheduleDiff:
    old, new = _expand(old_s), _expand(new_s)
    d = ScheduleDiff()
    groups: set[str] = set()
    teachers: set[str] = set()
    rooms: set[str] = set()
    for iid in sorted(set(old) | set(new), key=lambda x: (len(x), x)):
        a, b = old.get(iid, Counter()), new.get(iid, Counter())
        if a == b:
            continue
        p = new_p if iid in new_p.item_by_id else old_p
        it = p.item_by_id.get(iid)
        times_a = Counter((k, sid) for (k, sid, _), n in a.items() for _ in range(n))
        times_b = Counter((k, sid) for (k, sid, _), n in b.items() for _ in range(n))
        moved = sum((times_b - times_a).values())
        if not a:
            kind = "добавлено"
        elif not b:
            kind = "убрано"
        else:
            d.moved += moved
            for k, sid in times_a & times_b:  # то же время — сравниваем аудитории
                if _rooms_at(a, k, sid) != _rooms_at(b, k, sid):
                    d.room_changed += 1
            kind = "перенос" if moved else "аудитория"
        if it is None:
            continue  # элемента нет ни в одном снимке — не бывает при сравнении вариантов одной задачи
        g_names = sorted(p.group_by_id[g].name for g in p.item_groups(it) if g in p.group_by_id)
        t_names = sorted(p.teacher_by_id[t].name for t in it.teacher_ids if t in p.teacher_by_id)
        disc = p.discipline_by_id[it.discipline_id].name if it.discipline_id in p.discipline_by_id else it.id
        before, after = _placements(old_p, a), _placements(new_p, b)
        d.changes.append(ItemChange(iid, disc, g_names, t_names, kind, before, after))
        groups.update(g_names)
        teachers.update(t_names)
        rooms.update(x.room for x in [*before, *after] if x.room and (x not in before or x not in after))
    d.affected_groups, d.affected_teachers, d.affected_rooms = sorted(groups), sorted(teachers), sorted(rooms)
    return d
