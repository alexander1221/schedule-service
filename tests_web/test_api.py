"""API /api/v1/: вход (токен, сессия), права, семестры, пожелания, закрепления, запуски, варианты."""

import datetime as dt

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from accounts.models import ApiToken, User
from timetable import models as m
from timetable.services.problem import create_semester
from timetable.services.variants import save_variant

pytestmark = pytest.mark.django_db
API = "/api/v1"


@pytest.fixture
def dispatcher(db):
    call_command("init_roles")
    u = User.objects.create_user("disp@college.example", "pass-12345-word", is_staff=True)
    u.groups.add(Group.objects.get(name="Диспетчер"))
    return u


@pytest.fixture
def api(dispatcher):
    """Клиент с токеном диспетчера (так будет ходить MCP-сервер)."""
    _, raw = ApiToken.issue(dispatcher, "тест")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


@pytest.fixture
def variant(synthetic):
    sem, ids, planted, _ = synthetic
    from timetable.services.problem import build_problem

    p = build_problem(sem)
    return save_variant(sem, p, ids.schedule(planted), "Посаженное")


def jpost(c, url, data=None, **kw):
    return c.post(url, data or {}, content_type="application/json", **kw)


def jpatch(c, url, data):
    return c.patch(url, data, content_type="application/json")


# ------------------------------------------------------------------ вход и права


def test_anonymous_and_bad_token_rejected(synthetic):
    assert Client().get(f"{API}/semesters").status_code == 401
    assert Client(HTTP_AUTHORIZATION="Bearer rsp_wrong").get(f"{API}/semesters").status_code == 401


def test_token_lifecycle(dispatcher, synthetic):
    tok, raw = ApiToken.issue(dispatcher, "агент")
    assert raw.startswith("rsp_") and tok.prefix == raw[:12] and raw not in tok.key_hash
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    me = c.get(f"{API}/me").json()
    assert me["email"] == "disp@college.example" and "timetable.add_solverun" in me["permissions"]
    tok.refresh_from_db()
    assert tok.last_used_at is not None
    tok.revoked_at = timezone.now()
    tok.save()
    assert c.get(f"{API}/me").status_code == 401
    tok2, raw2 = ApiToken.issue(dispatcher, "истёк", days=1)
    ApiToken.objects.filter(pk=tok2.pk).update(expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw2}").get(f"{API}/me").status_code == 401


def test_token_of_non_staff_user_rejected(synthetic):
    u = User.objects.create_user("teacher@college.example", "pass-12345-word")
    _, raw = ApiToken.issue(u, "x")
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(f"{API}/me").status_code == 401


def test_permissions_enforced(synthetic):
    call_command("init_roles")
    u = User.objects.create_user("staff@college.example", "pass-12345-word", is_staff=True)  # без ролей
    _, raw = ApiToken.issue(u, "x")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    r = c.get(f"{API}/semesters")
    assert r.status_code == 403 and "Недостаточно прав" in r.json()["detail"]


def test_session_auth_requires_csrf_for_changes(dispatcher, synthetic):
    sem = synthetic[0]
    c = Client(enforce_csrf_checks=True)
    c.force_login(dispatcher)
    assert c.get(f"{API}/semesters").status_code == 200
    assert jpost(c, f"{API}/semesters/{sem.pk}/runs", {}).status_code == 403  # без X-CSRFToken
    c.get("/admin/")  # выдаёт cookie csrftoken
    token = c.cookies["csrftoken"].value
    assert jpost(c, f"{API}/semesters/{sem.pk}/runs", {}, HTTP_X_CSRFTOKEN=token).status_code == 201


def test_create_api_token_command(dispatcher, capsys):
    call_command("create_api_token", dispatcher.email, "--name", "mcp")
    raw = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("rsp_"))
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").get(f"{API}/me").status_code == 200


# ------------------------------------------------------------------ семестры, пожелания, закрепления


def test_semesters_and_precheck(api, synthetic):
    sem = synthetic[0]
    lst = api.get(f"{API}/semesters").json()
    assert [x["id"] for x in lst] == [sem.pk]
    assert lst[0]["days"] == [1, 2, 3, 4, 5] and lst[0]["active_run_id"] is None
    pc = api.get(f"{API}/semesters/{sem.pk}/precheck").json()
    assert pc["ok"] and pc["stats"]["items"] > 0
    assert api.get(f"{API}/semesters/999999").json() == {"detail": "Не найдено"}


def test_prefs_patch_merges_and_validates(api, synthetic):
    sem = synthetic[0]
    t = api.get(f"{API}/semesters/{sem.pk}/teachers").json()[0]
    url = f"{API}/semesters/{sem.pk}/teachers/{t['id']}/prefs"
    before = api.get(url).json()["prefs"]
    r = jpatch(api, url, {"set": {"5-4": -2, "4-6": 2}})
    assert r.status_code == 200, r.content
    got = r.json()["prefs"]
    assert got["5-4"] == -2 and got["4-6"] == 2
    assert {k: v for k, v in got.items() if k not in ("5-4", "4-6")} == {
        k: v for k, v in before.items() if k not in ("5-4", "4-6")
    }
    assert jpatch(api, url, {"reset": ["5-4"]}).json()["prefs"].get("5-4") is None
    assert jpatch(api, url, {"set": {"7-1": 1}}).status_code == 400  # нет воскресенья
    assert jpatch(api, url, {"set": {"1-1": 3}}).status_code == 400


