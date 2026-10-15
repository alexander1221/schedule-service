"""Независимый валидатор расписания.

Проверяет ЛЮБОЕ расписание — сгенерированное решателем или исправленное вручную —
на все жёсткие ограничения и считает мягкие штрафы по критериям.

Важно: модуль НЕ импортирует решатель и не использует его вспомогательные структуры.
Логика написана заново «в лоб», чтобы ошибка в модели CP-SAT не могла
«подтвердить сама себя». Тесты сверяют штраф решателя со штрафом валидатора.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .criteria import LABELS, Crit, WeightProfile
from .domain import (
    CYCLE_WEEKS,
    DAY_NAMES,
    PREF_FORBIDDEN,
    Assignment,
    Format,
    Problem,
    Schedule,
    Slot,
    Week,
    weeks_of,
)

WEEK_RU = {Week.ODD: "нечёт.", Week.EVEN: "чёт.", Week.BOTH: "еженед."}


@dataclass
class Violation:
    code: str
    message: str


@dataclass
class Issue:
    """Мягкое нарушение для «сводки проблем варианта»."""

    crit: Crit
    amount: int
    message: str


@dataclass
class Report:
    hard: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    breakdown: dict[Crit, int] = field(default_factory=lambda: {c: 0 for c in Crit})
    issues: list[Issue] = field(default_factory=list)
    total: int = 0

    @property
    def ok(self) -> bool:
        return not self.hard

    def summary(self, limit: int = 15) -> str:
        lines = [f"Жёстких нарушений: {len(self.hard)}; предупреждений: {len(self.warnings)}; штраф: {self.total}"]
        for v in self.hard[:limit]:
            lines.append(f"  ✗ [{v.code}] {v.message}")
        if len(self.hard) > limit:
            lines.append(f"  … и ещё {len(self.hard) - limit}")
        for c, v in self.breakdown.items():
            if v:
                lines.append(f"  {LABELS[c]}: {v}")
        return "\n".join(lines)


@dataclass
class _Event:
    """Фактическое занятие в (неделя, слот): одно или несколько объединённых вхождений."""

    item_ids: list[str]
    room_id: str | None
    online: bool  # для студентов — онлайн (форматные правила)
    needs_room: bool = False  # нужна аудитория: очно или дистант с бронью для преподавателя


def _sl(p: Problem, sid: str) -> str:
    s = p.grid.slot_by_id[sid]
    return f"{DAY_NAMES[s.day]} {s.pair} пара"


def validate(
    problem: Problem,
    schedule: Schedule,
    profile: WeightProfile | None = None,
    reference: Schedule | None = None,
) -> Report:
    p = problem
    prof = profile or WeightProfile()
    rep = Report()
    grid = p.grid
    hard = rep.hard.append

    # ------------------------------------------------ ссылки
    good: list[Assignment] = []
    for a in schedule.assignments:
        if a.item_id not in p.item_by_id:
            hard(Violation("unknown_item", f"Неизвестный элемент нагрузки {a.item_id}"))
        elif a.slot_id not in grid.slot_by_id:
            hard(Violation("unknown_slot", f"Неизвестный слот {a.slot_id} у {a.item_id}"))
        elif a.room_id is not None and a.room_id not in p.room_by_id:
            hard(Violation("unknown_room", f"Неизвестная аудитория {a.room_id} у {a.item_id}"))
        else:
            good.append(a)

    def name(iid: str) -> str:
        it = p.item_by_id[iid]
        groups = ", ".join(sorted(p.group_by_id[g].name for g in p.item_groups(it))) or "блок"
        return f"{p.discipline_by_id[it.discipline_id].name} ({groups})"

    # ------------------------------------------------ количество вхождений
    occ: dict[str, list[Assignment]] = defaultdict(list)
    for a in good:
        occ[a.item_id].append(a)
    for it in p.items:
        got = occ.get(it.id, [])
        n = sum(len(weeks_of(a.week)) for a in got)
        if n != it.pairs_per_cycle:
            hard(Violation("count", f"{name(it.id)}: размещено {n} пар за цикл вместо {it.pairs_per_cycle}"))
        both = sum(1 for a in got if a.week is Week.BOTH)
        if n == it.pairs_per_cycle and both != it.weekly_count:
            rep.warnings.append(
                Violation(
                    "structure", f"{name(it.id)}: {both} еженедельных вместо {it.weekly_count} (остальное — мигающие)"
                )
            )
        seen = Counter((a.slot_id, k) for a in got for k in weeks_of(a.week))
        for (sid, k), c in seen.items():
            if c > 1:
                hard(Violation("dup", f"{name(it.id)}: {c} раза в {_sl(p, sid)} ({WEEK_RU[k]})"))
        if it.is_alt_week and got:
            wks = {a.week for a in got}
            if Week.BOTH in wks or len(wks) > 1:
                hard(Violation("alt_week", f"{name(it.id)}: «через неделю» — все пары должны быть в одну неделю цикла"))
        if it.consecutive:
            # каждое вхождение — рядом с другим вхождением того же элемента в тот же день и ту же неделю
            for a in got:
                if not any(b.week is a.week and _adjacent(p, a.slot_id, b.slot_id) for b in got):
                    kind = "через неделю" if it.is_alt_week else "сдвоенная"
                    hard(Violation("consecutive", f"{name(it.id)} ({kind}): {_sl(p, a.slot_id)} без пары рядом"))

    # ------------------------------------------------ события по (неделя, слот)
    events: dict[tuple[Week, str], list[_Event]] = defaultdict(list)
    for k in CYCLE_WEEKS:
        bucket: dict[tuple[str, str | None, str], list[str]] = defaultdict(list)
        for a in good:
            if k not in weeks_of(a.week):
                continue
            m = p.merge_of_item.get(a.item_id)
            key = (m.id if m else "item:" + a.item_id, a.room_id, a.slot_id)
            bucket[key].append(a.item_id)
        for (_, room, sid), iids in bucket.items():
            online = all(p.item_by_id[i].format is Format.ONLINE for i in iids)
            needs_room = any(p.item_by_id[i].needs_room for i in iids)
            events[(k, sid)].append(_Event(sorted(set(iids)), room, online, needs_room))

    used_merges: set[str] = set()
    for evs in events.values():
        for e in evs:
            if len(e.item_ids) > 1:
                used_merges.add(p.merge_of_item[e.item_ids[0]].id)

    # ------------------------------------------------ аудитории
    for (k, sid), evs in events.items():
        for e in evs:
            items = [p.item_by_id[i] for i in e.item_ids]
            fmts = {(it.format, it.teacher_in_room) for it in items}
            if len(fmts) > 1:
                hard(
                    Violation("merge_format", f"Объединены занятия разного формата: {', '.join(map(name, e.item_ids))}")
                )
            if e.needs_room:
                if e.room_id is None:
                    hard(Violation("no_room", f"{name(e.item_ids[0])}: нет аудитории в {_sl(p, sid)} ({WEEK_RU[k]})"))
                    continue
                room = p.room_by_id[e.room_id]
                need = p.room_need(items)
                if not room.active:
                    hard(Violation("room_inactive", f"Аудитория {room.name} неактивна"))
                for it in items:
                    if it.room_type_id and it.room_type_id != room.type_id:
                        hard(Violation("room_type", f"{name(it.id)}: аудитория {room.name} неподходящего типа"))
                missing = p.required_equipment(items) - set(room.equipment)
                if missing:
                    hard(
                        Violation(
                            "room_equipment",
                            f"{name(e.item_ids[0])}: в {room.name} нет оборудования: {', '.join(sorted(missing))}",
                        )
                    )
                if sid in room.blocked:
                    hard(
                        Violation(
                            "room_blocked",
                            f"Аудитория {room.name} занята не нами в {_sl(p, sid)} — ставить в неё нельзя",
                        )
                    )
                if room.closes_min is not None:
                    # независимо от domain.room_closed: пара должна закончиться до закрытия площадки,
                    # а «можно закончить раньше» — хотя бы начаться до него
                    sl = grid.slot_by_id[sid]
                    early = all(it.may_end_early for it in items)
                    if (sl.start_min if early else sl.end_min) > room.closes_min - (1 if early else 0):
                        hh, mm = divmod(room.closes_min, 60)
                        hard(
                            Violation(
                                "site_closed",
                                f"{name(e.item_ids[0])}: {_sl(p, sid)} в {room.name} — площадка закрывается "
                                f"в {hh}:{mm:02d}, а пара заканчивается позже"
                                + ("" if early else " (если занятие можно закончить раньше — отметьте это у него)"),
                            )
                        )
                pref = {
                    r
                    for i in e.item_ids
                    for t in p.item_by_id[i].teacher_ids
                    for r in p.teacher_by_id[t].preferred_rooms
                }
                if pref and room.id not in pref:
                    rep.breakdown[Crit.ROOM_PREF] += 1
                    rep.issues.append(
                        Issue(
                            Crit.ROOM_PREF,
                            1,
                            f"{name(e.item_ids[0])}: {room.name} — не из "
                            f"предпочтительных аудиторий преподавателя ({WEEK_RU[k]})",
                        )
                    )
                if not p.fits(room, need):
                    hard(
                        Violation(
                            "room_capacity", f"{name(e.item_ids[0])}: в {room.name} {room.capacity} мест, нужно {need}"
                        )
                    )
            elif e.room_id is not None:
                rep.warnings.append(
                    Violation("online_room", f"{name(e.item_ids[0])}: онлайн-занятию назначена аудитория")
                )
        rooms = Counter(e.room_id for e in evs if e.room_id is not None and e.needs_room)
        for r, c in rooms.items():
            if c > 1:
                hard(
                    Violation(
                        "room_conflict", f"Аудитория {p.room_by_id[r].name}: {c} занятия в {_sl(p, sid)} ({WEEK_RU[k]})"
                    )
                )

    # ------------------------------------------------ преподаватели
    t_busy: dict[tuple[str, Week, str], int] = Counter()
    t_onsite: set[tuple[str, Week, str]] = set()  # преподаватель на площадке (очно или дистант из аудитории)
    for (k, sid), evs in events.items():
        for e in evs:
            teachers = {t for i in e.item_ids for t in p.item_by_id[i].teacher_ids}
            for t in teachers:
                t_busy[(t, k, sid)] += 1
                if e.needs_room:
                    t_onsite.add((t, k, sid))
    for (t, k, sid), c in t_busy.items():
        tn = p.teacher_by_id[t].name
        if c > 1:
            hard(Violation("teacher_conflict", f"{tn}: {c} занятия в {_sl(p, sid)} ({WEEK_RU[k]})"))
        if p.teacher_pref(t, sid) <= PREF_FORBIDDEN:
            hard(Violation("teacher_forbidden", f"{tn} поставлен в {_sl(p, sid)}, где отмечено «не могу»"))

    # ------------------------------------------------ группы и подгруппы (по «клеткам»)
    # cell_occ[(g, cell, k, sid)] = кто занимает клетку; все занятия одного
    # синхронного блока — один «занимающий» (группы распущены по языковым подгруппам).
    cell_occ: dict[tuple[str, tuple[str, ...], Week, str], set[str]] = defaultdict(set)
    g_busy: dict[tuple[str, Week, str], list[_Event]] = defaultdict(list)
    for (k, sid), evs in events.items():
        for n_ev, e in enumerate(evs):
            blk = p.block_of_item.get(e.item_ids[0])
            who = f"block:{blk.id}" if blk else f"ev:{n_ev}"
            touched: set[tuple[str, tuple[str, ...]]] = set()
            for i in e.item_ids:
                it = p.item_by_id[i]
                whole, subs = p.occupancy(it)
                for g in whole:
                    touched |= {(g, c) for c in p.group_cells(g)}
                for s in subs:
                    g = p.subgroup_by_id[s].group_id
                    touched |= {(g, c) for c in p.group_cells(g) if s in c}
            for g, c in touched:
                cell_occ[(g, c, k, sid)].add(who)
            for g in {g for g, _ in touched}:
                g_busy[(g, k, sid)].append(e)
    cell_busy = {key: len(v) for key, v in cell_occ.items()}
    reported: set[tuple[str, Week, str]] = set()
    for (g, _c, k, sid), cnt in cell_busy.items():
        if cnt > 1 and (g, k, sid) not in reported:
            reported.add((g, k, sid))
            hard(
                Violation("group_conflict", f"Группа {p.group_by_id[g].name}: накладка в {_sl(p, sid)} ({WEEK_RU[k]})")
            )
    for g, sid in sorted({(g, sid) for g, _k, sid in g_busy}):
        if p.group_pref(g, sid) <= PREF_FORBIDDEN:
            hard(Violation("group_forbidden", f"Группа {p.group_by_id[g].name} поставлена в запрещённый {_sl(p, sid)}"))

    # ------------------------------------------------ синхронные блоки
    for b in p.blocks:
        pats = {}
        for iid in b.item_ids:
            pats[iid] = sorted((a.slot_id, k.value) for a in occ.get(iid, []) for k in weeks_of(a.week))
        if len({tuple(v) for v in pats.values()}) > 1:
            hard(Violation("block_sync", f"Блок «{b.name}»: занятия стоят в разных слотах"))

    # ------------------------------------------------ закрепления
    for pin in p.pins:
        hit = [a for a in occ.get(pin.item_id, []) if a.week is pin.week and a.slot_id == pin.slot_id]
        if not hit:
            hard(Violation("pin", f"Закрепление {name(pin.item_id)} → {_sl(p, pin.slot_id)} не соблюдено"))
        elif pin.room_id and all(a.room_id != pin.room_id for a in hit):
            hard(Violation("pin_room", f"Закрепление {name(pin.item_id)}: не та аудитория"))

    # ------------------------------------------------ мягкие критерии
    br = rep.breakdown
    cost = prof.pref_cost
    issue = rep.issues.append

    def add(c: Crit, v: int, msg: str) -> None:
        if v:
            br[c] += v
            issue(Issue(c, v, msg))

    # предпочтения преподавателей
    for (t, k, sid), _c in t_busy.items():
        pv = p.teacher_pref(t, sid)
        if pv > PREF_FORBIDDEN:
            br[Crit.TEACHER_PREF] += cost.get(pv, 0)
            if pv == -1:
                issue(
                    Issue(
                        Crit.TEACHER_PREF,
                        cost.get(-1, 0),
                        f"{p.teacher_by_id[t].name}: «нежелательно» {_sl(p, sid)} ({WEEK_RU[k]})",
                    )
                )

    days = sorted(grid.days)
    day_slots: dict[int, list[Slot]] = {d: grid.day_slots(d) for d in days}

    # группы
    for g in p.groups:
        gname = g.name
        course = p.course_by_num.get(g.course)
        lim = course.limits if course else None
        for k in CYCLE_WEEKS:
            for d in days:
                ds = day_slots[d]
                busy = [bool(g_busy.get((g.id, k, s.id))) for s in ds]
                f_in = [any(not e.online for e in g_busy.get((g.id, k, s.id), [])) for s in ds]
                f_on = [any(e.online for e in g_busy.get((g.id, k, s.id), [])) for s in ds]
                where = f"группа {gname}, {DAY_NAMES[d]} ({WEEK_RU[k]})"
                for s, b in zip(ds, busy, strict=True):
                    if b:
                        pv = p.group_pref(g.id, s.id)
                        if pv > PREF_FORBIDDEN:
                            br[Crit.GROUP_PREF] += cost.get(pv, 0)
                idx = [i for i, b in enumerate(busy) if b]
                cnt = len(idx)
                if idx:
                    add(Crit.GROUP_GAPS, (idx[-1] - idx[0] + 1) - cnt, f"Окна: {where}")
                # очно -> онлайн без разрыва
                n_bad = 0
                for qi, q in enumerate(ds):
                    if f_on[qi] and any(
                        f_in[pi]
                        and ds[pi].start_min < q.start_min
                        and q.start_min - ds[pi].end_min < p.policy.format_gap_min
                        for pi in range(len(ds))
                    ):
                        n_bad += 1
                add(Crit.FMT_IN_TO_ONLINE, n_bad, f"Очно → онлайн без окна: {where}")
                o2i = any(
                    f_on[a] and f_in[b] and ds[a].start_min < ds[b].start_min
                    for a in range(len(ds))
                    for b in range(len(ds))
                )
                add(Crit.FMT_ONLINE_TO_IN, int(o2i), f"Онлайн → очно: {where}")
                add(Crit.FMT_MIXED_DAY, int(any(f_in) and any(f_on)), f"Смешанный формат: {where}")
                if lim:
                    if lim.max_per_day is not None and cnt > lim.max_per_day:
                        if lim.max_per_day_hard:
                            hard(Violation("course_max_day", f"{where}: {cnt} пар при максимуме {lim.max_per_day}"))
                        else:
                            add(Crit.COURSE_MAX_DAY, cnt - lim.max_per_day, f"{cnt} пар в день: {where}")
                    if lim.min_per_day is not None and 0 < cnt < lim.min_per_day:
                        if lim.min_per_day_hard:
                            hard(Violation("course_min_day", f"{where}: {cnt} пар при минимуме {lim.min_per_day}"))
                        else:
                            add(Crit.COURSE_MIN_DAY, lim.min_per_day - cnt, f"Всего {cnt} пар в день: {where}")
                    if lim.max_consecutive is not None:
                        m = lim.max_consecutive
                        ex = sum(max(0, sum(busy[i : i + m + 1]) - m) for i in range(len(ds) - m))
                        if ex and lim.max_consecutive_hard:
                            hard(Violation("course_consec", f"{where}: больше {m} пар подряд"))
                        elif ex:
                            add(Crit.COURSE_MAX_CONSEC, ex, f"Больше {m} пар подряд: {where}")
                # тяжёлые подряд
                hm = p.policy.heavy_max_consecutive
                heavy = [
                    any(p.is_heavy(p.item_by_id[i]) for e in g_busy.get((g.id, k, s.id), []) for i in e.item_ids)
                    for s in ds
                ]
                add(
                    Crit.HEAVY_CONSEC,
                    sum(max(0, sum(heavy[i : i + hm + 1]) - hm) for i in range(len(ds) - hm)),
                    f"Тяжёлые пары подряд: {where}",
                )
                # дисциплина в один день — по клеткам студентов, берётся худшая клетка
                per_disc: dict[str, int] = {}
                for cell in p.group_cells(g.id):
                    cnts: Counter[str] = Counter()
                    for s in ds:
                        if not cell_busy.get((g.id, cell, k, s.id)):
                            continue
                        discs = set()
                        for e in g_busy.get((g.id, k, s.id), []):
                            for i in e.item_ids:
                                it = p.item_by_id[i]
                                whole, subs = p.occupancy(it)
                                if g.id in whole or any(sb in cell for sb in subs):
                                    discs.add(it.discipline_id)
                        cnts.update(discs)
                    for dsc, c in cnts.items():
                        allowed = _disc_allowed(p, g.id, dsc)
                        per_disc[dsc] = max(per_disc.get(dsc, 0), max(0, c - allowed))
                for dsc, ex in per_disc.items():
                    add(Crit.DISCIPLINE_SPREAD, ex, f"{p.discipline_by_id[dsc].name} скоплена: {where}")
                # стабильность аудитории (этап 2)
                for i in range(len(ds) - 1):
                    r1 = _single_room(g_busy.get((g.id, k, ds[i].id), []))
                    r2 = _single_room(g_busy.get((g.id, k, ds[i + 1].id), []))
                    if r1 and r2 and r1 != r2:
                        br[Crit.ROOM_CHANGES] += 1

    # преподаватели: окна, лимит в день, «приезд ради одной пары»
    t_min = p.policy.teacher_min_per_day
    for t in p.teachers:
        limit, limit_hard = p.teacher_limit(t.id)
        for k in CYCLE_WEEKS:
            for d in days:
                ds = day_slots[d]
                idx = [i for i, s in enumerate(ds) if t_busy.get((t.id, k, s.id))]
                where = f"{t.name}, {DAY_NAMES[d]} ({WEEK_RU[k]})"
                if idx:
                    add(Crit.TEACHER_GAPS, (idx[-1] - idx[0] + 1) - len(idx), f"Окна: {where}")
                if limit is not None and len(idx) > limit:
                    if limit_hard:
                        hard(Violation("teacher_max_day", f"{where}: {len(idx)} пар при максимуме {limit}"))
                    else:
                        add(Crit.TEACHER_MAX_DAY, len(idx) - limit, f"{len(idx)} пар в день: {where}")
                if t_min is not None and t_min > 1:
                    onsite = sum(1 for s in ds if (t.id, k, s.id) in t_onsite)
                    if 0 < onsite < t_min:
                        add(Crit.TEACHER_MIN_DAY, t_min - onsite, f"Приезжает ради {onsite} пар(ы): {where}")

    # объединения
    for mid in sorted(used_merges):
        add(Crit.MERGE_USED, 1, f"Использовано объединение {mid}")

    # отличия от эталона
    if reference is not None:
        mine = Counter((a.item_id, a.week, a.slot_id) for a in good)
        for a in reference.assignments:
            key = (a.item_id, a.week, a.slot_id)
            if mine[key] > 0:
                mine[key] -= 1
            else:
                br[Crit.CHANGES] += 1
        # аудитории: занятие в то же время (элемент, неделя, слот), но в другой аудитории, чем в эталоне;
        # по неделям цикла и по элементам (объединённые элементы — каждый отдельно)
        ref_room: dict[tuple[str, Week, str], str] = {}
        for a in reference.assignments:
            if a.room_id:
                for k in weeks_of(a.week):
                    ref_room[(a.item_id, k, a.slot_id)] = a.room_id
        for a in good:
            if not a.room_id:
                continue
            for k in weeks_of(a.week):
                r = ref_room.get((a.item_id, k, a.slot_id))
                if r and r != a.room_id:
                    old = p.room_by_id[r].name if r in p.room_by_id else r
                    add(
                        Crit.ROOM_REF,
                        1,
                        f"{name(a.item_id)}: {_sl(p, a.slot_id)} ({WEEK_RU[k]}) — {old} → "
                        f"{p.room_by_id[a.room_id].name}",
                    )

    rep.total = prof.total(br)
    return rep


def _adjacent(p: Problem, a: str, b: str) -> bool:
    """Соседние пары одного дня (по порядку в сетке, не по номерам)."""
    sa, sb = p.grid.slot_by_id[a], p.grid.slot_by_id[b]
    if sa.day != sb.day:
        return False
    ds = p.grid.day_slots(sa.day)
    return abs(ds.index(sa) - ds.index(sb)) == 1


def _disc_allowed(p: Problem, group_id: str, discipline_id: str) -> int:
    allowed = p.policy.discipline_per_day
    for it in p.items:
        if it.consecutive and it.discipline_id == discipline_id and group_id in p.item_groups(it):
            return max(allowed, 2)
    return allowed


def _single_room(evs: list[_Event]) -> str | None:
    rooms = {e.room_id for e in evs if not e.online and e.room_id}
    return next(iter(rooms)) if len(rooms) == 1 and len(evs) == 1 else None
