"""Инструменты MCP-сервера против настоящего API (живой Django-сервер, HTTP, токен).

Библиотека mcp здесь не нужна: инструменты — обычные функции (mcp_server/tools.py). Протокол MCP
на версиях 1.x и 2.x проверяет tests_mcp/test_protocol.py.
"""

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command

from accounts.models import ApiToken, User
from mcp_server.client import ApiClient, ApiError
from mcp_server.tools import make_tools
from timetable import models as m
from timetable.services.problem import build_problem
from timetable.services.variants import approve_variant, save_variant

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def tools(live_server, synthetic):
    call_command("init_roles")
    u = User.objects.create_user("disp@college.example", "pass-12345-word", is_staff=True)
    u.groups.add(Group.objects.get(name="Диспетчер"))
    _, raw = ApiToken.issue(u, "агент")
    return {t.name: t.fn for t in make_tools(ApiClient(live_server.url, raw))}


@pytest.fixture
def approved(synthetic):
    sem, ids, planted, _ = synthetic
    v = save_variant(sem, build_problem(sem), ids.schedule(planted), "Утверждённый")
    approve_variant(v)
    return v


def test_teacher_move_scenario(tools, synthetic, approved):
    """Сценарий «преподаватель просит перенести пару» целиком через инструменты агента."""
    sem = synthetic[0]
    T = tools
    assert f"Семестр {sem.pk}" in T["semesters"]() and f"утверждённый вариант: {approved.pk}" in T["semesters"]()

    teacher = m.Teacher.objects.filter(items__semester=sem).distinct().first()
    found = T["find_teacher"](sem.pk, teacher.name.split()[0])
    assert f"teacher_id={teacher.pk}" in found

    a = approved.assignments.filter(item__teachers=teacher).first()
    week = "even" if a.week == "even" else "odd"
    sched = T["schedule"](approved.pk, "teacher", str(teacher.pk), week)
    assert teacher.name in sched and a.item.discipline.name in sched

    assert "Записано" in T["set_teacher_wish"](sem.pk, teacher.pk, a.day, a.pair, -2)
    assert T["set_teacher_wish"](sem.pk, teacher.pk, 4, 6, 2).endswith("(очень удобно).")
    wishes = T["teacher_wishes"](sem.pk, teacher.pk)
    assert f"{'ПнВтСрЧтПтСб'[(a.day - 1) * 2 : a.day * 2]} {a.pair}-я пара: -2 (нельзя)" in wishes
    assert m.TeacherAvailability.objects.get(semester=sem, teacher=teacher).prefs[f"{a.day}-{a.pair}"] == -2

    les = T["lessons"](sem.pk, teacher_id=teacher.pk)
    assert f"item_id={a.item_id}" in les
    free_item = m.WorkloadItem.objects.filter(semester=sem, pins=None, groups__isnull=False).first()
    pinned = T["pin_lesson"](sem.pk, free_item.pk, 2, 3)
    pin_id = int(pinned.split("pin_id=")[1].split(")")[0])
    assert f"pin_id={pin_id}" in T["lessons"](sem.pk, group_id=free_item.groups.first().pk)
    assert "снято" in T["unpin_lesson"](pin_id)

    assert "Ошибок нет" in T["check_semester"](sem.pk)
    move = T["check_move"](approved.pk, a.item_id, a.day, a.pair, a.day, a.pair, a.week)
    assert "(+0)" in move and "нарушений нет" in move

    started = T["start_rebuild"](sem.pk, minutes=0.25, variants=1)
    assert f"от варианта {approved.pk}" in started
    run_id = int(started.split("Расчёт ")[1].split()[0])
    assert "в очереди" in T["run_status"](run_id)
    with pytest.raises(ApiError, match="уже идёт расчёт"):
        T["start_rebuild"](sem.pk)
    call_command("solve_worker", "--once")
    status = T["run_status"](run_id)
    assert "готово" in status, status
    new_id = int(status.split("Варианты: ")[1].rstrip("."))

    vs = T["variants"](sem.pk)
    assert f"variant_id={approved.pk}" in vs and "УТВЕРЖДЁН" in vs
    assert f"variant_id={new_id}" in vs and "сдвинуто по времени" in vs
    cmp = T["compare_variants"](new_id)
    assert f"против {approved.pk}" in cmp or "совпадает" in cmp
    assert "сменили аудиторию" in cmp or "совпадает" in cmp
    assert "Свободных" in T["free_rooms"](new_id, 1, 1, 2) or "мест" in T["free_rooms"](new_id, 1, 1, 2)
    g = T["find_in_variant"](new_id, "group", "")
    assert g.startswith("key=")
    assert "готово" in T["stop_run"](run_id)  # уже закончен — ничего не ломается


def test_errors_are_readable(live_server, synthetic):
    bad = {t.name: t.fn for t in make_tools(ApiClient(live_server.url, "rsp_wrong"))}
    with pytest.raises(ApiError, match="Токен не принят"):
        bad["semesters"]()
    down = {t.name: t.fn for t in make_tools(ApiClient("http://127.0.0.1:9", "rsp_x", timeout=3))}
    with pytest.raises(ApiError, match="недоступен"):
        down["semesters"]()
    with pytest.raises(ApiError, match="1–6"):
        down["set_teacher_wish"](1, 1, 7, 1, 1)
