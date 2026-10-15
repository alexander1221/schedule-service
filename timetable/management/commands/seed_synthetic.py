"""Загрузить в БД синтетический семестр (для разработки и демонстрации)."""

from django.core.management.base import BaseCommand, CommandError

from scheduler.generator import ALT_DISCIPLINES, GenParams, generate
from timetable import models as m
from timetable.services.problem import import_problem


class Command(BaseCommand):
    help = "Создать семестр с синтетическими данными масштаба колледжа"

    def add_arguments(self, parser):
        parser.add_argument("--name", default=None, help="название семестра (по умолчанию «Синтетика seed=N»)")
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument("--tightness", type=float, default=0.5)
        parser.add_argument("--groups", type=int, default=25)
        parser.add_argument("--teachers", type=int, default=45)
        parser.add_argument("--if-empty", action="store_true", help="ничего не делать, если в БД уже есть семестры")

    def handle(self, *args, **o):
        if o["if_empty"] and m.Semester.objects.exists():
            self.stdout.write("Семестры уже есть — синтетика не загружается (--if-empty)")
            return
        name = o["name"] or f"Синтетика seed={o['seed']} t={o['tightness']}"
        if m.Semester.objects.filter(name=name).exists():
            raise CommandError(f"Семестр «{name}» уже есть — укажите другое --name")
        g = generate(
            GenParams(seed=o["seed"], tightness=o["tightness"], n_groups=o["groups"], n_teachers=o["teachers"])
        )
        sem, _ = import_problem(g.problem, name)
        # кафедры для фильтров: дисциплины «через неделю» — кино, дистант с бронью — ПиУП
        kino, _ = m.Department.objects.get_or_create(name="Кино")
        piup, _ = m.Department.objects.get_or_create(name="ПиУП")
        m.Discipline.objects.filter(name__in=[n for n, _ in ALT_DISCIPLINES]).update(department=kino)
        m.Discipline.objects.filter(items__semester=sem, items__teacher_in_room=True).update(department=piup)
        self.stdout.write(
            self.style.SUCCESS(
                f"Семестр «{sem}» (id={sem.pk}): {len(g.problem.groups)} групп, "
                f"{len(g.problem.items)} элементов нагрузки"
            )
        )
