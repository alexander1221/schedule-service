"""Атомарный перенос закреплений и постановка пересборки в очередь."""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from scheduler.domain import Week, weeks_of

from .. import models as m
from .runs import enqueue
from .variants import default_preset, load_variant


class RebuildError(ValueError):
    pass


@dataclass(frozen=True)
class LessonMove:
    item_id: int
    week: Week
    source: str
    target: str
    room_id: int | None = None


@transaction.atomic
def rebuild_variant(
    variant: m.ScheduleVariant,
    moves: list[LessonMove],
    *,
    time_limit_s: int = 300,
    variants: int = 1,
    user: Any = None,
) -> m.SolveRun:
    """Заменить только закрепления переносимых вхождений; любая ошибка откатывает всё."""
    sem = m.Semester.objects.select_for_update().get(pk=variant.semester_id)
    problem, schedule = load_variant(variant)
    if not moves:
        raise RebuildError("Нет переносов для пересборки")
    sources = {(a.item_id, a.week, a.slot_id) for a in schedule.assignments}
    by_source = {(str(mv.item_id), mv.week, mv.source): mv for mv in moves}
    if len(by_source) != len(moves):
        raise RebuildError("Одно занятие указано для переноса несколько раз")
    items = set(m.WorkloadItem.objects.filter(semester=sem).values_list("pk", flat=True))
    rooms = set(m.Room.objects.values_list("pk", flat=True))
    slots = {f"{d}-{p}" for d in sem.days for p in sem.pair_numbers()}
    for key, mv in by_source.items():
        if key not in sources:
            raise RebuildError(f"В исходном варианте нет занятия {mv.item_id}: {mv.source}, {mv.week.value}")
        if mv.item_id not in items:
            raise RebuildError(f"Элемент нагрузки {mv.item_id} удалён или относится к другому семестру")
        if mv.target not in slots:
            raise RebuildError(f"Нет слота {mv.target} в сетке семестра")
        if mv.room_id is not None and mv.room_id not in rooms:
            raise RebuildError(f"Нет аудитории {mv.room_id}")
        block = problem.block_of_item.get(key[0])
        if block:
            for iid in block.item_ids:
                other = by_source.get((iid, mv.week, mv.source))
                if other is None or other.target != mv.target:
                    raise RebuildError(f"Блок «{block.name}» нужно переносить целиком в одно время")

    # Не позволяем двумя переносами закрепить одно и то же вхождение дважды.
    occupied: Counter[tuple[str, Week, str]] = Counter()
    for a in schedule.assignments:
        mv = by_source.get((a.item_id, a.week, a.slot_id))
        for week in weeks_of(a.week):
            occupied[(a.item_id, week, mv.target if mv else a.slot_id)] += 1
    if any(n > 1 for n in occupied.values()):
        raise RebuildError("Две пары одного элемента нагрузки перенесены в одно время")

    run = enqueue(
        sem,
        time_limit_s=time_limit_s,
        variants=variants,
        reference=variant,
        preset=default_preset(),
        name_prefix="Перенос",
        user=user,
    )
    moved_weeks: dict[tuple[int, str], set[Week]] = {}
    for mv in moves:
        moved_weeks.setdefault((mv.item_id, mv.source), set()).update(weeks_of(mv.week))
    pins = list(m.Pin.objects.select_for_update().filter(item_id__in=[mv.item_id for mv in moves]))
    for pin in pins:
        moved = moved_weeks.get((pin.item_id, f"{pin.day}-{pin.pair}"), set())
        covered = set(weeks_of(Week(pin.week)))
        if not covered & moved:
            continue
        pin.delete()
        # При переносе только нечётной недели сохраняем закрепление чётной (и наоборот).
        for week in covered - moved:
            m.Pin.objects.get_or_create(
                item_id=pin.item_id, week=week.value, day=pin.day, pair=pin.pair, defaults={"room_id": pin.room_id}
            )
    for mv in moves:
        day, pair = map(int, mv.target.split("-"))
        # Время закрепляем, аудиторию решатель вправе подобрать заново, если её не указали явно.
        pin, created = m.Pin.objects.get_or_create(
            item_id=mv.item_id, week=mv.week.value, day=day, pair=pair, defaults={"room_id": mv.room_id}
        )
        if not created and mv.room_id is not None and pin.room_id != mv.room_id:
            raise RebuildError("В целевом слоте уже закреплена другая аудитория; обновите закрепления")
    return run
