"""Вход по коду на почту и личная страница преподавателя «Мои пожелания».

Преподаватель видит и меняет только своё: сетку пожеланий на семестр (до срока сдачи), свою
нагрузку и — когда вариант утверждён — своё расписание. Диспетчеры и администраторы после входа
попадают в админку.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from accounts import codes
from scheduler.domain import Week
from timetable import models as m
from timetable.models import validate_pref_grid
from timetable.services import calendar as cal
from timetable.services import views as sv
from timetable.services.variants import load_variant
from timetable.widgets import COLORS, SCALE, PrefGridWidget

SESSION_EMAIL = "login_email"


def home(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("login")
    if request.user.is_staff and not hasattr(request.user, "teacher"):
        return redirect("admin:index")
    return redirect("me")


def login_email(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("home")
    email = ""
    if request.method == "POST":
        email = codes.normalize(request.POST.get("email", ""))
        if "@" not in email:
            messages.error(request, "Введите адрес почты целиком, например ivanova@college.ru")
        else:
            res = codes.issue_code(email)
            if res.error:
                messages.error(
                    request,
                    "Не удалось отправить письмо. Сообщите диспетчеру: почта сервиса настроена неверно.",
                )
            else:
                if res.wait_s:
                    messages.warning(request, f"Код уже отправлен. Новый можно запросить через {res.wait_s} с.")
                request.session[SESSION_EMAIL] = email
                return redirect("login_code")
    return render(request, "portal/login.html", {"email": email})


def login_code(request: HttpRequest) -> HttpResponse:
    email = request.session.get(SESSION_EMAIL)
    if not email:
        return redirect("login")
    if request.method == "POST":
        user = codes.check_code(email, request.POST.get("code", ""))
        if user is not None:
            request.session.pop(SESSION_EMAIL, None)
            login(request, user, backend=f"{ModelBackend.__module__}.{ModelBackend.__name__}")
            return redirect("home")
        left = codes.remaining_attempts(email)
        if left:
            messages.error(request, f"Код не подошёл. Осталось попыток: {left}.")
        else:
            messages.error(request, "Код не подошёл или устарел. Запросите новый.")
    return render(request, "portal/code.html", {"email": email, "minutes": int(codes.CODE_TTL.total_seconds() // 60)})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")


def _semesters() -> list[m.Semester]:
    return list(m.Semester.objects.exclude(status=m.Semester.Status.ARCHIVED).order_by("-start_date"))


def _editable(sem: m.Semester) -> bool:
    if sem.status == m.Semester.Status.ARCHIVED:
        return False
    return sem.availability_deadline is None or timezone.localdate() <= sem.availability_deadline


@login_required
def me(request: HttpRequest) -> HttpResponse:
    teacher = m.Teacher.objects.filter(user=request.user).first()
    if teacher is None:
        return render(request, "portal/no_teacher.html", status=200)
    sems = _semesters()
    if not sems:
        return render(request, "portal/me.html", {"teacher": teacher, "semesters": []})
    sem = next((s for s in sems if str(s.pk) == request.GET.get("semester")), sems[0])
    av, _ = m.TeacherAvailability.objects.get_or_create(teacher=teacher, semester=sem)
    editable = _editable(sem)
    days, pairs = sem.days, sem.pair_numbers()
    times = {
        p.number: format_html("{}<small>{}–{}</small>", p.number, f"{p.start:%H:%M}", f"{p.end:%H:%M}")
        for p in sem.pairs.all()
    }
    if request.method == "POST":
        if not editable:
            messages.error(request, "Срок сдачи пожеланий прошёл — изменения не приняты. Напишите диспетчеру.")
            return redirect(f"{request.path}?semester={sem.pk}")
        grid = PrefGridWidget(days, pairs).value_from_datadict(request.POST, None, "prefs")
        allowed = {f"{d}-{p}" for d in days for p in pairs}
        grid = {k: v for k, v in grid.items() if k in allowed}
        grid.update({k: v for k, v in (av.prefs or {}).items() if k not in allowed})  # вне сетки — как было
        try:
            validate_pref_grid(grid)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            av.prefs, av.submitted_at = grid, timezone.now()
            av.save(update_fields=["prefs", "submitted_at"])
            messages.success(request, "Пожелания сохранены. Их можно менять до срока сдачи.")
            return redirect(f"{request.path}?semester={sem.pk}")
    widget = PrefGridWidget(days, pairs, pair_labels=times, disabled=not editable)
    counts = {v: sum(1 for x in (av.prefs or {}).values() if x == v) for v, _ in SCALE if v}
    ctx: dict[str, Any] = {
        "teacher": teacher,
        "semesters": sems,
        "sem": sem,
        "av": av,
        "editable": editable,
        "grid_html": widget.render("prefs", av.prefs or {}),
        "counts": [(label, COLORS[v], counts[v]) for v, label in SCALE if v],
        "items": _items(teacher, sem),
        "calendar_url": (
            request.build_absolute_uri(reverse("calendar_by_token", args=[teacher.calendar_token]))
            if teacher.calendar_token
            else ""
        ),
        **_schedule(teacher, sem),
    }
    return render(request, "portal/me.html", ctx)


def _items(teacher: m.Teacher, sem: m.Semester) -> list[dict[str, Any]]:
    out = []
    qs = (
        m.WorkloadItem.objects.filter(semester=sem, teachers=teacher)
        .select_related("discipline", "sync_block")
        .prefetch_related("groups", "subgroups__group")
        .order_by("discipline__name")
    )
    for it in qs:
        who = [g.name for g in it.groups.all()] + [f"{s.group.name}: {s.name}" for s in it.subgroups.all()]
        if not who and it.sync_block:
            who = [f"блок «{it.sync_block.name}»"]
        notes = []
        if it.format == m.WorkloadItem.Format.ONLINE:
            notes.append("онлайн, из аудитории" if it.teacher_in_room else "онлайн")
        if it.planning == m.WorkloadItem.Planning.MANUAL:
            notes.append("ставит диспетчер")
        out.append(
            {
                "discipline": it.discipline.name,
                "who": ", ".join(who) or "—",
                "per_week": _per_week(it),
                "notes": ", ".join(notes),
            }
        )
    return out


def _pairs(n: int) -> str:
    form = (
        "пара" if n % 10 == 1 and n % 100 != 11 else ("пары" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "пар")
    )
    return f"{n} {form}"


def _per_week(it: m.WorkloadItem) -> str:
    n = it.pairs_per_cycle
    if it.week_pattern == m.WorkloadItem.WeekPattern.ALT_WEEK:
        return f"{_pairs(n)} подряд раз в две недели"
    if n % 2:
        weekly = f"{_pairs(n // 2)} в неделю + " if n > 1 else ""
        return f"{weekly}1 пара через неделю"
    return f"{_pairs(n // 2)} в неделю"


def _schedule(teacher: m.Teacher, sem: m.Semester) -> dict[str, Any]:
    v = sem.variants.filter(status=m.ScheduleVariant.Status.APPROVED).order_by("-created_at").first()
    if v is None:
        return {"variant": None}
    p, s = load_variant(v)
    key = str(teacher.pk)
    if key not in p.teacher_by_id:
        return {"variant": v, "weeks": []}
    weeks = [
        (label, sv.grid(p, s, "teacher", key, wk))
        for label, wk in (("Нечётная неделя", Week.ODD), ("Чётная неделя", Week.EVEN))
    ]
    return {"variant": v, "weeks": weeks, "days": sv.day_headers(p)}


def _ics_response(text: str, filename: str) -> HttpResponse:
    resp = HttpResponse(text, content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


@login_required
def my_calendar(request: HttpRequest) -> HttpResponse:
    teacher = m.Teacher.objects.filter(user=request.user).first()
    text = cal.teacher_ics(teacher) if teacher else None
    if text is None:
        raise Http404("Утверждённого расписания пока нет")
    return _ics_response(text, "raspisanie.ics")


@login_required
@require_POST
def calendar_link(request: HttpRequest) -> HttpResponse:
    teacher = m.Teacher.objects.filter(user=request.user).first()
    if teacher is None:
        raise Http404
    reset = request.POST.get("reset") == "1"
    cal.ensure_token(teacher, reset=reset)
    messages.success(
        request,
        "Новая ссылка создана, старая больше не работает." if reset else "Ссылка для подписки готова.",
    )
    return redirect("me")


def calendar_by_token(request: HttpRequest, token: str) -> HttpResponse:
    """Подписка из календаря телефона: без входа, по секретной ссылке (её можно сбросить)."""
    teacher = m.Teacher.objects.filter(calendar_token=token).first() if len(token) >= 20 else None
    if teacher is None:
        raise Http404
    text = cal.teacher_ics(teacher)
    if text is None:
        raise Http404("Утверждённого расписания пока нет")
    return _ics_response(text, "raspisanie.ics")
