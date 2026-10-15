"""Веб-часть 0.3.0: схема «через неделю», бронь аудитории, элективы «вручную», языковой блок курса."""

import io

import pytest
from django.urls import reverse
from openpyxl import load_workbook

from scheduler.domain import WeekPattern
from scheduler.validator import validate
from timetable import models as m
from timetable.services.problem import build_problem, manual_items_status
from timetable.xlsx.workload import COLUMNS, SHEET, export_workload, import_workload

pytestmark = pytest.mark.django_db


def test_synthetic_roundtrip_keeps_new_fields(synthetic):
    sem, ids, planted, original = synthetic
    built = build_problem(sem)
    assert sum(i.is_alt_week for i in built.items) == sum(i.is_alt_week for i in original.items) > 0
    assert sum(i.teacher_in_room for i in built.items) == sum(i.teacher_in_room for i in original.items)
    assert built.policy.teacher_min_per_day == original.policy.teacher_min_per_day
    assert validate(built, ids.schedule(planted)).breakdown == validate(original, planted).breakdown


def test_model_clean_rules(synthetic):
    sem = synthetic[0]
    it = m.WorkloadItem.objects.filter(semester=sem, format="in_person").first()
    it.teacher_in_room = True
    with pytest.raises(Exception, match="только для онлайн"):
        it.full_clean()
    it.teacher_in_room = False
    it.week_pattern = "alt_week"
    it.pairs_per_cycle = 3
    with pytest.raises(Exception, match="чётное"):
        it.full_clean()


def test_manual_items_skipped_until_fully_pinned(synthetic):
    sem = synthetic[0]
    it = m.WorkloadItem.objects.filter(
        semester=sem, pairs_per_cycle=2, sync_block=None, merge_group=None, week_pattern="auto"
    ).first()
    it.pins.all().delete()
    it.planning = "manual"
    it.save()
    assert manual_items_status(sem)[it.pk] is False
    assert str(it.pk) not in {i.id for i in build_problem(sem).items}
    m.Pin.objects.create(item=it, week="both", day=1, pair=3)
    assert manual_items_status(sem)[it.pk] is True
    p = build_problem(sem)
    assert str(it.pk) in {i.id for i in p.items}
    assert any(pin.item_id == str(it.pk) for pin in p.pins)


def _col(ws, key):
    return next(c.column for c in ws[1] if c.value == COLUMNS[key][0])


