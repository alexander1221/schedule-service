"""Значения по умолчанию для пунктов, помеченных в ТЗ как [уточнить].

Всё здесь — разумные заглушки до согласования с учебным отделом. В веб-версии они
станут начальными значениями в настройках курса/преподавателя, которые диспетчер
меняет в интерфейсе. Список открытых вопросов — docs/OPEN_QUESTIONS.md.
"""

from __future__ import annotations

from .domain import Course, DayLimits, Level, TimeGrid, slot_id


def shift_prefs(course_number: int, grid: TimeGrid) -> dict[str, int]:
    """[уточнить] Сетка предпочтений смены для курса.

    1–2 курс — первая смена: пары 1–4 = +1, пары 5–7 = −1.
    3–4 курс — вторая смена: пары 1–2 = −1, пара 3 = 0, пары 4–7 = +1.
    """
    prefs: dict[str, int] = {}
    for day in grid.days:
        for p in grid.pairs:
            n = p.number
            if course_number <= 2:
                v = 1 if n <= 4 else -1
            else:
                v = -1 if n <= 2 else (0 if n == 3 else 1)
            if v:
                prefs[slot_id(day, n)] = v
    return prefs


def course_limits(course_number: int) -> DayLimits:
    """[уточнить] Лимиты пар в день: у 1–2 курса строже (несовершеннолетние).

    Все лимиты по умолчанию мягкие; жёсткость включается диспетчером.
    """
    if course_number <= 2:
        return DayLimits(max_per_day=4, min_per_day=2, max_consecutive=4)
    return DayLimits(max_per_day=5, min_per_day=2, max_consecutive=4)


def default_course(number: int, grid: TimeGrid, level: Level = Level.COLLEGE) -> Course:
    return Course(number=number, level=level, limits=course_limits(number), prefs=shift_prefs(number, grid))
