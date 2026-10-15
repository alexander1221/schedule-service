"""Фоновый запуск расчёта (этап 5): очередь в БД, обработчик, прогресс, остановка.

Очередь — таблица :class:`~timetable.models.SolveRun`. Кнопка в админке ставит запуск в очередь
(:func:`enqueue`), обработчик ``manage.py solve_worker`` забирает его (:func:`claim_next`) и считает
(:func:`execute`). Пока идёт расчёт, отдельный поток раз в несколько секунд пишет в запись прогресс,
журнал и «последний отклик» и читает флажок остановки. Одновременно у семестра может быть только
один активный запуск — это гарантирует ограничение в БД, а не проверка в коде.

Почему не Procrastinate из ТЗ: ему нужен PostgreSQL, а пилот и разработка идут на SQLite;
задача одна, редкая и долгая — своей таблицы хватает (решение заказчика, 0.8.0).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Value
from django.db.models.functions import Concat
from django.utils import timezone

from scheduler.solver import Progress, SolveParams, SolveResult, Status

from .. import models as m
from .problem import ProblemBuildError
from .variants import load_variant, profile_from_preset, run_and_save

log = logging.getLogger(__name__)

R = m.SolveRun.Status
#: Без отклика дольше этого — считаем, что обработчик остановили посреди расчёта.
STALE_AFTER = dt.timedelta(minutes=2)
#: Как часто поток прогресса пишет в БД и проверяет флажок остановки, с.
FLUSH_EVERY_S = 2.0
LOG_MAX_CHARS = 50_000


class RunConflict(Exception):
    """У семестра уже есть запуск в очереди или в работе."""

    def __init__(self, active: m.SolveRun | None):
        self.active = active
        super().__init__("Для этого семестра уже идёт расчёт — дождитесь его или остановите.")


def max_workers() -> int:
    return int(settings.SOLVER_MAX_WORKERS)


def active_run(sem: m.Semester) -> m.SolveRun | None:
    return m.SolveRun.objects.filter(semester=sem, status__in=m.SolveRun.ACTIVE).first()


def enqueue(
    sem: m.Semester,
    *,
    time_limit_s: float = 300,
    variants: int = 3,
    workers: int | None = None,
    seed: int = 0,
    min_diff: int = 10,
    preset: m.WeightPreset | None = None,
    reference: m.ScheduleVariant | None = None,
    name_prefix: str = "Вариант",
    user: Any = None,
    start_now: bool = False,
) -> m.SolveRun:
    """Поставить расчёт в очередь. ``start_now`` — сразу «считается» (запуск из командной строки,
    который считает сам, без обработчика). Второй активный запуск семестра → :class:`RunConflict`."""
    if reference is not None and reference.semester_id != sem.pk:
        raise ValueError("Эталонный вариант из другого семестра")
    now = timezone.now()
    try:
        with transaction.atomic():
            return m.SolveRun.objects.create(
                semester=sem,
                status=R.RUNNING if start_now else R.QUEUED,
                started_at=now if start_now else None,
                heartbeat_at=now if start_now else None,
                created_by=user if getattr(user, "pk", None) else None,
                time_limit_s=time_limit_s,
                variants_requested=variants,
                workers=max(1, min(workers or max_workers(), max_workers())),
                seed=seed,
                min_diff=min_diff,
                preset=preset,
                reference=reference,
                name_prefix=name_prefix or "Вариант",
            )
    except IntegrityError as e:
        raise RunConflict(active_run(sem)) from e


def request_cancel(run: m.SolveRun) -> m.SolveRun:
    """Остановить: из очереди — снимается сразу; идущий — останавливается за несколько секунд,
    найденные к этому моменту варианты сохраняются."""
    dequeued = m.SolveRun.objects.filter(pk=run.pk, status=R.QUEUED).update(
        status=R.CANCELLED, finished_at=timezone.now(), message="Снят с очереди до начала расчёта."
    )
    if not dequeued:
        m.SolveRun.objects.filter(pk=run.pk, status=R.RUNNING).update(cancel_requested=True)
    run.refresh_from_db()
    return run


def claim_next() -> m.SolveRun | None:
    """Забрать самый старый запуск из очереди. Атомарно: условное UPDATE — два обработчика
    не возьмут один запуск (и на SQLite, и на PostgreSQL)."""
    for pk in m.SolveRun.objects.filter(status=R.QUEUED).order_by("created_at", "pk").values_list("pk", flat=True)[:5]:
        now = timezone.now()
        if m.SolveRun.objects.filter(pk=pk, status=R.QUEUED).update(status=R.RUNNING, started_at=now, heartbeat_at=now):
            return m.SolveRun.objects.select_related("semester", "preset", "reference", "created_by").get(pk=pk)
    return None


def recover_stale(now: dt.datetime | None = None) -> int:
    """Запуски «считается» без отклика дольше STALE_AFTER → «не построено» (обработчик остановили)."""
    now = now or timezone.now()
    return m.SolveRun.objects.filter(status=R.RUNNING, heartbeat_at__lt=now - STALE_AFTER).update(
        status=R.FAILED,
        finished_at=now,
        message="Обработчик расчётов был остановлен во время расчёта (перезапуск сервера?). Запустите расчёт заново.",
    )


class ProgressLog:
    """Строки журнала из прогресса решателя: только заметные улучшения (≥5%) и не реже раза
    в 30 с, чтобы было видно, когда на самом деле пришло последнее улучшение."""

    def __init__(self) -> None:
        self._last: dict[tuple[int, str], tuple[float, float]] = {}

    def __call__(self, pr: Progress) -> str | None:
        if pr.objective is None:
            return f"вариант {pr.variant} [{pr.stage}] {pr.elapsed_s:.0f} с"
        key = (pr.variant, pr.stage)
        prev = self._last.get(key)
        if prev is None or pr.objective < prev[0] - max(1.0, abs(prev[0]) * 0.05) or pr.elapsed_s - prev[1] >= 30:
            self._last[key] = (pr.objective, pr.elapsed_s)
            return f"вариант {pr.variant} [{pr.stage}] {pr.elapsed_s:.0f} с: оценка штрафа по слотам {pr.objective:.0f}"
        return None


class _Reporter:
    """Мост «решатель → БД». Решатель зовёт :meth:`progress` из своих потоков — тут только
    память под замком; в БД пишет один поток раз в FLUSH_EVERY_S (у него своё соединение)."""

    def __init__(self, run_pk: int, of: int, echo: Callable[[str], None] | None = None):
        self.run_pk, self.of, self.echo = run_pk, of, echo
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Progress | None = None
        self._lines: list[str] = []
        self._fmt = ProgressLog()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"solve-run-{run_pk}", daemon=True)

    def progress(self, pr: Progress) -> None:
        line = self._fmt(pr)
        with self._lock:
            self._latest = pr
            if line:
                self._lines.append(line)
        if line and self.echo:
            self.echo("  " + line)

    def log(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
        if self.echo:
            self.echo(line)

    def __enter__(self) -> _Reporter:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._thread.join()
        self.flush()  # последние строки — из основного потока

    def _loop(self) -> None:
        try:
            while not self._done.wait(FLUSH_EVERY_S):
                try:
                    self.flush()
                except Exception:  # БД недоступна на миг — не роняем расчёт
                    log.exception("Не удалось записать прогресс запуска %s", self.run_pk)
        finally:
            connection.close()  # соединение этого потока

    def flush(self) -> None:
        with self._lock:
            pr, lines, self._lines = self._latest, self._lines, []
        upd: dict[str, Any] = {"heartbeat_at": timezone.now()}
        if pr is not None:
            upd["progress"] = {
                "variant": pr.variant,
                "of": self.of,
                "stage": pr.stage,
                "elapsed_s": round(pr.elapsed_s, 1),
                "solutions": pr.solutions,
                "objective": pr.objective,
                "bound": pr.bound,
                "gap": pr.gap,
            }
        if lines:
            stamp = timezone.localtime().strftime("%H:%M:%S")
            upd["log"] = Concat(F("log"), Value("".join(f"{stamp} {s}\n" for s in lines)))
        qs = m.SolveRun.objects.filter(pk=self.run_pk)
        qs.update(**upd)
        if qs.filter(cancel_requested=True).exists():
            self.stop.set()


def execute(
    run: m.SolveRun, echo: Callable[[str], None] | None = None
) -> tuple[SolveResult | None, list[m.ScheduleVariant]]:
    """Посчитать запуск (уже в состоянии «считается») и записать итог. Исключения не выпускает:
    любая ошибка становится состоянием «не построено» с понятным текстом."""
    result: SolveResult | None = None
    saved: list[m.ScheduleVariant] = []
    status, message = R.FAILED, ""
    interrupted = False
    with _Reporter(run.pk, run.variants_requested, echo) as rep:
        try:
            reference = load_variant(run.reference)[1] if run.reference else None
            params = SolveParams(
                time_limit_s=run.time_limit_s,
                workers=run.workers,
                seed=run.seed,
                variants=run.variants_requested,
                min_diff=run.min_diff,
                profile=profile_from_preset(run.preset),
                reference=reference,
                stop=rep.stop.is_set,
            )
            rep.log(
                f"Старт: {run.variants_requested} вар. × {run.time_limit_s:.0f} с, ядер {run.workers}, "
                f"пресет «{params.profile.name}»" + (f", пересборка от «{run.reference}»" if run.reference else "")
            )
            result, saved = run_and_save(
                run.semester, params, name_prefix=run.name_prefix, user=run.created_by, progress=rep.progress, run=run
            )
        except KeyboardInterrupt:  # Ctrl+C вне поиска (во время поиска его перехватывает сам CP-SAT)
            status, message, interrupted = R.CANCELLED, "Прервано в окне расчёта (Ctrl+C).", True
        except ProblemBuildError as e:
            message = f"Задача не собрана: {e}"
        except Exception as e:
            log.exception("Запуск %s упал", run.pk)
            message = f"Внутренняя ошибка решателя: {e}. Подробности — в окне/журнале обработчика расчётов."
        else:
            if saved:
                status = R.CANCELLED if result.stopped else R.DONE
                message = f"Сохранено вариантов: {len(saved)} из {run.variants_requested}."
                if result.stopped:
                    message += " Расчёт остановлен досрочно — сохранено найденное к этому моменту."
                elif len(saved) < run.variants_requested:
                    message += " Больше достаточно разных вариантов не нашлось."
            else:
                status = R.CANCELLED if result.status is Status.CANCELLED else R.FAILED
                message = "\n".join(result.diagnostics) or f"Расписание не построено ({result.status.value})."
        rep.log("Итог: " + (message.splitlines() or [""])[0])
    run_qs = m.SolveRun.objects.filter(pk=run.pk)
    run_qs.update(
        status=status,
        message=message,
        finished_at=timezone.now(),
        solver_status=result.status.value if result else "",
    )
    _trim_log(run.pk)
    run.refresh_from_db()
    if interrupted:
        raise KeyboardInterrupt
    return result, saved


def _trim_log(pk: int) -> None:
    text = m.SolveRun.objects.filter(pk=pk).values_list("log", flat=True).first() or ""
    if len(text) > LOG_MAX_CHARS:
        cut = text[-LOG_MAX_CHARS:]
        m.SolveRun.objects.filter(pk=pk).update(log="…(начало журнала обрезано)\n" + cut[cut.find("\n") + 1 :])
