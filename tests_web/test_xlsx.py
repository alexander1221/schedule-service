"""Импорт/экспорт xlsx: обратимость, ошибки по строкам, «всё или ничего», пробный прогон."""

import io

import pytest
from openpyxl import load_workbook

from timetable import models as m
from timetable.xlsx.references import export_references, import_references
from timetable.xlsx.workload import COLUMNS, SHEET, export_workload, import_workload

pytestmark = pytest.mark.django_db


def _wb(data: bytes):
    return load_workbook(io.BytesIO(data))


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _col(ws, key):
    title = COLUMNS[key][0]
    return next(c.column for c in ws[1] if c.value == title)


def test_workload_export_import_is_noop(synthetic):
    sem = synthetic[0]
    data = export_workload(sem)
    rep = import_workload(sem, data, dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert rep.applied
    assert rep.created == {} and rep.updated == {}  # ничего не изменилось


def test_workload_update_and_create(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    first_id = ws.cell(row=2, column=_col(ws, "id")).value
    ws.cell(row=2, column=_col(ws, "pairs")).value = 5
    # новая строка: копия второй без ID
    new_row = [c.value for c in ws[3]]
    new_row[_col(ws, "id") - 1] = None
    new_row[_col(ws, "comment") - 1] = "новая"
    ws.append(new_row)
    before = m.WorkloadItem.objects.filter(semester=sem).count()
    rep = import_workload(sem, _save(wb), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert rep.updated == {"элементы нагрузки": 1}
    assert rep.created.get("элементы нагрузки") == 1
    assert m.WorkloadItem.objects.get(pk=first_id).pairs_per_cycle == 5
    assert m.WorkloadItem.objects.filter(semester=sem).count() == before + 1


def test_dry_run_writes_nothing(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    item_id = ws.cell(row=2, column=_col(ws, "id")).value
    old = m.WorkloadItem.objects.get(pk=item_id).pairs_per_cycle
    ws.cell(row=2, column=_col(ws, "pairs")).value = old + 1
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert rep.ok and not rep.applied and rep.updated
    assert m.WorkloadItem.objects.get(pk=item_id).pairs_per_cycle == old


def test_errors_are_per_row_and_nothing_is_applied(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    item_id = ws.cell(row=2, column=_col(ws, "id")).value
    ws.cell(row=2, column=_col(ws, "pairs")).value = 6  # корректная правка...
    first_teacher = ws.cell(row=3, column=_col(ws, "teachers")).value.split(";")[0]
    ws.cell(row=3, column=_col(ws, "teachers")).value = first_teacher[:-1] + "ъ"  # ...и опечатка
    ws.cell(row=4, column=_col(ws, "kind")).value = "семинарище"
    ws.cell(row=5, column=_col(ws, "pairs")).value = "три"
    old = m.WorkloadItem.objects.get(pk=item_id).pairs_per_cycle
    rep = import_workload(sem, _save(wb), dry_run=False)
    assert not rep.ok and not rep.applied
    rows = {(e.row, e.column) for e in rep.errors}
    assert (3, "Преподаватели") in rows and (4, "Вид") in rows and (5, "Пар за 2 недели") in rows
    assert any("Возможно" in e.message for e in rep.errors if e.row == 3)
    assert m.WorkloadItem.objects.get(pk=item_id).pairs_per_cycle == old  # всё или ничего


def test_column_order_free_and_required_columns(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    ws.move_range("A1:A500", cols=20)  # перенесли ID в конец
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert rep.ok, [str(e) for e in rep.errors]
    ws.delete_cols(_col(ws, "discipline"))
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert not rep.ok and "Дисциплина" in rep.errors[0].message


def test_replace_mode_deletes_missing(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    ws.delete_rows(2, 3)
    before = m.WorkloadItem.objects.filter(semester=sem).count()
    rep = import_workload(sem, _save(wb), dry_run=False, replace=True)
    assert rep.ok
    assert rep.deleted == {"элементы нагрузки": 3}
    assert m.WorkloadItem.objects.filter(semester=sem).count() == before - 3


def test_in_person_without_room_type_is_rejected(synthetic):
    sem = synthetic[0]
    wb = _wb(export_workload(sem))
    ws = wb[SHEET]
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=_col(ws, "format")).value == "очно":
            ws.cell(row=r, column=_col(ws, "room_type")).value = None
            row = r
            break
    rep = import_workload(sem, _save(wb), dry_run=True)
    assert any(e.row == row and "тип аудитории" in e.message for e in rep.errors)


def test_not_xlsx_file(synthetic):
    rep = import_workload(synthetic[0], b"this is not excel", dry_run=True)
    assert not rep.ok and "xlsx" in rep.errors[0].message


def test_references_roundtrip_and_upsert(synthetic):
    sem = synthetic[0]
    data = export_references(sem)
    rep = import_references(sem, data, dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert rep.created == {} and rep.updated == {}
    wb = _wb(data)
    wb["Преподаватели"].append(["Новикова Н.Н.", "novikova@college.example", "да", 4, "нет"])
    wb["Аудитории"].append(["999", "Корпус 3", 40, "Обычная", "да"])
    ws = wb["Доступность"]
    ws.cell(row=2, column=3).value = -2  # Пн 1 у первого преподавателя
    rep = import_references(sem, _save(wb), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert rep.created["преподаватели"] == 1 and rep.created["аудитории"] == 1
    t = m.Teacher.objects.get(name=ws.cell(row=2, column=1).value)
    assert m.TeacherAvailability.objects.get(teacher=t, semester=sem).prefs["1-1"] == -2


def test_references_bad_values(synthetic):
    sem = synthetic[0]
    wb = _wb(export_references(sem))
    wb["Аудитории"].append(["998", "", "много", "Несуществующий", "да"])
    wb["Доступность"].cell(row=2, column=3).value = 5
    rep = import_references(sem, _save(wb), dry_run=False)
    assert not rep.ok
    assert not m.Room.objects.filter(name="998").exists()
    assert {e.sheet for e in rep.errors} == {"Аудитории", "Доступность"}
