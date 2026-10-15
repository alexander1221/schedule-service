"""Этап 2: назначение конкретных аудиторий по слотам.

Каждому занятию, которому нужна аудитория (очно или дистант с бронью), подходит своё
множество аудиторий: тип (если указан), вместимость, оборудование занятия и его
преподавателей, аудитория не занята «не нами» в этом слоте; закреплённая аудитория —
единственный вариант. Этап 1 уже гарантировал условие Холла для «узких» наборов,
так что обычно назначение существует.

Еженедельная пара стоит в ОДНОЙ аудитории обе недели. Мигающие пары нечётной и чётной
недели склеиваются попарно (если у них есть общие подходящие аудитории) и делят одну
аудиторию. Внутри слота — задача о назначениях (OR-Tools linear_sum_assignment, точно
и быстро). Если склейка мешает — точная маленькая CP-SAT-модель без склейки.

Стоимость: смена аудитории группой относительно предыдущей пары дня, занятие не в
предпочтительной аудитории преподавателя, при пересборке — аудитория не та, что в эталоне у того
же занятия в то же время (веса — из профиля), и немного — лишние места.

Если и точная модель не находит назначения, этап 2 сообщает этапу 1, что именно мешает:
* по какой-то неделе аудиторий не хватает → узкий набор аудиторий N(U′) из условия Холла
  (``RoomAssignmentError.cut``) — решатель ограничит его во всех слотах;
* по неделям хватает, но еженедельное занятие не может занять одну аудиторию обе недели →
  запрет этого сочетания занятий в слоте (``RoomAssignmentError.nogood``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ortools.graph.python import linear_sum_assignment as lsa
from ortools.sat.python import cp_model

from ..criteria import Crit, WeightProfile
from ..domain import CYCLE_WEEKS, Assignment, Format, Problem, Schedule, Week, weeks_of
from ..matching import max_matching

UNIT_COST = 1000  # цена одной единицы критерия этапа 2 при весе 1 (места — по 1)


class RoomAssignmentError(RuntimeError):
    def __init__(
        self,
        message: str,
        slot_id: str,
        cut: frozenset[str] | None = None,
        nogood: list[tuple[str, Week]] | None = None,
    ):
        super().__init__(message)
        self.slot_id = slot_id
        self.cut = cut
        self.nogood = nogood


@dataclass(frozen=True)
class _Costs:
    stab: int  # смена аудитории группой между соседними парами (за группу и неделю)
    pref: int  # не в предпочтительной аудитории преподавателя (за событие и неделю)
    ref: int  # не та аудитория, что в эталоне (за элемент и неделю) — как Crit.ROOM_REF в валидаторе
    ref_room: dict[tuple[str, Week, str], str]  # (элемент, неделя, слот) → аудитория эталона


@dataclass
class _Entity:
    """То, что займёт одну аудиторию в слоте: событие или склейка мигающих."""

    parts: list[list[Assignment]]  # каждое событие — список назначений (объединение = несколько)
    weeks: list[tuple[Week, ...]]
    eligible: frozenset[str]
    size: int
    groups: list[set[str]]
    preferred: list[frozenset[str]]  # предпочтительные аудитории преподавателей (пусто — без предпочтений)
    room: str | None = field(default=None)


def assign_rooms(
    problem: Problem,
    assignments: list[Assignment],
    profile: WeightProfile | None = None,
    reference: Schedule | None = None,
) -> list[Assignment]:
    """Возвращает копии назначений с заполненными room_id для очных занятий и дистанта с бронью.

    ``reference`` — эталон пересборки: занятие, оставшееся в то же время, держится в своей прежней
    аудитории, если она подходит и свободна."""
    p = problem
    prof = profile or WeightProfile()
    ref_room: dict[tuple[str, Week, str], str] = {}
    for a in reference.assignments if reference is not None else []:
        if a.room_id:
            for k in weeks_of(a.week):
                ref_room[(a.item_id, k, a.slot_id)] = a.room_id
    costs = _Costs(
        prof.weight(Crit.ROOM_CHANGES) * UNIT_COST,
        prof.weight(Crit.ROOM_PREF) * UNIT_COST,
        prof.weight(Crit.ROOM_REF) * UNIT_COST,
        ref_room,
    )
    pin_room = {(pin.item_id, pin.week, pin.slot_id): pin.room_id for pin in p.pins if pin.room_id}
    out = [Assignment(a.item_id, a.week, a.slot_id, None, a.teacher_remote) for a in assignments]
    by_slot: dict[str, list[Assignment]] = defaultdict(list)
    for a in out:
        if p.item_by_id[a.item_id].needs_room:
            by_slot[a.slot_id].append(a)

    # прошлая аудитория группы в предыдущей паре дня: (группа, неделя) -> room
    for day in sorted(p.grid.days):
        prev: dict[tuple[str, Week], str] = {}
        for s in p.grid.day_slots(day):
            slot_as = by_slot.get(s.id, [])
            _solve_slot(p, s.id, slot_as, pin_room, prev, costs)
            cur: dict[tuple[str, Week], str] = {}
            for a in slot_as:
                assert a.room_id is not None
                if p.item_by_id[a.item_id].format is not Format.IN_PERSON:
                    continue  # дистант с бронью: студенты дома, для стабильности аудитории группы не в счёт
                for g in p.item_groups(p.item_by_id[a.item_id]):
                    for k in weeks_of(a.week):
                        cur[(g, k)] = a.room_id
            prev = cur
    return out


def _entities(
    p: Problem,
    sid: str,
    slot_as: list[Assignment],
    pin_room: dict,
    pair: bool = True,
    ref_room: dict[tuple[str, Week, str], str] | None = None,
) -> list[_Entity]:
    # события: объединённые элементы в одном слоте/неделе -> одно занятие
    ev: dict[tuple[str, Week], list[Assignment]] = defaultdict(list)
    for a in slot_as:
        m = p.merge_of_item.get(a.item_id)
        ev[(m.id if m else "i:" + a.item_id, a.week)].append(a)
    weekly: list[_Entity] = []
    blink: dict[Week, list[_Entity]] = defaultdict(list)
    for (_, wk), part in ev.items():
        items = [p.item_by_id[a.item_id] for a in part]
        pinned = next(
            (pin_room[(a.item_id, a.week, a.slot_id)] for a in part if (a.item_id, a.week, a.slot_id) in pin_room), None
        )
        if pinned is not None:
            eligible = frozenset({pinned})
        else:
            eligible = frozenset(r.id for r in p.eligible_rooms(items) if p.room_free(r, sid, p.late_ok(items)))
        groups = set().union(*(p.item_groups(it) for it in items if it.format is Format.IN_PERSON))
        pref = frozenset(r for it in items for t in it.teacher_ids for r in p.teacher_by_id[t].preferred_rooms)
        e = _Entity([part], [weeks_of(wk)], eligible, p.room_need(items), [groups], [pref])
        if wk is Week.BOTH or not pair:
            weekly.append(e)
        else:
            blink[wk].append(e)
    ents = list(weekly)
    odd = sorted(blink.get(Week.ODD, []), key=lambda e: (len(e.eligible), -e.size))
    even = sorted(blink.get(Week.EVEN, []), key=lambda e: (len(e.eligible), -e.size))
    ref_room = ref_room or {}

    def was_in(e: _Entity, k: Week) -> str | None:  # аудитория мигающего события в эталоне
        return next((r for x in e.parts[0] if (r := ref_room.get((x.item_id, k, sid)))), None)

    def glue(a: _Entity, b: _Entity) -> _Entity:
        return _Entity(
            a.parts + b.parts,
            a.weeks + b.weeks,
            a.eligible & b.eligible,
            max(a.size, b.size),
            a.groups + b.groups,
            a.preferred + b.preferred,
        )

    # 1) пересборка: пары, делившие аудиторию в эталоне (если она подходит обеим), склеиваем первыми —
    #    иначе их партнёров заберут другие и прежние аудитории не сохранить
    if ref_room:
        for a in list(odd):
            ra = was_in(a, Week.ODD)
            b = next((b for b in even if ra and was_in(b, Week.EVEN) == ra and ra in a.eligible & b.eligible), None)
            if b is not None:
                odd.remove(a)
                even.remove(b)
                ents.append(glue(a, b))
    # 2) остальные — с чётной, у которой больше всего общих подходящих аудиторий
    for a in odd:
        best = max(even, key=lambda b: len(a.eligible & b.eligible), default=None)
        if best is not None and a.eligible & best.eligible:
            even.remove(best)
            ents.append(glue(a, best))
        else:
            ents.append(a)
    ents += even
    return ents


def _cost(p: Problem, e: _Entity, room_id: str, prev: dict[tuple[str, Week], str], costs: _Costs) -> int:
    c = abs(p.room_by_id[room_id].capacity - e.size)  # лишние (или недостающие) места
    for part, gs, wk, pref in zip(e.parts, e.groups, e.weeks, e.preferred, strict=True):
        for k in wk:
            for g in gs:
                r = prev.get((g, k))
                if r is not None and r != room_id:
                    c += costs.stab
            if pref and room_id not in pref:
                c += costs.pref
            if costs.ref_room:
                for x in part:
                    r = costs.ref_room.get((x.item_id, k, x.slot_id))
                    if r is not None and r != room_id:
                        c += costs.ref
    return c


def _solve_slot(
    p: Problem,
    sid: str,
    slot_as: list[Assignment],
    pin_room: dict,
    prev: dict[tuple[str, Week], str],
    costs: _Costs,
) -> None:
    if not slot_as:
        return
    ents = _entities(p, sid, slot_as, pin_room, ref_room=costs.ref_room)
    rooms = sorted({r for e in ents for r in e.eligible})
    n = len(rooms)
    if len(ents) <= n and all(e.eligible for e in ents):
        a = lsa.SimpleLinearSumAssignment()
        for i, e in enumerate(ents):
            for j, rid in enumerate(rooms):
                if rid in e.eligible:
                    a.add_arc_with_cost(i, j, _cost(p, e, rid, prev, costs))
        # фиктивные строки: OR-Tools решает только квадратную задачу (иначе INFEASIBLE),
        # а right_mate() после неуспешного solve() роняет процесс — зовём его только при OPTIMAL
        for i in range(len(ents), n):
            for j in range(n):
                a.add_arc_with_cost(i, j, 0)
        if a.solve() == a.OPTIMAL:
            for i, e in enumerate(ents):
                for part in e.parts:
                    for x in part:
                        x.room_id = rooms[a.right_mate(i)]
            return
    # запасной путь: без склейки мигающих, точная модель сама решит, кто с кем делит аудиторию
    singles = _entities(p, sid, slot_as, pin_room, pair=False)
    if _solve_slot_cpsat(p, singles, prev, costs):
        for e in singles:
            for x in e.parts[0]:
                x.room_id = e.room
        return
    raise _diagnose(p, sid, singles)


def _solve_slot_cpsat(p: Problem, ents: list[_Entity], prev: dict[tuple[str, Week], str], costs: _Costs) -> bool:
    """Точная модель слота: еженедельное — одна аудитория обе недели, мигающие делят по неделям."""
    if any(not e.eligible for e in ents):
        return False
    m = cp_model.CpModel()
    x: dict[tuple[int, str], cp_model.IntVar] = {}
    for i, e in enumerate(ents):
        for r in e.eligible:
            x[(i, r)] = m.new_bool_var("")
        m.add_exactly_one(x[(i, r)] for r in e.eligible)
    for r in {r for e in ents for r in e.eligible}:
        for k in CYCLE_WEEKS:
            terms = [x[(i, r)] for i, e in enumerate(ents) if (i, r) in x and any(k in w for w in e.weeks)]
            if len(terms) > 1:
                m.add(sum(terms) <= 1)
    m.minimize(sum(_cost(p, ents[i], r, prev, costs) * v for (i, r), v in x.items()))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = 10
    s.parameters.num_workers = 4
    if s.solve(m) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return False
    for (i, r), v in x.items():
        if s.value(v):
            ents[i].room = r
    return True


def _diagnose(p: Problem, sid: str, ents: list[_Entity]) -> RoomAssignmentError:
    sl = p.grid.slot_by_id[sid]
    where = f"слот {sl.day}-{sl.pair}"
    for k in CYCLE_WEEKS:
        active = [e for e in ents if any(k in w for w in e.weeks)]
        res = max_matching([sorted(e.eligible) for e in active])
        if not res.perfect:
            return RoomAssignmentError(
                f"{where}, неделя {k.value}: {len(res.deficient)} занятиям подходят только "
                f"{len(res.neighbourhood)} аудитории",
                sid,
                cut=res.neighbourhood,
            )
    present = [(a.item_id, a.week) for e in ents for part in e.parts for a in part]
    return RoomAssignmentError(
        f"{where}: еженедельные и мигающие занятия не делятся по аудиториям", sid, nogood=present
    )
