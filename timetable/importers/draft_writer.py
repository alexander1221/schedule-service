"""Запись черновика (из текущего расписания) в наши шаблоны импорта xlsx + отчёт о сомнительном."""

from __future__ import annotations

import io
import re
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment

from ..xlsx.common import finish_sheet, write_header
from ..xlsx.workload import COLUMNS, FORMAT_OUT, KIND_OUT, PLANNING_OUT, WEEK_OUT
from .current_schedule import (
    DEFAULT_CAPACITY,
    DEFAULT_GROUP_SIZE,
    EQUIPMENT_NAMES,
    PROPERTY_EQUIPMENT,
    PROPERTY_TYPE,
    SCHOOL_ROOM,
    SITE_CLOSES,
    SITE_NAME,
    Draft,
    DraftItem,
    _room_type,
)

TYPE_NAMES = dict(PROPERTY_TYPE.values())


def _save(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def references_xlsx(d: Draft, items: list[DraftItem]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Преподаватели"
    write_header(
        ws,
        [
            "ФИО",
            "Почта",
            "Работает",
            "Макс. пар в день",
            "Лимит жёсткий",
            "Нужно оборудование",
            "Предпочитает аудитории",
        ],
        [34, 28, 10, 12, 12, 24, 24],
    )
    for name in sorted(d.teachers.values()):
        ws.append([name, None, "да", None, "нет", None, None])
    finish_sheet(ws)

    ws = wb.create_sheet("Оборудование")
    write_header(ws, ["Код", "Название"], [16, 30])
    for code, name in EQUIPMENT_NAMES.items():
        ws.append([code, name])
    finish_sheet(ws)

    ws = wb.create_sheet("Типы аудиторий")
    write_header(ws, ["Код", "Название"], [16, 30])
    for code, name in PROPERTY_TYPE.values():
        ws.append([code, name])
    finish_sheet(ws)

    ws = wb.create_sheet("Площадки")
    write_header(ws, ["Название", "Закрывается в", "Примечание"], [20, 14, 50])
    ws.append([SITE_NAME, SITE_CLOSES, "аренда: строгое закрытие; Ц1, Ц2, АРТ"])
    finish_sheet(ws)

    ws = wb.create_sheet("Аудитории")
    write_header(
        ws,
        ["Номер", "Корпус", "Вместимость", "Тип", "Используется", "Оборудование", "Примечание", "Площадка"],
        [18, 10, 12, 22, 12, 30, 50, 12],
    )
    for name, r in sorted(d.rooms.items()):
        eq = []
        for prop in sorted(r["props"]):
            eq += [EQUIPMENT_NAMES[c] for c in PROPERTY_EQUIPMENT.get(prop, [])]
        if "java" in r["props"]:
            eq.append(EQUIPMENT_NAMES["java_sw"])
        notes = []
        cap = r["pcs"]
        if cap is None:
            cap = DEFAULT_CAPACITY
            notes.append("вместимость в файле не указана — уточните")
        else:
            notes.append(f"вместимость = число компьютеров ({cap}) — уточните")
        if r["note"]:
            notes.insert(0, r["note"])
        if len(r["sources"]) > 1:
            notes.append("в файле пишется как: " + ", ".join(sorted(r["sources"])))
        building = name.split(" ")[0] if re.match(r"^(Ц[12]|АРТ)\b", name) else ""
        ws.append(
            [
                name,
                building,
                cap,
                TYPE_NAMES[_room_type(r)[0]],
                "нет" if name == SCHOOL_ROOM else "да",
                "; ".join(dict.fromkeys(eq)),
                "; ".join(notes),
                SITE_NAME,
            ]
        )
    finish_sheet(ws)

    ws = wb.create_sheet("Дисциплины")
    write_header(ws, ["Название", "Сложность", "Кафедра"], [50, 10, 14])
    dep_of: dict[str, str] = {}
    for it in items:
        for g in it.groups or []:
            if g in d.groups:
                dep_of.setdefault(it.discipline, d.groups[g]["department"])
        if it.block:
            g0 = it.block.split("(")[-1].split(" ")[0]
            if g0 in d.groups:
                dep_of.setdefault(it.discipline, d.groups[g0]["department"])
    for disc in sorted({it.discipline for it in items}):
        ws.append([disc, 0, dep_of.get(disc, "")])
    finish_sheet(ws)

    ws = wb.create_sheet("Группы")
    write_header(ws, ["Название", "Курс", "Численность"], [14, 8, 12])
    for g, info in sorted(d.groups.items()):
        ws.append([g, info["course"], DEFAULT_GROUP_SIZE])
    finish_sheet(ws)
    return _save(wb)


def workload_xlsx(d: Draft, items: list[DraftItem], blocks: dict[str, list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Нагрузка"
    keys = list(COLUMNS)
    write_header(ws, [COLUMNS[k][0] for k in keys], [COLUMNS[k][1] for k in keys])
    for it in items:
        row = {
            "id": None,
            "discipline": it.discipline,
            "kind": KIND_OUT["practice"],
            "teachers": "; ".join(d.teachers.get(t, t) for t in it.teachers),
            "groups": "; ".join(it.groups) if not it.block else "",
            "subgroups": "",
            "pairs": it.pairs_per_cycle,
            "format": FORMAT_OUT["online" if it.online else "in_person"],
            "room_type": TYPE_NAMES.get(it.room_type or "", "") if it.room_type else "",
            "headcount": it.headcount,
            "week_pattern": WEEK_OUT[it.week_pattern],
            "teacher_in_room": "да" if it.teacher_in_room else "нет",
            "end_early": "да" if it.may_end_early else "нет",
            "planning": PLANNING_OUT[it.planning],
            "pins": "; ".join(it.pins),
            "double": "нет",
            "priority": 0,
            "block": it.block,
            "merge": "",
            "label": it.label,
            "comment": it.comment,
        }
        ws.append([row[k] for k in keys])
    finish_sheet(ws)
    wsb = wb.create_sheet("Блоки")
    write_header(wsb, ["Синхронный блок", "Группы"], [60, 40])
    for name, groups in sorted(blocks.items()):
        wsb.append([name, "; ".join(groups)])
    finish_sheet(wsb)
    return _save(wb)


def report_xlsx(d: Draft, items: list[DraftItem]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Что проверить"
    write_header(ws, ["Лист", "Ячейка", "Что в ячейке", "Что не так"], [18, 8, 70, 70])
    for i in d.issues:
        ws.append([i.sheet, i.cell, i.text, i.problem])
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    finish_sheet(ws)
    ws = wb.create_sheet("Итог")
    write_header(ws, ["Показатель", "Значение"], [50, 20])
    rows = [
        ("Аудиторий", len(d.rooms)),
        ("Групп", len(d.groups)),
        ("Преподавателей", len(d.teachers)),
        ("Распознано пар в расписании", len(d.entries)),
        ("Элементов нагрузки в черновике", len(items)),
        ("…из них «через неделю» (все пары в одну неделю)", sum(i.week_pattern == "alt_week" for i in items)),
        ("…из них в синхронных блоках", sum(bool(i.block) for i in items)),
        ("…без распознанного преподавателя", sum(not i.teachers for i in items)),
        ("…ставятся вручную (с закреплёнными парами)", sum(i.planning == "manual" for i in items)),
        ("Сомнительных мест (лист «Что проверить»)", len(d.issues)),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append([])
    ws.append(
        [
            "Порядок загрузки",
            "1) создайте семестр; 2) загрузите справочники (references_draft.xlsx); "
            "3) загрузите нагрузку (workload_draft.xlsx) сначала с «только проверить»",
        ]
    )
    ws.append(
        [
            "Допущения",
            f"численность групп — {DEFAULT_GROUP_SIZE}, вместимость аудиторий без данных — "
            f"{DEFAULT_CAPACITY}; «{SCHOOL_ROOM}» — не используется (школа)",
        ]
    )
    return _save(wb)


def draft_zip(d: Draft, items: list[DraftItem], blocks: dict[str, list[str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("references_draft.xlsx", references_xlsx(d, items))
        z.writestr("workload_draft.xlsx", workload_xlsx(d, items, blocks))
        z.writestr("report.xlsx", report_xlsx(d, items))
    return buf.getvalue()
