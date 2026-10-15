"""Критерии качества (мягкие ограничения), веса по умолчанию и пресеты.

Общий справочник для решателя и валидатора: оба считают одни и те же величины,
но независимо друг от друга. Все величины считаются ПО НЕДЕЛЯМ цикла: еженедельная
пара в «нежелательном» слоте даёт 2 единицы (нечётная + чётная неделя), мигающая — 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Crit(StrEnum):
    FMT_IN_TO_ONLINE = "fmt_in_to_online"
    FMT_ONLINE_TO_IN = "fmt_online_to_in"
    FMT_MIXED_DAY = "fmt_mixed_day"
    TEACHER_PREF = "teacher_pref"
    GROUP_PREF = "group_pref"
    GROUP_GAPS = "group_gaps"
    TEACHER_GAPS = "teacher_gaps"
    COURSE_MAX_DAY = "course_max_day"
    COURSE_MIN_DAY = "course_min_day"
    COURSE_MAX_CONSEC = "course_max_consec"
    TEACHER_MAX_DAY = "teacher_max_day"
    TEACHER_MIN_DAY = "teacher_min_day"
    DISCIPLINE_SPREAD = "discipline_spread"
    MERGE_USED = "merge_used"
    HEAVY_CONSEC = "heavy_consec"
    CHANGES = "changes"  # отличия от эталонного расписания (режим пересборки)
    ROOM_CHANGES = "room_changes"  # этап 2: смена аудитории группой между соседними парами
    ROOM_PREF = "room_pref"  # этап 2: занятие не в предпочтительной аудитории преподавателя
    #: этап 2, пересборка: занятие осталось в то же время, но не в той аудитории, что в эталоне
    ROOM_REF = "room_ref"


LABELS: dict[Crit, str] = {
    Crit.FMT_IN_TO_ONLINE: "Очно → онлайн без окна ≥ 90 мин",
    Crit.FMT_ONLINE_TO_IN: "Онлайн → очно в один день",
    Crit.FMT_MIXED_DAY: "Смешанный формат в день",
    Crit.TEACHER_PREF: "Предпочтения преподавателей",
    Crit.GROUP_PREF: "Предпочтения курсов и групп",
    Crit.GROUP_GAPS: "Окна у групп",
    Crit.TEACHER_GAPS: "Окна у преподавателей",
    Crit.COURSE_MAX_DAY: "Превышение максимума пар в день (курс)",
    Crit.COURSE_MIN_DAY: "Недобор минимума пар в учебный день (курс)",
    Crit.COURSE_MAX_CONSEC: "Превышение пар подряд (курс)",
    Crit.TEACHER_MAX_DAY: "Превышение пар в день (преподаватель)",
    Crit.TEACHER_MIN_DAY: "Преподаватель приезжает ради одной пары",
    Crit.DISCIPLINE_SPREAD: "Скопление дисциплины в один день",
    Crit.MERGE_USED: "Использованы объединения групп",
    Crit.HEAVY_CONSEC: "Тяжёлые пары подряд",
    Crit.CHANGES: "Отличия от текущего расписания",
    Crit.ROOM_CHANGES: "Смена аудитории между соседними парами",
    Crit.ROOM_PREF: "Не в предпочтительной аудитории преподавателя",
    Crit.ROOM_REF: "Другая аудитория, чем в текущем расписании",
}

#: Цена значения шкалы предпочтений (умножается на вес критерия).
#: -2 сюда не входит: это жёсткий запрет.
DEFAULT_PREF_COST: dict[int, int] = {-1: 10, 0: 0, 1: -1, 2: -2}

#: Веса по умолчанию. Требование: очно→онлайн ≫ «-1» у преподавателя ≈ онлайн→очно
#: > окна > прочее. «-1» у преподавателя = 10 (вес) × 10 (цена) = 100.
DEFAULT_WEIGHTS: dict[Crit, int] = {
    Crit.FMT_IN_TO_ONLINE: 1000,
    Crit.TEACHER_PREF: 10,
    Crit.FMT_ONLINE_TO_IN: 100,
    Crit.GROUP_GAPS: 30,
    Crit.TEACHER_GAPS: 15,
    Crit.GROUP_PREF: 3,
    Crit.COURSE_MAX_DAY: 40,
    Crit.COURSE_MIN_DAY: 25,
    Crit.COURSE_MAX_CONSEC: 20,
    Crit.TEACHER_MAX_DAY: 40,
    Crit.TEACHER_MIN_DAY: 20,
    Crit.FMT_MIXED_DAY: 15,
    Crit.MERGE_USED: 60,
    Crit.DISCIPLINE_SPREAD: 8,
    Crit.HEAVY_CONSEC: 5,
    Crit.CHANGES: 50,
    Crit.ROOM_CHANGES: 1,
    Crit.ROOM_PREF: 3,
    # пересборка: прежняя аудитория важнее стабильности группы и предпочтений (×1000 на этапе 2)
    Crit.ROOM_REF: 10,
}

#: Критерии этапа 2 (конкретные аудитории): решатель слотов их не видит, их оптимизирует
#: назначение аудиторий, а итог считает валидатор.
ROOM_STAGE_CRITS: frozenset[Crit] = frozenset({Crit.ROOM_CHANGES, Crit.ROOM_PREF, Crit.ROOM_REF})

#: Порядок по умолчанию для лексикографического режима (уровни сверху вниз).
DEFAULT_LEX_LEVELS: list[list[Crit]] = [
    [Crit.FMT_IN_TO_ONLINE],
    [Crit.TEACHER_PREF, Crit.FMT_ONLINE_TO_IN],
    [Crit.GROUP_GAPS, Crit.TEACHER_GAPS],
    [
        c
        for c in Crit
        if c
        not in {
            Crit.FMT_IN_TO_ONLINE,
            Crit.TEACHER_PREF,
            Crit.FMT_ONLINE_TO_IN,
            Crit.GROUP_GAPS,
            Crit.TEACHER_GAPS,
            *ROOM_STAGE_CRITS,
        }
    ],
]


class Mode(StrEnum):
    WEIGHTED = "weighted"
    LEXICOGRAPHIC = "lex"


@dataclass
class WeightProfile:
    """Набор весов — то, что диспетчер сохраняет как пресет."""

    name: str = "По умолчанию"
    mode: Mode = Mode.WEIGHTED
    weights: dict[Crit, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    pref_cost: dict[int, int] = field(default_factory=lambda: dict(DEFAULT_PREF_COST))
    #: Только для лексикографического режима; внутри уровня — веса из ``weights``.
    lex_levels: list[list[Crit]] = field(default_factory=lambda: [list(lv) for lv in DEFAULT_LEX_LEVELS])

    def weight(self, c: Crit) -> int:
        return self.weights.get(c, 0)

    def total(self, breakdown: dict[Crit, int]) -> int:
        return sum(self.weight(c) * v for c, v in breakdown.items())


PRESETS: dict[str, WeightProfile] = {
    "default": WeightProfile(),
    "teachers_first": WeightProfile(
        name="Сначала преподаватели",
        weights={**DEFAULT_WEIGHTS, Crit.TEACHER_PREF: 20, Crit.TEACHER_GAPS: 30},
    ),
    "students_first": WeightProfile(
        name="Сначала студенты",
        weights={**DEFAULT_WEIGHTS, Crit.GROUP_GAPS: 60, Crit.GROUP_PREF: 8, Crit.COURSE_MIN_DAY: 40},
    ),
    "lex": WeightProfile(name="Лексикографический", mode=Mode.LEXICOGRAPHIC),
}
