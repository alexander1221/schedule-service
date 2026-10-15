"""Построить варианты расписания семестра и сохранить их в БД (синхронно; фон — этап 5)."""

import os

from django.core.management.base import BaseCommand, CommandError

from scheduler.solver import SolveParams
from scheduler.solver.base import Status
from timetable import models as m
from timetable.services.problem import ProblemBuildError
from timetable.services.variants import default_preset, load_variant, profile_from_preset, run_and_save

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
            reference = load_variant(v)[1]
        params = SolveParams(
            time_limit_s=o["time"],
            workers=o["workers"],
            seed=o["seed"],
            variants=o["variants"],
            min_diff=o["min_diff"],
            profile=profile_from_preset(preset),
            reference=reference,
        )

        last: dict[tuple[int, str], tuple[float, float]] = {}

        def progress(pr):
            # печатаем заметные улучшения (≥5%) и не реже раза в 30 с — чтобы по
            # консоли было видно, когда на самом деле пришло последнее улучшение
            key = (pr.variant, pr.stage)
            if pr.objective is None:
                self.stdout.write(f"  вариант {pr.variant} [{pr.stage}] {pr.elapsed_s:.0f} с")
                return
            prev = last.get(key)
            if prev is None or pr.objective < prev[0] - max(1.0, abs(prev[0]) * 0.05) or pr.elapsed_s - prev[1] >= 30:
                last[key] = (pr.objective, pr.elapsed_s)
                self.stdout.write(
                    f"  вариант {pr.variant} [{pr.stage}] {pr.elapsed_s:.0f} с: "
                    f"оценка штрафа по слотам {pr.objective:.0f}"
                )

        try:
            result, saved = run_and_save(sem, params, progress=progress)
        except ProblemBuildError as e:
            raise CommandError(str(e)) from e
        if result.status in (Status.PRECHECK_FAILED, Status.INFEASIBLE, Status.UNKNOWN):
            for line in result.diagnostics:
                self.stdout.write(self.style.ERROR(line))
            raise CommandError(f"Расписание не построено: {result.status.value}")
        for v in saved:
            rooms = _room_part(v.breakdown, params.profile)
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