def test_items_and_pins(api, synthetic):
    sem = synthetic[0]
    items = api.get(f"{API}/semesters/{sem.pk}/items").json()
    assert items and {"discipline", "teachers", "groups", "pins"} <= set(items[0])
    t_id = items[0]["teachers"][0]["id"]
    assert all(
        any(t["id"] == t_id for t in it["teachers"])
        for it in api.get(f"{API}/semesters/{sem.pk}/items?teacher_id={t_id}").json()
    )
    free_item = next(it for it in items if not it["pins"])
    r = jpost(api, f"{API}/semesters/{sem.pk}/pins", {"item_id": free_item["id"], "day": 4, "pair": 6})
    assert r.status_code == 201, r.content
    pin = r.json()
    assert pin["week"] == "both" and pin["day"] == 4
    again = jpost(api, f"{API}/semesters/{sem.pk}/pins", {"item_id": free_item["id"], "day": 4, "pair": 6})
    assert again.status_code == 409
    no_slot = jpost(api, f"{API}/semesters/{sem.pk}/pins", {"item_id": free_item["id"], "day": 4, "pair": 11})
    assert no_slot.status_code == 400
    assert api.delete(f"{API}/pins/{pin['id']}").status_code == 204
    assert not m.Pin.objects.filter(pk=pin["id"]).exists()


# ------------------------------------------------------------------ запуски расчёта


