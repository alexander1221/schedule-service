"""Общие помощники импорта/экспорта xlsx: разбор значений, отчёт об ошибках, оформление."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

HEADER_FILL = PatternFill("solid", fgColor="DDE7F3")
HEADER_FONT = Font(bold=True)


@dataclass
class RowError:
    sheet: str
    row: int  # номер строки в Excel (как видит пользователь)
    column: str
    message: str

    def __str__(self) -> str:
        where = f"«{self.sheet}», строка {self.row}" + (f", «{self.column}»" if self.column else "")
        return f"{where}: {self.message}"


@dataclass
class ImportReport:
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)
    errors: list[RowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: bool = False  # изменения записаны (не пробный прогон и нет ошибок)

    @property
    def ok(self) -> bool:
        return not self.errors

    def bump(self, what: str, kind: str = "created", n: int = 1) -> None:
        bucket = getattr(self, kind)
        bucket[what] = bucket.get(what, 0) + n

    def summary(self) -> str:
        parts = []
        for title, bucket in (("создано", self.created), ("обновлено", self.updated), ("удалено", self.deleted)):
            if bucket:
                parts.append(f"{title}: " + ", ".join(f"{k} — {v}" for k, v in bucket.items()))
        state = "применено" if self.applied else ("проверка без записи" if self.ok else "НЕ применено: есть ошибки")
        return (
            f"{state}. "
            + ("; ".join(parts) if parts else "изменений нет")
            + (f". Ошибок: {len(self.errors)}" if self.errors else "")
        )


def norm(s: Any) -> str:
    """Нормализация для сравнения названий: регистр, «ё», пробелы, неразрывные пробелы."""
    text = "" if s is None else str(s)
    text = text.replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace(" ", " ").strip()


def split_list(v: Any) -> list[str]:
    """«Иванов И.И.; Петров П.П.» → два элемента. Разделители: ; , перевод строки."""
    return [x.strip() for x in re.split(r"[;\n,]", cell_str(v)) if x.strip()]


def parse_int(v: Any, *, lo: int | None = None, hi: int | None = None, required: bool = False) -> int | None:
    s = cell_str(v)
    if not s:
        if required:
            raise ValueError("обязательное поле пусто")
        return None
    try:
        n = int(float(s.replace(",", ".")))
    except ValueError as e:
        raise ValueError(f"«{s}» — не целое число") from e
    if float(s.replace(",", ".")) != n:
        raise ValueError(f"«{s}» — не целое число")
    if lo is not None and n < lo:
        raise ValueError(f"{n} меньше допустимого {lo}")
    if hi is not None and n > hi:
        raise ValueError(f"{n} больше допустимого {hi}")
    return n


TRUE = {"да", "1", "+", "true", "yes", "истина", "д", "y"}
FALSE = {"нет", "0", "-", "false", "no", "ложь", "н", "n", ""}


def parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = norm(v)
    if s in TRUE:
        return True
    if s in FALSE:
        return False if s else default
    raise ValueError(f"«{cell_str(v)}» — ожидается «да» или «нет»")


def suggest(name: str, choices: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(choices), n=2, cutoff=0.6)
    return f" Возможно: {', '.join('«' + c + '»' for c in close)}." if close else ""


def header_map(ws: Worksheet, known: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    """Сопоставить заголовки первой строки с ключами. known: нормализованный заголовок → ключ.

    Порядок колонок может быть любым — пользователь мог переставить их в Excel.
    """
    cols: dict[str, int] = {}
    unknown: list[str] = []
    for idx, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1)), start=1):
        title = norm(cell.value)
        if not title:
            continue
        key = known.get(title)
        if key:
            cols[key] = idx
        else:
            unknown.append(cell_str(cell.value))
    return cols, unknown


def data_rows(ws: Worksheet) -> Iterator[tuple[int, tuple[Any, ...]]]:
    """Непустые строки после заголовка: (номер строки Excel, значения)."""
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if any(cell_str(v) for v in row):
            yield n, row


def write_header(ws: Worksheet, titles: list[str], widths: list[int] | None = None) -> None:
    ws.append(titles)
    for i, _ in enumerate(titles, start=1):
        c = ws.cell(row=1, column=i)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = (widths or [])[i - 1] if widths and i <= len(widths) else 16
    ws.freeze_panes = "A2"


def finish_sheet(ws: Worksheet) -> None:
    if ws.max_row >= 1 and ws.max_column >= 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{max(ws.max_row, 2)}"


def add_list_validation(ws: Worksheet, col: int, source: str, rows: int = 2000) -> None:
    """Выпадающий список в колонке. source — диапазон вида '=Списки!$A$2:$A$10'."""
    dv = DataValidation(type="list", formula1=source, allow_blank=True, showErrorMessage=False)
    letter = get_column_letter(col)
    dv.add(f"{letter}2:{letter}{rows}")
    ws.add_data_validation(dv)
