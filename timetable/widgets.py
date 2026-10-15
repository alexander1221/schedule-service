"""Виджет сетки предпочтений «дни × пары» со шкалой −2…+2 для админки.

Хранится словарь {"<день>-<пара>": значение}; нули не храним (0 — значение по
умолчанию). Цвет ячейки — от красного («не могу») до зелёного («очень хочу»).
Простая форма преподавателя с телефона (клик циклически меняет значение) — этап 7.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from django import forms
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from scheduler.domain import DAY_NAMES

SCALE = [
    (-2, "−2 не могу"),
    (-1, "−1 нежелательно"),
    (0, "0 всё равно"),
    (1, "+1 удобно"),
    (2, "+2 очень хочу"),
]
COLORS = {-2: "#e5534b", -1: "#f0a35e", 0: "transparent", 1: "#a6d96a", 2: "#3fa34d"}

_STYLE = """
<style>
.pref-grid{border-collapse:collapse;margin:4px 0}
.pref-grid th,.pref-grid td{border:1px solid var(--hairline-color,#ccc);padding:2px 4px;text-align:center}
.pref-grid select{min-width:4.2em;border-radius:4px}
.pref-legend span{display:inline-block;padding:1px 6px;margin-right:4px;border-radius:4px}
</style>
"""
_SCRIPT = """
<script>
(function(){
 const C=%s;
 function paint(s){const c=C[s.value],on=c&&c!=='transparent';
  s.style.background=on?c:'';s.style.color=on?'#111':'';}
 document.querySelectorAll('select.pref-cell').forEach(s=>{paint(s);s.addEventListener('change',()=>paint(s));});
})();
</script>
"""


class PrefGridWidget(forms.Widget):
    def __init__(
        self,
        days: list[int] | None = None,
        pairs: list[int] | None = None,
        attrs: dict[str, Any] | None = None,
        pair_labels: dict[int, str] | None = None,
        disabled: bool = False,
    ):
        super().__init__(attrs)
        self.days = days or [1, 2, 3, 4, 5]
        self.pairs = pairs or [1, 2, 3, 4, 5, 6, 7]
        self.pair_labels = pair_labels or {}  # «1 · 09:30–11:00» вместо «1» (страница преподавателя)
        self.disabled = disabled

    def format_value(self, value: Any) -> dict[str, int]:
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except json.JSONDecodeError:
                value = {}
        return {str(k): int(v) for k, v in (value or {}).items()}

    def render(self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None) -> SafeString:
        vals = self.format_value(value)
        head = format_html(
            "<tr><th></th>{}</tr>", format_html_join("", "<th>{}</th>", ((DAY_NAMES[d],) for d in self.days))
        )
        rows = []
        for p in self.pairs:
            cells = []
            for d in self.days:
                key = f"{d}-{p}"
                cur = vals.get(key, 0)
                opts = format_html_join(
                    "",
                    '<option value="{}"{}>{}</option>',
                    ((v, mark_safe(" selected") if v == cur else "", label.split(" ")[0]) for v, label in SCALE),
                )
                cells.append(
                    format_html(
                        '<td><select class="pref-cell" name="{}__{}"{}>{}</select></td>',
                        name,
                        key,
                        mark_safe(" disabled") if self.disabled else "",
                        opts,
                    )
                )
            rows.append(format_html("<tr><th>{}</th>{}</tr>", self.pair_labels.get(p, p), mark_safe("".join(cells))))
        legend = format_html_join(
            "",
            '<span style="background:{};color:{}">{}</span>',
            ((COLORS[v], "#111" if v else "inherit", label) for v, label in SCALE),
        )
        extra = [k for k in vals if k not in {f"{d}-{p}" for d in self.days for p in self.pairs}]
        hidden = ""
        if extra:  # слоты вне текущей сетки (например, суббота выключена) сохраняем как есть
            hidden = format_html(
                '<input type="hidden" name="{}__extra" value="{}">', name, json.dumps({k: vals[k] for k in extra})
            )
        return format_html(
            '{}<div class="pref-legend">{}</div><table class="pref-grid">{}{}</table>{}{}',
            mark_safe(_STYLE),
            legend,
            head,
            mark_safe("".join(rows)),
            hidden,
            mark_safe(_SCRIPT % json.dumps({str(k): c for k, c in COLORS.items()})),
        )

    def value_from_datadict(self, data: Any, files: Any, name: str) -> dict[str, int]:
        out: dict[str, int] = {}
        extra = data.get(f"{name}__extra")
        if extra:
            with contextlib.suppress(ValueError, AttributeError):
                out.update({str(k): int(v) for k, v in json.loads(extra).items()})
        prefix = f"{name}__"
        for k in data:
            if k.startswith(prefix) and k != f"{name}__extra":
                try:
                    v = int(data.get(k))
                except (TypeError, ValueError):
                    continue
                if v:
                    out[k[len(prefix) :]] = v
                else:
                    out.pop(k[len(prefix) :], None)
        return out

    def value_omitted_from_data(self, data: Any, files: Any, name: str) -> bool:
        return not any(str(k).startswith(f"{name}__") for k in data)


class PrefGridField(forms.JSONField):
    widget = PrefGridWidget

    def prepare_value(self, value: Any) -> Any:
        return value if isinstance(value, dict) else super().prepare_value(value)

    def to_python(self, value: Any) -> Any:
        if isinstance(value, dict):
            return value
        return super().to_python(value)

    def bound_data(self, data: Any, initial: Any) -> Any:
        return data if isinstance(data, dict) else super().bound_data(data, initial)

    def has_changed(self, initial: Any, data: Any) -> bool:
        return (initial or {}) != (data or {})


def grid_for(semester: Any | None) -> tuple[list[int], list[int]]:
    """Дни и номера пар сетки семестра (или последнего семестра, или по умолчанию)."""
    from .models import Semester

    sem = semester or Semester.objects.order_by("-start_date").first()
    if sem is None:
        return [1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6, 7]
    return sem.days, sem.pair_numbers()


class BusyGridWidget(forms.Widget):
    """Сетка «дни × пары» с галочками: отмеченные слоты заняты (значение — список «день-пара»)."""

    def __init__(
        self, days: list[int] | None = None, pairs: list[int] | None = None, attrs: dict[str, Any] | None = None
    ):
        super().__init__(attrs)
        self.days = days or [1, 2, 3, 4, 5]
        self.pairs = pairs or [1, 2, 3, 4, 5, 6, 7]

    def render(self, name: str, value: Any, attrs: dict[str, Any] | None = None, renderer: Any = None) -> SafeString:
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except json.JSONDecodeError:
                value = []
        busy = set(value or [])
        head = format_html(
            "<tr><th></th>{}</tr>", format_html_join("", "<th>{}</th>", ((DAY_NAMES[d],) for d in self.days))
        )
        rows = []
        for p in self.pairs:
            cells = format_html_join(
                "",
                '<td><input type="checkbox" name="{}__{}"{}></td>',
                ((name, f"{d}-{p}", mark_safe(" checked") if f"{d}-{p}" in busy else "") for d in self.days),
            )
            rows.append(format_html("<tr><th>{}</th>{}</tr>", p, cells))
        return format_html(
            '{}<p>Отметьте пары, когда аудитория занята не нами.</p><table class="pref-grid">{}{}</table>',
            mark_safe(_STYLE),
            head,
            mark_safe("".join(rows)),
        )

    def value_from_datadict(self, data: Any, files: Any, name: str) -> list[str]:
        prefix = f"{name}__"
        return sorted(k[len(prefix) :] for k in data if str(k).startswith(prefix) and data.get(k))

    def value_omitted_from_data(self, data: Any, files: Any, name: str) -> bool:
        return False


class BusyGridField(forms.JSONField):
    widget = BusyGridWidget

    def prepare_value(self, value: Any) -> Any:
        return value if isinstance(value, list) else super().prepare_value(value)

    def to_python(self, value: Any) -> Any:
        return value if isinstance(value, list) else super().to_python(value)

    def bound_data(self, data: Any, initial: Any) -> Any:
        return data if isinstance(data, list) else super().bound_data(data, initial)

    def has_changed(self, initial: Any, data: Any) -> bool:
        return sorted(initial or []) != sorted(data or [])
