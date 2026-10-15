"""Календарь .ics (RFC 5545) для преподавателя, группы или аудитории.

Каждое занятие — повторяющееся событие: еженедельное (RRULE WEEKLY) или раз в две недели
(INTERVAL=2, начиная с первой недели нужной чётности). Чётность недель — от даты начала семестра
и «первая неделя — нечётная/чётная». Время — Europe/Moscow (без перехода на летнее время,
поэтому VTIMEZONE простой). UID стабильные: при обновлении подписки календарь заменяет события,
а не дублирует их.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from scheduler.domain import Problem, Schedule, Week
from timetable.services import views as sv

TZID = "Europe/Moscow"
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:STANDARD",
    "DTSTART:20140101T000000",
    "TZOFFSETFROM:+0300",
    "TZOFFSETTO:+0300",
    "TZNAME:MSK",
    "END:STANDARD",
    "END:VTIMEZONE",
]
DEFAULT_WEEKS = 18


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """Перенос строк длиннее 75 байт (UTF-8 не разрываем посреди символа)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return [out[0], *(" " + x for x in out[1:])]


def first_date(start: dt.date, weekday: int, parity: Week, first_week: Week) -> dt.date:
    """Первая дата не раньше ``start`` с днём недели ``weekday`` (1 — пн) в неделю нужной чётности."""
    monday = start - dt.timedelta(days=start.weekday())
    for n in range(0, 4):
        d = monday + dt.timedelta(days=7 * n + weekday - 1)
        week_parity = first_week if n % 2 == 0 else (Week.EVEN if first_week is Week.ODD else Week.ODD)
        if d >= start and (parity is Week.BOTH or parity is week_parity):
            return d
    raise AssertionError("недостижимо")


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def schedule_ics(
    p: Problem,
    s: Schedule,
    by: str,
    key: str,
    *,
    start: dt.date,
    end: dt.date | None,
    first_week: Week,
    cal_name: str,
    uid_prefix: str,
) -> str:
    end = end or start + dt.timedelta(weeks=DEFAULT_WEEKS)
    until = dt.datetime.combine(end, dt.time(23, 59)) - dt.timedelta(hours=3)  # в UTC
    pairs = sorted(p.grid.pairs, key=lambda x: x.start_min)
    days = sorted(p.grid.days)
    odd = sv.grid(p, s, by, key, Week.ODD)
    even = sv.grid(p, s, by, key, Week.EVEN)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//schedule-service//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(cal_name)}",
        f"X-WR-TIMEZONE:{TZID}",
        *VTIMEZONE,
    ]

    def ident(e: sv.Entry) -> tuple[str, str, str, str]:
        return (e.discipline, e.teachers, e.groups, e.room)

    for pi, pt in enumerate(pairs):
        for di, day in enumerate(days):
            o = {ident(e): e for e in odd[pi].cells[di]}
            v = {ident(e): e for e in even[pi].cells[di]}
            for ik in dict.fromkeys([*o, *v]):
                week = Week.BOTH if ik in o and ik in v else (Week.ODD if ik in o else Week.EVEN)
                e = o.get(ik) or v[ik]
                d0 = first_date(start, day, week, first_week)
                if d0 > end:
                    continue
                t0, t1 = _hm(pt.start), _hm(pt.end)
                marks = [mk for mk in e.marks if mk not in ("мигающая", "через неделю")]
                desc = []
                if by != "teacher" and e.teachers:
                    desc.append(f"Преподаватель: {e.teachers}")
                if by != "group" and e.groups:
                    desc.append(f"Группы: {e.groups}")
                if week is not Week.BOTH:
                    desc.append("По " + ("нечётным" if week is Week.ODD else "чётным") + " неделям")
                if marks:
                    desc.append(", ".join(marks))
                uid_src = f"{uid_prefix}|{by}|{key}|{day}|{pt.number}|{week.value}|{'|'.join(ik)}"
                uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20]
                summary = e.discipline if by != "room" else f"{e.discipline} — {e.groups}"
                lines += [
                    "BEGIN:VEVENT",
                    f"UID:{uid}@schedule-service",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART;TZID={TZID}:{dt.datetime.combine(d0, t0):%Y%m%dT%H%M%S}",
                    f"DTEND;TZID={TZID}:{dt.datetime.combine(d0, t1):%Y%m%dT%H%M%S}",
                    f"RRULE:FREQ=WEEKLY;INTERVAL={1 if week is Week.BOTH else 2};UNTIL={until:%Y%m%dT%H%M%SZ}",
                    f"SUMMARY:{_esc(summary)}",
                    f"LOCATION:{_esc(e.room or ('онлайн' if 'онлайн' in ' '.join(e.marks) else ''))}",
                    f"DESCRIPTION:{_esc(chr(10).join(desc))}",
                    "END:VEVENT",
                ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"
