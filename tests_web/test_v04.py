"""Веб-часть 0.4.0: оборудование и предпочтения аудиторий, занятость аудиторий, потоки, табель,
просмотр расписания и поиск свободных аудиторий."""

import io

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from scheduler.domain import Week
from scheduler.reports import teacher_hours
from scheduler.validator import validate
from timetable import models as m
from timetable.services import views as sv
from timetable.services.problem import build_problem
from timetable.services.variants import load_variant, save_variant
from timetable.xlsx.references import export_references, import_references

pytestmark = pytest.mark.django_db


@pytest.fixture
def variant(synthetic):
    sem, ids, planted, _ = synthetic
    return save_variant(sem, build_problem(sem), ids.schedule(planted), "Посаженное")


def test_roundtrip_keeps_equipment_prefs_and_blocking(synthetic):
    sem, ids, planted, original = synthetic
    built = build_problem(sem)
    assert sorted(sorted(r.equipment) for r in built.rooms) == sorted(sorted(r.equipment) for r in original.rooms)
    assert sum(bool(r.blocked) for r in built.rooms) == sum(bool(r.blocked) for r in original.rooms) == 1
    assert {tuple(t.required_equipment) for t in built.teachers} == {
        tuple(t.required_equipment) for t in original.teachers
    }
    assert validate(built, ids.schedule(planted)).breakdown == validate(original, planted).breakdown


