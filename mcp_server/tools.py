"""Инструменты агента диспетчера — обычные функции, без зависимости от библиотеки ``mcp``.

Каждый инструмент — тонкая обёртка над запросом API (docs/API.md), возвращает короткий текст по-русски:
локальной модели так проще, чем разбирать JSON. Регистрацию в MCP-сервере (v1 или v2 библиотеки)
делает ``compat.py``. Утверждения варианта здесь нет намеренно: утверждает только человек.

Аннотации типов — настоящие объекты (без ``from __future__ import annotations``): по ним обе версии
библиотеки строят JSON-схему параметров.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .client import ApiClient, ApiError

DAYS = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб"}
WEEKS = {"odd": "нечётная неделя", "even": "чётная неделя", "both": "каждую неделю"}
PREF = {-2: "нельзя", -1: "нежелательно", 0: "всё равно", 1: "удобно", 2: "очень удобно"}
Who = Literal["group", "teacher", "room"]
Week = Literal["odd", "even"]
AnyWeek = Literal["both", "odd", "even"]


@dataclass(frozen=True)
class ToolSpec:
    fn: Callable[..., str]
    title: str
    read_only: bool = True
    destructive: bool = False

    @property
    def name(self) -> str:
        return self.fn.__name__


def _slot(day: int, pair: int) -> str:
    return f"{DAYS.get(day, day)} {pair}-я пара"


def _check_slot(day: int, pair: int) -> None:
    if day not in DAYS:
        raise ApiError(f"День {day}: нужен номер 1–6 (1 — понедельник, 6 — суббота).")
    if not 1 <= pair <= 12:
        raise ApiError(f"Пара {pair}: нужен номер пары 1–12.")


def make_tools(api: ApiClient) -> list[ToolSpec]:
    """Инструменты, привязанные к клиенту API."""

    def semesters() -> str:
        """Список семестров: id, даты, утверждённый вариант расписания и идущий расчёт. Начинайте с него."""
        rows = api.get("/semesters")
        if not rows:
            return "Семестров нет."
        out = []
        for s in rows:
            line = f"Семестр {s['id']}: «{s['name']}», с {s['start_date']}"
            line += f"; утверждённый вариант: {s['approved_variant_id'] or 'нет'}"
            if s["active_run_id"]:
                line += f"; сейчас идёт расчёт {s['active_run_id']}"
            out.append(line)
        return "\n".join(out)

    def find_teacher(semester_id: int, name: str) -> str:
        """Найти преподавателя семестра по части фамилии. Возвращает id преподавателя (teacher_id)."""
        rows = api.get(f"/semesters/{semester_id}/teachers", q=name)
        if not rows:
            return f"Преподаватель «{name}» в семестре {semester_id} не найден (ищется по части ФИО)."
        return "\n".join(
            f"teacher_id={t['id']}: {t['name']}, занятий в нагрузке: {t['items']}, "
            f"пожелания {'заполнены' if t['has_prefs'] else 'не заполнены'}"
            for t in rows[:20]
        )

    def find_in_variant(variant_id: int, who: Who, name: str = "") -> str:
        """Найти группу, преподавателя или аудиторию в варианте расписания по части названия.
        Возвращает ключ (key) для инструмента schedule.
        who: group — группа, teacher — преподаватель, room — аудитория."""
        rows = api.get(f"/variants/{variant_id}/entities", by=who)
        found = [r for r in rows if name.lower() in r["name"].lower()]
        if not found:
            return f"Ничего не найдено по «{name}»."
        return "\n".join(f"key={r['id']}: {r['name']}" for r in found[:30])

    def schedule(variant_id: int, who: Who, key: str, week: Week = "odd") -> str:
        """Расписание группы, преподавателя или аудитории в варианте на неделю (odd — нечётная, even — чётная).
        key — из find_in_variant (для преподавателя это его teacher_id)."""
        g = api.get(f"/variants/{variant_id}/grid", by=who, key=key, week=week)
        lines = [f"{g['name']} — {WEEKS[week]}, вариант {variant_id}:"]
        for di, day in enumerate(g["days"]):
            day_lines = []
            for r in g["rows"]:
                for e in r["cells"][di]:
                    room = f", ауд. {e['room']}" if e["room"] else ""
                    marks = f" [{', '.join(e['marks'])}]" if e["marks"] else ""
                    other = e["groups"] if who != "group" else e["teachers"]
                    day_lines.append(f"  {r['pair']} ({r['time']}): {e['discipline']} — {other}{room}{marks}")
            lines.append(f"{day}:" + ("\n" + "\n".join(day_lines) if day_lines else " нет занятий"))
        return "\n".join(lines)

    def teacher_wishes(semester_id: int, teacher_id: int) -> str:
        """Пожелания преподавателя по времени на семестр: шкала −2 (нельзя) … +2 (очень удобно)."""
        p = api.get(f"/semesters/{semester_id}/teachers/{teacher_id}/prefs")
        marked = sorted(p["prefs"].items(), key=lambda kv: tuple(int(x) for x in kv[0].split("-")))
        head = f"{p['teacher']['name']}, семестр {semester_id}. Не указано — «всё равно»."
        if not marked:
            return head + " Пожеланий нет."
        body = [f"  {_slot(*(int(x) for x in k.split('-')))}: {v:+d} ({PREF[v]})" for k, v in marked]
        return head + "\n" + "\n".join(body)

    def set_teacher_wish(semester_id: int, teacher_id: int, day: int, pair: int, value: int) -> str:
        """Записать пожелание преподавателя на один слот. day: 1=Пн … 6=Сб; pair: номер пары;
        value: −2 нельзя, −1 нежелательно, 0 всё равно, 1 удобно, 2 очень удобно.
        Решатель учтёт это при следующем расчёте (−2 — жёсткий запрет)."""
        _check_slot(day, pair)
        if value not in PREF:
            raise ApiError("value: нужно число от −2 до 2.")
        p = api.patch(f"/semesters/{semester_id}/teachers/{teacher_id}/prefs", {"set": {f"{day}-{pair}": value}})
        return f"Записано: {p['teacher']['name']}, {_slot(day, pair)} — {value:+d} ({PREF[value]})."

    def lessons(semester_id: int, teacher_id: int | None = None, group_id: int | None = None) -> str:
        """Занятия (нагрузка) семестра преподавателя или группы: item_id, дисциплина, пар за 2 недели, закрепления.
        item_id нужен для pin_lesson и check_move."""
        rows = api.get(f"/semesters/{semester_id}/items", teacher_id=teacher_id, group_id=group_id)
        if not rows:
            return "Занятий не найдено."
        out = []
        for it in rows[:60]:
            line = (
                f"item_id={it['id']}: {it['discipline']} ({it['kind']}, {it['format']}), "
                f"группы: {', '.join(g['name'] for g in it['groups']) or '—'}, "
                f"преподаватели: {', '.join(t['name'] for t in it['teachers']) or '—'}, "
                f"пар за 2 недели: {it['pairs_per_cycle']}"
            )
            if it["pins"]:
                line += "; закреплено: " + ", ".join(
                    f"pin_id={p['id']} {_slot(p['day'], p['pair'])} ({WEEKS[p['week']]})" for p in it["pins"]
                )
            out.append(line)
        return "\n".join(out)

    def pin_lesson(semester_id: int, item_id: int, day: int, pair: int, week: AnyWeek = "both") -> str:
        """Закрепить занятие в слоте: решатель обязательно поставит его сюда. day: 1=Пн … 6=Сб.
        Используйте, только если просьба жёсткая; для мягкой — set_teacher_wish."""
        _check_slot(day, pair)
        p = api.post(f"/semesters/{semester_id}/pins", {"item_id": item_id, "day": day, "pair": pair, "week": week})
        return f"Закреплено (pin_id={p['id']}): занятие {item_id} → {_slot(day, pair)}, {WEEKS[week]}."

    def unpin_lesson(pin_id: int) -> str:
        """Снять закрепление по pin_id (виден в lessons)."""
        api.delete(f"/pins/{pin_id}")
        return f"Закрепление {pin_id} снято."

    def check_semester(semester_id: int) -> str:
        """Быстрая проверка данных семестра до расчёта: противоречия, при которых расписание не построить."""
        r = api.get(f"/semesters/{semester_id}/precheck")
        if r["build_error"]:
            return "Задача не собирается: " + r["build_error"]
        out = ["Ошибок нет — можно считать." if r["ok"] else f"Ошибки ({len(r['errors'])}) — расписание не построить:"]
        out += [f"  ✗ {x}" for x in r["errors"][:15]]
        if r["warnings"]:
            out.append(f"Предупреждения ({len(r['warnings'])}):")
            out += [f"  ! {x}" for x in r["warnings"][:10]]
        return "\n".join(out)

    def start_rebuild(
        semester_id: int, minutes: float = 5, variants: int = 2, from_variant_id: int | None = None
    ) -> str:
        """Запустить расчёт расписания в фоне. По умолчанию — пересборка от утверждённого варианта с минимумом
        изменений (пары и аудитории стараются остаться на местах). from_variant_id — от другого варианта.
        Расчёт идёт около minutes × variants минут; следите через run_status."""
        if from_variant_id is None:
            from_variant_id = api.get(f"/semesters/{semester_id}")["approved_variant_id"]
        body: dict[str, Any] = {
            "time_limit_s": max(10, round(minutes * 60)),
            "variants": variants,
            "reference_variant_id": from_variant_id,
            "name_prefix": "Агент",
        }
        try:
            r = api.post(f"/semesters/{semester_id}/runs", body)
        except ApiError as e:
            raise ApiError(f"{e} Посмотрите идущий расчёт через semesters / run_status.") from None
        base = f"от варианта {from_variant_id}" if from_variant_id else "с нуля (утверждённого варианта нет)"
        return (
            f"Расчёт {r['id']} поставлен в очередь: {variants} вар. по {minutes:g} мин, {base}. "
            f"Проверяйте run_status({r['id']})."
        )

    def run_status(run_id: int) -> str:
        """Ход и итог расчёта: состояние, текущий штраф, готовые варианты (их id — для compare_variants)."""
        r = api.get(f"/runs/{run_id}")
        line = f"Расчёт {run_id}: {r['status_label']}."
        p = r["progress"] or {}
        if r["status"] == "running" and p:
            est = f", оценка штрафа {p['objective']:.0f}" if p.get("objective") is not None else ""
            line += f" Вариант {p.get('variant')} из {p.get('of')}, {p.get('stage')}{est}."
        if r["status"] == "queued":
            line += " Ждёт обработчик расчётов (если долго — он не запущен на сервере)."
        if r["message"]:
            line += " " + r["message"]
        if r["variant_ids"]:
            line += f" Варианты: {', '.join(str(x) for x in r['variant_ids'])}."
        return line

    def stop_run(run_id: int) -> str:
        """Остановить расчёт: найденное к этому моменту сохранится как вариант. Из очереди — снимается."""
        r = api.post(f"/runs/{run_id}/cancel")
        return f"Расчёт {run_id}: {r['status_label']}" + (
            " — останавливается, найденное сохранится." if r["status"] == "running" else "."
        )

    def variants(semester_id: int) -> str:
        """Варианты расписания семестра: id, штраф (меньше — лучше), утверждён ли, сколько изменений при пересборке."""
        rows = api.get(f"/semesters/{semester_id}/variants")
        if not rows:
            return "Вариантов нет."
        out = []
        for v in rows[:20]:
            line = f"variant_id={v['id']}: «{v['name']}», штраф {v['total']}"
            line += ", УТВЕРЖДЁН" if v["status"] == "approved" else ""
            if v["changes"] is not None:
                line += f", сдвинуто по времени {v['changes']}, сменили аудиторию {v['room_changes'] or 0}"
            out.append(line)
        return "\n".join(out)

    def compare_variants(variant_id: int, against_variant_id: int | None = None, limit: int = 15) -> str:
        """Что изменилось в варианте по сравнению с другим (по умолчанию — с эталоном пересборки или утверждённым):
        переносы, смены аудиторий, кого затронуло."""
        d = api.get(f"/variants/{variant_id}/diff", against=against_variant_id)
        if not d["changes"]:
            return f"Вариант {variant_id} совпадает с вариантом {d['against_variant_id']}."
        out = [
            f"Вариант {variant_id} против {d['against_variant_id']}: перенесено занятий {d['moved']} "
            f"(по неделям), сменили аудиторию {d['room_changed']}.",
            f"Затронуты группы ({len(d['affected_groups'])}): {', '.join(d['affected_groups'][:20])}",
            f"Затронуты преподаватели ({len(d['affected_teachers'])}): {', '.join(d['affected_teachers'][:20])}",
            "Изменения:",
        ]
        for c in d["changes"][:limit]:
            before = "; ".join(x["label"] for x in c["before"]) or "—"
            after = "; ".join(x["label"] for x in c["after"]) or "—"
            out.append(f"  · {c['discipline']} ({', '.join(c['groups'])}; {', '.join(c['teachers'])}) — {c['kind']}:")
            out.append(f"      было: {before}\n      стало: {after}")
        if len(d["changes"]) > limit:
            out.append(f"  … и ещё {len(d['changes']) - limit}")
        return "\n".join(out)

    def check_move(
        variant_id: int, item_id: int, day: int, pair: int, to_day: int, to_pair: int, week: AnyWeek = "both"
    ) -> str:
        """Проверить, что будет, если вручную перенести занятие (вариант не меняется): нарушения и изменение штрафа.
        day/pair — где занятие сейчас, to_day/to_pair — куда; дни 1=Пн … 6=Сб."""
        _check_slot(day, pair)
        _check_slot(to_day, to_pair)
        mv = {"item_id": item_id, "week": week, "day": day, "pair": pair, "to_day": to_day, "to_pair": to_pair}
        r = api.post(f"/variants/{variant_id}/check", {"moves": [mv]})
        head = f"Перенос {_slot(day, pair)} → {_slot(to_day, to_pair)}: штраф {r['base_total']} → {r['total']} "
        head += f"({r['delta']:+d})."
        if r["ok"]:
            return head + " Жёстких нарушений нет."
        return head + " НЕЛЬЗЯ — нарушения:\n" + "\n".join(f"  ✗ {v['message']}" for v in r["hard"][:10])

    def free_rooms(
        variant_id: int, day: int, pair_from: int, pair_to: int | None = None, week: AnyWeek = "both"
    ) -> str:
        """Свободные аудитории в варианте на день и пары (pair_from..pair_to). day: 1=Пн … 6=Сб."""
        _check_slot(day, pair_from)
        rows = api.get(
            f"/variants/{variant_id}/free-rooms", day=day, pair_from=pair_from, pair_to=pair_to or pair_from, week=week
        )
        if not rows:
            return "Свободных аудиторий нет."
        return "\n".join(
            f"{r['name']} ({r['type_name']}, {r['capacity']} мест)"
            + ("" if r["fully_free"] else f" — свободна только на парах {', '.join(map(str, r['free_pairs']))}")
            for r in rows[:30]
        )

    return [
        ToolSpec(semesters, "Семестры"),
        ToolSpec(find_teacher, "Найти преподавателя"),
        ToolSpec(find_in_variant, "Найти группу, преподавателя или аудиторию"),
        ToolSpec(schedule, "Расписание на неделю"),
        ToolSpec(teacher_wishes, "Пожелания преподавателя"),
        ToolSpec(set_teacher_wish, "Записать пожелание", read_only=False),
        ToolSpec(lessons, "Занятия (нагрузка)"),
        ToolSpec(pin_lesson, "Закрепить занятие", read_only=False),
        ToolSpec(unpin_lesson, "Снять закрепление", read_only=False, destructive=True),
        ToolSpec(check_semester, "Проверить данные семестра"),
        ToolSpec(start_rebuild, "Запустить пересборку", read_only=False),
        ToolSpec(run_status, "Ход расчёта"),
        ToolSpec(stop_run, "Остановить расчёт", read_only=False),
        ToolSpec(variants, "Варианты расписания"),
        ToolSpec(compare_variants, "Сравнить варианты"),
        ToolSpec(check_move, "Проверить перенос"),
        ToolSpec(free_rooms, "Свободные аудитории"),
    ]


INSTRUCTIONS = """\
Ты помогаешь диспетчеру учебной части колледжа менять расписание через сервис расписания.
Дни: 1=Пн, 2=Вт, 3=Ср, 4=Чт, 5=Пт, 6=Сб. Пары — номера 1–7. Недели: odd — нечётная, even — чётная, both — каждая.
Типичный порядок: semesters → find_teacher → teacher_wishes / lessons → set_teacher_wish (мягкая просьба) или
pin_lesson (жёсткая) → check_semester → start_rebuild → run_status (повторять, пока не «готово») →
compare_variants → рассказать диспетчеру, что изменилось и кого затронуло.
Утверждать вариант ты не можешь и не должен: это делает человек в админке.
Не придумывай id — бери их из ответов инструментов. Если инструмент вернул ошибку — перескажи её диспетчеру.
"""


def move_request_prompt(teacher: str, from_slot: str, to_slot: str) -> str:
    """Преподаватель просит перенести пару: пошаговый план для агента."""
    return (
        f"Преподаватель {teacher} просит перенести занятие: сейчас {from_slot}, хочет {to_slot}.\n"
        "1. semesters — возьми семестр и утверждённый вариант.\n"
        f"2. find_teacher — найди «{teacher}», затем schedule(who=teacher) — убедись, что занятие там есть.\n"
        "3. Спроси диспетчера, жёсткая ли просьба. Мягкая — set_teacher_wish: старый слот −2, новый +2. "
        "Жёсткая — pin_lesson на новый слот.\n"
        "4. check_move — заранее оцени перенос; check_semester — нет ли противоречий.\n"
        "5. start_rebuild от утверждённого варианта; run_status, пока не «готово».\n"
        "6. compare_variants для нового варианта: скажи, сколько пар сдвинулось, у кого сменилась аудитория,\n"
        "   какие группы и преподаватели затронуты. Утверждает диспетчер сам."
    )
