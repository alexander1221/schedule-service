"""Отчёты по готовому расписанию (не зависят от решателя и от БД).

Часы к табелю: поток считается по каждой группе отдельно. Если 2КП1 и 2КП2 стоят одной
парой (элемент-поток на две группы или использованное допустимое объединение), преподавателю
в табель идут часы за обе группы — 4 академических часа за одну фактическую пару в 2 часа.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .domain import Problem, Schedule, weeks_of


@dataclass
class TeacherHours:
    teacher_id: str
    teacher: str
    pairs_actual: int  # фактически проведённых пар за двухнедельный цикл (поток — одна пара)
    hours_actual: int  # академических часов «в аудитории»
    hours_tabel: int  # академических часов к табелю (поток — по каждой группе)
    lines: list[tuple[str, str, int, int]]  # (дисциплина, группы, пар за цикл по элементу, часов к табелю)

    @property
    def per_week_tabel(self) -> float:
        return self.hours_tabel / 2


def teacher_hours(p: Problem, s: Schedule) -> list[TeacherHours]:
    hp = p.policy.hours_per_pair
    occ: dict[str, set[tuple[str, str]]] = defaultdict(set)  # преподаватель -> {(слот, неделя)}
    per_item: dict[str, int] = defaultdict(int)  # элемент -> пар за цикл
    for a in s.assignments:
        it = p.item_by_id.get(a.item_id)
        if it is None:
            continue
        n = len(weeks_of(a.week))
        per_item[it.id] += n
        for t in it.teacher_ids:
            for k in weeks_of(a.week):
                occ[t].add((a.slot_id, k.value))
    out = []
    for t in p.teachers:
        lines = []
        for it in p.items:
            if t.id in it.teacher_ids and per_item.get(it.id):
                groups = ", ".join(sorted(p.group_by_id[g].name for g in p.item_groups(it))) or (it.label or "блок")
                # элемент-поток на несколько групп целиком — часы за каждую группу
                mult = max(1, len(it.group_ids))
                lines.append(
                    (p.discipline_by_id[it.discipline_id].name, groups, per_item[it.id], per_item[it.id] * hp * mult)
                )
        if not lines:
            continue
        actual = len(occ[t.id])
        out.append(TeacherHours(t.id, t.name, actual, actual * hp, sum(x[3] for x in lines), sorted(lines)))
    return sorted(out, key=lambda r: r.teacher)