def test_xlsx_new_columns_roundtrip_and_edit(synthetic):
    sem = synthetic[0]
    data = export_workload(sem)
    rep = import_workload(sem, data, dry_run=False)
    assert rep.ok and rep.updated == {}, [str(e) for e in rep.errors]
    wb = load_workbook(io.BytesIO(data))
    ws = wb[SHEET]
    target = None
    for r in range(2, ws.max_row + 1):
        if (
            ws.cell(r, _col(ws, "pairs")).value == 2
            and ws.cell(r, _col(ws, "week_pattern")).value == "обычная"
            and not ws.cell(r, _col(ws, "block")).value
            and not ws.cell(r, _col(ws, "merge")).value
        ):
            target = r
            break
    item_id = ws.cell(target, _col(ws, "id")).value
    ws.cell(target, _col(ws, "week_pattern")).value = "через неделю"
    ws.cell(target, _col(ws, "planning")).value = "вручную"
    buf = io.BytesIO()
    wb.save(buf)
    rep = import_workload(sem, buf.getvalue(), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    it = m.WorkloadItem.objects.get(pk=item_id)
    assert it.week_pattern == "alt_week" and it.planning == "manual"


def test_xlsx_old_template_keeps_new_fields(synthetic):
    """Файл без новых колонок (старый шаблон) не сбрасывает схему недель и бронь."""
    sem = synthetic[0]
    booked = m.WorkloadItem.objects.filter(semester=sem, teacher_in_room=True).count()
    alt = m.WorkloadItem.objects.filter(semester=sem, week_pattern="alt_week").count()
    wb = load_workbook(io.BytesIO(export_workload(sem)))
    ws = wb[SHEET]
    for key in ("planning", "teacher_in_room", "week_pattern"):
        ws.delete_cols(_col(ws, key))
    buf = io.BytesIO()
    wb.save(buf)
    rep = import_workload(sem, buf.getvalue(), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert m.WorkloadItem.objects.filter(semester=sem, teacher_in_room=True).count() == booked
    assert m.WorkloadItem.objects.filter(semester=sem, week_pattern="alt_week").count() == alt


def test_xlsx_booking_without_room_type_is_allowed(synthetic):
    """С 0.4.0: для дистанта с бронью тип аудитории не обязателен — подойдёт любая свободная."""
    sem = synthetic[0]
    wb = load_workbook(io.BytesIO(export_workload(sem)))
    ws = wb[SHEET]
    item_id = None
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, _col(ws, "format")).value == "онлайн":
            ws.cell(r, _col(ws, "teacher_in_room")).value = "да"
            ws.cell(r, _col(ws, "room_type")).value = None
            item_id = ws.cell(r, _col(ws, "id")).value
            break
    buf = io.BytesIO()
    wb.save(buf)
    rep = import_workload(sem, buf.getvalue(), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    it = m.WorkloadItem.objects.get(pk=item_id)
    assert it.teacher_in_room and it.room_type_id is None


def test_admin_actions_and_hint_filters(admin_client_ru, synthetic):
    sem = synthetic[0]
    url = reverse("admin:timetable_workloaditem_changelist")
    for hint in ("alt", "booking"):
        r = admin_client_ru.get(url + f"?semester__id__exact={sem.pk}&hint={hint}")
        assert r.status_code == 200
    online = m.WorkloadItem.objects.filter(semester=sem, format="online", teacher_in_room=False).first()
    r = admin_client_ru.post(url, {"action": "book_room", "_selected_action": [online.pk]})
    assert r.status_code == 302
    online.refresh_from_db()
    assert online.teacher_in_room and online.room_type_id
    item = m.WorkloadItem.objects.filter(semester=sem, pairs_per_cycle=2, sync_block=None, week_pattern="auto").first()
    admin_client_ru.post(url, {"action": "set_alt_week", "_selected_action": [item.pk]})
    item.refresh_from_db()
    assert item.week_pattern == "alt_week"
    admin_client_ru.post(url, {"action": "set_manual", "_selected_action": [item.pk]})
    item.refresh_from_db()
    assert item.planning == "manual"
    r = admin_client_ru.get(reverse("admin:timetable_semester_precheck", args=[sem.pk]))
    html = r.content.decode()
    assert "Подсказки" in html and "Ставятся вручную" in html


def test_language_block_for_course(admin_client_ru, synthetic):
    sem = synthetic[0]
    course = sem.courses.get(number=2)
    groups = list(course.groups.all())
    eng = m.Discipline.objects.get(name="Английский язык")
    teachers = list(m.Teacher.objects.all()[:3])
    url = reverse("admin:timetable_course_language_block", args=[course.pk])
    assert admin_client_ru.get(url).status_code == 200
    r = admin_client_ru.post(
        url,
        {
            "discipline": eng.pk,
            "teachers": [t.pk for t in teachers],
            "pairs_per_cycle": 4,
            "room_type": m.RoomType.objects.get(code="regular").pk,
            "replace_group_items": "on",
        },
    )
    assert r.status_code == 302, r.content.decode()[:2000]
    block = m.SyncBlock.objects.get(semester=sem, name=f"{eng}, 2 курс")
    assert set(block.groups.all()) == set(groups)
    assert block.items.count() == 3
    assert sum(i.headcount for i in block.items.all()) == sum(g.size for g in groups)
    # отдельных элементов английского у групп курса больше нет
    assert not m.WorkloadItem.objects.filter(semester=sem, discipline=eng, groups__in=groups, sync_block=None).exists()
    p = build_problem(sem)
    blk = next(b for b in p.blocks if b.name == block.name)
    assert len(blk.item_ids) == 3 and len(blk.group_ids) == len(groups)


def test_domain_enum_matches_db_choices():
    assert {c for c, _ in m.WorkloadItem.WeekPattern.choices} == {w.value for w in WeekPattern}
