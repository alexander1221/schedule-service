"""Модели БД расписания.

Справочники организации (преподаватели, дисциплины, аудитории) живут вне семестра.
Всё, что меняется каждый семестр (сетка пар, курсы с лимитами и сетками смен,
группы, доступность преподавателей, нагрузка, блоки, закрепления, варианты), —
внутри семестра. Ядро (`scheduler`) о БД не знает: объект задачи собирает
`timetable.services.problem.build_problem`.

Поля «на вырост» (ответы на открытые вопросы, docs/OPEN_QUESTIONS.md) заведены
сразу, чтобы потом не мигрировать данные: `WorkloadItem.week_pattern`,
`WorkloadItem.alt_room_types`, блок без привязки к курсу, число пар у каждого
элемента блока. Пока решатель их не использует, это проверяет `clean()`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from scheduler.domain import DAY_NAMES, DEFAULT_PAIRS


def default_pref_grid() -> dict[str, int]:
    return {}


def validate_pref_grid(value: Any) -> None:
    """Сетка предпочтений: {"<день>-<пара>": -2..2}."""
    if not isinstance(value, dict):
        raise ValidationError("Сетка предпочтений должна быть словарём «слот → значение»")
    for k, v in value.items():
        parts = str(k).split("-")
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or not 1 <= int(parts[0]) <= 6:
            raise ValidationError(f"Неверный слот «{k}»: ожидается «день-пара», день 1–6")
        if not isinstance(v, int) or not -2 <= v <= 2:
            raise ValidationError(f"Слот {k}: значение {v!r} вне шкалы −2…+2")


class Week(models.TextChoices):
    BOTH = "both", "каждую неделю"
    ODD = "odd", "по нечётным"
    EVEN = "even", "по чётным"


class Timestamped(models.Model):
    created_at = models.DateTimeField("создано", auto_now_add=True)
    updated_at = models.DateTimeField("изменено", auto_now=True)

    class Meta:
        abstract = True


# ====================================================================== семестр


class Semester(Timestamped):
    class Status(models.TextChoices):
        DRAFT = "draft", "подготовка"
        ACTIVE = "active", "действует"
        ARCHIVED = "archived", "архив"

    class FirstWeek(models.TextChoices):
        ODD = "odd", "нечётная"
        EVEN = "even", "чётная"

    name = models.CharField("название", max_length=100, unique=True)
    start_date = models.DateField("дата начала")
    end_date = models.DateField(
        "дата окончания занятий",
        null=True,
        blank=True,
        help_text="До какой даты повторять занятия в календаре (.ics). Пусто — 18 недель от начала",
    )
    first_week = models.CharField(
        "чётность первой недели", max_length=4, choices=FirstWeek.choices, default=FirstWeek.ODD
    )
    saturday = models.BooleanField("суббота учебная", default=False)
    status = models.CharField("статус", max_length=10, choices=Status.choices, default=Status.DRAFT)
    availability_deadline = models.DateField("срок сдачи доступности преподавателями", null=True, blank=True)
    # --- политика учебного заведения на семестр (scheduler.domain.Policy)
    format_gap_min = models.PositiveSmallIntegerField(
        "окно очно → онлайн, мин", default=90, help_text="Минимальный разрыв между концом очной и началом онлайн-пары"
    )
    discipline_per_day = models.PositiveSmallIntegerField("пар одной дисциплины в день без штрафа", default=1)
    heavy_threshold = models.PositiveSmallIntegerField("сложность «тяжёлой» дисциплины, от", default=2)
    heavy_max_consecutive = models.PositiveSmallIntegerField("тяжёлых пар подряд без штрафа", default=2)
    teacher_max_per_day = models.PositiveSmallIntegerField(
        "максимум пар в день у преподавателя",
        null=True,
        blank=True,
        default=5,
        help_text="[уточнить] Пусто — без лимита",
    )
    teacher_max_per_day_hard = models.BooleanField("лимит преподавателя жёсткий", default=False)
    hours_per_pair = models.PositiveSmallIntegerField(
        "академических часов в паре", default=2, help_text="Для отчёта «часы к табелю»"
    )
    check_capacity = models.BooleanField(
        "учитывать вместимость аудиторий",
        default=False,
        help_text="Выключено — численность групп не ограничивает выбор аудитории и потоки (посещаемость не 100%)",
    )
    teacher_min_per_day = models.PositiveSmallIntegerField(
        "минимум пар у преподавателя в день приезда",
        null=True,
        blank=True,
        default=2,
        help_text="[уточнить] Мягко: штраф, если преподаватель приезжает ради меньшего числа пар. Пусто — не учитывать",
    )

    class Meta:
        verbose_name = "семестр"
        verbose_name_plural = "семестры"
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return self.name

    @property
    def days(self) -> list[int]:
        return [1, 2, 3, 4, 5, 6] if self.saturday else [1, 2, 3, 4, 5]

    def pair_numbers(self) -> list[int]:
        nums = list(self.pairs.order_by("number").values_list("number", flat=True))
        return nums or [p.number for p in DEFAULT_PAIRS]

    def week_parity(self, date: dt.date) -> str:
        """Чётность недели, в которую попадает дата (для экспорта и календаря)."""
        start_monday = self.start_date - dt.timedelta(days=self.start_date.weekday())
        n = (date - start_monday).days // 7
        same = n % 2 == 0
        if self.first_week == self.FirstWeek.ODD:
            return "odd" if same else "even"
        return "even" if same else "odd"

    def ensure_default_pairs(self) -> None:
        if not self.pairs.exists():
            PairTime.objects.bulk_create(
                [
                    PairTime(
                        semester=self,
                        number=p.number,
                        start=dt.time(p.start_min // 60, p.start_min % 60),
                        end=dt.time(p.end_min // 60, p.end_min % 60),
                    )
                    for p in DEFAULT_PAIRS
                ]
            )


class PairTime(models.Model):
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="pairs", verbose_name="семестр")
    number = models.PositiveSmallIntegerField("пара №", validators=[MinValueValidator(1), MaxValueValidator(12)])
    start = models.TimeField("начало")
    end = models.TimeField("конец")

    class Meta:
        verbose_name = "пара (время)"
        verbose_name_plural = "сетка пар"
        ordering = ["semester", "number"]
        constraints = [models.UniqueConstraint(fields=["semester", "number"], name="uniq_pair_number")]

    def clean(self) -> None:
        if self.start and self.end and self.start >= self.end:
            raise ValidationError("Конец пары должен быть позже начала")

    def __str__(self) -> str:
        return f"{self.number}: {self.start:%H:%M}–{self.end:%H:%M}"


class Course(models.Model):
    class Level(models.TextChoices):
        COLLEGE = "college", "колледж"
        UNIVERSITY = "university", "вуз"

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="courses", verbose_name="семестр")
    number = models.PositiveSmallIntegerField("курс", validators=[MinValueValidator(1), MaxValueValidator(6)])
    level = models.CharField("уровень", max_length=12, choices=Level.choices, default=Level.COLLEGE)
    max_per_day = models.PositiveSmallIntegerField("макс. пар в день", null=True, blank=True)
    max_per_day_hard = models.BooleanField("максимум жёсткий", default=False)
    min_per_day = models.PositiveSmallIntegerField("мин. пар в учебный день", null=True, blank=True)
    min_per_day_hard = models.BooleanField("минимум жёсткий", default=False)
    max_consecutive = models.PositiveSmallIntegerField("макс. пар подряд", null=True, blank=True)
    max_consecutive_hard = models.BooleanField("«подряд» жёстко", default=False)
    prefs = models.JSONField(
        "сетка предпочтений (смена)", default=default_pref_grid, blank=True, validators=[validate_pref_grid]
    )

    class Meta:
        verbose_name = "курс"
        verbose_name_plural = "курсы"
        ordering = ["semester", "number"]
        constraints = [models.UniqueConstraint(fields=["semester", "number"], name="uniq_course")]

    def __str__(self) -> str:
        return f"{self.number} курс ({self.semester})"


class Group(models.Model):
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="groups", verbose_name="курс")
    name = models.CharField("название", max_length=50)
    size = models.PositiveSmallIntegerField("численность", validators=[MinValueValidator(1)])
    prefs_override = models.JSONField(
        "переопределение сетки курса",
        default=default_pref_grid,
        blank=True,
        validators=[validate_pref_grid],
        help_text="Только отличающиеся от сетки курса слоты",
    )

    class Meta:
        verbose_name = "группа"
        verbose_name_plural = "группы"
        ordering = ["course__semester", "course__number", "name"]
        constraints = [models.UniqueConstraint(fields=["course", "name"], name="uniq_group_name_in_course")]

    def __str__(self) -> str:
        return self.name

    @property
    def semester(self) -> Semester:
        return self.course.semester

    def clean(self) -> None:
        if (
            self.course_id
            and Group.objects.filter(course__semester_id=self.course.semester_id, name=self.name)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"name": "Группа с таким названием в этом семестре уже есть"})


class Subgroup(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="subgroups", verbose_name="группа")
    name = models.CharField("название", max_length=50)
    size = models.PositiveSmallIntegerField("численность", validators=[MinValueValidator(1)])
    partition = models.CharField(
        "деление",
        max_length=30,
        default="lab",
        help_text="Подгруппы одного деления не пересекаются по студентам (п/г 1 и п/г 2 на лабах); "
        "разных делений — пересекаются",
    )

    class Meta:
        verbose_name = "подгруппа"
        verbose_name_plural = "подгруппы"
        ordering = ["group", "partition", "name"]
        constraints = [models.UniqueConstraint(fields=["group", "partition", "name"], name="uniq_subgroup")]

    def __str__(self) -> str:
        return f"{self.group.name}: {self.name}"


# ====================================================================== справочники организации


class Equipment(models.Model):
    """Оборудование аудиторий — настраиваемый справочник: проектор, компьютеры, компьютеры с RTX,
    ПО (Java) и т.п. Нужно преподавателю (жёстко) или занятию; есть у аудитории."""

    code = models.SlugField("код", max_length=40, unique=True)
    name = models.CharField("название", max_length=120, unique=True)

    class Meta:
        verbose_name = "оборудование"
        verbose_name_plural = "оборудование"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Teacher(models.Model):
    name = models.CharField("ФИО", max_length=150)
    email = models.EmailField("корпоративная почта", null=True, blank=True, unique=True)
    active = models.BooleanField("работает", default=True)
    max_per_day = models.PositiveSmallIntegerField(
        "личный максимум пар в день", null=True, blank=True, help_text="Пусто — лимит семестра"
    )
    max_per_day_hard = models.BooleanField("жёстко", default=False)
    required_equipment = models.ManyToManyField(
        Equipment,
        blank=True,
        related_name="teachers",
        verbose_name="необходимое оборудование",
        help_text="Жёстко: все занятия преподавателя на площадке — только в аудиториях с этим оборудованием",
    )
    preferred_rooms = models.ManyToManyField(
        "Room",
        blank=True,
        related_name="preferred_by",
        verbose_name="предпочтительные аудитории",
        help_text="Мягко: по возможности ставить сюда",
    )
    calendar_token = models.CharField(
        "ключ ссылки на календарь", max_length=40, unique=True, null=True, blank=True, editable=False
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teacher",
        verbose_name="учётная запись",
    )

    class Meta:
        verbose_name = "преподаватель"
        verbose_name_plural = "преподаватели"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = (self.email or "").strip().lower() or None
        super().save(*args, **kwargs)


class TeacherAvailability(models.Model):
    """Сетка доступности преподавателя на семестр (одна на обе недели)."""

    teacher = models.ForeignKey(
        Teacher, on_delete=models.CASCADE, related_name="availability", verbose_name="преподаватель"
    )
    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, related_name="availability", verbose_name="семестр"
    )
    prefs = models.JSONField("сетка −2…+2", default=default_pref_grid, blank=True, validators=[validate_pref_grid])
    submitted_at = models.DateTimeField("заполнено", null=True, blank=True)

    class Meta:
        verbose_name = "доступность преподавателя"
        verbose_name_plural = "доступность преподавателей"
        ordering = ["semester", "teacher__name"]
        constraints = [models.UniqueConstraint(fields=["teacher", "semester"], name="uniq_availability")]

    def __str__(self) -> str:
        return f"{self.teacher} — {self.semester}"


class Department(models.Model):
    """Кафедра: для фильтров и согласования приоритетов (кино, ПиУП, …)."""

    name = models.CharField("название", max_length=150, unique=True)

    class Meta:
        verbose_name = "кафедра"
        verbose_name_plural = "кафедры"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Discipline(models.Model):
    name = models.CharField("название", max_length=200, unique=True)
    department = models.ForeignKey(
        Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="disciplines", verbose_name="кафедра"
    )
    difficulty = models.PositiveSmallIntegerField(
        "сложность", default=0, help_text="0 — обычная; от порога семестра — «тяжёлая»"
    )

    class Meta:
        verbose_name = "дисциплина"
        verbose_name_plural = "дисциплины"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class RoomType(models.Model):
    code = models.SlugField("код", max_length=30, unique=True)
    name = models.CharField("название", max_length=100, unique=True)

    class Meta:
        verbose_name = "тип аудитории"
        verbose_name_plural = "типы аудиторий"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Site(models.Model):
    """Площадка (ВДНХ, свои здания…) — у арендованной может быть жёсткое время закрытия."""

    name = models.CharField("название", max_length=100, unique=True)
    closes_at = models.TimeField(
        "закрывается в",
        null=True,
        blank=True,
        help_text="Пары, которые заканчиваются позже, на площадке не ставятся (кроме занятий «можно закончить "
        "раньше»). Пусто — без ограничения",
    )
    note = models.CharField("примечание", max_length=200, blank=True)

    class Meta:
        verbose_name = "площадка"
        verbose_name_plural = "площадки"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Room(models.Model):
    name = models.CharField("номер", max_length=30, unique=True)
    building = models.CharField("корпус", max_length=50, blank=True)
    site = models.ForeignKey(
        Site, on_delete=models.SET_NULL, null=True, blank=True, related_name="rooms", verbose_name="площадка"
    )
    capacity = models.PositiveSmallIntegerField("вместимость", validators=[MinValueValidator(1)])
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms", verbose_name="тип")
    active = models.BooleanField(
        "используется", default=True, help_text="Снимите, чтобы решатель не брал аудиторию в расчёт вовсе"
    )
    equipment = models.ManyToManyField(Equipment, blank=True, related_name="rooms", verbose_name="оборудование")
    note = models.CharField("примечание", max_length=200, blank=True)

    class Meta:
        verbose_name = "аудитория"
        verbose_name_plural = "аудитории"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class RoomAvailability(models.Model):
    """Слоты семестра, в которые аудитория занята «не нами» (школа, другая площадка, мероприятия)."""

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="availability", verbose_name="аудитория")
    semester = models.ForeignKey(
        Semester, on_delete=models.CASCADE, related_name="room_availability", verbose_name="семестр"
    )
    blocked = models.JSONField("занятые слоты", default=list, blank=True)
    reason = models.CharField("кем занята", max_length=200, blank=True)

    class Meta:
        verbose_name = "занятость аудитории"
        verbose_name_plural = "занятость аудиторий (не нами)"
        ordering = ["semester", "room__name"]
        constraints = [models.UniqueConstraint(fields=["room", "semester"], name="uniq_room_availability")]

    def __str__(self) -> str:
        return f"{self.room} — {self.semester}"

    def clean(self) -> None:
        if not isinstance(self.blocked, list) or any(not isinstance(x, str) for x in self.blocked):
            raise ValidationError({"blocked": "Ожидается список слотов вида «день-пара»"})
        validate_pref_grid({x: -2 for x in self.blocked})


# ====================================================================== нагрузка


class SyncBlock(models.Model):
    """Синхронный (языковой) блок: элементы блока идут в одних и тех же слотах."""

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="blocks", verbose_name="семестр")
    name = models.CharField("название", max_length=150)
    groups = models.ManyToManyField(Group, related_name="sync_blocks", verbose_name="группы (распускаются)")

    class Meta:
        verbose_name = "синхронный блок"
        verbose_name_plural = "синхронные блоки"
        ordering = ["semester", "name"]
        constraints = [models.UniqueConstraint(fields=["semester", "name"], name="uniq_block")]

    def __str__(self) -> str:
        return self.name


class MergeGroup(models.Model):
    """Допустимое объединение: решатель МОЖЕТ свести элементы в одно занятие (со штрафом)."""

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="merges", verbose_name="семестр")
    name = models.CharField("название", max_length=150)

    class Meta:
        verbose_name = "допустимое объединение"
        verbose_name_plural = "допустимые объединения"
        ordering = ["semester", "name"]
        constraints = [models.UniqueConstraint(fields=["semester", "name"], name="uniq_merge")]

    def __str__(self) -> str:
        return self.name


class WorkloadItem(Timestamped):
    class Kind(models.TextChoices):
        LECTURE = "lecture", "лекция"
        PRACTICE = "practice", "практика"
        LAB = "lab", "лаба"

    class Format(models.TextChoices):
        IN_PERSON = "in_person", "очно"
        ONLINE = "online", "онлайн"

    class WeekPattern(models.TextChoices):
        AUTO = "auto", "обычная: каждую неделю (+ мигающая при нечётном числе)"
        ALT_WEEK = "alt_week", "через неделю: все пары в одну неделю, по две подряд"

    class Planning(models.TextChoices):
        AUTO = "auto", "решатель"
        MANUAL = "manual", "вручную"

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="items", verbose_name="семестр")
    discipline = models.ForeignKey(
        Discipline, on_delete=models.PROTECT, related_name="items", verbose_name="дисциплина"
    )
    kind = models.CharField("вид", max_length=10, choices=Kind.choices, default=Kind.PRACTICE)
    teachers = models.ManyToManyField(Teacher, related_name="items", verbose_name="преподаватели")
    groups = models.ManyToManyField(
        Group,
        related_name="items",
        blank=True,
        verbose_name="группы",
        help_text="Несколько групп — фиксированный поток",
    )
    subgroups = models.ManyToManyField(Subgroup, related_name="items", blank=True, verbose_name="подгруппы")
    pairs_per_cycle = models.PositiveSmallIntegerField(
        "пар за 2 недели",
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text="3 = одна пара каждую неделю + одна мигающая",
    )
    format = models.CharField("формат", max_length=10, choices=Format.choices, default=Format.IN_PERSON)
    room_type = models.ForeignKey(
        RoomType, on_delete=models.PROTECT, null=True, blank=True, related_name="items", verbose_name="тип аудитории"
    )
    alt_room_types = models.ManyToManyField(
        RoomType,
        related_name="alt_items",
        blank=True,
        verbose_name="допустимые замены типа",
        help_text="Зарезервировано: пока решатель требует основной тип (docs/OPEN_QUESTIONS.md, п. 5)",
    )
    headcount = models.PositiveSmallIntegerField(
        "численность вручную",
        null=True,
        blank=True,
        help_text="Для языковых подгрупп внутри блока; иначе считается по группам/подгруппам",
    )
    double = models.BooleanField(
        "сдвоенная пара", default=False, help_text="Еженедельные вхождения — подряд в один день"
    )
    week_pattern = models.CharField(
        "схема недель",
        max_length=10,
        choices=WeekPattern.choices,
        default=WeekPattern.AUTO,
        help_text="«Через неделю» — кафедра кино: 1 пара в неделю ставится 2 парами подряд раз в две недели",
    )
    may_end_early = models.BooleanField(
        "можно закончить раньше",
        default=False,
        help_text="К закрытию площадки (киноклуб и т. п.): такому занятию разрешена пара, которая заканчивается "
        "после закрытия, — оно просто кончится раньше",
    )
    teacher_in_room = models.BooleanField(
        "бронь аудитории преподавателю",
        default=False,
        help_text="Для онлайн-занятий: студенты дома, а преподаватель ведёт с площадки — ему нужна аудитория. "
        "Тип можно не указывать: подойдёт любая свободная (все пригодны для онлайна)",
    )
    required_equipment = models.ManyToManyField(
        Equipment, blank=True, related_name="items", verbose_name="оборудование для занятия"
    )
    planning = models.CharField(
        "кто ставит",
        max_length=10,
        choices=Planning.choices,
        default=Planning.AUTO,
        help_text="«Вручную» — элективы и кружки, которые появляются после старта семестра: решатель их не "
        "ставит, но учитывает, когда закреплены все пары",
    )
    priority = models.SmallIntegerField("приоритет", default=0)
    label = models.CharField("подпись", max_length=30, blank=True, help_text="Например, уровень «B1»")
    sync_block = models.ForeignKey(
        SyncBlock,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
        verbose_name="синхронный блок",
    )
    merge_group = models.ForeignKey(
        MergeGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
        verbose_name="допустимое объединение",
    )
    comment = models.CharField("комментарий", max_length=300, blank=True)

    class Meta:
        verbose_name = "элемент нагрузки"
        verbose_name_plural = "нагрузка"
        ordering = ["semester", "discipline__name", "id"]

    def __str__(self) -> str:
        return f"{self.discipline} — {self.participants_display()}"

    def participants_display(self) -> str:
        if not self.pk:
            return "—"
        names = [g.name for g in self.groups.all()] + [str(s) for s in self.subgroups.all()]
        if not names and self.sync_block_id:
            names = [f"блок «{self.sync_block}»"]
        return ", ".join(names) or "—"

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.format == self.Format.IN_PERSON and not self.room_type_id:
            errors["room_type"] = "Очному занятию нужен тип аудитории"
        if self.teacher_in_room and self.format != self.Format.ONLINE:
            errors["teacher_in_room"] = "Бронь аудитории преподавателю — только для онлайн-занятий"
        if self.week_pattern == self.WeekPattern.ALT_WEEK and self.pairs_per_cycle and self.pairs_per_cycle % 2:
            errors["pairs_per_cycle"] = "«Через неделю» ставит пары по две подряд — нужно чётное число пар"
        if self.sync_block_id and self.merge_group_id:
            errors["merge_group"] = "Элемент синхронного блока не может входить в объединение"
        if self.sync_block_id and self.sync_block.semester_id != self.semester_id:
            errors["sync_block"] = "Блок из другого семестра"
        if self.merge_group_id and self.merge_group.semester_id != self.semester_id:
            errors["merge_group"] = "Объединение из другого семестра"
        if errors:
            raise ValidationError(errors)

    def clean_relations(self) -> list[str]:
        """Проверки связей «многие-ко-многим» (доступны только после сохранения)."""
        problems: list[str] = []
        sem = self.semester_id
        if any(g.course.semester_id != sem for g in self.groups.select_related("course")):
            problems.append("группа из другого семестра")
        if any(s.group.course.semester_id != sem for s in self.subgroups.select_related("group__course")):
            problems.append("подгруппа из другого семестра")
        if not self.sync_block_id and not self.groups.exists() and not self.subgroups.exists():
            problems.append("нет участников: укажите группы, подгруппы или синхронный блок")
        if not self.teachers.exists():
            problems.append("не указан преподаватель")
        return problems


class Pin(models.Model):
    """Закрепление: вхождение элемента → слот (и, при желании, аудитория)."""

    item = models.ForeignKey(WorkloadItem, on_delete=models.CASCADE, related_name="pins", verbose_name="элемент")
    week = models.CharField("неделя", max_length=4, choices=Week.choices, default=Week.BOTH)
    day = models.PositiveSmallIntegerField("день", choices=[(d, DAY_NAMES[d]) for d in range(1, 7)])
    pair = models.PositiveSmallIntegerField("пара", validators=[MinValueValidator(1), MaxValueValidator(12)])
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="аудитория")

    class Meta:
        verbose_name = "закрепление"
        verbose_name_plural = "закрепления"
        constraints = [models.UniqueConstraint(fields=["item", "week", "day", "pair"], name="uniq_pin")]

    def __str__(self) -> str:
        return f"{self.item} → {DAY_NAMES.get(self.day, self.day)} {self.pair} ({self.get_week_display()})"


# ====================================================================== запуск и варианты


class WeightPreset(models.Model):
    class Mode(models.TextChoices):
        WEIGHTED = "weighted", "взвешенная сумма"
        LEX = "lex", "лексикографический"

    name = models.CharField("название", max_length=100, unique=True)
    mode = models.CharField("режим", max_length=10, choices=Mode.choices, default=Mode.WEIGHTED)
    weights = models.JSONField("веса", default=dict, blank=True)
    pref_cost = models.JSONField(
        "цена значений шкалы", default=dict, blank=True, help_text='Например {"-1": 10, "1": -1, "2": -2}'
    )
    lex_levels = models.JSONField("уровни (лексикография)", default=list, blank=True)
    is_default = models.BooleanField("по умолчанию", default=False)
    note = models.CharField("согласовано с", max_length=200, blank=True)

    class Meta:
        verbose_name = "пресет весов"
        verbose_name_plural = "пресеты весов"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ScheduleVariant(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "черновик"
        APPROVED = "approved", "утверждён"

    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="variants", verbose_name="семестр")
    name = models.CharField("название", max_length=150)
    status = models.CharField("статус", max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField("создан", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="кто запустил"
    )
    solver = models.CharField("решатель", max_length=30, blank=True)
    solver_status = models.CharField("статус решателя", max_length=20, blank=True)
    params = models.JSONField("параметры запуска", default=dict, blank=True)
    #: Снимок входных данных (scheduler.serialization) — вариант воспроизводим даже после правок справочников.
    problem_snapshot = models.JSONField("снимок входных данных", default=dict, blank=True)
    breakdown = models.JSONField("штраф по критериям", default=dict, blank=True)
    total = models.IntegerField("штраф", null=True, blank=True)
    objective = models.FloatField("целевая функция решателя", null=True, blank=True)
    wall_time_s = models.FloatField("время, с", null=True, blank=True)
    note = models.TextField("заметки", blank=True)

    class Meta:
        verbose_name = "вариант расписания"
        verbose_name_plural = "варианты расписания"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"


class ScheduleAssignment(models.Model):
    """Назначение варианта. *_key — идентификаторы из снимка: вариант читается и
    после удаления элемента или аудитории из справочников."""

    variant = models.ForeignKey(
        ScheduleVariant, on_delete=models.CASCADE, related_name="assignments", verbose_name="вариант"
    )
    item = models.ForeignKey(
        WorkloadItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments",
        verbose_name="элемент нагрузки",
    )
    item_key = models.CharField(max_length=40)
    week = models.CharField("неделя", max_length=4, choices=Week.choices)
    day = models.PositiveSmallIntegerField("день", choices=[(d, DAY_NAMES[d]) for d in range(1, 7)])
    pair = models.PositiveSmallIntegerField("пара")
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="аудитория")
    room_key = models.CharField(max_length=40, blank=True)
    teacher_remote = models.BooleanField("преподаватель удалённо", default=False)

    class Meta:
        verbose_name = "назначение"
        verbose_name_plural = "назначения"
        ordering = ["variant", "day", "pair"]
        indexes = [models.Index(fields=["variant", "day", "pair"])]

    def __str__(self) -> str:
        return f"{self.item_key} → {DAY_NAMES.get(self.day, self.day)} {self.pair} ({self.week})"
