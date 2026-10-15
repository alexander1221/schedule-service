"""0.4.1: вместимость можно не учитывать; колонка «Закрепления» в шаблоне нагрузки."""

import io

import pytest
from openpyxl import load_workbook

from timetable import models as m
from timetable.services.problem import build_problem
from timetable.xlsx.workload import export_workload, import_workload

pytestmark = pytest.mark.django_db


def _set(ws, row, title, value):
    col = next(c.column for c in ws[1] if c.value == title)
    ws.cell(row, col).value = value


def _save(wb):
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_check_capacity_goes_to_problem(synthetic):
    sem = synthetic[0]
    assert build_problem(sem).policy.check_capacity is True  # синтетика переносит свою политику
    sem.check_capacity = False
    sem.save()
    p = build_problem(sem)
    assert p.policy.check_capacity is False
    small = min(p.active_rooms, key=lambda r: r.capacity)
    it = next(i for i in p.items if i.needs_room and i.room_type_id == small.type_id and not p.required_equipment([i]))
    assert small in p.eligible_rooms([it])


def test_new_semester_ignores_capacity_by_default():
    assert m.Semester._meta.get_field("check_capacity").default is False


def test_pins_column_roundtrip_and_errors(synthetic):
    sem = synthetic[0]
    item = m.WorkloadItem.objects.filter(semester=sem, sync_block=None).first()
    room = m.Room.objects.filter(active=True).first()
    wb = load_workbook(io.BytesIO(export_workload(sem)))
    ws = wb["Нагрузка"]
    row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1).value == item.pk)
    _set(ws, row, "Закрепления", f"Пт 6 ({room.name}); пт 7 нечёт")
    rep = import_workload(sem, _save(wb), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    got = sorted((p.week, p.day, p.pair, p.room_id) for p in item.pins.all())
    assert got == [("both", 5, 6, room.pk), ("odd", 5, 7, None)]
    # экспорт показывает то же самое, повторный импорт ничего не меняет
    ws2 = load_workbook(io.BytesIO(export_workload(sem)))["Нагрузка"]
    col = next(c.column for c in ws2[1] if c.value == "Закрепления")
    row2 = next(r for r in range(2, ws2.max_row + 1) if ws2.cell(r, 1).value == item.pk)
    assert ws2.cell(row2, col).value == f"Пт 6 ({room.name}); Пт 7 нечёт"
    rep = import_workload(sem, export_workload(sem), dry_run=False)
    assert rep.ok and not rep.updated, rep.updated
    # ошибки: непонятный текст и неизвестная аудитория
    _set(ws, row, "Закрепления", "пятница вечером")
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert any(e.column == "Закрепления" for e in rep.errors)
    _set(ws, row, "Закрепления", "Пт 6 (Марс 1)")
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert any("Марс 1" in e.message for e in rep.errors)
    # пустая ячейка снимает закрепления
    _set(ws, row, "Закрепления", None)
    assert import_workload(sem, _save(wb), dry_run=False).ok
    assert not item.pins.exists()


def test_semester_added_in_admin_gets_default_courses(admin_client_ru):
    from django.urls import reverse

    url = reverse("admin:timetable_semester_add")
    page = admin_client_ru.get(url).content.decode()
    import re

    data = {
        "name": "Новый",
        "start_date": "2026-09-01",
        "first_week": "odd",
        "status": "draft",
        "format_gap_min": 90,
        "discipline_per_day": 1,
        "heavy_threshold": 2,
        "heavy_max_consecutive": 2,
        "teacher_max_per_day": 5,
        "teacher_min_per_day": 2,
        "hours_per_pair": 2,
    }
    for prefix in set(re.findall(r'name="([\w-]+)-TOTAL_FORMS"', page)):
        data.update({f"{prefix}-TOTAL_FORMS": 0, f"{prefix}-INITIAL_FORMS": 0})
    r = admin_client_ru.post(url, data)
    assert r.status_code == 302, r.content.decode()[:2000]
    sem = m.Semester.objects.get(name="Новый")
    assert sorted(sem.courses.values_list("number", flat=True)) == [1, 2, 3, 4]
    assert sem.pairs.count() == 7


def test_site_closing_roundtrip_and_problem(synthetic):
    """Площадка с закрытием 21:00: справочники xlsx, флажок «можно раньше» в нагрузке, задача решателя."""
    import datetime as dt

    from timetable.xlsx.references import export_references, import_references

    sem = synthetic[0]
    wb = load_workbook(io.BytesIO(export_references(sem)))
    assert "Площадки" in wb.sheetnames
    wb["Площадки"].append(["ВДНХ", "21.00", "аренда"])
    ws = wb["Аудитории"]
    col = next(c.column for c in ws[1] if c.value == "Площадка")
    for r in range(2, ws.max_row + 1):
        ws.cell(r, col).value = "ВДНХ"
    rep = import_references(sem, _save(wb), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    site = m.Site.objects.get(name="ВДНХ")
    assert site.closes_at == dt.time(21, 0) and site.rooms.count() == m.Room.objects.count()
    # повторный импорт выгрузки ничего не меняет
    rep = import_references(sem, export_references(sem), dry_run=False)
    assert rep.ok and not rep.updated, rep.updated
    # неизвестная площадка — понятная ошибка
    ws.cell(2, col).value = "Луна"
    rep = import_references(sem, _save(wb), dry_run=True)
    assert any(e.column == "Площадка" and "Луна" in e.message for e in rep.errors)

    item = m.WorkloadItem.objects.filter(semester=sem, format="in_person", sync_block=None).first()
    wl = load_workbook(io.BytesIO(export_workload(sem)))
    w = wl["Нагрузка"]
    row = next(r for r in range(2, w.max_row + 1) if w.cell(r, 1).value == item.pk)
    _set(w, row, "Можно раньше", "да")
    assert import_workload(sem, _save(wl), dry_run=False).ok
    item.refresh_from_db()
    assert item.may_end_early

    p = build_problem(sem)
    assert {r.closes_min for r in p.rooms} == {21 * 60}
    it = p.item_by_id[str(item.pk)]
    assert it.may_end_early
    last = max(p.grid.pairs, key=lambda x: x.start_min).number
    other = next(i for i in p.items if i.needs_room and not i.may_end_early and i.id not in p.block_of_item)
    assert all(s.pair != last for s in p.allowed_slots(other))


def test_site_admin_and_actions(admin_client_ru, synthetic):
    from django.urls import reverse

    sem = synthetic[0]
    assert admin_client_ru.get(reverse("admin:timetable_site_changelist")).status_code == 200
    ids = list(m.WorkloadItem.objects.filter(semester=sem).values_list("pk", flat=True)[:3])
    url = reverse("admin:timetable_workloaditem_changelist")
    admin_client_ru.post(url, {"action": "set_end_early", "_selected_action": ids})
    assert m.WorkloadItem.objects.filter(pk__in=ids, may_end_early=True).count() == 3
    admin_client_ru.post(url, {"action": "unset_end_early", "_selected_action": ids})
    assert not m.WorkloadItem.objects.filter(pk__in=ids, may_end_early=True).exists()