def test_runs_create_conflict_cancel(api, synthetic, settings):
    settings.SOLVER_MAX_WORKERS = 2
    sem = synthetic[0]
    r = jpost(api, f"{API}/semesters/{sem.pk}/runs", {"time_limit_s": 60, "variants": 2, "workers": 8})
    assert r.status_code == 201, r.content
    run = r.json()
    assert run["status"] == "queued" and run["workers"] == 2 and run["variants_requested"] == 2
    c = jpost(api, f"{API}/semesters/{sem.pk}/runs", {})
    assert c.status_code == 409 and c.json()["active_run_id"] == run["id"]
    assert api.get(f"{API}/semesters/{sem.pk}").json()["active_run_id"] == run["id"]
    assert api.get(f"{API}/runs/{run['id']}?log=true").json()["log"] == ""
    assert jpost(api, f"{API}/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    assert [x["id"] for x in api.get(f"{API}/semesters/{sem.pk}/runs").json()] == [run["id"]]
    assert jpost(api, f"{API}/semesters/{sem.pk}/runs", {"time_limit_s": 5}).status_code == 422  # меньше 10 с
    assert jpost(api, f"{API}/semesters/{sem.pk}/runs", {"time_limit_s": 10}).status_code == 201


def test_run_reference_must_be_same_semester(api, synthetic, variant):
    other = create_semester("Другой", dt.date(2026, 9, 1))
    r = jpost(api, f"{API}/semesters/{other.pk}/runs", {"reference_variant_id": variant.pk})
    assert r.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_run_via_api_processed_by_worker(api, synthetic):
    sem = synthetic[0]
    run = jpost(api, f"{API}/semesters/{sem.pk}/runs", {"time_limit_s": 10, "variants": 1, "workers": 2}).json()
    call_command("solve_worker", "--once")
    got = api.get(f"{API}/runs/{run['id']}").json()
    assert got["status"] == "done", got["message"]
    assert len(got["variant_ids"]) == 1
    v = api.get(f"{API}/variants/{got['variant_ids'][0]}").json()
    assert v["run_id"] == run["id"] and v["breakdown"]


# ------------------------------------------------------------------ варианты


def test_variant_views(api, synthetic, variant):
    sem = synthetic[0]
    assert [x["id"] for x in api.get(f"{API}/semesters/{sem.pk}/variants").json()] == [variant.pk]
    groups = api.get(f"{API}/variants/{variant.pk}/entities?by=group").json()
    g = api.get(f"{API}/variants/{variant.pk}/grid?by=group&key={groups[0]['id']}&week=odd").json()
    assert g["name"] == groups[0]["name"] and len(g["days"]) == 5 and g["rows"]
    entries = [e for row in g["rows"] for cell in row["cells"] for e in cell]
    assert entries and all(e["lessons"] for e in entries)  # интерфейсу нужно, что переносить
    lesson = entries[0]["lessons"][0]
    assert lesson["week"] in ("both", "odd") and variant.assignments.filter(item_id=lesson["item_id"]).exists()
    assert api.get(f"{API}/variants/{variant.pk}/grid?by=group&key=nope").status_code == 400
    fr = api.get(f"{API}/variants/{variant.pk}/free-rooms?day=1&pair_from=1&pair_to=2").json()
    assert all({"name", "free_pairs", "fully_free"} <= set(x) for x in fr)


def test_check_as_is_and_with_move(api, variant):
    base = jpost(api, f"{API}/variants/{variant.pk}/check").json()
    assert base["ok"] and base["delta"] == 0 and base["total"] == base["base_total"]
    a = variant.assignments.exclude(item=None).first()
    # перенос на занятый группой слот почти наверняка что-то нарушит; главное — честный пересчёт
    mv = {"item_id": a.item_id, "week": a.week, "day": a.day, "pair": a.pair, "to_day": a.day, "to_pair": a.pair}
    same = jpost(api, f"{API}/variants/{variant.pk}/check", {"moves": [mv]}).json()
    assert same["ok"] and same["delta"] == 0
    # перенос на занятое время другого занятия того же преподавателя → нарушение знает клетку и элементы
    b = (
        variant.assignments.filter(item__teachers__in=a.item.teachers.all(), week=a.week)
        .exclude(item_id=a.item_id)
        .exclude(day=a.day, pair=a.pair)
        .first()
    )
    assert b is not None  # синтетика детерминирована: у преподавателя есть другие занятия
    if b is not None:
        clash = jpost(
            api, f"{API}/variants/{variant.pk}/check", {"moves": [{**mv, "to_day": b.day, "to_pair": b.pair}]}
        )
        hard = clash.json()["hard"]
        tc = [v for v in hard if v["code"] == "teacher_conflict"]
        assert tc and tc[0]["slot"] == f"{b.day}-{b.pair}" and a.item_id in tc[0]["items"]
    other_day = 1 if a.day != 1 else 2
    moved = jpost(api, f"{API}/variants/{variant.pk}/check", {"moves": [{**mv, "to_day": other_day}]}).json()
    assert moved["total"] != base["total"] or not moved["ok"] or moved["delta"] == 0
    bad = jpost(api, f"{API}/variants/{variant.pk}/check", {"moves": [{**mv, "pair": 12}]})
    assert bad.status_code == 400
    variant.refresh_from_db()
    assert variant.assignments.count() > 0  # вариант не меняется


def test_approve_and_diff(api, synthetic, variant):
    sem, ids, planted, _ = synthetic
    from timetable.services.problem import build_problem

    p = build_problem(sem)
    # второй вариант: одно занятие перенесено на другой день
    sched = ids.schedule(planted)
    a0 = sched.assignments[0]
    day, pair = a0.slot_id.split("-")
    new_slot = f"{1 if day != '1' else 2}-{pair}"
    sched.assignments[0] = type(a0)(a0.item_id, a0.week, new_slot, a0.room_id, a0.teacher_remote)
    v2 = save_variant(sem, p, sched, "Изменённый")
    assert api.post(f"{API}/variants/{v2.pk}/diff").status_code == 405
    assert api.get(f"{API}/variants/{v2.pk}/diff").status_code == 400  # нет утверждённого и эталона
    r = jpost(api, f"{API}/variants/{variant.pk}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert api.get(f"{API}/semesters/{sem.pk}").json()["approved_variant_id"] == variant.pk
    d = api.get(f"{API}/variants/{v2.pk}/diff").json()  # по умолчанию — с утверждённым
    assert d["against_variant_id"] == variant.pk
    assert d["moved"] >= 1 and len(d["changes"]) == 1
    ch = d["changes"][0]
    assert ch["item_id"] == a0.item_id and ch["kind"] == "перенос" and ch["before"] and ch["after"]
    assert ch["after"][0]["label"]
    same = api.get(f"{API}/variants/{variant.pk}/diff?against={variant.pk}").json()
    assert same["changes"] == [] and same["moved"] == 0


def test_changes_are_logged_with_token_name(api, synthetic):
    from django.contrib.admin.models import LogEntry

    sem = synthetic[0]
    t = m.Teacher.objects.filter(items__semester=sem).first()
    jpatch(api, f"{API}/semesters/{sem.pk}/teachers/{t.pk}/prefs", {"set": {"5-4": -2}})
    run = jpost(api, f"{API}/semesters/{sem.pk}/runs", {"time_limit_s": 10}).json()
    jpost(api, f"{API}/runs/{run['id']}/cancel")
    msgs = [e.change_message for e in LogEntry.objects.order_by("pk")]
    assert msgs[0].startswith("Пожелания 5-4: -2 — через API, токен «тест»")
    assert "Расчёт поставлен в очередь" in msgs[1] and "Остановка расчёта" in msgs[2]
    assert all(e.user.email == "disp@college.example" for e in LogEntry.objects.all())


def test_docs_require_staff(dispatcher):
    assert Client().get(f"{API}/docs").status_code == 302  # на вход в админку
    c = Client()
    c.force_login(dispatcher)
    assert c.get(f"{API}/docs").status_code == 200
    assert c.get(f"{API}/openapi.json").status_code == 200
