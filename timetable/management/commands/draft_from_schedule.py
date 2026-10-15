"""Черновик справочников и нагрузки из текущего расписания учебного отдела (xlsx)."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from timetable.importers.current_schedule import aggregate, parse_workbook
from timetable.importers.draft_writer import references_xlsx, report_xlsx, workload_xlsx


class Command(BaseCommand):
    help = "Из текущего расписания (xlsx) сделать черновики: справочники, нагрузка, отчёт о сомнительном"

    def add_arguments(self, parser):
        parser.add_argument("path", help="xlsx с текущим расписанием")
        parser.add_argument("-o", "--output", default="output/draft", help="каталог для черновиков")

    def handle(self, *args, **o):
        src = Path(o["path"])
        if not src.is_file():
            raise CommandError(f"Файл не найден: {src}")
        d = parse_workbook(src)
        items, blocks = aggregate(d)
        out = Path(o["output"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "references_draft.xlsx").write_bytes(references_xlsx(d, items))
        (out / "workload_draft.xlsx").write_bytes(workload_xlsx(d, items, blocks))
        (out / "report.xlsx").write_bytes(report_xlsx(d, items))
        self.stdout.write(
            self.style.SUCCESS(
                f"Аудиторий {len(d.rooms)}, групп {len(d.groups)}, преподавателей {len(d.teachers)}, "
                f"элементов нагрузки {len(items)}, сомнительных мест {len(d.issues)} → {out}/"
            )
        )
