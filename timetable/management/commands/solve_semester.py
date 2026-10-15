"""Построить варианты расписания семестра в этом окне (без очереди) и сохранить их в БД.

Запуск попадает в общую историю «Запуски расчёта» и тоже не даёт считать семестр дважды одновременно.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from timetable import models as m
from timetable.services.runs import RunConflict, enqueue, execute
from timetable.services.variants import default_preset, profile_from_preset

from .export_problem import get_semester


class Command(BaseCommand):
    help = "Запустить решатель для семестра и сохранить варианты"

    def add_arguments(self, parser):
        parser.add_argument("semester", help="id или название семестра")
        parser.add_argument("--time", type=float, default=60, help="лимит на вариант, с")
        parser.add_argument("--variants", type=int, default=1)
        parser.add_argument("--min-diff", type=int, default=10)
        parser.add_argument("--workers", type=int, default=os.cpu_count() or 8)
        parser.add_argument("--seed", type=int, default=0)
        parser.add_argument("--preset", help="название пресета весов (по умолчанию — отмеченный «по умолчанию»)")
        parser.add_argument("--reference-variant", type=int, help="id варианта: пересборка с минимальными изменениями")

    def handle(self, *args, **o):
        sem = get_semester(o["semester"])
        preset = None
        if o["preset"]:
            preset = m.WeightPreset.objects.filter(name=o["preset"]).first()
            if preset is None:
                raise CommandError(f"Пресет «{o['preset']}» не найден")
        else:
            preset = default_preset()
        reference = None
        if o["reference_variant"]:
            v = m.ScheduleVariant.objects.filter(pk=o["reference_variant"], semester=sem).first()
            if v is None:
                raise CommandError("Эталонный вариант не найден в этом семестре")
            reference = v
        try:
            run = enqueue(
                sem,
                time_limit_s=o["time"],
                variants=o["variants"],
                workers=o["workers"],
                seed=o["seed"],
                min_diff=o["min_diff"],
                preset=preset,
                reference=reference,
                start_now=True,
            )
        except RunConflict as e:
            raise CommandError(f"{e} (запуск №{e.active.pk if e.active else '?'})") from e
        self.stdout.write(f"Расчёт №{run.pk} (виден в админке: «Запуски расчёта»); Ctrl+C — прервать")
        _, saved = execute(run, echo=self.stdout.write)
        if not saved:
            for line in run.message.splitlines():
                self.stdout.write(self.style.ERROR(line))
            raise CommandError(f"Расписание не построено: {run.get_status_display()}")
        profile = profile_from_preset(preset)
        for v in saved:
            rooms = _room_part(v.breakdown, profile)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Сохранён «{v.name}» (id={v.pk}): штраф {v.total} = расстановка по слотам "
                    f"{v.total - rooms} + выбор аудиторий {rooms} (точный пересчёт независимой проверкой)"
                )
            )


def _room_part(breakdown: dict[str, int], profile) -> int:  # type: ignore[no-untyped-def]
    """Часть штрафа, которую даёт этап 2 (выбор аудиторий): в прогрессе её ещё нет."""
    from scheduler.criteria import ROOM_STAGE_CRITS, WeightProfile

    prof = profile or WeightProfile()
    return sum(prof.weight(c) * breakdown.get(c.value, 0) for c in ROOM_STAGE_CRITS)
