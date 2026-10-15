"""Фоновые расчёты (этап 5): очередь в БД, обработчик, прогресс, остановка, админка."""

import datetime as dt
import time

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from timetable import models as m
from timetable.services import runs
from timetable.services.problem import create_semester
from timetable.services.runs import RunConflict, claim_next, enqueue, execute, recover_stale, request_cancel

pytestmark = pytest.mark.django_db
R = m.SolveRun.Status
FAST = dict(time_limit_s=5, variants=1, workers=2)


def test_one_active_run_per_semester(synthetic):
    sem = synthetic[0]
    first = enqueue(sem, **FAST)
    assert first.status == R.QUEUED
    with pytest.raises(RunConflict) as e:
        enqueue(sem, **FAST)
    assert e.value.active == first
    request_cancel(first)
    assert enqueue(sem, **FAST).status == R.QUEUED  # снятый не мешает новому


def test_workers_clamped_to_setting(synthetic, settings):
    settings.SOLVER_MAX_WORKERS = 2
    assert enqueue(synthetic[0], time_limit_s=5, workers=64).workers == 2


def test_reference_from_other_semester_rejected(synthetic):
    sem = synthetic[0]
    other = create_semester("Другой", dt.date(2026, 9, 1))
    v = m.ScheduleVariant.objects.create(semester=other, name="x")
    with pytest.raises(ValueError):
        enqueue(sem, reference=v, **FAST)


def test_claim_oldest_once(synthetic):
    sem = synthetic[0]
    other = create_semester("Второй", dt.date(2026, 9, 1))
    a = enqueue(sem, **FAST)
    b = enqueue(other, **FAST)
    got = claim_next()
    assert got.pk == a.pk and got.status == R.RUNNING and got.started_at
    assert claim_next().pk == b.pk
    assert claim_next() is None


def test_cancel_queued_and_running(synthetic):
    sem = synthetic[0]
    q = request_cancel(enqueue(sem, **FAST))
    assert q.status == R.CANCELLED and q.finished_at
    r = enqueue(sem, start_now=True, **FAST)
    r = request_cancel(r)
    assert r.status == R.RUNNING and r.cancel_requested  # идущий останавливает сам расчёт


def test_recover_stale(synthetic):
    r = enqueue(synthetic[0], start_now=True, **FAST)
    assert recover_stale() == 0
    assert recover_stale(timezone.now() + dt.timedelta(minutes=5)) == 1
    r.refresh_from_db()
    assert r.status == R.FAILED and "заново" in r.message


@pytest.mark.django_db(transaction=True)
def test_execute_saves_variants_and_log(synthetic):
    sem = synthetic[0]
    run = enqueue(sem, **FAST, start_now=True)
    _, saved = execute(run)
    assert run.status == R.DONE, run.message
    assert len(saved) == 1 and saved[0].run_id == run.pk
    assert run.finished_at and run.solver_status in ("optimal", "feasible")
    assert "Старт" in run.log and "Итог" in run.log
    assert run.heartbeat_at


@pytest.mark.django_db(transaction=True)
def test_execute_stops_on_request(synthetic, monkeypatch):
    """Остановка из админки доходит до решателя за секунды; лимит 120 с не ждём."""
    monkeypatch.setattr(runs, "FLUSH_EVERY_S", 0.2)
    run = enqueue(synthetic[0], time_limit_s=120, variants=3, workers=2, start_now=True)
    m.SolveRun.objects.filter(pk=run.pk).update(cancel_requested=True)
    t0 = time.monotonic()
    execute(run)
    assert time.monotonic() - t0 < 30
    assert run.status == R.CANCELLED
    assert run.variants.count() <= 1


def test_execute_reports_build_error(db):
    sem = m.Semester.objects.create(name="Пустой", start_date=dt.date(2026, 9, 1))
    run = enqueue(sem, **FAST, start_now=True)
    result, saved = execute(run)
    assert result is None and not saved
    assert run.status == R.FAILED and run.message.startswith("Задача не собрана")


@pytest.mark.django_db(transaction=True)
def test_worker_once_processes_queue(synthetic):
    run = enqueue(synthetic[0], **FAST)
    call_command("solve_worker", "--once")
    run.refresh_from_db()
    assert run.status == R.DONE and run.variants.count() == 1


