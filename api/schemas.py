"""Схемы запросов и ответов API (pydantic через django-ninja). Поля и описания — по-русски."""

import datetime as dt
from typing import Any, Literal

from ninja import Field, Schema

WeekIn = Literal["both", "odd", "even"]


class Error(Schema):
    detail: str


class Ref(Schema):
    id: int
    name: str


class Me(Schema):
    email: str
    name: str
    version: str
    permissions: list[str]


# ------------------------------------------------------------------ семестры


class SemesterOut(Schema):
    id: int
    name: str
    start_date: dt.date
    end_date: dt.date | None
    status: str
    days: list[int]
    pairs: list[int]
    active_run_id: int | None
    approved_variant_id: int | None


class PrecheckOut(Schema):
    ok: bool = Field(description="ошибок нет (решение не исключено)")
    build_error: str = ""
    errors: list[str]
    warnings: list[str]
    hints: list[str]
    stats: dict[str, int]


class TeacherOut(Schema):
    id: int
    name: str
    active: bool
    items: int = Field(description="элементов нагрузки в семестре")
    has_prefs: bool = Field(description="сетка пожеланий на семестр заполнена")


class PrefsOut(Schema):
    teacher: Ref
    semester_id: int
    prefs: dict[str, int] = Field(description="«день-пара» → −2…+2; нет ключа — «всё равно» (0)")
    days: list[int]
    pairs: list[int]
    submitted_at: dt.datetime | None


class PrefsPatch(Schema):
    set: dict[str, int] = Field(default_factory=dict, description="«день-пара» → −2…+2 (−2 — нельзя)")
    reset: list[str] = Field(default_factory=list, description="слоты, вернуть в «всё равно»")


class PinOut(Schema):
    id: int
    item_id: int
    week: str
    day: int
    pair: int
    room_id: int | None
    room: str


class ItemOut(Schema):
    id: int
    discipline: str
    kind: str
    format: str
    planning: str
    pairs_per_cycle: int
    teachers: list[Ref]
    groups: list[Ref]
    pins: list[PinOut]


class PinIn(Schema):
    item_id: int
    week: WeekIn = "both"
    day: int = Field(ge=1, le=6)
    pair: int = Field(ge=1, le=12)
    room_id: int | None = None


# ------------------------------------------------------------------ запуски расчёта


class RunIn(Schema):
    time_limit_s: int = Field(300, ge=10, le=7200, description="время на один вариант, с")
    variants: int = Field(3, ge=1, le=10)
    preset_id: int | None = Field(None, description="пресет весов; пусто — отмеченный «по умолчанию»")
    reference_variant_id: int | None = Field(None, description="пересборка от варианта (минимум изменений)")
    workers: int | None = Field(None, ge=1, description="ядер; не больше SOLVER_MAX_WORKERS")
    seed: int = 0
    min_diff: int = Field(10, ge=0)
    name_prefix: str = Field("Вариант", max_length=80)


class RunOut(Schema):
    id: int
    semester_id: int
    status: str = Field(description="queued | running | done | failed | cancelled")
    status_label: str
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    elapsed_s: float | None
    time_limit_s: float
    variants_requested: int
    workers: int
    preset_id: int | None
    reference_variant_id: int | None
    cancel_requested: bool
    progress: dict[str, Any] = Field(description="variant, of, stage, elapsed_s, objective, bound, gap")
    message: str
    solver_status: str
    variant_ids: list[int]
    log: str | None = None


class Conflict(Schema):
    detail: str
    active_run_id: int | None


# ------------------------------------------------------------------ варианты


class VariantOut(Schema):
    id: int
    semester_id: int
    name: str
    status: str = Field(description="draft | approved")
    total: int | None = Field(description="штраф по независимой проверке; меньше — лучше")
    solver_status: str
    created_at: dt.datetime
    wall_time_s: float | None
    run_id: int | None
    changes: int | None = Field(description="при пересборке: занятий сдвинуто по времени относительно эталона")
    room_changes: int | None = Field(description="при пересборке: то же время, но другая аудитория, чем в эталоне")


class Crit(Schema):
    code: str
    label: str
    count: int


class VariantDetail(VariantOut):
    breakdown: list[Crit]
    params: dict[str, Any]
    note: str


class Violation(Schema):
    code: str
    message: str
    slot: str | None = Field(None, description="где: «день-пара»; пусто — не привязано к клетке")
    week: str | None = Field(None, description="odd | even; пусто — обе недели или не важно")
    items: list[int] = Field(default_factory=list, description="элементы нагрузки, из-за которых нарушение")


class Move(Schema):
    item_id: int
    week: WeekIn = "both"
    day: int
    pair: int
    to_day: int
    to_pair: int
    to_room_id: int | None = Field(None, description="пусто — аудитория прежняя")


class CheckIn(Schema):
    moves: list[Move] = Field(default_factory=list, description="перенести занятия и проверить (вариант не меняется)")


class RebuildIn(Schema):
    moves: list[Move] = Field(min_length=1, description="переносы из исходного варианта")
    time_limit_s: int = Field(300, ge=10, le=7200)
    variants: int = Field(1, ge=1, le=10)


class CheckOut(Schema):
    ok: bool = Field(description="жёстких нарушений нет")
    hard: list[Violation]
    warnings: list[Violation]
    total: int
    base_total: int = Field(description="штраф варианта без переносов")
    delta: int
    breakdown: list[Crit]


class Entity(Schema):
    id: str
    name: str


class Lesson(Schema):
    item_id: int
    week: str = Field(description="both | odd | even — неделя назначения (для переноса в /check)")


class GridEntry(Schema):
    discipline: str
    teachers: str
    groups: str
    room: str
    marks: list[str]
    lessons: list[Lesson] = Field(description="что стоит в клетке: элементы нагрузки и их недели")
    block_id: str = Field("", description="синхронный блок (английский по подгруппам); пусто — не блок")
    block: str = Field("", description="название блока")


class GridRow(Schema):
    pair: int
    time: str
    cells: list[list[GridEntry]] = Field(description="по дням недели (см. days)")


class GridOut(Schema):
    by: str
    key: str
    name: str
    week: str
    days: list[str]
    rows: list[GridRow]


class FreeRoomOut(Schema):
    room_id: str
    name: str
    capacity: int
    type_name: str
    equipment: str
    free_pairs: list[int]
    fully_free: bool


class PlacementOut(Schema):
    week: str
    day: int
    pair: int
    room: str
    label: str


class ChangeOut(Schema):
    item_id: str
    discipline: str
    groups: list[str]
    teachers: list[str]
    kind: str = Field(description="перенос | аудитория | добавлено | убрано")
    before: list[PlacementOut]
    after: list[PlacementOut]


class DiffOut(Schema):
    variant_id: int
    against_variant_id: int
    moved: int = Field(description="занятий (по неделям цикла) в другое время")
    room_changed: int
    affected_groups: list[str]
    affected_teachers: list[str]
    affected_rooms: list[str]
    changes: list[ChangeOut]
