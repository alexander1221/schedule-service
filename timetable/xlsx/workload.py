"""Импорт/экспорт нагрузки семестра в xlsx.

ВРЕМЕННЫЙ ШАБЛОН: колонки придуманы до получения образца таблицы учебного отдела
(docs/XLSX_FORMAT.md). Заголовки сопоставляются по названию, порядок колонок
неважен, поэтому подстройка под реальный формат — это правка словаря COLUMNS.

Правила импорта:
* всё или ничего: при любой ошибке не записывается ни одна строка;
* «пробный прогон» проверяет файл и показывает, что изменится, ничего не записывая;
* строка с ID обновляет существующий элемент, без ID — создаёт новый;
* режим «replace» дополнительно удаляет элементы семестра, которых нет в файле;
* дисциплины, преподаватели, группы должны уже быть в справочниках (иначе ошибка
  с подсказкой похожего названия); блоки и объединения создаются по названию.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any, BinaryIO

from django.core.exceptions import ValidationError
from django.db import transaction
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

from scheduler.domain import DAY_NAMES

from .. import models as m
from .common import (
    ImportReport,
    RowError,
    add_list_validation,
    cell_str,
    data_rows,
    finish_sheet,
    header_map,
    norm,
    parse_bool,
    parse_int,
    split_list,
    suggest,
    write_header,
)

SHEET = "Нагрузка"
BLOCK_SHEET = "Блоки"

# ключ → (заголовок, ширина, пояснение для листа «Справка»)
COLUMNS: dict[str, tuple[str, int, str]] = {
    "id": ("ID", 7, "Номер элемента в системе. Пусто — новая строка. Не меняйте у существующих строк."),
    "discipline": ("Дисциплина", 30, "Название из справочника дисциплин."),
    "kind": ("Вид", 11, "лекция / практика / лаба."),
    "teachers": ("Преподаватели", 28, "ФИО или почта; несколько — через «;»."),
    "groups": ("Группы", 22, "Названия групп через «;». Несколько групп — поток."),
    "subgroups": ("Подгруппы", 22, "«Группа: подгруппа», например «ИС-11: п/г 1»; несколько — через «;»."),
    "pairs": ("Пар за 2 недели", 10, "Целое 1–28. 3 = одна пара каждую неделю + одна мигающая."),
    "format": ("Формат", 9, "очно / онлайн."),
    "room_type": ("Тип аудитории", 18, "Обязателен для очных: название или код типа."),
    "headcount": ("Численность", 11, "Только если считать по группам нельзя (языковые подгруппы блока)."),
    "week_pattern": (
        "Схема недель",
        14,
        "обычная / через неделю. «Через неделю» — все пары в одну неделю из двух, "
        "по две подряд (кафедра кино: 1 пара в неделю → 2 пары раз в две недели).",
    ),
    "teacher_in_room": (
        "Бронь аудитории",
        10,
        "да / нет — для онлайн: студенты дома, а преподаватель ведёт с "
        "площадки, ему бронируется аудитория (нужен тип аудитории).",
    ),
    "planning": (
        "Кто ставит",
        11,
        "решатель / вручную. «Вручную» — элективы и кружки после старта семестра: "
        "решатель их учтёт, только когда все пары закреплены.",
    ),
    "end_early": (
        "Можно раньше",
        10,
        "да / нет — занятие можно закончить раньше, к закрытию площадки (киноклуб): ему разрешена "
        "пара, которая заканчивается после закрытия (на ВДНХ — 7-я, до 21:00 вместо 21:30).",
    ),
    "pins": (
        "Закрепления",
        26,
        "Пары, закреплённые за элементом, через «;»: «Пт 6», с неделей «Пт 6 нечёт», с аудиторией "
        "«Пт 6 (Ц1 301)». Пусто — закреплений нет; колонки нет в файле — закрепления не трогаем.",
    ),
    "double": ("Сдвоенная", 10, "да / нет — еженедельные пары подряд в один день."),
    "priority": ("Приоритет", 9, "Целое, по умолчанию 0."),
    "block": ("Синхронный блок", 22, "Название блока (группы блока — на листе «Блоки»)."),
    "merge": ("Объединение", 18, "Название допустимого объединения (одинаковое у объединяемых строк)."),
    "label": ("Подпись", 10, "Короткая подпись, например уровень «B1»."),
    "comment": ("Комментарий", 30, "Любой текст."),
}
REQUIRED = ["discipline", "teachers", "pairs"]

KIND_IN = {
    "лекция": "lecture",
    "лек": "lecture",
    "лекции": "lecture",
    "практика": "practice",
    "пр": "practice",
    "практ": "practice",
    "семинар": "practice",
    "лаба": "lab",
    "лаб": "lab",
    "лабораторная": "lab",
    "лабораторная работа": "lab",
    "lecture": "lecture",
    "practice": "practice",
    "lab": "lab",
}
KIND_OUT = {"lecture": "лекция", "practice": "практика", "lab": "лаба"}
FORMAT_IN = {
    "очно": "in_person",
    "очная": "in_person",
    "онлайн": "online",
    "дистанционно": "online",
    "дистант": "online",
    "in_person": "in_person",
    "online": "online",
}
FORMAT_OUT = {"in_person": "очно", "online": "онлайн"}
WEEK_IN = {
    "обычная": "auto",
    "каждую неделю": "auto",
    "auto": "auto",
    "через неделю": "alt_week",
    "раз в две недели": "alt_week",
    "alt_week": "alt_week",
}
WEEK_OUT = {"auto": "обычная", "alt_week": "через неделю"}
PLANNING_IN = {"решатель": "auto", "авто": "auto", "auto": "auto", "вручную": "manual", "manual": "manual"}
PLANNING_OUT = {"auto": "решатель", "manual": "вручную"}


PIN_WEEK_IN = {"": "both", "чёт": "even", "чет": "even", "нечёт": "odd", "нечет": "odd"}
PIN_WEEK_OUT = {"both": "", "even": " чёт", "odd": " нечёт"}
_DAY_IN = {v.lower(): k for k, v in DAY_NAMES.items()}
_PIN_RE = re.compile(r"^(пн|вт|ср|чт|пт|сб)\.?\s*(\d{1,2})\s*(нечёт|нечет|чёт|чет)?\s*(?:\((.+)\))?$", re.I)


def format_pins(pins: list[m.Pin]) -> str:
    out = []
    for p in sorted(pins, key=lambda x: (x.day, x.pair, x.week)):
        room = f" ({p.room.name})" if p.room_id and p.room else ""
        out.append(f"{DAY_NAMES[p.day]} {p.pair}{PIN_WEEK_OUT[p.week]}{room}")
    return "; ".join(out)


def parse_pins(value: Any, rooms: dict[str, m.Room]) -> list[tuple[str, int, int, m.Room | None]]:
    """«Пт 6 нечёт (Ц1 301); Пт 7» → [(неделя, день, пара, аудитория)]; ValueError с понятным текстом."""
    out = []
    for part in split_list(value):
        mt = _PIN_RE.match(part.strip())
        if not mt:
            raise ValueError(f"«{part}» — ожидается «Пт 6», «Пт 6 нечёт» или «Пт 6 (Ц1 301)»")
        day, pair, wk, room_name = mt.groups()
        room = None
        if room_name:
            room = rooms.get(norm(room_name))
            if room is None:
                raise ValueError(
                    f"аудитории «{room_name}» нет в справочнике." + suggest(room_name, [r.name for r in rooms.values()])
                )
        out.append((PIN_WEEK_IN[(wk or "").lower()], _DAY_IN[day.lower()], int(pair), room))
    return out


def _known_headers() -> dict[str, str]:
    known = {norm(title): key for key, (title, _, _) in COLUMNS.items()}
    known.update({norm("Пар за цикл"): "pairs", norm("Преподаватель"): "teachers", norm("Группа"): "groups"})
    return known


# ====================================================================== экспорт


def export_workload(sem: m.Semester) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET
    keys = list(COLUMNS)
    write_header(ws, [COLUMNS[k][0] for k in keys], [COLUMNS[k][1] for k in keys])

    teacher_label = _teacher_labels()
    items = (
        m.WorkloadItem.objects.filter(semester=sem)
        .select_related("discipline", "room_type", "sync_block", "merge_group")
        .prefetch_related("teachers", "groups", "subgroups__group", "pins__room")
        .order_by("discipline__name", "id")
    )
    for it in items:
        row = {
            "id": it.pk,
            "discipline": it.discipline.name,
            "kind": KIND_OUT[it.kind],
            "teachers": "; ".join(teacher_label[t.pk] for t in it.teachers.all()),
            "groups": "; ".join(sorted(g.name for g in it.groups.all())),
            "subgroups": "; ".join(_subgroup_label(s) for s in it.subgroups.all()),
            "pairs": it.pairs_per_cycle,
            "format": FORMAT_OUT[it.format],
            "room_type": it.room_type.name if it.room_type else "",
            "headcount": it.headcount,
            "week_pattern": WEEK_OUT[it.week_pattern],
            "teacher_in_room": "да" if it.teacher_in_room else "нет",
            "planning": PLANNING_OUT[it.planning],
            "end_early": "да" if it.may_end_early else "нет",
            "pins": format_pins(list(it.pins.all())),
            "double": "да" if it.double else "нет",
            "priority": it.priority,
            "block": it.sync_block.name if it.sync_block else "",
            "merge": it.merge_group.name if it.merge_group else "",
            "label": it.label,
            "comment": it.comment,
        }
        ws.append([row[k] for k in keys])
    finish_sheet(ws)

    # лист «Блоки»: какие группы распускаются в каждом блоке
    wsb = wb.create_sheet(BLOCK_SHEET)
    write_header(wsb, ["Синхронный блок", "Группы"], [40, 40])
    for b in sem.blocks.prefetch_related("groups"):
        wsb.append([b.name, "; ".join(sorted(g.name for g in b.groups.all()))])
    finish_sheet(wsb)

    # скрытый лист со списками для выпадающих меню
    wl = wb.create_sheet("Списки")
    lists = {
        "A": ["Вид", *KIND_OUT.values()],
        "B": ["Формат", *FORMAT_OUT.values()],
        "C": ["Тип аудитории", *m.RoomType.objects.values_list("name", flat=True)],
        "D": ["Да/нет", "да", "нет"],
        "F": ["Схема недель", *WEEK_OUT.values()],
        "G": ["Кто ставит", *PLANNING_OUT.values()],
        "E": ["Дисциплина", *m.Discipline.objects.values_list("name", flat=True)],
    }
    for col, values in lists.items():
        for i, v in enumerate(values, start=1):
            wl[f"{col}{i}"] = v
    wl.sheet_state = "hidden"
    idx = {k: i for i, k in enumerate(keys, start=1)}
    for key, col in (
        ("kind", "A"),
        ("format", "B"),
        ("room_type", "C"),
        ("double", "D"),
        ("discipline", "E"),
        ("teacher_in_room", "D"),
        ("week_pattern", "F"),
        ("planning", "G"),
    ):
        add_list_validation(ws, idx[key], f"=Списки!${col}$2:${col}${max(len(lists[col]), 2)}")

    # справка
    wh = wb.create_sheet("Справка")
    write_header(wh, ["Колонка", "Что писать"], [22, 100])
    for title, _, help_text in COLUMNS.values():
        wh.append([title, help_text])
    wh.append([])
    wh.append(
        [
            "Правила",
            "Порядок колонок можно менять. Пустые строки пропускаются. При любой ошибке файл не "
            "применяется целиком — исправьте отмеченные строки и загрузите снова.",
        ]
    )
    for row in wh.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _teacher_labels() -> dict[int, str]:
    """ФИО; при совпадении ФИО у двух преподавателей — почта (однозначный ключ для импорта)."""
    teachers = list(m.Teacher.objects.all())
    counts: dict[str, int] = {}
    for t in teachers:
        counts[norm(t.name)] = counts.get(norm(t.name), 0) + 1
    return {t.pk: (t.email if counts[norm(t.name)] > 1 and t.email else t.name) for t in teachers}


def _subgroup_label(s: m.Subgroup) -> str:
    dup = m.Subgroup.objects.filter(group_id=s.group_id, name=s.name).count() > 1
    return f"{s.group.name}: {s.partition}: {s.name}" if dup else f"{s.group.name}: {s.name}"


# ====================================================================== импорт


@dataclass
class _Lookup:
    """Справочники в памяти: один запрос на таблицу вместо запроса на ячейку."""

    disciplines: dict[str, m.Discipline]
    teachers_by_name: dict[str, list[m.Teacher]]
    teachers_by_email: dict[str, m.Teacher]
    groups: dict[str, m.Group]
    subgroups: dict[tuple[str, str], list[m.Subgroup]]
    room_types: dict[str, m.RoomType]
    rooms: dict[str, m.Room]

    @classmethod
    def load(cls, sem: m.Semester) -> _Lookup:
        tbn: dict[str, list[m.Teacher]] = {}
        for t in m.Teacher.objects.all():
            tbn.setdefault(norm(t.name), []).append(t)
        sgs: dict[tuple[str, str], list[m.Subgroup]] = {}
        for s in m.Subgroup.objects.filter(group__course__semester=sem).select_related("group"):
            sgs.setdefault((norm(s.group.name), norm(s.name)), []).append(s)
        rts: dict[str, m.RoomType] = {}
        for rt in m.RoomType.objects.all():
            rts[norm(rt.name)] = rt
            rts[norm(rt.code)] = rt
        return cls(
            disciplines={norm(x.name): x for x in m.Discipline.objects.all()},
            teachers_by_name=tbn,
            teachers_by_email={t.email.lower(): t for t in m.Teacher.objects.exclude(email=None)},
            groups={norm(g.name): g for g in m.Group.objects.filter(course__semester=sem)},
            subgroups=sgs,
            room_types=rts,
            rooms={norm(r.name): r for r in m.Room.objects.all()},
        )


def import_workload(
    sem: m.Semester, file: BinaryIO | bytes, *, dry_run: bool = True, replace: bool = False
) -> ImportReport:
    rep = ImportReport()
    try:
        wb = load_workbook(io.BytesIO(file) if isinstance(file, bytes) else file, data_only=True, read_only=False)
    except Exception as e:  # битый файл, не xlsx
        rep.errors.append(RowError("", 0, "", f"не удалось открыть файл как xlsx: {e}"))
        return rep
    if SHEET not in wb.sheetnames:
        rep.errors.append(RowError(SHEET, 0, "", f"нет листа «{SHEET}» (есть: {', '.join(wb.sheetnames)})"))
        return rep
    ws = wb[SHEET]
    cols, unknown = header_map(ws, _known_headers())
    if unknown:
        rep.warnings.append("Колонки не распознаны и пропущены: " + ", ".join(unknown))
    missing = [COLUMNS[k][0] for k in REQUIRED if k not in cols]
    if missing:
        rep.errors.append(RowError(SHEET, 1, "", "нет обязательных колонок: " + ", ".join(missing)))
        return rep

    lk = _Lookup.load(sem)
    with transaction.atomic():
        blocks = _import_blocks(sem, wb, lk, rep)
        merges: dict[str, m.MergeGroup] = {}
        seen_ids: set[int] = set()
        existing = {it.pk: it for it in m.WorkloadItem.objects.filter(semester=sem)}
        for rown, row in data_rows(ws):
            _import_row(sem, rown, row, cols, lk, blocks, merges, existing, seen_ids, rep)
        if replace and not rep.errors:
            to_delete = [pk for pk in existing if pk not in seen_ids]
            if to_delete:
                m.WorkloadItem.objects.filter(pk__in=to_delete).delete()
                rep.bump("элементы нагрузки", "deleted", len(to_delete))
        if rep.errors or dry_run:
            transaction.set_rollback(True)
        else:
            rep.applied = True
    return rep


def _import_blocks(sem: m.Semester, wb: Any, lk: _Lookup, rep: ImportReport) -> dict[str, m.SyncBlock]:
    blocks = {norm(b.name): b for b in sem.blocks.all()}
    if BLOCK_SHEET not in wb.sheetnames:
        return blocks
    ws = wb[BLOCK_SHEET]
    for rown, row in data_rows(ws):
        name = cell_str(row[0] if row else "")
        if not name:
            rep.errors.append(RowError(BLOCK_SHEET, rown, "Синхронный блок", "пустое название"))
            continue
        gnames = split_list(row[1] if len(row) > 1 else "")
        groups = []
        for gn in gnames:
            g = lk.groups.get(norm(gn))
            if g is None:
                rep.errors.append(
                    RowError(
                        BLOCK_SHEET,
                        rown,
                        "Группы",
                        f"группа «{gn}» не найдена в семестре." + suggest(gn, _names(lk.groups)),
                    )
                )
            else:
                groups.append(g)
        blk = blocks.get(norm(name))
        if blk is None:
            blk = m.SyncBlock.objects.create(semester=sem, name=name)
            blocks[norm(name)] = blk
            rep.bump("синхронные блоки")
        elif set(blk.groups.values_list("pk", flat=True)) != {g.pk for g in groups}:
            rep.bump("синхронные блоки", "updated")
        blk.groups.set(groups)
    return blocks


def _names(d: dict[str, Any]) -> list[str]:
    return [getattr(v, "name", str(v)) for v in d.values()]


def _import_row(
    sem: m.Semester,
    rown: int,
    row: tuple[Any, ...],
    cols: dict[str, int],
    lk: _Lookup,
    blocks: dict[str, m.SyncBlock],
    merges: dict[str, m.MergeGroup],
    existing: dict[int, m.WorkloadItem],
    seen_ids: set[int],
    rep: ImportReport,
) -> None:
    errs: list[RowError] = []

    def get(key: str) -> Any:
        i = cols.get(key)
        return row[i - 1] if i and i - 1 < len(row) else None

    def err(key: str, msg: str) -> None:
        errs.append(RowError(SHEET, rown, COLUMNS[key][0], msg))

    def safe(key: str, fn: Any, *a: Any, **kw: Any) -> Any:
        try:
            return fn(get(key), *a, **kw)
        except ValueError as e:
            err(key, str(e))
            return None

    # --- ID
    item: m.WorkloadItem | None = None
    item_id = safe("id", parse_int, lo=1) if "id" in cols else None
    if item_id is not None:
        item = existing.get(item_id)
        if item is None:
            err("id", f"элемента с ID {item_id} нет в этом семестре; для новой строки оставьте ID пустым")
        elif item_id in seen_ids:
            err("id", f"ID {item_id} встречается в файле дважды")
        else:
            seen_ids.add(item_id)

    # --- дисциплина
    dname = cell_str(get("discipline"))
    discipline = lk.disciplines.get(norm(dname))
    if not dname:
        err("discipline", "обязательное поле пусто")
    elif discipline is None:
        err("discipline", f"«{dname}» нет в справочнике дисциплин." + suggest(dname, _names(lk.disciplines)))

    kind = "practice"
    if cell_str(get("kind")):
        kind = KIND_IN.get(norm(get("kind")), "")
        if not kind:
            err("kind", f"«{cell_str(get('kind'))}» — ожидается лекция, практика или лаба")
    fmt = "in_person"
    if cell_str(get("format")):
        fmt = FORMAT_IN.get(norm(get("format")), "")
        if not fmt:
            err("format", f"«{cell_str(get('format'))}» — ожидается очно или онлайн")

    # --- преподаватели
    teachers: list[m.Teacher] = []
    tnames = split_list(get("teachers"))
    if not tnames:
        err("teachers", "обязательное поле пусто")
    for tn in tnames:
        if "@" in tn:
            t = lk.teachers_by_email.get(tn.lower())
            if t is None:
                err("teachers", f"преподавателя с почтой {tn} нет в справочнике")
            else:
                teachers.append(t)
            continue
        found = lk.teachers_by_name.get(norm(tn), [])
        if not found:
            err(
                "teachers",
                f"«{tn}» нет в справочнике преподавателей."
                + suggest(tn, [ts[0].name for ts in lk.teachers_by_name.values()]),
            )
        elif len(found) > 1:
            err("teachers", f"«{tn}» — несколько преподавателей с таким ФИО; укажите почту")
        else:
            teachers.append(found[0])

    # --- участники
    groups = []
    for gn in split_list(get("groups")):
        g = lk.groups.get(norm(gn))
        if g is None:
            err("groups", f"группа «{gn}» не найдена в семестре." + suggest(gn, _names(lk.groups)))
        else:
            groups.append(g)
    subgroups = []
    for sn in split_list(get("subgroups")):
        parts = [p.strip() for p in sn.split(":")]
        if len(parts) not in (2, 3):
            err("subgroups", f"«{sn}» — ожидается «Группа: подгруппа»")
            continue
        cands = lk.subgroups.get((norm(parts[0]), norm(parts[-1])), [])
        if len(parts) == 3:
            cands = [s for s in cands if norm(s.partition) == norm(parts[1])]
        if not cands:
            err("subgroups", f"подгруппа «{sn}» не найдена")
        elif len(cands) > 1:
            err("subgroups", f"«{sn}» неоднозначна: укажите деление — «Группа: деление: подгруппа»")
        else:
            subgroups.append(cands[0])

    pairs = safe("pairs", parse_int, lo=1, hi=28, required=True)
    rt_name = cell_str(get("room_type"))
    room_type = lk.room_types.get(norm(rt_name)) if rt_name else None
    if rt_name and room_type is None:
        err("room_type", f"тип «{rt_name}» не найден." + suggest(rt_name, [r.name for r in lk.room_types.values()]))
    headcount = safe("headcount", parse_int, lo=1, hi=1000)
    double = safe("double", parse_bool) if "double" in cols else False
    # новые колонки (0.3.0): если колонки в файле нет (старый шаблон) — значение элемента не трогаем
    extra: dict[str, Any] = {}
    if "week_pattern" in cols and cell_str(get("week_pattern")):
        extra["week_pattern"] = WEEK_IN.get(norm(get("week_pattern")), "")
        if not extra["week_pattern"]:
            err("week_pattern", f"«{cell_str(get('week_pattern'))}» — ожидается «обычная» или «через неделю»")
    if "teacher_in_room" in cols:
        extra["teacher_in_room"] = bool(safe("teacher_in_room", parse_bool))
    if "end_early" in cols:
        extra["may_end_early"] = bool(safe("end_early", parse_bool))
    if "planning" in cols and cell_str(get("planning")):
        extra["planning"] = PLANNING_IN.get(norm(get("planning")), "")
        if not extra["planning"]:
            err("planning", f"«{cell_str(get('planning'))}» — ожидается «решатель» или «вручную»")
    priority = safe("priority", parse_int, lo=-100, hi=100) if "priority" in cols else 0
    pins = safe("pins", parse_pins, lk.rooms) if "pins" in cols else None

    block = None
    bname = cell_str(get("block"))
    if bname:
        block = blocks.get(norm(bname))
        if block is None:
            block = m.SyncBlock.objects.create(semester=sem, name=bname)
            blocks[norm(bname)] = block
            rep.bump("синхронные блоки")
            rep.warnings.append(f"Блок «{bname}» создан без групп — укажите их на листе «{BLOCK_SHEET}» или в админке")
    merge = None
    mname = cell_str(get("merge"))
    if mname:
        merge = merges.get(norm(mname)) or sem.merges.filter(name=mname).first()
        if merge is None:
            merge = m.MergeGroup.objects.create(semester=sem, name=mname)
            rep.bump("объединения")
        merges[norm(mname)] = merge

    if errs:
        rep.errors.extend(errs)
        return
    assert discipline is not None and pairs is not None

    values = dict(
        discipline=discipline,
        kind=kind,
        pairs_per_cycle=pairs,
        format=fmt,
        room_type=room_type,
        headcount=headcount,
        double=bool(double),
        priority=priority or 0,
        sync_block=block,
        merge_group=merge,
        label=cell_str(get("label"))[:30],
        comment=cell_str(get("comment"))[:300],
        **extra,
    )
    creating = item is None
    if creating:
        item = m.WorkloadItem(semester=sem, **values)
    else:
        assert item is not None
        before = _snapshot(item)
        for k, v in values.items():
            setattr(item, k, v)
    try:
        item.full_clean(exclude=["semester"])
    except ValidationError as e:
        for field_name, msgs in e.message_dict.items():
            col = next(
                (
                    COLUMNS[k][0]
                    for k, f in (
                        ("room_type", "room_type"),
                        ("merge", "merge_group"),
                        ("block", "sync_block"),
                        ("teacher_in_room", "teacher_in_room"),
                        ("pairs", "pairs_per_cycle"),
                    )
                    if f == field_name
                ),
                "",
            )
            for msg in msgs:
                rep.errors.append(RowError(SHEET, rown, col, msg))
        return
    item.save()
    item.teachers.set(teachers)
    item.groups.set(groups)
    item.subgroups.set(subgroups)
    for problem in item.clean_relations():
        rep.errors.append(RowError(SHEET, rown, "", problem))
    pins_changed = False
    if pins is not None:
        old = sorted((p.week, p.day, p.pair, p.room_id) for p in item.pins.all())
        new = sorted({(w, d, n, r.pk if r else None) for w, d, n, r in pins})
        if old != new:
            pins_changed = True
            item.pins.all().delete()
            m.Pin.objects.bulk_create([m.Pin(item=item, week=w, day=d, pair=n, room_id=r) for w, d, n, r in new])
            rep.bump("закрепления", "created", len(new))
    if creating:
        rep.bump("элементы нагрузки")
    elif _snapshot(item) != before or pins_changed:
        rep.bump("элементы нагрузки", "updated")


def _snapshot(it: m.WorkloadItem) -> tuple[Any, ...]:
    if not it.pk:
        return ()
    return (
        it.discipline_id,
        it.kind,
        it.pairs_per_cycle,
        it.format,
        it.room_type_id,
        it.headcount,
        it.double,
        it.week_pattern,
        it.teacher_in_room,
        it.planning,
        it.may_end_early,
        it.priority,
        it.sync_block_id,
        it.merge_group_id,
        it.label,
        it.comment,
        tuple(sorted(it.teachers.values_list("pk", flat=True))),
        tuple(sorted(it.groups.values_list("pk", flat=True))),
        tuple(sorted(it.subgroups.values_list("pk", flat=True))),
    )
