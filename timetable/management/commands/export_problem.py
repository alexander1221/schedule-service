"""Выгрузить семестр как JSON-задачу ядра (тот же формат, что у schedule_cli.py)."""

from django.core.management.base import BaseCommand, CommandError

from scheduler.precheck import precheck
from scheduler.serialization import problem_to_dict, save_json
from timetable import models as m
from timetable.services.problem import ProblemBuildError, build_problem


class Command(BaseCommand):
    help = "Собрать задачу из БД и сохранить в JSON; заодно показать быстрые проверки"

    def add_arguments(self, parser):
        parser.add_argument("semester", help="id или название семестра")
        parser.add_argument("-o", "--output", default="data/problem_from_db.json")

    def handle(self, *args, **o):
        sem = get_semester(o["semester"])
        try:
            problem = build_problem(sem)
        except ProblemBuildError as e:
            raise CommandError(str(e)) from e
        save_json(problem_to_dict(problem), o["output"])
        for f in precheck(problem):
            style = self.style.ERROR if f.level == "error" else self.style.WARNING
            self.stdout.write(style(f"{f.level}: {f.message}"))
        self.stdout.write(self.style.SUCCESS(f"Задача «{sem}»: {len(problem.items)} элементов → {o['output']}"))


def get_semester(key: str) -> m.Semester:
    qs = m.Semester.objects.filter(pk=int(key)) if key.isdigit() else m.Semester.objects.filter(name=key)
    sem = qs.first()
    if sem is None:
        names = ", ".join(f"{s.pk}: {s.name}" for s in m.Semester.objects.all()[:10]) or "нет ни одного"
        raise CommandError(f"Семестр «{key}» не найден. Есть: {names}")
    return sem
