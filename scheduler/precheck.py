"""Быстрые проверки задачи до запуска решателя.

Ловят очевидные противоречия за миллисекунды и формулируют их по-человечески
(«у Петрова 8 пар за цикл и 6 доступных слотов»). Ошибка (level="error")
означает, что решения точно нет; предупреждение — что-то подозрительное.
Модуль не зависит от решателя.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .domain import (
    DAY_NAMES,
    PREF_FORBIDDEN,
    Format,
    Problem,
    Slot,
    SyncBlock,
    Week,
    WorkloadItem,
)
from .matching import max_matching


@dataclass
class Finding:
    level: str  # "error" — решения точно нет; "warning" — подозрительно; "info" — подсказка диспетчеру
    code: str
    message: str


def _iname(p: Problem, it: WorkloadItem) -> str:
    d = p.discipline_by_id.get(it.discipline_id)
    groups = ", ".join(sorted(p.group_by_id[g].name for g in p.item_groups(it) if g in p.group_by_id))
    return f"«{d.name if d else it.discipline_id}» ({groups or it.id})"


def precheck(p: Problem) -> list[Finding]:
    out: list[Finding] = []

    def err(code: str, msg: str) -> None:
        out.append(Finding("error", code, msg))

    def warn(code: str, msg: str) -> None:
        out.append(Finding("warning", code, msg))

    # ---------------------------------------------------- ссылочная целостность
    ids = [it.id for it in p.items]
    if len(set(ids)) != len(ids):
        err("dup_item", "Повторяющиеся идентификаторы элементов нагрузки")
    room_types = {t.id for t in p.room_types}
    broken: set[str] = set()
    for it in p.items:
        problems = []
        if it.discipline_id not in p.discipline_by_id:
            problems.append(f"дисциплина {it.discipline_id}")
        problems += [f"преподаватель {t}" for t in it.teacher_ids if t not in p.teacher_by_id]
        problems += [f"группа {g}" for g in it.group_ids if g not in p.group_by_id]
        problems += [f"подгруппа {s}" for s in it.subgroup_ids if s not in p.subgroup_by_id]
        if problems:
            err("bad_ref", f"Элемент {it.id}: неизвестные ссылки — {', '.join(problems)}")
            broken.add(it.id)
        if not it.teacher_ids:
            err("no_teacher", f"Элемент {it.id}: не указан преподаватель")
            broken.add(it.id)
        if it.pairs_per_cycle <= 0:
            err("bad_count", f"Элемент {it.id}: число пар за цикл должно быть > 0")
            broken.add(it.id)
        if it.format is Format.IN_PERSON and it.room_type_id not in room_types:
            err("no_room_type", f"Элемент {it.id}: очному занятию нужен тип аудитории из справочника")
            broken.add(it.id)
        elif it.room_type_id is not None and it.room_type_id not in room_types:
            err("no_room_type", f"Элемент {it.id}: неизвестный тип аудитории {it.room_type_id}")
            broken.add(it.id)
        if not it.group_ids and not it.subgroup_ids and it.id not in p.block_of_item:
            err("no_participants", f"Элемент {it.id}: нет участников (групп или подгрупп)")
            broken.add(it.id)
    for b in p.blocks:
        for g in b.group_ids:
            if g not in p.group_by_id:
                err("bad_ref", f"Блок «{b.name}»: неизвестная группа {g}")
        for iid in b.item_ids:
            if iid not in p.item_by_id:
                err("bad_ref", f"Блок «{b.name}»: неизвестный элемент {iid}")
                broken.add(iid)
    if out:
        return out  # дальше без целостных ссылок проверять бессмысленно

    items = p.items
    slots = p.slots
    allowed = {it.id: p.allowed_slots(it) for it in items}

    # ---------------------------------------------------- элементы
    for it in items:
        name = _iname(p, it)
        need_slots = it.weekly_count + it.blink_count
        if len(allowed[it.id]) < need_slots:
            err(
                "item_slots",
                f"{name}: нужно {need_slots} разных слотов, а без запретов «не могу» доступно {len(allowed[it.id])}",
            )
        if it.is_alt_week:
            if it.pairs_per_cycle % 2:
                err(
                    "alt_odd",
                    f"{name}: схема «через неделю» ставит пары по две подряд — нужно чётное число "
                    f"пар за цикл, указано {it.pairs_per_cycle}",
                )
            elif not _has_adjacent_pair(p, allowed[it.id]):
                err("alt_no_pair", f"{name}: схема «через неделю» — нет ни одной пары соседних слотов без «не могу»")
        elif it.double and (it.weekly_count < 2 or it.weekly_count % 2):
            err("double", f"{name}: сдвоенная пара требует чётного числа еженедельных вхождений (≥ 2)")
        if it.teacher_in_room and it.format is Format.IN_PERSON:
            warn("in_room_in_person", f"{name}: «бронь аудитории преподавателю» имеет смысл только для онлайн-занятий")
        if it.needs_room and not p.eligible_rooms([it]):
            code, msg = _why_no_room(p, it)
            err(code, f"{name}: {msg}")
        for t in it.teacher_ids:
            tc = sum(1 for x in it.teacher_ids if x == t)
            if tc > 1:
                err("dup_teacher", f"{name}: преподаватель указан дважды")

    # ---------------------------------------------------- синхронные блоки
    for b in p.blocks:
        b_items = [p.item_by_id[i] for i in b.item_ids]
        if not b_items:
            err("block_empty", f"Блок «{b.name}» пуст")
            continue
        patterns = {(it.week_pattern, it.double) for it in b_items}
        if len(patterns) > 1:
            err(
                "block_pattern",
                f"Блок «{b.name}»: у занятий разная схема недель или сдвоенность — "
                f"в синхронном блоке она должна совпадать",
            )
        _block_rooms(p, b, b_items, err, warn)
        counts = {it.pairs_per_cycle for it in b_items}
        if len(counts) > 1:
            err(
                "block_counts",
                f"Блок «{b.name}»: у занятий разное число пар за цикл {sorted(counts)} — "
                f"сейчас все подгруппы блока обязаны идти синхронно",
            )
        teachers = [t for it in b_items for t in it.teacher_ids]
        dup = {t for t in teachers if teachers.count(t) > 1}
        for t in sorted(dup):
            err("block_teacher", f"Блок «{b.name}»: {p.teacher_by_id[t].name} ведёт две подгруппы одновременно")
        common = allowed[b_items[0].id]
        if not common:
            err("block_no_slot", f"Блок «{b.name}»: нет ни одного слота, где свободны все его группы и преподаватели")
        if any(it.id in p.merge_of_item for it in b_items):
            err("block_merge", f"Блок «{b.name}»: элементы блока не могут входить в допустимое объединение")

    # ---------------------------------------------------- объединения
    for m in p.merges:
        ms = [p.item_by_id[i] for i in m.item_ids if i in p.item_by_id]
        if len(ms) < 2:
            err("merge_small", f"Объединение {m.id}: нужно минимум два элемента")
            continue
        keys = {
            (
                it.discipline_id,
                tuple(sorted(it.teacher_ids)),
                it.pairs_per_cycle,
                it.format,
                it.room_type_id,
                it.week_pattern,
                it.double,
                it.teacher_in_room,
            )
            for it in ms
        }
        if len(keys) > 1:
            err(
                "merge_mismatch",
                f"Объединение {m.id}: у элементов должны совпадать дисциплина, преподаватель, "
                f"число пар, формат, тип аудитории и схема недель",
            )
            continue
        if ms[0].needs_room and not p.eligible_rooms(ms):
            warn(
                "merge_room",
                f"Объединение {m.id}: объединённому занятию на {p.room_need(ms)} мест нет подходящей аудитории "
                f"({'вместимость, ' if p.policy.check_capacity else ''}тип, оборудование) — объединение невозможно",
            )
    seen_m: dict[str, str] = {}
    for m in p.merges:
        for i in m.item_ids:
            if i in seen_m:
                err("merge_overlap", f"Элемент {i} входит в два объединения ({seen_m[i]}, {m.id})")
            seen_m[i] = m.id

    # ---------------------------------------------------- преподаватели
    load_t: dict[str, int] = defaultdict(int)
    counted_merge: set[str] = set()
    for it in items:
        m = p.merge_of_item.get(it.id)
        if m:  # нижняя оценка: объединение считаем один раз
            if m.id in counted_merge:
                continue
            counted_merge.add(m.id)
        for t in it.teacher_ids:
            load_t[t] += it.pairs_per_cycle
    for t in p.teachers:
        load = load_t.get(t.id, 0)
        if not load:
            continue
        limit, hard = p.teacher_limit(t.id)
        free_by_day: dict[int, int] = defaultdict(int)
        for s in slots:
            if t.prefs.get(s.id, 0) > PREF_FORBIDDEN:
                free_by_day[s.day] += 1
        avail = sum(free_by_day.values())
        cap = sum(min(v, limit) if (hard and limit is not None) else v for v in free_by_day.values())
        if load > 2 * cap:
            extra = f" с учётом лимита {limit} пар в день" if cap < avail else ""
            err(
                "teacher_capacity",
                f"У {t.name} {load} пар за двухнедельный цикл, а доступных слотов {2 * cap} (по {cap} в неделю{extra})",
            )
        elif load > 2 * avail - 2 and avail:
            warn(
                "teacher_tight",
                f"У {t.name} почти не осталось свободы: {load} пар за цикл на {2 * avail} доступных слотов",
            )

    # ---------------------------------------------------- группы (по «клеткам» студентов)
    for g in p.groups:
        lim = p.course_by_num[g.course].limits if g.course in p.course_by_num else None
        free_by_day = defaultdict(int)
        for s in slots:
            if p.group_pref(g.id, s.id) > PREF_FORBIDDEN:
                free_by_day[s.day] += 1
        cap = sum(
            min(v, lim.max_per_day) if (lim and lim.max_per_day_hard and lim.max_per_day is not None) else v
            for v in free_by_day.values()
        )
        for cell in p.group_cells(g.id):
            load = 0
            seen_blocks: set[str] = set()
            for it in items:
                whole, subs = p.occupancy(it)
                if g.id in whole or any(s in cell for s in subs):
                    blk = p.block_of_item.get(it.id)
                    if blk:
                        if blk.id in seen_blocks:
                            continue
                        seen_blocks.add(blk.id)
                    load += it.pairs_per_cycle
            if load > 2 * cap:
                who = g.name + (f" ({', '.join(p.subgroup_by_id[s].name for s in cell)})" if cell else "")
                err(
                    "group_capacity",
                    f"У группы {who} {load} пар за цикл, а доступно {2 * cap} слотов (по {cap} в неделю)",
                )

    # ---------------------------------------------------- аудитории в целом
    # для каждого «набора подходящих аудиторий» S: занятий, которым годятся только аудитории из S,
    # за цикл не больше, чем |S| аудиторий × слоты × 2 недели (минус занятые не нами слоты)
    sets: dict[frozenset[str], int] = defaultdict(int)
    for it in items:
        if it.needs_room:
            rs = frozenset(r.id for r in p.eligible_rooms([it]))
            if rs:
                sets[rs] += it.pairs_per_cycle
    for S in sets:
        demand = sum(d for rs, d in sets.items() if rs <= S)
        supply = sum(2 * sum(1 for sl in slots if p.room_free(p.room_by_id[r], sl.id, late_ok=True)) for r in S)
        if demand > supply:
            names = ", ".join(sorted(p.room_by_id[r].name for r in S)[:6]) + ("…" if len(S) > 6 else "")
            err(
                "rooms_total",
                f"Аудиторий не хватает в принципе: занятиям, которым подходят только {names}, нужно {demand} "
                f"пар-аудиторий за цикл, а есть {supply}",
            )

    _candidates(p, out)

    # ---------------------------------------------------- закрепления
    seen_pin: set[tuple[str, Week, str]] = set()
    for pin in p.pins:
        it = p.item_by_id.get(pin.item_id)
        if it is None:
            err("pin_ref", f"Закрепление ссылается на неизвестный элемент {pin.item_id}")
            continue
        name = _iname(p, it)
        slot = p.grid.slot_by_id.get(pin.slot_id)
        if slot is None:
            err("pin_slot", f"Закрепление {name}: слота {pin.slot_id} нет в сетке")
            continue
        where = f"{DAY_NAMES[slot.day]} {slot.pair} пара"
        if pin.week is Week.BOTH and it.weekly_count == 0:
            err("pin_week", f"Закрепление {name}: у элемента нет еженедельных пар")
        if pin.week is not Week.BOTH and it.blink_count == 0:
            err("pin_week", f"Закрепление {name}: у элемента нет мигающей пары")
        rs = p.eligible_rooms([it]) if it.needs_room else []
        if rs and all(p.room_closed(r, slot.id, it.may_end_early) for r in rs):
            err(
                "pin_closed",
                f"Закрепление {name} → {where}: площадка к концу этой пары закрыта"
                + ("" if it.may_end_early else " — если занятие можно закончить раньше, отметьте это у него"),
            )
        elif slot not in allowed[it.id]:
            err("pin_forbidden", f"Закрепление {name} → {where}: в этом слоте у участников «не могу»")
        key = (pin.item_id, pin.week, pin.slot_id)
        if key in seen_pin:
            err("pin_dup", f"Закрепление {name} → {where} указано дважды")
        seen_pin.add(key)
        if pin.room_id and pin.room_id not in p.room_by_id:
            err("pin_room", f"Закрепление {name}: неизвестная аудитория {pin.room_id}")
        if pin.item_id in p.block_of_item:
            warn("pin_block", f"Закрепление {name} действует на весь синхронный блок")
    by_item: dict[str, dict[Week, int]] = defaultdict(lambda: defaultdict(int))
    for pin in p.pins:
        by_item[pin.item_id][pin.week] += 1
    for iid, ws in by_item.items():
        it = p.item_by_id.get(iid)
        if it is None:
            continue
        if ws[Week.BOTH] > it.weekly_count or ws[Week.ODD] + ws[Week.EVEN] > it.blink_count:
            err("pin_count", f"Закреплений {_iname(p, it)} больше, чем пар в нагрузке")
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)


def _has_adjacent_pair(p: Problem, allowed: list[Slot]) -> bool:
    ok = {s.id for s in allowed}
    for d in p.grid.days:
        ds = p.grid.day_slots(d)
        if any(a.id in ok and b.id in ok for a, b in itertools.pairwise(ds)):
            return True
    return False


def _block_rooms(p: Problem, b: SyncBlock, b_items: list[WorkloadItem], err: Any, warn: Any) -> None:
    """Все подгруппы блока идут одновременно — хватит ли аудиторий в один момент?"""
    needing = [it for it in b_items if it.needs_room]
    if needing:
        res = max_matching([[r.id for r in p.eligible_rooms([it])] for it in needing])
        if not res.perfect:
            short = [needing[i] for i in res.deficient]
            err(
                "block_rooms",
                f"Блок «{b.name}»: одновременно нужно {len(needing)} аудиторий; подгруппам "
                f"{', '.join(it.label or it.id for it in short)} подходят только "
                f"{len(res.neighbourhood)} аудитории — не хватает по "
                f"{'вместимости, ' if p.policy.check_capacity else ''}типу или оборудованию",
            )
    sizes = [it.headcount for it in b_items]
    if p.policy.check_capacity and sizes and all(x is not None for x in sizes):
        students = sum(p.group_by_id[g].size for g in b.group_ids if g in p.group_by_id)
        placed = sum(x for x in sizes if x is not None)
        if placed < students:
            warn(
                "block_headcount",
                f"Блок «{b.name}»: в подгруппах {placed} мест, а в группах блока {students} "
                f"студентов — не все распределены по уровням",
            )


def _candidates(p: Problem, out: list[Finding]) -> None:
    """Подсказки диспетчеру (не ошибки): к каким элементам, вероятно, применимы особые схемы."""
    for its in p.stream_candidates():
        disc = p.discipline_by_id[its[0].discipline_id].name
        groups = " + ".join(sorted(p.group_by_id[it.group_ids[0]].name for it in its))
        teachers = ", ".join(p.teacher_by_id[t].name for t in its[0].teacher_ids)
        out.append(
            Finding(
                "info",
                "stream_candidate",
                f"«{disc}» ({teachers}): {groups} — одна дисциплина курса, те же часы и преподаватель — "
                f"можно разрешить поток (одна пара на все группы)",
            )
        )
    by_teacher: dict[str, list[WorkloadItem]] = defaultdict(list)
    for it in p.items:
        for t in it.teacher_ids:
            by_teacher[t].append(it)
    for t, its in by_teacher.items():
        # кафедра кино: единственная дисциплина преподавателя, 1 пара в неделю → «через неделю»
        if (
            len({it.discipline_id for it in its}) == 1
            and all(it.pairs_per_cycle == 2 and not it.is_alt_week for it in its)
            and not any(it.id in p.block_of_item for it in its)
        ):
            disc = p.discipline_by_id[its[0].discipline_id].name
            out.append(
                Finding(
                    "info",
                    "alt_candidate",
                    f"{p.teacher_by_id[t].name}: единственная дисциплина «{disc}», 1 пара в неделю — "
                    f"кандидат на схему «через неделю» (2 пары подряд раз в две недели)",
                )
            )
        # дистант у преподавателя, который в этом семестре ведёт и очные занятия → нужна бронь
        if any(it.format is Format.IN_PERSON for it in its):
            for it in its:
                if it.format is Format.ONLINE and not it.teacher_in_room:
                    out.append(
                        Finding(
                            "info",
                            "room_booking_candidate",
                            f"{_iname(p, it)}: онлайн, а {p.teacher_by_id[t].name} ведёт и очные "
                            f"занятия — возможно, нужна бронь аудитории преподавателю",
                        )
                    )


def _why_no_room(p: Problem, it: WorkloadItem) -> tuple[str, str]:
    """Человекочитаемая причина, почему занятию не подходит ни одна аудитория."""
    rooms = p.active_rooms
    if it.room_type_id:
        rooms = [r for r in rooms if r.type_id == it.room_type_id]
        tname = next((t.name for t in p.room_types if t.id == it.room_type_id), it.room_type_id)
        if not rooms:
            return "no_rooms", f"нет ни одной активной аудитории типа «{tname}»"
    need = p.room_need([it])
    big = [r for r in rooms if p.fits(r, need)]
    if not big:
        return (
            "room_too_small",
            f"нужно {need} мест, а самая большая подходящая аудитория — {max(r.capacity for r in rooms)}",
        )
    req = p.required_equipment([it])
    best = max(big, key=lambda r: len(req & set(r.equipment)))
    missing = sorted(req - set(best.equipment))
    return "room_equipment", (
        f"нет аудитории со всем нужным оборудованием ({', '.join(sorted(req))}); "
        f"ближе всего {best.name}, но в ней нет: {', '.join(missing)}"
    )
