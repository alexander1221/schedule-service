"""Календари .ics из утверждённых вариантов."""

from __future__ import annotations

import secrets

from scheduler.domain import Week
from timetable import models as m
from timetable.export.ics import schedule_ics
from timetable.services.variants import load_variant


def variant_ics(v: m.ScheduleVariant, by: str, key: str, cal_name: str) -> str:
    p, s = load_variant(v)
    sem = v.semester
    return schedule_ics(
        p,
        s,
        by,
        key,
        start=sem.start_date,
        end=sem.end_date,
        first_week=Week(sem.first_week),
        cal_name=cal_name,
        uid_prefix=f"sem{sem.pk}",
    )


def current_variant_for_teacher(teacher: m.Teacher) -> m.ScheduleVariant | None:
    """Утверждённый вариант самого свежего действующего семестра, где у преподавателя есть нагрузка."""
    sems = (
        m.Semester.objects.exclude(status=m.Semester.Status.ARCHIVED)
        .filter(items__teachers=teacher)
        .distinct()
        .order_by("-start_date")
    )
    for sem in sems:
        v = sem.variants.filter(status=m.ScheduleVariant.Status.APPROVED).order_by("-created_at").first()
        if v is not None:
            return v
    return None


def teacher_ics(teacher: m.Teacher) -> str | None:
    v = current_variant_for_teacher(teacher)
    if v is None:
        return None
    return variant_ics(v, "teacher", str(teacher.pk), f"Расписание: {teacher.name}")


def ensure_token(teacher: m.Teacher, reset: bool = False) -> str:
    if reset or not teacher.calendar_token:
        teacher.calendar_token = secrets.token_urlsafe(24)
        teacher.save(update_fields=["calendar_token"])
    assert teacher.calendar_token
    return teacher.calendar_token
