"""Этап 7: вход по коду на почту и страница преподавателя «Мои пожелания»."""

import datetime as dt
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts import codes
from accounts.models import AllowedEmail, LoginCode, User
from timetable import models as m

pytestmark = pytest.mark.django_db
EMAIL = "petrova@college.example"


def _code(mail) -> str:
    return re.search(r"\b(\d{6})\b", mail.subject).group(1)


def _request_code(client, email=EMAIL):
    return client.post(reverse("login"), {"email": email})


@pytest.fixture
def teacher(synthetic):
    sem = synthetic[0]
    t = m.Teacher.objects.filter(items__semester=sem).distinct().first()
    t.email = EMAIL
    t.save()
    AllowedEmail.objects.create(email=EMAIL, role=AllowedEmail.Role.TEACHER)
    return t


def _login(client, mailoutbox):
    _request_code(client)
    return client.post(reverse("login_code"), {"code": _code(mailoutbox[-1])})


def test_unknown_email_gets_no_mail_but_same_answer(client, mailoutbox, db):
    r = _request_code(client, "stranger@example.com")
    assert r.status_code == 302 and r["Location"] == reverse("login_code")  # как и для «своих»
    assert mailoutbox == []
    r = client.post(reverse("login_code"), {"code": "123456"}, follow=True)
    assert not r.context["user"].is_authenticated


def test_code_login_links_teacher_and_role(client, mailoutbox, teacher):
    r = _request_code(client)
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [EMAIL]
    code = _code(mailoutbox[0])
    assert code in mailoutbox[0].body
    assert not LoginCode.objects.filter(code_hash__contains=code).exists()  # хранится только хэш
    r = client.post(reverse("login_code"), {"code": code}, follow=True)
    assert r.redirect_chain[-1][0] == reverse("me")
    user = User.objects.get(email=EMAIL)
    teacher.refresh_from_db()
    assert teacher.user == user and not user.is_staff
    assert user.groups.filter(name="Преподаватель").exists()
    # код одноразовый
    client.logout()
    assert codes.check_code(EMAIL, code) is None


def test_wrong_attempts_and_expiry(client, mailoutbox, teacher):
    _request_code(client)
    good = _code(mailoutbox[-1])
    bad = "000000" if good != "000000" else "111111"
    for _ in range(codes.MAX_ATTEMPTS):
        client.post(reverse("login_code"), {"code": bad})
    assert codes.check_code(EMAIL, good) is None  # после 5 ошибок код сгорел
    # просроченный код не принимается
    LoginCode.objects.all().delete()
    _request_code(client)
    LoginCode.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert codes.check_code(EMAIL, _code(mailoutbox[-1])) is None


def test_resend_throttle(client, mailoutbox, teacher):
    _request_code(client)
    r = _request_code(client)
    assert len(mailoutbox) == 1
    LoginCode.objects.update(created_at=timezone.now() - dt.timedelta(seconds=61))
    _request_code(client)
    assert len(mailoutbox) == 2
    assert LoginCode.objects.filter(used_at=None).count() == 1  # действует только последний код
    assert r.status_code == 302


def test_removed_from_whitelist_cannot_finish_login(client, mailoutbox, teacher):
    _request_code(client)
    AllowedEmail.objects.all().delete()
    client.post(reverse("login_code"), {"code": _code(mailoutbox[-1])})
    assert not User.objects.filter(email=EMAIL).exists()


def test_dispatcher_goes_to_admin(client, mailoutbox, db):
    AllowedEmail.objects.create(email="disp@college.example", role=AllowedEmail.Role.DISPATCHER)
    _request_code(client, "disp@college.example")
    r = client.post(reverse("login_code"), {"code": _code(mailoutbox[-1])}, follow=True)
    assert r.redirect_chain[-1][0] == reverse("admin:index")
    assert User.objects.get(email="disp@college.example").is_staff


def test_me_page_save_prefs_and_deadline(client, mailoutbox, teacher, synthetic):
    sem = synthetic[0]
    _login(client, mailoutbox)
    r = client.get(reverse("me"))
    assert r.status_code == 200
    html = r.content.decode()
    assert teacher.name in html and 'name="prefs__1-1"' in html
    assert "09:30" in html  # пары подписаны временем
    disc = m.WorkloadItem.objects.filter(semester=sem, teachers=teacher).first().discipline.name
    assert disc in html
    r = client.post(reverse("me") + f"?semester={sem.pk}", {"prefs__1-1": "-2", "prefs__2-3": "1", "prefs__1-2": "0"})
    assert r.status_code == 302
    av = m.TeacherAvailability.objects.get(teacher=teacher, semester=sem)
    assert av.prefs.get("1-1") == -2 and av.prefs.get("2-3") == 1 and "1-2" not in av.prefs
    assert av.submitted_at is not None
    # срок прошёл — изменения не принимаются, форма только для просмотра
    sem.availability_deadline = timezone.localdate() - dt.timedelta(days=1)
    sem.save()
    client.post(reverse("me") + f"?semester={sem.pk}", {"prefs__1-1": "2"})
    av.refresh_from_db()
    assert av.prefs.get("1-1") == -2
    assert "disabled" in client.get(reverse("me")).content.decode()


def test_me_page_shows_only_own_approved_schedule(client, mailoutbox, teacher, synthetic):
    from timetable.services.problem import build_problem
    from timetable.services.variants import save_variant

    sem, ids, planted, _ = synthetic
    v = save_variant(sem, build_problem(sem), ids.schedule(planted), "Утверждённый")
    _login(client, mailoutbox)
    assert "ещё не утверждено" in client.get(reverse("me")).content.decode()
    v.status = m.ScheduleVariant.Status.APPROVED
    v.save()
    html = client.get(reverse("me")).content.decode()
    assert "Нечётная неделя" in html and 'class="sg"' in html
    others = m.Teacher.objects.exclude(pk=teacher.pk).filter(items__semester=sem).values_list("name", flat=True)
    assert not any(f"<h1>{n}</h1>" in html for n in others)


def test_no_teacher_card(client, mailoutbox, db):
    AllowedEmail.objects.create(email="new@college.example")
    _request_code(client, "new@college.example")
    r = client.post(reverse("login_code"), {"code": _code(mailoutbox[-1])}, follow=True)
    assert "не привязан" in r.content.decode()


def test_admin_wishes_and_allow_login(admin_client_ru, synthetic):
    sem = synthetic[0]
    t = m.Teacher.objects.filter(items__semester=sem).first()
    t.email = "ivanov@college.example"
    t.save()
    r = admin_client_ru.get(reverse("admin:timetable_semester_wishes", args=[sem.pk]))
    assert r.status_code == 200 and t.name in r.content.decode()
    admin_client_ru.post(
        reverse("admin:timetable_teacher_changelist"), {"action": "allow_login", "_selected_action": [t.pk]}
    )
    assert AllowedEmail.objects.filter(email="ivanov@college.example", role="teacher").exists()


def test_env_file_loader(tmp_path, monkeypatch):
    from webapp.settings import _load_env_file

    f = tmp_path / ".env"
    f.write_text('# comment\nX_TEST_A=1\nX_TEST_B="two words"\nbad line\n', encoding="utf-8")
    monkeypatch.delenv("X_TEST_A", raising=False)
    monkeypatch.setenv("X_TEST_B", "from-system")
    _load_env_file(f)
    import os

    assert os.environ["X_TEST_A"] == "1"
    assert os.environ["X_TEST_B"] == "from-system"  # система важнее файла
    monkeypatch.delenv("X_TEST_A")
