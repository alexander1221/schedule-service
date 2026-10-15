"""Создать пустой семестр с сеткой пар и курсами по умолчанию."""

import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from timetable import models as m
from timetable.services.problem import create_semester


class Command(BaseCommand):
    help = "Создать семестр: сетка 7 пар (МСК), курсы с лимитами и сетками смен по умолчанию"

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("start_date", help="ГГГГ-ММ-ДД")
        parser.add_argument("--courses", type=int, default=4)
        parser.add_argument("--saturday", action="store_true")
        parser.add_argument("--first-week", choices=["odd", "even"], default="odd")

    def handle(self, *args, **o):
        if m.Semester.objects.filter(name=o["name"]).exists():
            raise CommandError(f"Семестр «{o['name']}» уже есть")
        try:
            start = dt.date.fromisoformat(o["start_date"])
        except ValueError as e:
            raise CommandError(f"Дата начала: {e}") from e
        sem = create_semester(o["name"], start, o["courses"], o["saturday"], o["first_week"])
        self.stdout.write(self.style.SUCCESS(f"Создан семестр «{sem}» (id={sem.pk})"))