@pytest.mark.django_db(transaction=True)
def test_solve_semester_command_is_recorded(synthetic):
    sem = synthetic[0]
    call_command("solve_semester", str(sem.pk), "--time", "5", "--workers", "2")
    run = m.SolveRun.objects.get(semester=sem)
    assert run.status == R.DONE and run.variants.count() == 1


# ------------------------------------------------------------------ админка


def test_admin_solve_form_enqueues_and_shows_progress(admin_client_ru, synthetic):
    sem = synthetic[0]
    url = reverse("admin:timetable_semester_solve", args=[sem.pk])
    assert admin_client_ru.get(url).status_code == 200
    data = {"minutes": 1, "variants": 2, "name_prefix": "Осень", "workers": 1, "min_diff": 10, "seed": 0}
    resp = admin_client_ru.post(url, data)
    run = m.SolveRun.objects.get(semester=sem)
    assert resp.status_code == 302 and resp["Location"] == reverse("admin:timetable_solverun_change", args=[run.pk])
    assert run.time_limit_s == 60 and run.variants_requested == 2 and run.name_prefix == "Осень"
    assert run.created_by is not None
    page = admin_client_ru.get(resp["Location"])
    assert page.status_code == 200
    assert "в очереди" in page.content.decode() and 'http-equiv="refresh"' in page.content.decode()
    # второй запуск того же семестра — отказ с переходом на идущий
    again = admin_client_ru.post(url, data)
    assert again.status_code == 302 and str(run.pk) in again["Location"]
    assert m.SolveRun.objects.filter(semester=sem).count() == 1
    # снять с очереди
    resp = admin_client_ru.post(reverse("admin:timetable_solverun_cancel", args=[run.pk]))
    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.status == R.CANCELLED
    assert admin_client_ru.get(reverse("admin:timetable_solverun_changelist")).status_code == 200


def test_admin_run_page_with_progress_and_variants(admin_client_ru, synthetic):
    sem = synthetic[0]
    run = enqueue(sem, **FAST, start_now=True)
    m.SolveRun.objects.filter(pk=run.pk).update(
        progress={"variant": 1, "of": 1, "stage": "слоты", "elapsed_s": 3, "objective": 120.0, "bound": 80.0}
    )
    html = admin_client_ru.get(reverse("admin:timetable_solverun_change", args=[run.pk])).content.decode()
    assert "Текущая оценка штрафа" in html and "Остановить и сохранить найденное" in html
    v = m.ScheduleVariant.objects.create(semester=sem, name="Вариант 1", run=run, total=5)
    m.SolveRun.objects.filter(pk=run.pk).update(status=R.DONE, finished_at=timezone.now(), message="Сохранено 1")
    html = admin_client_ru.get(reverse("admin:timetable_solverun_change", args=[run.pk])).content.decode()
    assert "Вариант 1" in html and reverse("admin:timetable_variant_grid", args=[v.pk]) in html
    assert 'http-equiv="refresh"' not in html


def test_cancel_requires_post_and_permission(client, synthetic):
    from accounts.models import User

    sem = synthetic[0]
    run = enqueue(sem, **FAST)
    call_command("init_roles")
    viewer = User.objects.create_user("viewer@college.example", "pass-12345-word", is_staff=True)
    viewer.groups.add(Group.objects.get(name="Преподаватель"))
    client.force_login(viewer)
    assert client.post(reverse("admin:timetable_solverun_cancel", args=[run.pk])).status_code == 403
    assert client.get(reverse("admin:timetable_semester_solve", args=[sem.pk])).status_code == 403
    disp = User.objects.create_user("disp@college.example", "pass-12345-word", is_staff=True)
    disp.groups.add(Group.objects.get(name="Диспетчер"))
    client.force_login(disp)
    assert client.get(reverse("admin:timetable_solverun_cancel", args=[run.pk])).status_code == 403
    assert client.post(reverse("admin:timetable_solverun_cancel", args=[run.pk])).status_code == 302
    assert client.get(reverse("admin:timetable_semester_solve", args=[sem.pk])).status_code == 200
