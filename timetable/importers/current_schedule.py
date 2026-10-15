"""Черновик справочников и нагрузки из текущего расписания учебного отдела (xlsx).

Учебный отдел ведёт расписание в Excel в нескольких разрезах: листы групп (ПиУП: ячейка
«дисциплина / преподаватель / группы / аудитория», кино: «дисциплина  Преподаватель» и
аудитория в соседней колонке), лист аудиторий со свойствами («компы», «лекционка», число
машин). Таблицы нагрузки как таковой нет — её можно восстановить: сколько раз в неделю
стоит пара «дисциплина + преподаватель + группы».

Данные живые и неоднородные (одна аудитория пишется по-разному, преподаватель слит с
дисциплиной, чётность — словами), поэтому результат — ЧЕРНОВИК в форматах наших шаблонов
импорта плюс список сомнительных ячеек. Диспетчер проверяет его в Excel и загружает
обычным импортом (сначала справочники, потом нагрузку). Ничего не пишется в БД сразу.

Модуль не зависит от Django: на входе путь к xlsx, на выходе ``Draft``.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from scheduler.domain import DAY_NAMES

DAYS = {"пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6}
NBSP = " "

#: Аудитория школы — по указанию учебного отдела в расчёт не брать (все написания).
SCHOOL_ROOM = "Ц2 1 этаж новая"
SCHOOL_ALIASES = re.compile(r"^ц2\s*(1\s*(э|эт\.?|этаж)\.?\s*(новая)?|эт\.?\s*новая)$", re.I)

PROPERTY_EQUIPMENT = {
    "компы": ["pc"],
    "лекционка": ["projector"],
}
EQUIPMENT_NAMES = {"pc": "Компьютеры", "projector": "Проектор", "java_sw": "ПО: Java"}
PROPERTY_TYPE = {"компы": ("computer", "Компьютерный класс"), "лекционка": ("lecture", "Лекционная")}
DEFAULT_CAPACITY = 25  # вместимости в файле нет — значение по умолчанию, помечается «уточнить»
DEFAULT_GROUP_SIZE = 25


@dataclass
class Issue:
    sheet: str
    cell: str
    text: str
    problem: str


@dataclass
class Entry:
    """Одна распознанная пара в листе группы."""

    sheet: str
    cell: str
    group: str  # группа строки листа
    day: int
    pair: int
    discipline: str
    teachers: list[str]
    groups: list[str]  # участники (поток — несколько)
    room: str | None
    online: bool
    parity: str | None  # None — каждую неделю; "odd"/"even"/"blink" — через неделю (неделя неизвестна → blink)
    block_teachers: list[tuple[str, str | None, bool]] = field(default_factory=list)  # английский: (преп, ауд, онлайн)


@dataclass
class Draft:
    rooms: dict[str, dict[str, Any]] = field(default_factory=dict)  # каноническое имя -> свойства
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    teachers: dict[str, str] = field(default_factory=dict)  # ключ (фамилия) -> полное имя
    entries: list[Entry] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


# ====================================================================== нормализация


def clean(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace(NBSP, " ").strip()


def canon_room(text: str, known: dict[str, dict[str, Any]] | None = None) -> str | None:
    """«арт 212» → «АРТ 212», «Ц1.206.1» → «Ц1 206.1», «ц1 холл» → «Ц1 ХОЛЛ», школа → SCHOOL_ROOM."""
    t = re.sub(r"\s+", " ", clean(text)).strip(" .")
    if not t:
        return None
    t = re.split(r"/студия", t, flags=re.I)[0].strip()
    if SCHOOL_ALIASES.match(t):
        return SCHOOL_ROOM
    m = re.match(r"^(ц\s*[12]|арт)[\s.]*(.+)$", t, re.I)
    if m:
        building = m.group(1).upper().replace(" ", "")
        rest = m.group(2).strip().upper() if m.group(2).strip().lower() == "холл" else m.group(2).strip()
        t = f"{building} {rest}"
    elif known and re.match(r"^\d+(\.\d+)?$", t):
        # номер без корпуса: если в справочнике ровно одна такая аудитория — берём её
        same = [k for k in known if k.split(" ", 1)[-1] == t]
        if len(same) == 1:
            return same[0]
    return t


def short_group(text: str) -> str:
    """«3П1.24 (Java)» → «3П1», «2КП1.25» → «2КП1», «1ИТ1» → «1ИТ1»."""
    t = clean(text).split("\n")[0].strip()
    return re.split(r"[.\s(]", t, maxsplit=1)[0].upper()


TEACHER_LINE = re.compile(r"^([А-ЯЁ][а-яё\-]+)(?:\s+[А-ЯЁ][а-яё]*\.?)*(?:\s*\(([^)]*)\))?$")


def split_groups(text: str) -> list[str]:
    text = re.sub(r"(?i)\s*(мигающ\w*|(не)?ч[её]тн\w*|онлайн)\s*", " ", clean(text)).strip()
    parts = re.split(r"\s*\+\s*|\s*,\s*", text)
    return [short_group(p) for p in parts if re.match(r"^\d\S*", p.strip())]


def is_group_list(text: str) -> bool:
    t = re.sub(r"(?i)\s*(мигающ\w*|(не)?ч[её]тн\w*|онлайн)\s*", " ", clean(text)).strip().upper()
    return bool(re.match(r"^\d[А-ЯA-Z]+\d+(\.\d+)?(\s*\+\s*\d[А-ЯA-Z]+\d+(\.\d+)?)*$", t))


def parity_of(text: str) -> tuple[str | None, str]:
    low = text.lower()
    if re.search(r"\bнечётн|\bнечетн", low):
        return "odd", re.sub(r"(?i)\s*не[чч][её]тные?", "", text).strip()
    if re.search(r"\bчётн|\bчетн", low):
        return "even", re.sub(r"(?i)\s*ч[её]тные?", "", text).strip()
    if "мигающ" in low:
        return "blink", re.sub(r"(?i)\s*мигающая.*$", "", text).strip()
    return None, text


# ====================================================================== листы


def _header_days(ws: Any) -> dict[int, int]:
    """Номер колонки → день недели по первой строке листа."""
    out = {}
    for cell in next(ws.iter_rows(min_row=1, max_row=1)):
        key = clean(cell.value).lower()[:2]
        if key in DAYS:
            out[cell.column] = DAYS[key]
    return out


def parse_rooms_sheet(ws: Any, draft: Draft) -> None:
    """Лист аудиторий: A — аудитория, B — свойство («компы», «лекционка», «компы (ПО Джава)»),
    дальше — занятость по дням, где-то справа — «26к» (число компьютеров)."""
    for row in ws.iter_rows(min_row=2):
        name = canon_room(clean(row[0].value))
        if not name:
            continue
        prop = clean(row[1].value).lower()
        r = draft.rooms.setdefault(name, {"props": set(), "pcs": None, "sources": set(), "note": ""})
        r["sources"].add(clean(row[0].value))
        if prop:
            base = prop.split("(")[0].strip()
            r["props"].add(base)
            if "java" in prop or "джава" in prop:
                r["props"].add("java")
        for c in row[2:]:
            mm = re.match(r"^(\d+)\s*к$", clean(c.value).lower())
            if mm:
                r["pcs"] = int(mm.group(1))


def _teacher_key(name: str) -> str:
    return clean(name).split(" ")[0].replace("ё", "е").lower()


PARITY_WORDS = re.compile(r"(?i)\b(не)?ч[её]тн\w*|\bмигающ\w*|\bчередующ\w*")
ROOM_RE = re.compile(
    r"(?i)(ц\s*[12]|арт)[\s.]*(холл(\s*2\s*этаж)?|\d+(?:\.\d+)?|1\s*(э|эт\.?|этаж)\.?\s*(новая)?|эт\.?\s*новая)"
)


def _register_teacher(draft: Draft, name: str) -> str:
    """Регистрирует преподавателя, возвращает ключ. Полное ФИО предпочитается фамилии.

    Пустая строка — если это не похоже на ФИО (тогда вызывающий пишет замечание)."""
    name = re.sub(r"\s+", " ", PARITY_WORDS.sub("", re.sub(r"\(.*?\)", "", clean(name)))).strip(" .,-/\\")
    words = name.split()
    if not words or len(words) > 3 or not all(w[0].isupper() for w in words) or re.search(r"[:\d]", name):
        return ""
    if name.isupper():
        name = name.title()
    key = _teacher_key(name)
    if not key or key == "куратор":
        return ""
    cur = draft.teachers.get(key, "")
    if len(name) > len(cur):
        draft.teachers[key] = name
    return key


def parse_teacher_names(ws: Any, draft: Draft) -> None:
    for row in ws.iter_rows(min_row=1, values_only=True):
        v = clean(row[0]) if row else ""
        if v and re.match(r"^[А-ЯЁ][а-яё]+", v) and v.lower() not in ("преподаватель", "преподаватели"):
            _register_teacher(draft, v)


def _split_discipline_teacher(body: str, draft: Draft) -> tuple[str, str]:
    """«Основы режиссуры Громов Павел» → («Основы режиссуры», «Громов Павел»).

    1) явный разделитель — 2+ пробела; 2) первая известная фамилия (из листов преподавателей
    и предварительного прохода); 3) «Фамилия Имя» в конце строки."""
    body = re.sub(r"\s*-\s*(?=[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.)", "  ", body)  # «звук- Тихонов И.»
    if re.search(r"\s{2,}", body):
        d, t = [x.strip() for x in re.split(r"\s{2,}", body, maxsplit=1)]
        return d.strip(" -"), t
    tokens = body.split()
    for i in range(1, len(tokens)):
        if _teacher_key(tokens[i].split("/")[0]) in draft.teachers and tokens[i][0].isupper():
            return " ".join(tokens[:i]).strip(" -"), " ".join(tokens[i:])
    mm = re.match(r"^(.*\S)\s+([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\.?)$", body)
    if mm:
        return mm.group(1).strip(" -"), mm.group(2)
    return body.strip(" -"), ""


def _prepass_teachers(ws: Any, days: dict[int, int], draft: Draft) -> None:
    """Кино: ФИО, отделённые от дисциплины двумя пробелами, — надёжный источник фамилий."""
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if c.column in days:
                text = clean(c.value)
                if re.search(r"\S\s{2,}[А-ЯЁ]", text):
                    tail = re.split(r"\s{2,}", text, maxsplit=1)[1]
                    for t in re.split(r"/+", tail):
                        _register_teacher(draft, t)


def _cell_ref(cell: Any) -> str:
    return f"{cell.column_letter}{cell.row}"


def parse_group_sheet(ws: Any, draft: Draft, department: str) -> None:
    days = _header_days(ws)
    if not days:
        return
    # формат: многострочные ячейки (ПиУП) или «дисциплина Преподаватель» + аудитория справа (кино)
    multiline = sum(1 for r in ws.iter_rows(min_row=2) for c in r if c.column in days and "\n" in clean(c.value))
    single = sum(
        1
        for r in ws.iter_rows(min_row=2)
        for c in r
        if c.column in days and clean(c.value) and "\n" not in clean(c.value)
    )
    kino_style = single > multiline
    if kino_style:
        _prepass_teachers(ws, days, draft)
    for row in ws.iter_rows(min_row=2):
        group_raw = clean(row[0].value)
        pair_raw = clean(row[1].value)
        if not group_raw or not pair_raw.isdigit() or not re.match(r"^\d", group_raw):
            continue
        group = short_group(group_raw)
        draft.groups.setdefault(group, {"course": int(group[0]), "department": department, "sources": set()})
        draft.groups[group]["sources"].add(group_raw)
        for c in row:
            if c.column not in days:
                continue
            text = clean(c.value)
            if not text or text.lower() in ("з",) or "онлайн день" in text.lower():
                continue
            room_cell = clean(ws.cell(row=c.row, column=c.column + 1).value) if kino_style else ""
            for part, parity in _split_alternating(text):
                e = _parse_cell(
                    part, room_cell, group, days[c.column], int(pair_raw), ws.title, _cell_ref(c), draft, kino_style
                )
                if e is not None:
                    if parity:
                        e.parity = parity
                    draft.entries.append(e)


def _split_alternating(text: str) -> list[tuple[str, str | None]]:
    """Две дисциплины в одной клетке через неделю.

    «А … ЧЁТНЫЕ/ Б …» → [(А, even), (Б, odd)]; «А … ЧЕРЕДУЮЩАЯСЯ \\ Б …» → [(А, blink), (Б, blink)]."""
    m = re.match(r"(?is)^(.*?)\s*(не)?ч[её]тные\s*/\s*(.+)$", text)
    if m and m.group(3).strip():
        first = "odd" if m.group(2) else "even"
        second = "even" if first == "odd" else "odd"
        return [(m.group(1).strip(), first), (m.group(3).strip(), second)]
    m = re.match(r"(?is)^(.*?)\s*чередующ\w*\s*[\\/]\s*(.+)$", text)
    if m:
        return [(m.group(1).strip(), "blink"), (m.group(2).strip(), "blink")]
    return [(text, None)]


def _parse_cell(
    text: str, room_cell: str, group: str, day: int, pair: int, sheet: str, ref: str, draft: Draft, kino_style: bool
) -> Entry | None:
    issue = lambda msg: draft.issues.append(Issue(sheet, ref, text, msg))  # noqa: E731
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    online = any("онлайн" in x.lower() for x in lines) or "онлайн" in room_cell.lower()
    parity, _ = parity_of(text)
    # --- английский и прочие «блоки»: строки «Фамилия (аудитория)» + строка групп (её может не быть)
    head = lines[:-1] if lines and is_group_list(lines[-1]) else list(lines)
    disc_line = None
    if head and not TEACHER_LINE.match(head[0]):
        disc_line, head = head[0], head[1:]
    if (
        head
        and all(TEACHER_LINE.match(h) for h in head)
        and (is_group_list(lines[-1]) or len(head) >= 3)
        and (disc_line is None or "язык" in disc_line.lower() or not kino_style)
    ):
        if is_group_list(lines[-1]):
            groups = split_groups(lines[-1])
        else:
            groups = [group]
            issue("у блока не указаны группы — взята только группа строки")
        discipline = disc_line or "Иностранный язык"
        teachers = []
        for h in head:
            mm = TEACHER_LINE.match(h)
            assert mm is not None
            note = (mm.group(2) or "").strip()
            t_online = "онлайн" in note.lower()
            mroom = ROOM_RE.search(note)
            room = canon_room(mroom.group(0), draft.rooms) if mroom and not t_online else None
            key = _register_teacher(draft, h)
            if key:
                teachers.append((key, room, t_online))
        if not teachers:
            issue("не распознаны преподаватели")
            return None
        if len(teachers) == 1:  # один преподаватель на поток — обычный поток, не блок
            t, room, t_online = teachers[0]
            return Entry(sheet, ref, group, day, pair, discipline, [t], groups, room, t_online or online, parity)
        return Entry(
            sheet,
            ref,
            group,
            day,
            pair,
            discipline,
            [t for t, _, _ in teachers],
            groups,
            None,
            False,
            parity,
            block_teachers=teachers,
        )
    if not kino_style:
        # ПиУП: первая строка — дисциплина; дальше в любом порядке: преподаватель(и), группы,
        # аудитория, «ОНЛАЙН», «МИГАЮЩАЯ»
        if len(lines) < 3:
            issue("ожидалось «дисциплина / преподаватель / группы / аудитория»")
            return None
        discipline = re.sub(r"\s*/\s*", " / ", lines[0])
        groups: list[str] = []
        teachers = []
        room = None
        for x in lines[1:]:
            if is_group_list(x):
                groups = split_groups(x)
                continue
            mroom = ROOM_RE.search(x)
            if mroom:
                room = canon_room(mroom.group(0), draft.rooms)
                x = (x[: mroom.start()] + x[mroom.end() :]).strip()
            x = re.sub(r"(?i)онлайн|мигающ\w*|\(.*?\)", "", x).strip(" /")
            for t in re.split(r"[/,]", x):
                if t.strip():
                    key = _register_teacher(draft, t)
                    if key:
                        teachers.append(key)
                    elif t.strip().lower() != "куратор":
                        issue(f"«{t.strip()}» не похоже на ФИО преподавателя")
        if not groups:
            issue("не найдена строка групп — взята группа строки")
            groups = [group]
        if not teachers:
            issue("не указан преподаватель")
        if room is None and not online:
            issue("не указана аудитория")
        return Entry(sheet, ref, group, day, pair, discipline, teachers, groups, room, online, parity)
    # кино: «Дисциплина   Фамилия Имя» (иногда без преподавателя), аудитория — в соседней колонке
    body = lines[0] if len(lines) == 1 else " ".join(lines)
    _, body = parity_of(body)
    discipline, teacher_part = _split_discipline_teacher(body, draft)
    teachers = []
    for t in re.split(r"/+", teacher_part):
        if t.strip():
            key = _register_teacher(draft, t)
            if key:
                teachers.append(key)
            else:
                issue(f"«{t.strip()}» не похоже на ФИО преподавателя")
    if not teachers:
        issue("преподаватель не указан или не отделён от дисциплины — допишите в черновике нагрузки")
    room_raw = room_cell
    room = None
    if room_raw and "онлайн" not in room_raw.lower():
        if "/" in room_raw:
            issue(f"несколько аудиторий «{room_raw}» — взята первая")
            room_raw = room_raw.split("/")[0]
        room = canon_room(room_raw, draft.rooms)
    elif not room_raw and not online:
        issue("не указана аудитория")
    if room and room not in draft.rooms:
        issue(f"аудитории «{room}» нет на листе аудиторий")
    return Entry(sheet, ref, group, day, pair, discipline, teachers, [group], room, online, parity)


# ====================================================================== всё вместе


def parse_workbook(
    path: str | Path,
    kino_sheets: list[str] | None = None,
    piup_sheets: list[str] | None = None,
    room_sheet: str | None = None,
    teacher_sheets: list[str] | None = None,
) -> Draft:
    wb = load_workbook(path, data_only=True)
    names = wb.sheetnames
    visible = [n for n in names if wb[n].sheet_state == "visible"]
    room_sheet = room_sheet or next((n for n in visible if "аудитор" in n.lower()), None)
    kino_sheets = (
        kino_sheets
        if kino_sheets is not None
        else [n for n in visible if "кино" in n.lower() and "расписан" in n.lower()]
    )
    piup_sheets = piup_sheets if piup_sheets is not None else [n for n in visible if n.lower().startswith("группы")]
    teacher_sheets = (
        teacher_sheets
        if teacher_sheets is not None
        else [
            n
            for n in visible
            if n.lower().startswith("преподаватели") or n.lower().startswith("англ") or "возможност" in n.lower()
        ]
    )
    draft = Draft()
    if room_sheet:
        parse_rooms_sheet(wb[room_sheet], draft)
    for n in teacher_sheets:
        parse_teacher_names(wb[n], draft)
    for n in piup_sheets:
        parse_group_sheet(wb[n], draft, "ПиУП")
    for n in kino_sheets:
        parse_group_sheet(wb[n], draft, "Кино")
    for e in draft.entries:
        if e.room and e.room not in draft.rooms:
            draft.rooms.setdefault(
                e.room,
                {"props": set(), "pcs": None, "sources": {e.room}, "note": "есть в расписании, нет на листе аудиторий"},
            )
    if SCHOOL_ROOM in draft.rooms:
        draft.rooms[SCHOOL_ROOM]["note"] = "принадлежит школе — в расчёт не брать"
    _similar_names(draft)
    return draft


def _similar_names(draft: Draft) -> None:
    import difflib

    keys = sorted(draft.teachers)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
                draft.issues.append(
                    Issue(
                        "—",
                        "—",
                        f"{draft.teachers[a]} / {draft.teachers[b]}",
                        "похожие фамилии — возможно, один человек (опечатка); объедините вручную",
                    )
                )
    known = [k for k, r in draft.rooms.items() if r["props"]]
    for name, r in draft.rooms.items():
        if not r["props"] and name != SCHOOL_ROOM:
            near = difflib.get_close_matches(name, known, n=1, cutoff=0.8)
            hint = f"; возможно, это «{near[0]}»" if near else ""
            draft.issues.append(Issue("—", "—", name, f"аудитории нет на листе аудиторий{hint}"))


# ====================================================================== нагрузка из расписания


@dataclass
class DraftItem:
    discipline: str
    teachers: list[str]
    groups: list[str]
    pairs_per_cycle: int
    online: bool
    room_type: str | None
    teacher_in_room: bool = False
    block: str = ""
    label: str = ""
    headcount: int | None = None
    week_pattern: str = "auto"
    comment: str = ""
    planning: str = "auto"  # "manual" — ставит диспетчер, решатель учитывает только закреплённое
    pins: list[str] = field(default_factory=list)  # «Пт 6 (Ц1 301)» — в формате колонки «Закрепления»
    may_end_early: bool = False  # можно закончить раньше, к закрытию площадки


#: Мероприятия, которые всегда проходят в одной аудитории и ставятся вручную (не решателем).
#: Ключ — название дисциплины в нижнем регистре; значение — аудитория.
FIXED_ROOM_EVENTS = {"киноклуб": "Ц1 301"}
#: Все аудитории текущего файла — арендованная площадка ВДНХ (Ц1, Ц2, АРТ), строго до 21:00.
SITE_NAME, SITE_CLOSES = "ВДНХ", "21:00"


def aggregate(draft: Draft) -> tuple[list[DraftItem], dict[str, list[str]]]:
    """Сколько раз в неделю стоит каждая пара «дисциплина + преподаватели + группы» → нагрузка.

    Поток (2КП1 + 2КП2) виден в строках обеих групп — считается один раз по (день, пара).
    Английский (несколько преподавателей + список групп) — синхронный блок курса.
    Возвращает элементы нагрузки и блоки {название: группы}.
    """
    room_type = {name: _room_type(r)[0] for name, r in draft.rooms.items()}
    # Названия групп в ячейке потока/блока могут быть условными («3КП1 + 3КП2» в строках всех групп
    # 3 курса кино, а группы 3КП2 нет) — тогда участники = группы строк, где эта ячейка стоит.
    rows_at: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for e in draft.entries:
        rows_at[(e.sheet, e.day, e.pair, e.discipline, tuple(sorted(e.groups)))].add(e.group)
    for e in draft.entries:
        unknown = [g for g in e.groups if g not in draft.groups]
        if unknown:
            real = sorted(
                rows_at[(e.sheet, e.day, e.pair, e.discipline, tuple(sorted(e.groups)))]
                | {g for g in e.groups if g in draft.groups}
            )
            draft.issues.append(
                Issue(
                    e.sheet,
                    e.cell,
                    " + ".join(e.groups),
                    f"групп {', '.join(unknown)} нет — участники взяты по строкам листа: {' + '.join(real)}",
                )
            )
            e.groups = real
    fixed: list[Entry] = []
    rest: list[Entry] = []
    for e in draft.entries:
        (fixed if e.discipline.strip().lower() in FIXED_ROOM_EVENTS else rest).append(e)
    fixed_items = _fixed_room_items(draft, fixed, room_type)
    seen: set[tuple[Any, ...]] = set()
    counts: dict[tuple[Any, ...], Counter[str]] = defaultdict(Counter)
    rooms_used: dict[tuple[Any, ...], Counter[str]] = defaultdict(Counter)
    blocks: dict[str, list[str]] = {}
    block_items: dict[tuple[Any, ...], Counter[str]] = defaultdict(Counter)
    # Ячейки английского с одним преподавателем («Дугина (Ц2 202) / 2Ф1 + 2Ф2») — тоже пары блока
    # этого сочетания групп, только в это время занимается не каждая подгруппа.
    block_keys = {(e.discipline, frozenset(e.groups)) for e in rest if e.block_teachers}
    block_slots: dict[str, dict[tuple[int, int], set[str]]] = defaultdict(lambda: defaultdict(set))
    online_votes: dict[tuple[Any, ...], Counter[bool]] = defaultdict(Counter)
    for e in rest:
        bt = e.block_teachers
        if not bt and e.teachers and (e.discipline, frozenset(e.groups)) in block_keys:
            bt = [(t, e.room, e.online) for t in e.teachers]
        if bt:
            gs = sorted(set(e.groups))
            bname = f"{e.discipline}, {gs[0][0]} курс ({' + '.join(gs)})"
            blocks[bname] = gs
            block_slots[bname][(e.day, e.pair)].update(t for t, _, _ in bt)
            for t, room, t_online in bt:
                key = (bname, t)
                online_votes[key][t_online] += 1
                slot = (key, e.day, e.pair)
                if slot not in seen:
                    seen.add(slot)
                    block_items[key][e.parity or "weekly"] += 1
                    if room:
                        rooms_used[key][room] += 1
            continue
        key = (e.discipline, tuple(sorted(e.teachers)), tuple(sorted(set(e.groups))), e.online)
        slot = (key, e.day, e.pair, e.parity)
        if slot in seen:
            continue
        seen.add(slot)
        counts[key][e.parity or "weekly"] += 1
        if e.room:
            rooms_used[key][e.room] += 1
    items = []
    for key, c in counts.items():
        disc, teachers, groups, online = key
        n = 2 * c["weekly"] + c["odd"] + c["even"] + c["blink"]
        rt = _majority_type(rooms_used[key], room_type)
        comment = []
        pattern = "auto"
        if not c["weekly"] and not c["blink"] and bool(c["odd"]) != bool(c["even"]) and n % 2 == 0:
            # все пары — в одну неделю цикла: это схема «через неделю» (кафедра кино)
            pattern = "alt_week"
            comment.append("в текущем расписании — только по " + ("нечётным" if c["odd"] else "чётным"))
        elif c["odd"] or c["even"] or c["blink"]:
            comment.append("в текущем расписании есть мигающие пары")
        if not online and rt is None:
            rt = "lecture"
            comment.append("аудитория в расписании не указана — проверьте тип")
        if not teachers:
            comment.append("ПРЕПОДАВАТЕЛЬ НЕ РАСПОЗНАН — впишите")
        items.append(
            DraftItem(
                disc,
                list(teachers),
                list(groups),
                n,
                online,
                None if online else rt,
                teacher_in_room=online and bool(rooms_used[key]),
                week_pattern=pattern,
                comment="; ".join(comment),
            )
        )
    apart: dict[str, int] = {}
    for bname, sl in block_slots.items():
        everyone = set().union(*sl.values())
        apart[bname] = sum(1 for ts in sl.values() if ts != everyone)
        if apart[bname]:
            draft.issues.append(
                Issue(
                    "—",
                    "—",
                    bname,
                    f"в текущем расписании {apart[bname]} из {len(sl)} пар в неделю идут не у всех подгрупп "
                    f"одновременно; в черновике блок — все пары вместе (уточняется у диспетчера)",
                )
            )
    per_block: dict[str, set[int]] = defaultdict(set)
    for (bname, t), c in block_items.items():
        n = 2 * c["weekly"] + c["odd"] + c["even"] + c["blink"]
        per_block[bname].add(n)
        votes = online_votes[(bname, t)]
        t_online = votes[True] > votes[False]
        rt = _majority_type(rooms_used[(bname, t)], room_type)
        notes = []
        if votes[True] and votes[False]:
            notes.append(f"в расписании то онлайн, то очно — принято {'онлайн' if t_online else 'очно'}")
        if not t_online and rt is None:
            rt = "lecture"
            notes.append("аудитория в расписании не указана — проверьте тип")
        if apart.get(bname):
            notes.append("в текущем расписании не все пары блока шли одновременно — здесь все вместе")
        comment = "; ".join(notes)
        items.append(
            DraftItem(
                bname.split(",")[0],
                [t],
                [],
                n,
                t_online,
                None if t_online else rt,
                block=bname,
                label=draft.teachers.get(t, t).split()[0],
                comment=comment,
            )
        )
    for bname, ns in per_block.items():
        if len(ns) > 1:
            draft.issues.append(
                Issue(
                    "—",
                    "—",
                    bname,
                    f"в блоке у преподавателей разное число пар за цикл "
                    f"{sorted(ns)} — в синхронном блоке оно должно совпадать",
                )
            )
    items += fixed_items
    return sorted(items, key=lambda x: (x.block, x.groups, x.discipline)), blocks


def _fixed_room_items(draft: Draft, entries: list[Entry], room_type: dict[str, str]) -> list[DraftItem]:
    """Киноклуб и подобное: одно занятие для всех групп, где оно стоит, — «вручную», пары закреплены
    по текущему расписанию в постоянной аудитории (студенты приходят по желанию, поэтому группы
    одного вечера объединены)."""
    by_disc: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_disc[e.discipline.strip().lower()].append(e)
    out = []
    for key, es in by_disc.items():
        room = FIXED_ROOM_EVENTS[key]
        if room not in draft.rooms:
            draft.issues.append(Issue("—", "—", es[0].discipline, f"аудитории {room} нет на листе аудиторий"))
        slots = sorted({(e.day, e.pair, e.parity) for e in es})
        teachers = sorted({t for e in es for t in e.teachers})
        groups = sorted({g for e in es for g in e.groups})
        week_word = {None: "", "odd": " нечёт", "even": " чёт", "blink": " нечёт"}
        pins = [f"{DAY_NAMES[d]} {n}{week_word[par]} ({room})" for d, n, par in slots]
        n = sum(2 if par is None else 1 for _, _, par in slots)
        comment = [
            f"всегда в {room} вечером; пары закреплены по текущему расписанию "
            f"({', '.join(f'{DAY_NAMES[d]} {p}' for d, p, _ in slots)}) — проверьте; ставится вручную"
        ]
        if any(par == "blink" for _, _, par in slots):
            comment.append("неделя мигающей пары неизвестна — поставлена нечётная")
        if not teachers:
            comment.append("ПРЕПОДАВАТЕЛЬ НЕ РАСПОЗНАН — впишите")
        out.append(
            DraftItem(
                es[0].discipline.strip().capitalize(),
                teachers,
                groups,
                n,
                False,
                room_type.get(room) or "lecture",
                planning="manual",
                pins=pins,
                may_end_early=True,
                comment="; ".join(comment),
            )
        )
    return out


def _room_type(r: dict[str, Any]) -> tuple[str, str]:
    props = r.get("props") or set()
    if "компы" in props:
        return PROPERTY_TYPE["компы"]
    return PROPERTY_TYPE["лекционка"]


def _majority_type(used: Counter[str], room_type: dict[str, str]) -> str | None:
    if not used:
        return None
    types = Counter()
    for room, n in used.items():
        if room != SCHOOL_ROOM:
            types[room_type.get(room, "lecture")] += n
    return types.most_common(1)[0][0] if types else "lecture"
