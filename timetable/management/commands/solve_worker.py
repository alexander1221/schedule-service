"""Обработчик расчётов: забирает запуски из очереди (кнопка «Построить расписание») и считает их."""

import signal
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections

from timetable.services.runs import claim_next, execute, max_workers, recover_stale


class Command(BaseCommand):
    help = "Обработчик фоновых расчётов расписания (держать запущенным рядом с сайтом)"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="обработать не больше одного запуска и выйти")
        parser.add_argument("--poll", type=float, default=3.0, help="как часто проверять очередь, с")

    def handle(self, *args, **o):
        self._stop = False

        def on_term(signum, frame):  # docker stop: доделать текущий опрос и выйти
            self._stop = True

        signal.signal(signal.SIGTERM, on_term)
        self.stdout.write(
            f"Обработчик расчётов запущен (ядер решателю: не больше {max_workers()}). "
            "Окно не закрывайте; остановка — Ctrl+C."
        )
        warned = False
        while not self._stop:
            close_old_connections()  # долгоживущий процесс: соединение могло устареть
            try:
                n = recover_stale()
                if n:
                    self.stdout.write(self.style.WARNING(f"Прерванных расчётов отмечено: {n}"))
                run = claim_next()
                warned = False
            except DatabaseError as e:  # таблиц ещё нет (миграции идут) или БД недоступна
                if not warned:
                    self.stdout.write(self.style.WARNING(f"База недоступна, жду: {e}"))
                    warned = True
                run = None
            if run is None:
                if o["once"]:
                    break
                time.sleep(o["poll"])
                continue
            self.stdout.write(f"Расчёт №{run.pk}: {run.semester}")
            execute(run, echo=self.stdout.write)
            style = self.style.SUCCESS if run.status == run.Status.DONE else self.style.WARNING
            self.stdout.write(style(f"Расчёт №{run.pk}: {run.get_status_display()}"))
            if o["once"]:
                break
