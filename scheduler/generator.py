"""Генератор синтетических задач масштаба колледжа (§1, §12 ТЗ).

Задача строится вокруг «посаженного» решения (planted solution): сначала
нагрузка случайно, но корректно расставляется по слотам и аудиториям, и только
потом преподавателям раздаются запреты «-2» — исключительно в слотах, которые
они в посаженном решении не используют. Поэтому каждая сгенерированная задача
гарантированно разрешима, а посаженное расписание годится как эталон для тестов
(валидатор обязан принять его с нулём жёстких нарушений).

Параметр ``tightness`` (0…1) — «теснота»: чем больше, тем выше нагрузка групп,
тем больше запретов у преподавателей и тем больше «узких» преподавателей
(вплоть до 6 доступных слотов в неделю).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .defaults import default_course
from .domain import (
    CYCLE_WEEKS,
    Assignment,
    Discipline,
    Format,
    Group,
    LessonKind,
    Merge,
    Pin,
    Problem,
    Room,
    RoomType,
    Schedule,
    Semester,
    Slot,
    Subgroup,
    SyncBlock,
    Teacher,
    TimeGrid,
    Week,
    WeekPattern,
    WorkloadItem,
    slot_id,
)

SURNAMES = [
    "Иванов",
    "Петров",
    "Сидоров",
    "Смирнов",
    "Кузнецов",
    "Попов",
    "Васильев",
    "Соколов",
    "Михайлов",
    "Новиков",
    "Фёдоров",
    "Морозов",
    "Волков",
    "Алексеев",
    "Лебедев",
    "Семёнов",
    "Егоров",
    "Павлов",
    "Козлов",
    "Степанов",
    "Николаев",
    "Орлов",
    "Андреев",
    "Макаров",
    "Никитин",
    "Захаров",
    "Зайцев",
    "Соловьёв",
    "Борисов",
    "Яковлев",
    "Григорьев",
    "Романов",
    "Воробьёв",
    "Сергеев",
    "Кузьмин",
    "Фролов",
    "Александров",
    "Дмитриев",
    "Королёв",
    "Гусев",
    "Киселёв",
    "Ильин",
    "Максимов",
    "Поляков",
    "Сорокин",
    "Виноградов",
    "Ковалёв",
    "Белов",
    "Медведев",
    "Антонов",
    "Тарасов",
    "Жуков",
    "Баранов",
    "Филиппов",
    "Комаров",
    "Давыдов",
]
DISCIPLINES = [
    ("Математика", 3),
    ("Физика", 2),
    ("Информатика", 1),
    ("Программирование", 2),
    ("Базы данных", 2),
    ("История", 0),
    ("Обществознание", 0),
    ("Литература", 0),
    ("Русский язык", 1),
    ("Физкультура", 0),
    ("Химия", 2),
    ("Биология", 1),
    ("ОБЖ", 0),
    ("Экономика", 1),
    ("Право", 0),
    ("Психология", 0),
    ("Сети и телекоммуникации", 2),
    ("Операционные системы", 2),
    ("Веб-разработка", 1),
    ("Компьютерная графика", 1),
    ("Дискретная математика", 3),
    ("Теория вероятностей", 3),
    ("Электротехника", 2),
    ("Менеджмент", 0),
    ("Маркетинг", 0),
    ("Философия", 1),
    ("Статистика", 2),
    ("Алгоритмы", 3),
    ("Информационная безопасность", 2),
    ("Проектная деятельность", 0),
    ("Тестирование ПО", 1),
    ("Архитектура ЭВМ", 2),
    ("Документоведение", 0),
    ("Бухучёт", 1),
]
#: Буквы специальностей в названиях групп — как у колледжа: «2П1», «3Ф2» (курс, буква, номер).
PREFIXES = ["П", "Р", "Ф", "К", "Д", "И", "Э"]
#: Дисциплины «кафедры кино»: у преподавателя это единственная дисциплина, 1 пара в неделю,
#: ставится по схеме «через неделю» (2 пары подряд раз в две недели).
ALT_DISCIPLINES = [("Режиссура", 1), ("Операторское мастерство", 1), ("Монтаж", 1), ("Сценарное мастерство", 1)]


@dataclass
class GenParams:
    seed: int = 1
    n_groups: int = 25
    n_teachers: int = 45
    courses: int = 4
    tightness: float = 0.5
    n_blocks: int = 2
    online_share: float = 0.1
    n_merges: int = 2
    n_pins: int = 2
    saturday: bool = False
    #: Преподаватели «кафедры кино» со схемой «через неделю» (у каждого 2–3 группы).
    alt_teachers: int = 3
    #: Доля онлайн-занятий, которые преподаватель ведёт из аудитории (бронь).
    room_booking_share: float = 0.5


@dataclass
class Generated:
    problem: Problem
    planted: Schedule


class _Planter:
    """Жадная случайная расстановка с учётом всех жёстких ограничений."""

    def __init__(self, p: Problem, rng: random.Random, max_per_day: dict[int, int]):
        self.p, self.rng = p, rng
        self.max_per_day = max_per_day
        # (сущность, неделя, слот) -> занято
        self.teacher: set[tuple[str, Week, str]] = set()
        self.cell: set[tuple[str, tuple[str, ...], Week, str]] = set()
        self.room: set[tuple[str, Week, str]] = set()
        self.gday: dict[tuple[str, Week, int], int] = {}
        self.out: list[Assignment] = []

    def _cells(self, item: WorkloadItem) -> list[tuple[str, tuple[str, ...]]]:
        whole, subs = self.p.occupancy(item)
        res = [(g, c) for g in whole for c in self.p.group_cells(g)]
        for s in subs:
            g = self.p.subgroup_by_id[s].group_id
            res += [(g, c) for c in self.p.group_cells(g) if s in c]
        return res

    def _free_room(self, item: WorkloadItem, weeks: tuple[Week, ...], sid: str, taken: set[str]) -> str | None:
        cands = [
            r
            for r in self.p.eligible_rooms([item])
            if self.p.room_free(r, sid) and r.id not in taken and all((r.id, k, sid) not in self.room for k in weeks)
        ]
        if not cands:
            return None
        return min(cands, key=lambda r: (r.capacity, r.id)).id

    def _try(self, items: list[WorkloadItem], weeks: tuple[Week, ...], s: Slot) -> list[str | None] | None:
        """Подходит ли слот (преподаватели, клетки, лимиты, аудитории); если да — список аудиторий."""
        if not self._fits(items, weeks, s):
            return None
        rooms: list[str | None] = []
        taken: set[str] = set()
        for it in items:
            if not it.needs_room:
                rooms.append(None)
                continue
            r = self._free_room(it, weeks, s.id, taken)
            if r is None:
                return None
            rooms.append(r)
            taken.add(r)
        return rooms

    def place_alt(self, items: list[WorkloadItem], all_slots: list[Slot]) -> bool:
        """«Через неделю»: n пар в одну (случайную) неделю, парами соседних слотов одного дня."""
        n = items[0].pairs_per_cycle
        k = self.rng.choice(CYCLE_WEEKS)
        snapshot = (set(self.teacher), set(self.cell), set(self.room), dict(self.gday))
        plan: list[tuple[Slot, list[str | None]]] = []
        days = sorted({s.day for s in all_slots})
        self.rng.shuffle(days)
        for _ in range(n // 2):
            placed = False
            for d in days:
                ds = [s for s in all_slots if s.day == d]
                starts = list(range(len(ds) - 1))
                self.rng.shuffle(starts)
                for i in starts:
                    r1 = self._try(items, (k,), ds[i])
                    if r1 is None:
                        continue
                    before = (set(self.teacher), set(self.cell), set(self.room), dict(self.gday))
                    self._book(items, (k,), ds[i], r1)
                    r2 = self._try(items, (k,), ds[i + 1])
                    if r2 is None:
                        self.teacher, self.cell, self.room, self.gday = before
                        continue
                    self._book(items, (k,), ds[i + 1], r2)
                    plan += [(ds[i], r1), (ds[i + 1], r2)]
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                self.teacher, self.cell, self.room, self.gday = snapshot
                return False
        for s, rooms in plan:
            for it, r in zip(items, rooms, strict=True):
                self.out.append(Assignment(it.id, k, s.id, r))
        return True

    def place_unit(self, items: list[WorkloadItem], pref_slots: list[Slot], all_slots: list[Slot]) -> bool:
        """Расставить все вхождения единицы (элемент или блок целиком). Атомарно."""
        if items[0].is_alt_week:
            return self.place_alt(items, all_slots)
        n = items[0].pairs_per_cycle
        plan: list[tuple[Week, Slot, list[str | None]]] = []
        snapshot = (set(self.teacher), set(self.cell), set(self.room), dict(self.gday))
        occ_weeks: list[Week] = [Week.BOTH] * (n // 2) + ([self.rng.choice(CYCLE_WEEKS)] if n % 2 else [])
        used_days: set[int] = set()
        for wk in occ_weeks:
            weeks = CYCLE_WEEKS if wk is Week.BOTH else (wk,)
            order = list(pref_slots) + [s for s in all_slots if s not in pref_slots]
            self.rng.shuffle(order)
            order.sort(key=lambda s: (s not in pref_slots, s.day in used_days))
            done = False
            for s in order:
                rooms = self._try(items, weeks, s)
                if rooms is None:
                    continue
                self._book(items, weeks, s, rooms)
                plan.append((wk, s, rooms))
                used_days.add(s.day)
                done = True
                break
            if not done:
                self.teacher, self.cell, self.room, self.gday = snapshot
                return False
        for wk, s, rooms in plan:
            for it, r in zip(items, rooms, strict=True):
                self.out.append(Assignment(it.id, wk, s.id, r))
        return True

    def _fits(self, items: list[WorkloadItem], weeks: tuple[Week, ...], s: Slot) -> bool:
        for k in weeks:
            seen_cells: set[tuple[str, tuple[str, ...]]] = set()
            for it in items:
                for t in it.teacher_ids:
                    if (t, k, s.id) in self.teacher:
                        return False
                seen_cells |= set(self._cells(it))
            for g, c in seen_cells:
                if (g, c, k, s.id) in self.cell:
                    return False
            for g in {g for g, _ in seen_cells}:
                course = self.p.group_by_id[g].course
                if self.gday.get((g, k, s.day), 0) >= self.max_per_day[course]:
                    return False
        return True

    def _book(self, items: list[WorkloadItem], weeks: tuple[Week, ...], s: Slot, rooms: list[str | None]) -> None:
        for k in weeks:
            cells: set[tuple[str, tuple[str, ...]]] = set()
            for it, r in zip(items, rooms, strict=True):
                for t in it.teacher_ids:
                    self.teacher.add((t, k, s.id))
                if r:
                    self.room.add((r, k, s.id))
                cells |= set(self._cells(it))
            new_groups = {g for g, c in cells if all((g, cc, k, s.id) not in self.cell for cc in self.p.group_cells(g))}
            for g, c in cells:
                self.cell.add((g, c, k, s.id))
            for g in new_groups:
                self.gday[(g, k, s.day)] = self.gday.get((g, k, s.day), 0) + 1


def generate(params: GenParams | None = None) -> Generated:
    gp = params or GenParams()
    rng = random.Random(gp.seed)
    tight = min(max(gp.tightness, 0.0), 1.0)
    grid = TimeGrid(days=[1, 2, 3, 4, 5, 6] if gp.saturday else [1, 2, 3, 4, 5])
    semester = Semester(name=f"Синтетика seed={gp.seed}", grid=grid)
    courses = [default_course(n, grid) for n in range(1, gp.courses + 1)]

    # ---- аудитории
    room_types = [
        RoomType("regular", "Обычная"),
        RoomType("lecture", "Лекционная"),
        RoomType("computer", "Компьютерный класс"),
        RoomType("lab", "Лаборатория"),
    ]
    rooms: list[Room] = []
    for i, cap in enumerate([60, 75, 90]):
        rooms.append(Room(f"L{i + 1}", f"Л-{i + 1}", cap, "lecture", "Корпус 1", equipment=["projector"]))
    for i in range(12):
        rooms.append(
            Room(
                f"R{i + 1}",
                f"{101 + i}",
                rng.choice([28, 30, 32]),
                "regular",
                f"Корпус {1 + i % 2}",
                equipment=["projector"] if i % 3 else [],
            )
        )
    for i in range(7):
        # компьютерные классы: в двух — машины с RTX (графика, монтаж)
        rooms.append(
            Room(
                f"C{i + 1}",
                f"К-{i + 1}",
                16,
                "computer",
                "Корпус 2",
                equipment=["projector", "pc"] + (["pc_rtx"] if i < 2 else []),
            )
        )
    for i in range(3):
        rooms.append(Room(f"B{i + 1}", f"Лаб-{i + 1}", 16, "lab", "Корпус 2"))

    # ---- группы и подгруппы (деление «лаб» на две)
    per_course = [gp.n_groups // gp.courses + (1 if c < gp.n_groups % gp.courses else 0) for c in range(gp.courses)]
    groups: list[Group] = []
    subgroups: list[Subgroup] = []
    for c, cnt in enumerate(per_course, start=1):
        for j in range(cnt):
            gid = f"g{c}{j + 1:02d}"
            size = rng.randint(18, 28)
            groups.append(Group(gid, f"{c}{PREFIXES[j % len(PREFIXES)]}{j // len(PREFIXES) + 1}", c, size))
            subgroups.append(Subgroup(f"{gid}a", gid, "п/г 1", (size + 1) // 2, "lab"))
            subgroups.append(Subgroup(f"{gid}b", gid, "п/г 2", size // 2, "lab"))

    disciplines = [Discipline(f"d{i + 1:02d}", n, diff) for i, (n, diff) in enumerate(DISCIPLINES)]
    disciplines.append(Discipline("eng", "Английский язык", 1))
    alt_disc = [Discipline(f"k{i + 1}", n, diff) for i, (n, diff) in enumerate(ALT_DISCIPLINES)]
    disciplines += alt_disc
    teachers = [
        Teacher(
            f"t{i + 1:02d}",
            f"{SURNAMES[i % len(SURNAMES)]} {chr(1040 + rng.randrange(32))}.{chr(1040 + rng.randrange(32))}.",
            f"t{i + 1:02d}@college.example",
        )
        for i in range(gp.n_teachers)
    ]
    # преподаватели «кафедры кино» — вне общего пула: у них одна дисциплина «через неделю»
    n_alt = min(gp.alt_teachers, len(alt_disc), max(0, gp.n_teachers - 10))
    alt_teachers = [t.id for t in teachers[-n_alt:]] if n_alt else []
    t_load: dict[str, int] = {t.id: 0 for t in teachers if t.id not in alt_teachers}  # пар за цикл
    t_cap = 22 + int(8 * tight)

    def pick_teacher(need: int, exclude: set[str] = frozenset()) -> str:  # type: ignore[assignment]
        cands = [t for t in t_load if t_load[t] + need <= t_cap and t not in exclude]
        if not cands:
            cands = [t for t in t_load if t not in exclude]
        t = min(rng.sample(cands, min(4, len(cands))), key=lambda x: t_load[x])
        t_load[t] += need
        return t

    items: list[WorkloadItem] = []
    blocks: list[SyncBlock] = []
    merges: list[Merge] = []
    seq = iter(range(1, 10_000))

    def new_item(**kw: object) -> WorkloadItem:
        it = WorkloadItem(id=f"w{next(seq):03d}", **kw)  # type: ignore[arg-type]
        items.append(it)
        return it

    by_course: dict[int, list[Group]] = {}
    for g in groups:
        by_course.setdefault(g.course, []).append(g)

    # ---- английский: один синхронный блок на курс — ВСЕ группы курса распускаются по уровням,
    # все подгруппы и все преподаватели курса в одно время (ответ учебного отдела, 0.3.0).
    # Подгрупп столько, чтобы в каждой было ~20 человек; преподаватели английского — общий пул
    # на все курсы (курсы идут в разное время, поэтому пересечений нет).
    block_groups: set[str] = set()
    levels = ["A1", "A2", "B1", "B2"]
    n_sub = {c: -(-sum(g.size for g in by_course[c]) // 20) for c in range(1, min(gp.n_blocks, gp.courses) + 1)}
    eng_pool: list[str] = []
    for _ in range(max(n_sub.values(), default=0)):
        eng_pool.append(pick_teacher(0, set(eng_pool)))
    for c, k_sub in n_sub.items():
        cg = by_course[c]
        total = sum(g.size for g in cg)
        sizes = [total // k_sub + (1 if i < total % k_sub else 0) for i in range(k_sub)]
        b_items = []
        for i, sz in enumerate(sizes):
            t = eng_pool[i]
            t_load[t] += 4
            # участников нет: занятость студентов задаёт блок, численность — headcount
            b_items.append(
                new_item(
                    discipline_id="eng",
                    teacher_ids=[t],
                    pairs_per_cycle=4,
                    kind=LessonKind.PRACTICE,
                    room_type_id="regular",
                    headcount=sz,
                    label=f"{levels[i * len(levels) // k_sub]}-{i + 1}",
                )
            )
        blocks.append(SyncBlock(f"blk{c}", f"Английский, {c} курс", [g.id for g in cg], [it.id for it in b_items]))
        block_groups |= {g.id for g in cg}

    # ---- потоковые лекции: по одной на курс, 2–3 группы
    group_load: dict[str, int] = {g.id: 0 for g in groups}  # пар за цикл (для группы целиком)
    for bl in blocks:
        for gid in bl.group_ids:
            group_load[gid] += 4
    for cg in by_course.values():
        stream = cg[-3:] if len(cg) >= 3 else cg
        if sum(g.size for g in stream) > 90:
            stream = stream[:2]
        d = rng.choice(disciplines[:30])
        t = pick_teacher(2)
        new_item(
            discipline_id=d.id,
            teacher_ids=[t],
            pairs_per_cycle=2,
            kind=LessonKind.LECTURE,
            group_ids=[g.id for g in stream],
            room_type_id="lecture",
        )
        for g in stream:
            group_load[g.id] += 2

    # ---- остальная нагрузка по группам
    target = {1: 30, 2: 30, 3: 28, 4: 26}  # пар за цикл (две недели)
    # «кафедра кино»: у каждого такого преподавателя одна дисциплина на 2–3 группы, 1 пара в неделю
    alt_load: dict[str, int] = {}
    for i, t in enumerate(alt_teachers):
        pool = [g for g in groups if g.course >= 2] or groups
        for g in rng.sample(pool, min(len(pool), rng.choice([2, 3]))):
            new_item(
                discipline_id=alt_disc[i].id,
                teacher_ids=[t],
                pairs_per_cycle=2,
                group_ids=[g.id],
                room_type_id="regular",
                week_pattern=WeekPattern.ALT_WEEK,
            )
            alt_load[g.id] = alt_load.get(g.id, 0) + 2
    for gid, n in alt_load.items():
        group_load[gid] += n

    for g in groups:
        tgt = target.get(g.course, 28) + int(6 * tight)
        used_disc: set[str] = set()
        # лабораторная по подгруппам (компьютерный класс)
        d = rng.choice([x for x in disciplines if x.id in {"d03", "d04", "d05", "d17", "d18", "d19", "d28"}])
        used_disc.add(d.id)
        t = pick_teacher(4)
        for sg in ("a", "b"):
            new_item(
                discipline_id=d.id,
                teacher_ids=[t],
                pairs_per_cycle=2,
                kind=LessonKind.LAB,
                subgroup_ids=[f"{g.id}{sg}"],
                room_type_id="computer",
            )
        group_load[g.id] += 2  # подгруппы параллельно/последовательно: для группы ~1 пара в неделю
        if g.id not in block_groups:
            t = pick_teacher(2)
            new_item(discipline_id="eng", teacher_ids=[t], pairs_per_cycle=2, group_ids=[g.id], room_type_id="regular")
            group_load[g.id] += 2
        while group_load[g.id] < tgt:
            d = rng.choice([x for x in disciplines[: len(DISCIPLINES)] if x.id not in used_disc])
            used_disc.add(d.id)
            n = min(rng.choice([2, 2, 3, 4]), tgt - group_load[g.id]) or 1
            online = rng.random() < gp.online_share and d.difficulty <= 1
            booked = online and rng.random() < gp.room_booking_share  # дистант, преподаватель в аудитории
            t = pick_teacher(n)
            new_item(
                discipline_id=d.id,
                teacher_ids=[t],
                pairs_per_cycle=n,
                group_ids=[g.id],
                format=Format.ONLINE if online else Format.IN_PERSON,
                room_type_id="regular" if (booked or not online) else None,
                teacher_in_room=booked,
            )
            group_load[g.id] += n

    # ---- допустимые объединения: одна дисциплина, один преподаватель, две группы курса
    for m in range(gp.n_merges):
        c = 3 + m % 2 if gp.courses >= 4 else 1
        cg = [g for g in by_course.get(c, []) if g.id not in block_groups][:2]
        if len(cg) < 2:
            continue
        d = disciplines[24 + m]
        t = pick_teacher(4)
        ids = []
        for g in cg:
            it = new_item(
                discipline_id=d.id,
                teacher_ids=[t],
                pairs_per_cycle=2,
                kind=LessonKind.LECTURE,
                group_ids=[g.id],
                room_type_id="lecture",
            )
            ids.append(it.id)
        merges.append(Merge(f"m{m + 1}", ids))

    problem = Problem(
        semester=semester,
        courses=courses,
        groups=groups,
        teachers=teachers,
        disciplines=disciplines,
        room_types=room_types,
        rooms=rooms,
        items=items,
        subgroups=subgroups,
        merges=merges,
        blocks=blocks,
    )

    # ---- оборудование и аудитории «не наши»
    # одна обычная аудитория занята другой площадкой по понедельникам и вторникам
    rooms[3].blocked = [slot_id(d, pr.number) for d in (1, 2) for pr in grid.pairs]
    # преподаватели лабораторных по графике/монтажу — только в классах с RTX; часть лекторов — с проектором
    graphics = {d.id for d in disciplines if d.name in ("Компьютерная графика", "Веб-разработка")}
    for it in items:
        if it.kind is LessonKind.LAB and it.discipline_id in graphics:
            for t in it.teacher_ids:
                tch = next(x for x in teachers if x.id == t)
                if all(i2.room_type_id == "computer" for i2 in items if t in i2.teacher_ids):
                    tch.required_equipment = ["pc_rtx"]
        if it.kind is LessonKind.LECTURE and rng.random() < 0.5:
            for t in it.teacher_ids:
                tch = next(x for x in teachers if x.id == t)
                if all(i2.room_type_id in ("lecture", "regular", None) for i2 in items if t in i2.teacher_ids):
                    tch.required_equipment = ["projector"]

    # ---- посадка решения
    max_day = {c.number: (c.limits.max_per_day or 7) for c in courses}
    pl = _Planter(problem, rng, max_day)
    units: list[list[WorkloadItem]] = [[problem.item_by_id[i] for i in b.item_ids] for b in blocks]
    in_block = {i for b in blocks for i in b.item_ids}
    rest = [it for it in items if it.id not in in_block]
    rest.sort(key=lambda it: (not it.is_alt_week, -len(it.group_ids), it.format is Format.ONLINE, -it.pairs_per_cycle))
    units += [[it] for it in rest]
    dropped: set[str] = set()
    for unit in units:
        gids = set().union(*(problem.item_groups(it) for it in unit))
        pref = [s for s in problem.slots if all(problem.group_pref(g, s.id) > 0 for g in gids)]
        if not pl.place_unit(unit, pref, problem.slots):
            dropped |= {it.id for it in unit}
    if dropped:
        problem.items = [it for it in items if it.id not in dropped]
        problem.blocks = [b for b in blocks if not set(b.item_ids) & dropped]
        problem.merges = [m for m in merges if not set(m.item_ids) & dropped]
        for key in ("item_by_id", "block_of_item", "merge_of_item"):
            problem.__dict__.pop(key, None)

    # ---- личные предпочтения аудиторий: у части преподавателей — аудитории, где они в посадке
    room_used: dict[str, set[str]] = {}
    for a in pl.out:
        if a.room_id:
            for t in problem.item_by_id[a.item_id].teacher_ids:
                room_used.setdefault(t, set()).add(a.room_id)
    for t in teachers:
        if t.id in room_used and rng.random() < 0.3:
            t.preferred_rooms = sorted(room_used[t.id])[:2]

    # ---- предпочтения преподавателей вокруг посаженного решения
    used_slots: dict[str, set[str]] = {t.id: set() for t in teachers}
    for a in pl.out:
        for t in problem.item_by_id[a.item_id].teacher_ids:
            used_slots[t].add(a.slot_id)
    all_ids = [s.id for s in problem.slots]
    narrow_share = 0.05 + 0.2 * tight
    for t in teachers:
        free = [s for s in all_ids if s not in used_slots[t.id]]
        rng.shuffle(free)
        if rng.random() < narrow_share:
            keep = max(0, 6 - len(used_slots[t.id]))
            banned = free[keep:]
        else:
            banned = free[: int(len(free) * (0.15 + 0.45 * tight))]
        prefs = {s: -2 for s in banned}
        rest_ids = [s for s in all_ids if s not in prefs]
        for s in rest_ids:
            r = rng.random()
            if r < 0.12:
                prefs[s] = -1
            elif r < 0.30:
                prefs[s] = 1
            elif r < 0.38:
                prefs[s] = 2
        t.prefs = prefs

    # ---- закрепления из посаженного решения
    cands = [a for a in pl.out if a.item_id not in in_block]
    for a in rng.sample(cands, min(gp.n_pins, len(cands))):
        problem.pins.append(Pin(a.item_id, a.week, a.slot_id))

    planted = Schedule(pl.out, meta={"source": "planted", "seed": gp.seed, "dropped_items": sorted(dropped)})
    return Generated(problem, planted)
