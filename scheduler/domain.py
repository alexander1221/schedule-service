"""Доменная модель задачи расписания (без веба, без решателя).

Всё, что нужно решателю и валидатору, описано здесь простыми dataclass'ами.
Django-модели (этап 4) будут собирать из БД ровно такой же объект `Problem`,
поэтому ядро не зависит ни от ORM, ни от конкретного решателя.

Соглашения
----------
* Идентификаторы — строки. В БД это будут первичные ключи, приведённые к str.
* Слот — пара (день, номер пары); id слота имеет вид ``"<день>-<пара>"``,
  день 1 = понедельник ... 6 = суббота. Воскресенья нет никогда.
* Двухнедельный цикл: неделя ``ODD`` (нечётная) и ``EVEN`` (чётная).
  Занятие с ``Week.BOTH`` стоит в слоте каждую неделю, ``ODD``/``EVEN`` —
  «мигающая» пара.
* Нагрузка элемента задаётся числом пар ЗА ЦИКЛ: ``n // 2`` еженедельных
  вхождений и ``n % 2`` мигающих (например, 3 = 1 еженедельная + 1 мигающая).
* Шкала предпочтений: -2 не могу (жёсткий запрет), -1 нежелательно,
  0 всё равно, +1 удобно, +2 очень хочу.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from itertools import pairwise, product

# ---------------------------------------------------------------- перечисления


class Week(StrEnum):
    BOTH = "both"  # еженедельная пара
    ODD = "odd"  # только по нечётным неделям
    EVEN = "even"  # только по чётным неделям


#: Недели цикла в фиксированном порядке; индекс используется в решателе.
CYCLE_WEEKS: tuple[Week, Week] = (Week.ODD, Week.EVEN)


def weeks_of(week: Week) -> tuple[Week, ...]:
    """Какие недели цикла занимает назначение."""
    return CYCLE_WEEKS if week is Week.BOTH else (week,)


class Format(StrEnum):
    IN_PERSON = "in_person"  # очно: нужна аудитория
    ONLINE = "online"  # онлайн: аудитория не нужна


class WeekPattern(StrEnum):
    """Как пары за двухнедельный цикл раскладываются по неделям."""

    #: n // 2 еженедельных пар + n % 2 мигающих (3 = одна каждую неделю + одна мигающая).
    AUTO = "auto"
    #: «Через неделю»: все n пар — в одну неделю цикла (решатель выбирает, в какую),
    #: парами подряд в один день; в другую неделю — ничего. Кафедра кино: дисциплина
    #: 1 пара в неделю → 2 пары подряд раз в две недели, чтобы преподаватель приезжал
    #: на две пары, а не на одну.
    ALT_WEEK = "alt_week"


class LessonKind(StrEnum):
    LECTURE = "lecture"
    PRACTICE = "practice"
    LAB = "lab"


class Level(StrEnum):
    COLLEGE = "college"
    UNIVERSITY = "university"


PREF_MIN, PREF_MAX = -2, 2
PREF_FORBIDDEN = -2

DAY_NAMES = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб"}


def slot_id(day: int, pair: int) -> str:
    return f"{day}-{pair}"


def _hm(text: str) -> int:
    """'09:30' -> минуты от полуночи."""
    h, m = text.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- время


@dataclass(frozen=True)
class PairTime:
    number: int
    start: str  # "HH:MM", МСК
    end: str

    @property
    def start_min(self) -> int:
        return _hm(self.start)

    @property
    def end_min(self) -> int:
        return _hm(self.end)


@dataclass(frozen=True)
class Slot:
    id: str
    day: int
    pair: int
    start_min: int
    end_min: int
    index: int  # порядковый номер в сетке (для массивов)


DEFAULT_PAIRS: tuple[PairTime, ...] = (
    PairTime(1, "09:30", "11:00"),
    PairTime(2, "11:05", "12:35"),
    PairTime(3, "12:45", "14:15"),
    PairTime(4, "15:00", "16:30"),
    PairTime(5, "16:40", "18:10"),
    PairTime(6, "18:20", "19:50"),
    PairTime(7, "20:00", "21:30"),
)


@dataclass
class TimeGrid:
    """Настраиваемая сетка: учебные дни и список пар с реальным временем."""

    days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    pairs: list[PairTime] = field(default_factory=lambda: list(DEFAULT_PAIRS))

    def __post_init__(self) -> None:
        if 7 in self.days:
            raise ValueError("Воскресенье (день 7) не может быть учебным")
        if any(d < 1 or d > 6 for d in self.days):
            raise ValueError(f"Недопустимые дни сетки: {self.days}")
        nums = [p.number for p in self.pairs]
        if len(set(nums)) != len(nums):
            raise ValueError("Номера пар в сетке повторяются")
        ordered = sorted(self.pairs, key=lambda p: p.start_min)
        for a, b in pairwise(ordered):
            if a.end_min > b.start_min:
                raise ValueError(f"Пары {a.number} и {b.number} пересекаются по времени")

    @property
    def saturday(self) -> bool:
        return 6 in self.days

    @cached_property
    def slots(self) -> list[Slot]:
        out: list[Slot] = []
        for day in sorted(self.days):
            for p in sorted(self.pairs, key=lambda x: x.start_min):
                out.append(Slot(slot_id(day, p.number), day, p.number, p.start_min, p.end_min, len(out)))
        return out

    @cached_property
    def slot_by_id(self) -> dict[str, Slot]:
        return {s.id: s for s in self.slots}

    def day_slots(self, day: int) -> list[Slot]:
        """Слоты дня в порядке времени."""
        return [s for s in self.slots if s.day == day]

    def next_in_day(self, slot: Slot) -> Slot | None:
        ds = self.day_slots(slot.day)
        i = ds.index(slot)
        return ds[i + 1] if i + 1 < len(ds) else None


@dataclass
class Semester:
    name: str = "Семестр"
    start_date: dt.date = dt.date(2026, 9, 1)
    #: Чётность первой учебной недели семестра — для экспорта и календаря.
    first_week: Week = Week.ODD
    grid: TimeGrid = field(default_factory=TimeGrid)

    def __post_init__(self) -> None:
        if self.first_week is Week.BOTH:
            raise ValueError("Первая неделя семестра должна быть ODD или EVEN")


# ---------------------------------------------------------------- справочники


@dataclass
class DayLimits:
    """Лимиты пар в день. None = не ограничено. *_hard — жёсткое ограничение."""

    max_per_day: int | None = None
    max_per_day_hard: bool = False
    min_per_day: int | None = None  # минимум пар в учебный (непустой) день
    min_per_day_hard: bool = False
    max_consecutive: int | None = None
    max_consecutive_hard: bool = False


@dataclass
class Course:
    number: int
    level: Level = Level.COLLEGE
    limits: DayLimits = field(default_factory=DayLimits)
    prefs: dict[str, int] = field(default_factory=dict)  # slot_id -> -2..2


@dataclass
class Group:
    id: str
    name: str
    course: int
    size: int
    prefs_override: dict[str, int] = field(default_factory=dict)  # перекрывает сетку курса


@dataclass
class Subgroup:
    """Подгруппа. Подгруппы одного деления (partition) не пересекаются по студентам;
    подгруппы разных делений одной группы пересекаются и конфликтуют между собой."""

    id: str
    group_id: str
    name: str
    size: int
    partition: str = "default"


@dataclass
class Teacher:
    id: str
    name: str
    email: str = ""
    prefs: dict[str, int] = field(default_factory=dict)  # одна сетка на обе недели
    max_per_day: int | None = None  # None -> Policy.teacher_max_per_day
    max_per_day_hard: bool = False
    #: Оборудование, без которого преподаватель не может вести (жёстко, для всех его занятий
    #: на площадке): коды из справочника оборудования — «проектор», «компьютеры с RTX» и т.п.
    required_equipment: list[str] = field(default_factory=list)
    #: Личные предпочтения аудиторий (мягко): id аудиторий.
    preferred_rooms: list[str] = field(default_factory=list)


@dataclass
class Discipline:
    id: str
    name: str
    difficulty: int = 0  # >= Policy.heavy_threshold -> «тяжёлая»


@dataclass
class RoomType:
    id: str
    name: str


@dataclass
class Room:
    id: str
    name: str
    capacity: int
    type_id: str
    building: str = ""
    active: bool = True
    #: Коды оборудования аудитории.
    equipment: list[str] = field(default_factory=list)
    #: Слоты, в которые аудитория занята не нами (другая площадка, школа, мероприятие) — жёстко.
    blocked: list[str] = field(default_factory=list)
    #: Площадка закрывается в это время (минуты от полуночи); None — без ограничения. Пары,
    #: которые заканчиваются позже, в аудитории не ставятся, кроме занятий «можно закончить раньше».
    closes_min: int | None = None


# ---------------------------------------------------------------- нагрузка


@dataclass
class WorkloadItem:
    """Элемент нагрузки — центральная сущность.

    Участники: группы целиком и/или подгруппы. Несколько групп — фиксированный поток.
    """

    id: str
    discipline_id: str
    teacher_ids: list[str]
    pairs_per_cycle: int
    kind: LessonKind = LessonKind.PRACTICE
    group_ids: list[str] = field(default_factory=list)
    subgroup_ids: list[str] = field(default_factory=list)
    format: Format = Format.IN_PERSON
    room_type_id: str | None = None  # обязателен для очных
    #: Численность вручную (языковые подгруппы внутри синхронного блока).
    headcount: int | None = None
    #: [уточнить] Сдвоенная пара: еженедельные вхождения ставятся подряд в один день.
    double: bool = False
    priority: int = 0
    #: Короткая подпись для отображения (например, уровень языковой подгруппы «B1»).
    label: str = ""
    week_pattern: WeekPattern = WeekPattern.AUTO
    #: Онлайн-занятие, которое преподаватель ведёт с площадки: студенты дома (для них это
    #: онлайн), но преподавателю бронируется аудитория (дистант кафедры ПиУП).
    teacher_in_room: bool = False
    #: Оборудование, нужное самому занятию (дополнительно к требованиям преподавателя).
    required_equipment: list[str] = field(default_factory=list)
    #: Можно закончить раньше конца пары, к закрытию площадки (киноклуб и т. п.): такому занятию
    #: разрешена пара, которая начинается до закрытия, но заканчивается после.
    may_end_early: bool = False

    @property
    def is_alt_week(self) -> bool:
        return self.week_pattern is WeekPattern.ALT_WEEK

    @property
    def weekly_count(self) -> int:
        return 0 if self.is_alt_week else self.pairs_per_cycle // 2

    @property
    def blink_count(self) -> int:
        return self.pairs_per_cycle if self.is_alt_week else self.pairs_per_cycle % 2

    @property
    def consecutive(self) -> bool:
        """Вхождения ставятся парами подряд в один день (сдвоенная или «через неделю»)."""
        return self.double or self.is_alt_week

    @property
    def needs_room(self) -> bool:
        return self.format is Format.IN_PERSON or self.teacher_in_room


@dataclass
class Merge:
    """Допустимое объединение: решатель МОЖЕТ свести элементы в одно занятие (со штрафом).

    Сейчас объединение «всё или ничего» для всего набора.
    """

    id: str
    item_ids: list[str]


@dataclass
class SyncBlock:
    """Синхронный блок (языковой): все элементы стоят в одних и тех же слотах.

    ``group_ids`` — группы, которые «распускаются» и заняты во всех слотах блока.
    Элементы блока — языковые подгруппы со своими преподавателями, численностью
    (``headcount``) и требованием к аудитории. Занятость студентов задаёт блок,
    а не участники элементов.
    """

    id: str
    name: str
    group_ids: list[str]
    item_ids: list[str]


@dataclass
class Pin:
    """Закрепление: вхождение элемента нагрузки -> слот (и, опционально, аудитория)."""

    item_id: str
    week: Week
    slot_id: str
    room_id: str | None = None


# ---------------------------------------------------------------- политика


@dataclass
class Policy:
    """Правила, относящиеся к учебному заведению (не к запуску решателя)."""

    #: Очно -> онлайн: минимальный разрыв (минуты) между концом очной и началом онлайн.
    format_gap_min: int = 90
    #: Сколько пар одной дисциплины у группы в день без штрафа.
    discipline_per_day: int = 1
    #: Сложность, начиная с которой дисциплина считается «тяжёлой».
    heavy_threshold: int = 2
    #: Сколько тяжёлых пар подряд без штрафа.
    heavy_max_consecutive: int = 2
    #: [уточнить] Лимит пар в день для преподавателя по умолчанию.
    teacher_max_per_day: int | None = 5
    teacher_max_per_day_hard: bool = False
    #: [уточнить] Минимум пар в день, когда преподаватель приезжает (мягко): чтобы не
    #: ездить ради одной пары. None — критерий выключен. Онлайн-пары из дома не считаются.
    teacher_min_per_day: int | None = 2
    #: Академических часов в одной паре — для отчёта «часы к табелю».
    hours_per_pair: int = 2
    #: Учитывать вместимость аудиторий как жёсткое ограничение. Выключено — численность
    #: групп и места в аудиториях не мешают ни расстановке, ни потокам (посещаемость не
    #: 100%); при выборе аудитории размер остаётся лишь мягким ориентиром «по росту».
    check_capacity: bool = True


# ---------------------------------------------------------------- задача


@dataclass
class Problem:
    semester: Semester
    courses: list[Course]
    groups: list[Group]
    teachers: list[Teacher]
    disciplines: list[Discipline]
    room_types: list[RoomType]
    rooms: list[Room]
    items: list[WorkloadItem]
    subgroups: list[Subgroup] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)
    blocks: list[SyncBlock] = field(default_factory=list)
    pins: list[Pin] = field(default_factory=list)
    policy: Policy = field(default_factory=Policy)

    # ---- индексы (problem считается неизменяемым после построения)

    @property
    def grid(self) -> TimeGrid:
        return self.semester.grid

    @property
    def slots(self) -> list[Slot]:
        return self.grid.slots

    @cached_property
    def course_by_num(self) -> dict[int, Course]:
        return {c.number: c for c in self.courses}

    @cached_property
    def group_by_id(self) -> dict[str, Group]:
        return {g.id: g for g in self.groups}

    @cached_property
    def subgroup_by_id(self) -> dict[str, Subgroup]:
        return {s.id: s for s in self.subgroups}

    @cached_property
    def teacher_by_id(self) -> dict[str, Teacher]:
        return {t.id: t for t in self.teachers}

    @cached_property
    def discipline_by_id(self) -> dict[str, Discipline]:
        return {d.id: d for d in self.disciplines}

    @cached_property
    def room_by_id(self) -> dict[str, Room]:
        return {r.id: r for r in self.rooms}

    @cached_property
    def item_by_id(self) -> dict[str, WorkloadItem]:
        return {i.id: i for i in self.items}

    @cached_property
    def block_of_item(self) -> dict[str, SyncBlock]:
        return {iid: b for b in self.blocks for iid in b.item_ids}

    @cached_property
    def merge_of_item(self) -> dict[str, Merge]:
        return {iid: m for m in self.merges for iid in m.item_ids}

    @cached_property
    def active_rooms(self) -> list[Room]:
        return [r for r in self.rooms if r.active]

    # ---- производные величины

    def item_size(self, item: WorkloadItem) -> int:
        """Численность участников занятия (для вместимости аудитории)."""
        if item.headcount is not None:
            return item.headcount
        return sum(self.group_by_id[g].size for g in item.group_ids) + sum(
            self.subgroup_by_id[s].size for s in item.subgroup_ids
        )

    def room_need(self, items: list[WorkloadItem]) -> int:
        """Сколько мест нужно в аудитории занятию (одному элементу или объединённым).

        Очное — суммарная численность участников; онлайн с бронью — одно место
        (в аудитории только преподаватель).
        """
        in_person = [it for it in items if it.format is Format.IN_PERSON]
        if in_person:
            return sum(self.item_size(it) for it in in_person)
        return 1 if any(it.teacher_in_room for it in items) else 0

    def fits(self, room: Room, need: int) -> bool:
        """Хватает ли мест (всегда да, если вместимость не учитывается)."""
        return not self.policy.check_capacity or room.capacity >= need

    def required_equipment(self, items: list[WorkloadItem]) -> set[str]:
        """Оборудование занятия: требования элементов и всех их преподавателей."""
        req: set[str] = set()
        for it in items:
            req |= set(it.required_equipment)
            for t in it.teacher_ids:
                if t in self.teacher_by_id:
                    req |= set(self.teacher_by_id[t].required_equipment)
        return req

    def eligible_rooms(self, items: list[WorkloadItem]) -> list[Room]:
        """Аудитории, в которые можно поставить занятие (без учёта занятости слота).

        Тип — если указан у элемента (для дистанта с бронью тип может быть пуст: подойдёт
        любая аудитория), вместимость ≥ нужного числа мест (если учитывается), всё оборудование.
        """
        if not any(it.needs_room for it in items):
            return []
        types = {it.room_type_id for it in items if it.room_type_id}
        need = self.room_need(items)
        req = self.required_equipment(items)
        return [
            r
            for r in self.active_rooms
            if (not types or r.type_id in types) and self.fits(r, need) and req <= set(r.equipment)
        ]

    def room_free(self, room: Room, sid: str, late_ok: bool = False) -> bool:
        """Аудиторию можно занять в слоте: не занята «не нами» и площадка ещё открыта.

        ``late_ok`` — занятие «можно закончить раньше»: ему достаточно начать до закрытия.
        """
        return sid not in self._blocked_sets[room.id] and not self.room_closed(room, sid, late_ok)

    def room_closed(self, room: Room, sid: str, late_ok: bool = False) -> bool:
        """Площадка аудитории закрыта к концу (или, для ``late_ok``, к началу) пары."""
        if room.closes_min is None:
            return False
        s = self.grid.slot_by_id[sid]
        return s.start_min >= room.closes_min if late_ok else s.end_min > room.closes_min

    @staticmethod
    def late_ok(items: list[WorkloadItem]) -> bool:
        return bool(items) and all(it.may_end_early for it in items)

    @cached_property
    def _blocked_sets(self) -> dict[str, set[str]]:
        return {r.id: set(r.blocked) for r in self.rooms}

    def stream_key(self, item: WorkloadItem) -> tuple[object, ...] | None:
        """Ключ «можно поставить потоком»: одна дисциплина, один курс, те же преподаватели, те же
        часы и схема. Элементы с одинаковым ключом и разными группами — кандидаты на поток
        (кафедра кино: 2КП1 + 2КП2 одной парой). None — элемент в потоки не годится."""
        if item.id in self.block_of_item or item.id in self.merge_of_item or item.subgroup_ids:
            return None
        if len(item.group_ids) != 1 or item.group_ids[0] not in self.group_by_id:
            return None
        course = self.group_by_id[item.group_ids[0]].course
        return (
            item.discipline_id,
            course,
            tuple(sorted(item.teacher_ids)),
            item.pairs_per_cycle,
            item.week_pattern,
            item.format,
            item.teacher_in_room,
            item.kind,
            item.room_type_id,
            tuple(sorted(item.required_equipment)),
        )

    def stream_candidates(self) -> list[list[WorkloadItem]]:
        """Наборы элементов (≥ 2 групп), которые можно объединять в поток."""
        by_key: dict[tuple[object, ...], list[WorkloadItem]] = {}
        for it in self.items:
            key = self.stream_key(it)
            if key is not None:
                by_key.setdefault(key, []).append(it)
        return [its for its in by_key.values() if len({it.group_ids[0] for it in its}) >= 2]

    def teacher_on_site(self, item: WorkloadItem) -> bool:
        """Преподаватель физически на площадке (очное занятие или дистант из аудитории)."""
        return item.needs_room

    def occupancy(self, item: WorkloadItem) -> tuple[list[str], list[str]]:
        """Кого занимает занятие: (группы целиком, подгруппы).

        Для элемента синхронного блока — группы блока целиком.
        """
        block = self.block_of_item.get(item.id)
        if block is not None:
            return list(block.group_ids), []
        return list(item.group_ids), list(item.subgroup_ids)

    def item_groups(self, item: WorkloadItem) -> set[str]:
        """Все группы, чьи студенты участвуют (с учётом подгрупп)."""
        whole, subs = self.occupancy(item)
        return set(whole) | {self.subgroup_by_id[s].group_id for s in subs}

    def group_pref(self, group_id: str, sid: str) -> int:
        g = self.group_by_id[group_id]
        if sid in g.prefs_override:
            return g.prefs_override[sid]
        course = self.course_by_num.get(g.course)
        return course.prefs.get(sid, 0) if course else 0

    def teacher_pref(self, teacher_id: str, sid: str) -> int:
        return self.teacher_by_id[teacher_id].prefs.get(sid, 0)

    def group_cells(self, group_id: str) -> list[tuple[str, ...]]:
        """«Клетки» студентов группы: декартово произведение делений на подгруппы.

        Две вещи конфликтуют, если затрагивают общую клетку. Группа без подгрупп —
        одна пустая клетка.
        """
        parts: dict[str, list[str]] = {}
        for s in self.subgroups:
            if s.group_id == group_id:
                parts.setdefault(s.partition, []).append(s.id)
        if not parts:
            return [()]
        return [tuple(c) for c in product(*(sorted(v) for _, v in sorted(parts.items())))]

    def teacher_limit(self, teacher_id: str) -> tuple[int | None, bool]:
        t = self.teacher_by_id[teacher_id]
        if t.max_per_day is not None:
            return t.max_per_day, t.max_per_day_hard
        return self.policy.teacher_max_per_day, self.policy.teacher_max_per_day_hard

    def is_heavy(self, item: WorkloadItem) -> bool:
        return self.discipline_by_id[item.discipline_id].difficulty >= self.policy.heavy_threshold

    def allowed_slots(self, item: WorkloadItem) -> list[Slot]:
        """Слоты без жёсткого запрета (-2) у преподавателей и групп-участников."""
        groups = self.item_groups(item)
        block = self.block_of_item.get(item.id)
        teachers = set(item.teacher_ids)
        if block is not None:  # блок стоит целиком: учитываем всех его преподавателей
            for iid in block.item_ids:
                teachers |= set(self.item_by_id[iid].teacher_ids)
        room_items = [self.item_by_id[i] for i in block.item_ids] if block is not None else [item]
        eligible = [(it.may_end_early, self.eligible_rooms([it])) for it in room_items if it.needs_room]
        out = []
        for s in self.slots:
            if any(self.teacher_pref(t, s.id) <= PREF_FORBIDDEN for t in teachers):
                continue
            if any(self.group_pref(g, s.id) <= PREF_FORBIDDEN for g in groups):
                continue
            # нужна хотя бы одна подходящая аудитория, свободная в этом слоте
            if any(rs and not any(self.room_free(r, s.id, late) for r in rs) for late, rs in eligible):
                continue
            out.append(s)
        return out


# ---------------------------------------------------------------- результат


@dataclass
class Assignment:
    """Одно вхождение занятия в расписание."""

    item_id: str
    week: Week
    slot_id: str
    room_id: str | None = None  # None для онлайн
    #: Пометка диспетчера «преподаватель удалённо» (форс-мажор, только для отображения).
    teacher_remote: bool = False


@dataclass
class Schedule:
    assignments: list[Assignment]
    #: Параметры запуска, seed, разбивка штрафа и т. п. — для воспроизводимости.
    meta: dict[str, object] = field(default_factory=dict)

    def by_item(self) -> dict[str, list[Assignment]]:
        out: dict[str, list[Assignment]] = {}
        for a in self.assignments:
            out.setdefault(a.item_id, []).append(a)
        return out
