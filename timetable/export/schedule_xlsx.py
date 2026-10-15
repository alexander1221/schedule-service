"""Выгрузка готового варианта в Excel — в привычном учебному отделу виде.

Лист на каждое представление: «Группы», «Преподаватели», «Аудитории». Строки — сущность × пара,
колонки — дни недели. Если чётная и нечётная недели совпадают, в ячейке одна запись; если нет —
«нечёт: …» и «чёт: …» в одной ячейке. Плюс лист «О варианте» (что выгружено и как читать).
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from scheduler.domain import Problem, Schedule, Week
from timetable.services import views as sv

FONT = Font(name="Arial", size=9)
BOLD = Font(name="Arial", size=9, bold=True)
HEAD = Font(name="Arial", size=10, bold=True)
HEAD_FILL = PatternFill("solid", fgColor="DDE4EE")
ODD_EVEN_FILL = PatternFill("solid", fgColor="FFF4E0")  # недели различаются
THIN = Side(style="thin", color="A6A6A6")
THICK = Side(style="medium", color="404040")
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)

#: Пометки, которые в выгрузке не нужны: чётность и так видна по «нечёт:/чёт:».
_SKIP_MARKS = {"мигающая", "через неделю"}

VIEWS = (
    ("group", "Группы", "Группа"),
    ("teacher", "Преподаватели", "Преподаватель"),
    ("room", "Аудитории", "Аудитория"),
)


def _entry_text(e: sv.Entry, by: str) -> str:
    lines = [e.discipline]
    if by != "teacher" and e.teachers:
        lines.append(e.teachers)
    if by != "group" and e.groups:
        lines.append(e.groups)
    if by != "room" and e.room:
        lines.append(e.room)
    marks = [mk for mk in e.marks if mk not in _SKIP_MARKS]
    if marks:
        lines.append("(" + ", ".join(marks) + ")")
    return "\n".join(lines)


def _cell_text(entries: list[sv.Entry], by: str) -> str:
    """Записи ячейки. Несколько подгрупп одной дисциплины в одной паре (английский блоком) —
    одной записью: «Иностранный язык, по подгруппам: Иванова — Ц1 301; …»."""
    by_disc: dict[str, list[sv.Entry]] = {}
    for e in entries:
        by_disc.setdefault(e.discipline, []).append(e)
    parts = []
    for disc, es in by_disc.items():
        if by == "group" and len(es) > 1:
            subs = [f"{(e.teachers or '—').split(' ')[0]} — {e.room or 'онлайн'}" for e in es]
            parts.append(f"{disc}, по подгруппам:\n" + "\n".join(subs))
        else:
            parts.extend(_entry_text(e, by) for e in es)
    return "\n— — —\n".join(parts)


def _lines(text: str, width_chars: int = 34) -> int:
    """Сколько строк займёт текст в ячейке ширины ~30 при шрифте 9 (с переносом длинных строк)."""
    if not text:
        return 1
    return sum(max(1, -(-len(line) // width_chars)) for line in text.split("\n"))


def _combine(odd: str, even: str) -> tuple[str, bool]:
    """Текст ячейки и признак «недели различаются»."""
    if odd == even:
        return odd, False
    parts = []
    if odd:
        parts.append("нечёт: " + odd)
    if even:
        parts.append("чёт: " + even)
    return "\n\n".join(parts), True


def _sheet(ws: Worksheet, p: Problem, s: Schedule, by: str, title: str) -> int:
    days = sv.day_headers(p)
    pairs = sorted(p.grid.pairs, key=lambda x: x.start_min)
    headers = [title, "Пара", "Время", *days]
    for i, h in enumerate(headers, 1):
        c = ws.cell(1, i, h)
        c.font, c.fill, c.alignment = HEAD, HEAD_FILL, CENTER
        c.border = Border(top=THIN, bottom=THICK, left=THIN, right=THIN)
    widths = [16, 6, 12] + [30] * len(days)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    busy_keys = _busy_keys(p, s, by)
    row = 2
    shown = 0
    ents = sv.entities(p, by)
    if by == "group":  # по курсам, внутри — по названию
        ents = sorted(ents, key=lambda kn: (p.group_by_id[kn[0]].course, kn[1]))
    for key, name in ents:
        if by != "group" and key not in busy_keys:
            continue  # преподаватели и аудитории без занятий — не нужны
        odd = sv.grid(p, s, by, key, Week.ODD)
        even = sv.grid(p, s, by, key, Week.EVEN)
        first = row
        for pi, pt in enumerate(pairs):
            ws.cell(row, 2, pt.number)
            ws.cell(row, 3, f"{pt.start}–{pt.end}")
            lines = 1
            for di in range(len(days)):
                text, differs = _combine(_cell_text(odd[pi].cells[di], by), _cell_text(even[pi].cells[di], by))
                c = ws.cell(row, 4 + di, text or None)
                if differs:
                    c.fill = ODD_EVEN_FILL
                lines = max(lines, _lines(text))
            ws.row_dimensions[row].height = max(15, 12 * lines)
            for col in range(1, 4 + len(days)):
                c = ws.cell(row, col)
                c.font = FONT if col != 1 else BOLD
                c.alignment = WRAP if col >= 4 else CENTER
                c.border = Border(
                    left=THIN,
                    right=THIN,
                    top=THICK if pi == 0 else THIN,
                    bottom=THICK if pi == len(pairs) - 1 else THIN,
                )
            row += 1
        ws.merge_cells(start_row=first, start_column=1, end_row=row - 1, end_column=1)
        ws.cell(first, 1, name)
        shown += 1
    ws.freeze_panes = "D2"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    return shown


def _busy_keys(p: Problem, s: Schedule, by: str) -> set[str]:
    if by == "room":
        return {a.room_id for a in s.assignments if a.room_id}
    if by == "teacher":
        return {t for a in s.assignments for t in p.item_by_id[a.item_id].teacher_ids}
    return set()


def schedule_xlsx(
    p: Problem,
    s: Schedule,
    *,
    title: str,
    notes: Iterable[str] = (),
    views: Iterable[str] = ("group", "teacher", "room"),
) -> bytes:
    wb = Workbook()
    info = wb.active
    info.title = "О варианте"
    info.column_dimensions["A"].width = 110
    lines = [
        title,
        "",
        "Как читать: строки — пары, колонки — дни недели.",
        "Если по чётным и нечётным неделям занятия разные, в ячейке написано «нечёт: …» и «чёт: …», "
        "ячейка выделена цветом.",
        f"Первая неделя семестра ({p.semester.start_date:%d.%m.%Y}) — "
        f"{'нечётная' if p.semester.first_week is Week.ODD else 'чётная'}.",
        "Пометки в скобках: поток — несколько групп вместе; онлайн; онлайн, бронь — преподаватель ведёт "
        "из аудитории; до 21:00 — занятие заканчивается к закрытию площадки.",
        *notes,
    ]
    for i, text in enumerate(lines, 1):
        c = info.cell(i, 1, text)
        c.font = HEAD if i == 1 else FONT
        c.alignment = Alignment(wrap_text=True, vertical="top")
    wanted = set(views)
    for by, sheet_title, col_title in VIEWS:
        if by in wanted:
            _sheet(wb.create_sheet(sheet_title), p, s, by, col_title)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