def test_references_xlsx_equipment_and_room_busy(synthetic):
    sem = synthetic[0]
    data = export_references(sem)
    rep = import_references(sem, data, dry_run=False)
    assert rep.ok and rep.updated == {} and rep.created == {}, ([str(e) for e in rep.errors], rep.updated)
    wb = load_workbook(io.BytesIO(data))
    assert {"Оборудование", "Занятость аудиторий"} <= set(wb.sheetnames)
    wb["Оборудование"].append(["cam", "Камера"])
    ws = wb["Аудитории"]
    col = next(c.column for c in ws[1] if c.value == "Оборудование")
    ws.cell(2, col).value = "Камера; Проектор"
    room_name = ws.cell(2, 1).value
    busy = wb["Занятость аудиторий"]
    busy.append([room_name, "школа", "x", "x"])  # Пн 1, Пн 2
    t = wb["Преподаватели"]
    pcol = next(c.column for c in t[1] if c.value == "Предпочитает аудитории")
    t.cell(2, pcol).value = room_name
    buf = io.BytesIO()
    wb.save(buf)
    rep = import_references(sem, buf.getvalue(), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    room = m.Room.objects.get(name=room_name)
    assert {e.code for e in room.equipment.all()} == {"cam", "projector"}
    av = m.RoomAvailability.objects.get(room=room, semester=sem)
    assert av.blocked == ["1-1", "1-2"] and av.reason == "школа"
    teacher = m.Teacher.objects.get(name=t.cell(2, 1).value)
    assert list(teacher.preferred_rooms.all()) == [room]


def test_references_unknown_equipment_is_error(synthetic):
    sem = synthetic[0]
    wb = load_workbook(io.BytesIO(export_references(sem)))
    ws = wb["Аудитории"]
    col = next(c.column for c in ws[1] if c.value == "Оборудование")
    ws.cell(2, col).value = "Голограф"
    buf = io.BytesIO()
    wb.save(buf)
    rep = import_references(sem, buf.getvalue(), dry_run=True)
    assert any(e.column == "Оборудование" and "Голограф" in e.message for e in rep.errors)


def _two_group_items(sem):
    disc = m.Discipline.objects.create(name="Операторское мастерство (тест)")
    t = m.Teacher.objects.create(name="Оператор Тест")
    course = sem.courses.get(number=2)
    groups = list(course.groups.all()[:2])
    regular = m.RoomType.objects.get(code="regular")
    items = []
    for g in groups:
        it = m.WorkloadItem.objects.create(semester=sem, discipline=disc, pairs_per_cycle=2, room_type=regular)
        it.teachers.set([t])
        it.groups.set([g])
        items.append(it)
    return items, groups


def test_stream_candidates_and_actions(admin_client_ru, synthetic):
    sem = synthetic[0]
    items, groups = _two_group_items(sem)
    p = build_problem(sem)
    assert any({it.id for it in cl} == {str(i.pk) for i in items} for cl in p.stream_candidates())
    url = reverse("admin:timetable_workloaditem_changelist")
    r = admin_client_ru.get(url + f"?semester__id__exact={sem.pk}&hint=stream")
    assert r.status_code == 200 and "Операторское мастерство (тест)" in r.content.decode()
    admin_client_ru.post(url, {"action": "allow_stream", "_selected_action": [i.pk for i in items]})
    mg = m.WorkloadItem.objects.get(pk=items[0].pk).merge_group
    assert mg is not None and mg.items.count() == 2 and mg.name.startswith("Поток:")
    # «всегда вместе»: снимаем объединение и сливаем в один элемент
    m.WorkloadItem.objects.filter(pk__in=[i.pk for i in items]).update(merge_group=None)
    admin_client_ru.post(url, {"action": "make_stream", "_selected_action": [i.pk for i in items]})
    left = m.WorkloadItem.objects.filter(pk__in=[i.pk for i in items])
    assert left.count() == 1 and set(left.get().groups.all()) == set(groups)


def test_tabel_counts_stream_per_group(synthetic):
    """2КП1 + 2КП2 одной парой: фактически 1 пара (2 ч), в табель — 4 ч."""
    sem = synthetic[0]
    items, _ = _two_group_items(sem)
    m.WorkloadItem.objects.filter(pk__in=[i.pk for i in items[1:]]).delete()
    items[0].groups.set(list(sem.courses.get(number=2).groups.all()[:2]))
    p = build_problem(sem)
    from scheduler.domain import Assignment, Schedule

    s = Schedule([Assignment(str(items[0].pk), Week.BOTH, "1-1", None)])
    row = next(r for r in teacher_hours(p, s) if r.teacher == "Оператор Тест")
    assert (row.pairs_actual, row.hours_actual, row.hours_tabel) == (2, 4, 8)  # за 2 недели


def test_schedule_views_and_free_rooms(admin_client_ru, variant):
    for by in ("group", "teacher", "room"):
        r = admin_client_ru.get(reverse("admin:timetable_variant_grid", args=[variant.pk]) + f"?by={by}&week=even")
        assert r.status_code == 200 and 'class="sg"' in r.content.decode()
    r = admin_client_ru.get(
        reverse("admin:timetable_variant_free_rooms") + f"?variant={variant.pk}&day=1&pair_from=1&pair_to=3&week=both"
    )
    assert r.status_code == 200
    r = admin_client_ru.get(reverse("admin:timetable_variant_hours", args=[variant.pk]))
    assert r.status_code == 200 and "Итого" in r.content.decode()
    r = admin_client_ru.get(reverse("admin:timetable_variant_hours", args=[variant.pk]) + "?format=xlsx")
    assert r.content[:2] == b"PK"


def test_free_rooms_logic(variant):
    p, s = load_variant(variant)
    res = sv.free_rooms(p, s, 1, 1, 7, (Week.ODD, Week.EVEN))
    busy = {(a.room_id, a.slot_id) for a in s.assignments if a.room_id}
    for r in res:
        assert all((r.room_id, f"1-{n}") not in busy for n in r.free_pairs)
        room = p.room_by_id[r.room_id]
        assert all(f"1-{n}" not in room.blocked for n in r.free_pairs)
    blocked = next(r for r in p.rooms if r.blocked)
    assert all(x.room_id != blocked.id for x in res)  # в синтетике занята «не нами» по Пн и Вт целиком
    only_full = sv.free_rooms(p, s, 1, 1, 7, (Week.ODD, Week.EVEN), include_partial=False)
    assert all(x.fully_free for x in only_full)


def test_group_grid_contains_all_group_classes(variant):
    p, s = load_variant(variant)
    g = p.groups[0]
    rows = sv.grid(p, s, "group", g.id, Week.ODD)
    shown = sum(len(c) for row in rows for c in row.cells)
    expected_slots = {
        a.slot_id
        for a in s.assignments
        if Week.ODD in (a.week, Week.BOTH) and g.id in p.item_groups(p.item_by_id[a.item_id])
    }
    assert shown >= len(expected_slots) > 0
