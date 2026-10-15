"""Черновик из текущего расписания: вымышленная книга в форматах учебного отдела (ПиУП и кино).

Настоящий файл с ФИО в репозиторий не кладётся — здесь его структура воспроизведена на
выдуманных данных: многострочные ячейки ПиУП, «Дисциплина  Фамилия Имя» + аудитория справа
в кино, английский блоком, поток «1КП1 + 1КП2», чётность словами, школьная аудитория,
разные написания одной аудитории.
"""

import io
import zipfile

import pytest
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from timetable import models as m
from timetable.importers.current_schedule import SCHOOL_ROOM, aggregate, canon_room, parse_workbook
from timetable.importers.draft_writer import references_xlsx, workload_xlsx
from timetable.services.problem import create_semester
from timetable.xlsx.references import import_references
from timetable.xlsx.workload import import_workload

pytestmark = pytest.mark.django_db
HDR = ["Группа", "Пара", "ПН", "", "ВТ", "", "СР", "", "ЧТ", "", "ПТ", ""]


def _book() -> bytes:
    wb = Workbook()
    rooms = wb.active
    rooms.title = "Аудитории шаблон"
    rooms.append([None, "св-во ауд.", "№", "ПН", "ВТ", "СР", "ЧТ", "ПТ", None])
    for name, prop, pcs in [
        ("Ц1 104", "компы", "20к"),
        ("Ц1 301", "лекционка", None),
        ("АРТ 212", "компы", "16к"),
        ("Ц2 1 этаж новая", "компы", "27к"),
        ("Ц1 207", "компы (ПО Джава)", "24к"),
    ]:
        rooms.append([name, prop, 1, None, None, None, None, None, pcs])
    piup = wb.create_sheet("Группы 1")
    piup.append(HDR)
    piup.append(
        [
            "1ИТ1",
            1,
            "Математика\nПетрова\n1ИТ1\nЦ1 301",
            None,
            "Иностранный язык\nСидорова (Ц1 301)\nОрлова (Ц1 104)\n1ИТ1+1ИТ2",
            None,
            "Python\nКозлов\n1ИТ1\nЦ1.207",
            None,
            None,
            None,
            None,
            None,
        ]
    )
    piup.append(
        [
            "1ИТ1",
            2,
            "Математика\nПетрова\n1ИТ1\nЦ1 301",
            None,
            None,
            None,
            "Big Data\nКозлов\n1ИТ1    МИГАЮЩАЯ\nОНЛАЙН",
            None,
            None,
            None,
            None,
            None,
        ]
    )
    piup.append(
        [
            "1ИТ2",
            1,
            None,
            None,
            "Иностранный язык\nСидорова (Ц1 301)\nОрлова (Ц1 104)\n1ИТ1+1ИТ2",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ]
    )
    piup.append(
        ["1ИТ2", 2, "Элементы математики\n1ИТ2\nЗайцев\nЦ1 301", None, None, None, None, None, None, None, None, None]
    )
    # английский: пара сочетания, где занимается только одна подгруппа (один преподаватель)
    piup.append(["1ИТ2", 3, None, None, "Иностранный язык\nОрлова\n1ИТ1+1ИТ2\nЦ1 104", None] + [None] * 6)
    kino = wb.create_sheet("Расписание Кино")
    kino.append(HDR)
    kino.append(
        [
            "1КП1.26",
            1,
            "Режиссура                 Громов Павел",
            "арт 212",
            "Монтаж Белова Анна",
            "Ц1 104",
            "Звук- Тихонов И. ЧЁТНЫЕ/Свет Яркова Ольга",
            "Ц1 301",
            None,
            None,
            None,
            None,
        ]
    )
    kino.append(
        [
            "1КП1.26",
            2,
            "Режиссура                 Громов Павел",
            "АРТ 212",
            None,
            None,
            "Звук- Тихонов И. ЧЁТНЫЕ/Свет Яркова Ольга",
            "Ц1 301",
            "История кино Громов Павел",
            "Ц2 эт.новая",
            None,
            None,
        ]
    )
    kino.append(
        [
            "1КП2.26",
            1,
            "Монтаж Белова Анна",
            "Ц1 104",
            None,
            None,
            None,
            None,
            None,
            None,
            "Сидорова\nОрлова\n1КП1 + 1КП9",
            None,
        ]
    )
    kino.append(["1КП1.26", 3, None, None, None, None, None, None, None, None, "Сидорова\nОрлова\n1КП1 + 1КП9", None])
    kino.append(["1КП1.26", 6, None, None, None, None, None, None, None, None, "Киноклуб Громов Павел", None])
    kino.append(["1КП2.26", 6, None, None, None, None, None, None, None, None, "киноклуб Громов Павел", None])
    kino.append(["1КП2.26", 7, None, None, None, None, None, None, None, None, "киноклуб", None])
    teachers = wb.create_sheet("Преподаватели")
    teachers.append([None, "№", "ПН"])
    teachers.append(["Петрова", 1, None])
    teachers.append(["Козлов", 1, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def draft():
    d = parse_workbook(io.BytesIO(_book()))
    items, blocks = aggregate(d)
    return d, items, blocks


def test_room_names_are_normalised():
    known = {"Ц1 205.2": {}}
    assert canon_room("арт 212") == "АРТ 212"
    assert canon_room("Ц1.206.1") == "Ц1 206.1"
    assert canon_room("ц1 холл") == "Ц1 ХОЛЛ"
    assert canon_room("205.2", known) == "Ц1 205.2"
    for school in ("Ц2 1 эт новая", "Ц2 эт.новая", "Ц2 1Э", "Ц2 1 этаж новая"):
        assert canon_room(school) == SCHOOL_ROOM


def test_rooms_and_school_room(draft):
    d, _, _ = draft
    assert d.rooms["Ц1 104"]["pcs"] == 20 and "компы" in d.rooms["Ц1 104"]["props"]
    assert "java" in d.rooms["Ц1 207"]["props"]
    assert "школ" in d.rooms[SCHOOL_ROOM]["note"]
    # «арт 212» в листе кино и «АРТ 212» на листе аудиторий — одна аудитория
    assert "арт 212" not in d.rooms and "АРТ 212" in d.rooms


def test_groups_and_departments(draft):
    d, _, _ = draft
    assert set(d.groups) == {"1ИТ1", "1ИТ2", "1КП1", "1КП2"}
    assert d.groups["1КП1"]["department"] == "Кино" and d.groups["1ИТ1"]["department"] == "ПиУП"


def _item(items, d, disc, group=None):
    return next(it for it in items if it.discipline == disc and (group is None or group in it.groups))


def test_workload_counts_and_kino_teacher_split(draft):
    d, items, _ = draft
    assert _item(items, d, "Математика").pairs_per_cycle == 4  # 2 пары в неделю
    rez = _item(items, d, "Режиссура")
    assert [d.teachers[t] for t in rez.teachers] == ["Громов Павел"] and rez.pairs_per_cycle == 4
    assert rez.room_type == "computer"
    mont = _item(items, d, "Монтаж", "1КП1")
    assert [d.teachers[t] for t in mont.teachers] == ["Белова Анна"]


def test_kinoclub_is_one_manual_item_pinned_to_its_room(draft):
    """Киноклуб всегда в Ц1 301 вечером: одно занятие на все группы, где стоит, пары закреплены."""
    d, items, _ = draft
    clubs = [it for it in items if it.discipline.lower() == "киноклуб"]
    assert len(clubs) == 1
    club = clubs[0]
    assert club.planning == "manual" and club.groups == ["1КП1", "1КП2"]
    assert club.pins == ["Пт 6 (Ц1 301)", "Пт 7 (Ц1 301)"] and club.pairs_per_cycle == 4
    assert [d.teachers[t] for t in club.teachers] == ["Громов Павел"]


def test_single_teacher_english_cell_belongs_to_block(draft):
    """«Орлова / 1ИТ1+1ИТ2» без остальных — пара того же блока, а не отдельная нагрузка."""
    d, items, _ = draft
    blk = {d.teachers[it.teachers[0]]: it for it in items if it.block and "1ИТ" in it.block}
    assert blk["Орлова"].pairs_per_cycle == 4 and blk["Сидорова"].pairs_per_cycle == 2
    assert not [it for it in items if it.discipline == "Иностранный язык" and not it.block]
    assert any("не у всех подгрупп одновременно" in i.problem for i in d.issues)


def test_teacher_after_groups_line_is_recognised(draft):
    d, items, _ = draft
    it = _item(items, d, "Элементы математики")
    assert [d.teachers[t] for t in it.teachers] == ["Зайцев"]


def test_parity_split_gives_alt_week(draft):
    """«Звук … ЧЁТНЫЕ/ Свет …» — две дисциплины через неделю; по 2 пары подряд в одну неделю."""
    d, items, _ = draft
    zvuk, svet = _item(items, d, "Звук"), _item(items, d, "Свет")
    assert zvuk.week_pattern == svet.week_pattern == "alt_week"
    assert zvuk.pairs_per_cycle == svet.pairs_per_cycle == 2
    assert "чётным" in zvuk.comment and "нечётным" in svet.comment


def test_blink_and_online(draft):
    d, items, _ = draft
    bd = _item(items, d, "Big Data")
    assert bd.online and bd.pairs_per_cycle == 1 and bd.room_type is None


def test_english_block_and_unknown_group_fallback(draft):
    d, items, blocks = draft
    assert any(set(g) == {"1ИТ1", "1ИТ2"} for g in blocks.values())
    # «1КП1 + 1КП9»: группы 1КП9 нет → участники по строкам листа, где стоит ячейка
    kino_block = next(g for name, g in blocks.items() if "1КП" in name)
    assert set(kino_block) == {"1КП1", "1КП2"}
    assert any("1КП9" in i.problem for i in d.issues)
    blk_items = [it for it in items if it.block and "1ИТ" in it.block]
    assert {d.teachers[it.teachers[0]] for it in blk_items} == {"Сидорова", "Орлова"}


def test_drafts_load_through_regular_importers(draft):
    d, items, blocks = draft
    sem = create_semester("Черновик", __import__("datetime").date(2026, 9, 1))
    rep = import_references(sem, references_xlsx(d, items), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert not m.Room.objects.get(name=SCHOOL_ROOM).active
    assert m.Discipline.objects.get(name="Режиссура").department.name == "Кино"
    rep = import_workload(sem, workload_xlsx(d, items, blocks), dry_run=False)
    assert rep.ok, [str(e) for e in rep.errors]
    assert m.WorkloadItem.objects.filter(semester=sem, week_pattern="alt_week").count() == 2
    assert m.SyncBlock.objects.filter(semester=sem).count() == len(blocks)
    club = m.WorkloadItem.objects.get(semester=sem, discipline__name="Киноклуб")
    assert club.planning == "manual" and club.pairs_per_cycle == 4
    assert {g.name for g in club.groups.all()} == {"1КП1", "1КП2"}
    assert sorted((p.week, p.day, p.pair, p.room.name) for p in club.pins.all()) == [
        ("both", 5, 6, "Ц1 301"),
        ("both", 5, 7, "Ц1 301"),
    ]


def test_admin_draft_page(admin_client_ru):
    from django.core.files.uploadedfile import SimpleUploadedFile

    url = reverse("admin:timetable_draft_from_schedule")
    assert admin_client_ru.get(url).status_code == 200
    r = admin_client_ru.post(url, {"file": SimpleUploadedFile("current.xlsx", _book())})
    assert r.status_code == 200 and r["Content-Type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert set(names) == {"references_draft.xlsx", "workload_draft.xlsx", "report.xlsx"}
    rep = load_workbook(io.BytesIO(zipfile.ZipFile(io.BytesIO(r.content)).read("report.xlsx")))
    assert "Что проверить" in rep.sheetnames
    r = admin_client_ru.post(url, {"file": SimpleUploadedFile("bad.xlsx", b"not excel")})
    assert r.status_code == 200 and "Не удалось прочитать" in r.content.decode()


def test_draft_rooms_are_vdnh_until_21(draft):
    import datetime as dt

    d, items, _ = draft
    sem = create_semester("Черновик ВДНХ", __import__("datetime").date(2026, 9, 1))
    assert import_references(sem, references_xlsx(d, items), dry_run=False).ok
    site = m.Site.objects.get(name="ВДНХ")
    assert site.closes_at == dt.time(21, 0)
    assert not m.Room.objects.filter(site=None).exists()
    club = next(it for it in items if it.discipline.lower() == "киноклуб")
    assert club.may_end_early
