"""Этап 8: выгрузка расписания в Excel и календарь .ics."""

import datetime as dt
import io
import re

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from scheduler.domain import Week
from timetable import models as m
from timetable.export.ics import first_date
from timetable.services.calendar import ensure_token
from timetable.services.problem import build_problem
from timetable.services.variants import save_variant

pytestmark = pytest.mark.django_db


@pytest.fixture
def approved(synthetic):
    sem, ids, planted, _ = synthetic
    sem.end_date = sem.start_date + dt.timedelta(weeks=16)
    sem.save()
    v = save_variant(sem, build_problem(sem), ids.schedule(planted), "Вариант 1")
    v.status = m.ScheduleVariant.Status.APPROVED
    v.save()
    return v


def test_xlsx_export(admin_client_ru, approved):
    r = admin_client_ru.get(reverse("admin:timetable_variant_export", args=[approved.pk]))
    assert r.status_code == 200 and r.content[:2] == b"PK"
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["О варианте", "Группы", "Преподаватели", "Аудитории"]
    ws = wb["Группы"]
    n_groups = m.Group.objects.filter(course__semester=approved.semester).count()
    n_pairs = approved.semester.pairs.count()
    assert ws.max_row == 1 + n_groups * n_pairs
    texts = [c.value for row in ws.iter_rows(min_row=2, min_col=4) for c in row if c.value]
    assert texts, "в выгрузке нет занятий"
    # мигающие и «через неделю» видны как «нечёт:/чёт:»
    has_alt = m.WorkloadItem.objects.filter(semester=approved.semester, pairs_per_cycle__in=[1, 3, 5]).exists()
    if has_alt:
        assert any(t.startswith(("нечёт:", "чёт:")) for t in texts)
    # блок: несколько подгрупп одной дисциплины в паре — одной записью «по подгруппам»
    if m.SyncBlock.objects.filter(semester=approved.semester).exists():
        assert any("по подгруппам" in t for t in texts)


def test_first_date_parity():
    start = dt.date(2026, 9, 1)  # вторник; неделя с 31.08 — нечётная
    assert first_date(start, 1, Week.BOTH, Week.ODD) == dt.date(2026, 9, 7)  # пн 31.08 раньше начала
    assert first_date(start, 2, Week.ODD, Week.ODD) == dt.date(2026, 9, 1)
    assert first_date(start, 2, Week.EVEN, Week.ODD) == dt.date(2026, 9, 8)
    assert first_date(start, 1, Week.ODD, Week.ODD) == dt.date(2026, 9, 14)  # след. нечётная
    assert first_date(start, 3, Week.ODD, Week.EVEN) == dt.date(2026, 9, 9)


def _events(text):
    return re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text.replace("\r\n ", ""), re.S)


def test_admin_ics_for_group(admin_client_ru, approved):
    g = m.Group.objects.filter(course__semester=approved.semester).first()
    url = reverse("admin:timetable_variant_ics", args=[approved.pk]) + f"?by=group&key={g.pk}"
    r = admin_client_ru.get(url)
    assert r.status_code == 200 and r["Content-Type"].startswith("text/calendar")
    text = r.content.decode()
    assert text.startswith("BEGIN:VCALENDAR") and text.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    evs = _events(text)
    assert evs and all("RRULE:FREQ=WEEKLY" in e for e in evs)
    until = (approved.semester.end_date).strftime("%Y%m%d")
    assert all(f"UNTIL={until}" in e for e in evs)
    assert admin_client_ru.get(url.replace(f"key={g.pk}", "key=nope")).status_code == 404


def test_teacher_calendar_subscription(client, approved):
    from accounts.models import User

    sem = approved.semester
    t = m.Teacher.objects.filter(items__semester=sem).distinct().first()
    u = User.objects.create_user("t1@college.example")
    t.user = u
    t.save()
    client.force_login(u)
    html = client.get(reverse("me")).content.decode()
    assert "Скачать в календарь" in html and "Получить ссылку" in html
    r = client.get(reverse("my_calendar"))
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.content.decode()
    client.post(reverse("calendar_link"))
    t.refresh_from_db()
    token = t.calendar_token
    assert token and token in client.get(reverse("me")).content.decode()
    client.logout()
    # подписка работает без входа
    r = client.get(reverse("calendar_by_token", args=[token]))
    assert r.status_code == 200 and "BEGIN:VCALENDAR" in r.content.decode()
    # сброс — старая ссылка перестаёт работать
    new = ensure_token(t, reset=True)
    assert new != token
    assert client.get(reverse("calendar_by_token", args=[token])).status_code == 404
    assert client.get(reverse("calendar_by_token", args=["short"])).status_code == 404


def test_no_calendar_before_approval(client, synthetic):
    from accounts.models import User

    sem = synthetic[0]
    t = m.Teacher.objects.filter(items__semester=sem).distinct().first()
    u = User.objects.create_user("t2@college.example")
    t.user = u
    t.save()
    client.force_login(u)
    assert client.get(reverse("my_calendar")).status_code == 404
