"""Конструктор маленьких задач для юнит-тестов (ручные кейсы с известным ответом)."""

from __future__ import annotations

from scheduler.domain import (
    Course,
    Discipline,
    Format,
    Group,
    Problem,
    Room,
    RoomType,
    Semester,
    Subgroup,
    Teacher,
    TimeGrid,
    WorkloadItem,
)


def tiny(
    days: list[int] | None = None, n_groups: int = 1, n_teachers: int = 1, rooms: int = 2, room_cap: int = 30
) -> Problem:
    """Один курс без предпочтений и лимитов, группы по 20 человек, обычные аудитории."""
    grid = TimeGrid(days=days or [1])
    return Problem(
        semester=Semester(grid=grid),
        courses=[Course(1)],
        groups=[Group(f"g{i}", f"Г-{i}", 1, 20) for i in range(1, n_groups + 1)],
        teachers=[Teacher(f"t{i}", f"Преподаватель {i}") for i in range(1, n_teachers + 1)],
        disciplines=[Discipline("d1", "Математика", 3), Discipline("d2", "История"), Discipline("d3", "Физика", 2)],
        room_types=[RoomType("reg", "Обычная"), RoomType("lec", "Лекционная")],
        rooms=[Room(f"r{i}", f"{100 + i}", room_cap, "reg") for i in range(1, rooms + 1)],
        items=[],
    )


def add_item(
    p: Problem,
    iid: str,
    teacher: str = "t1",
    groups: list[str] | None = None,
    n: int = 2,
    disc: str = "d1",
    online: bool = False,
    subgroups: list[str] | None = None,
    room_type: str = "reg",
    **kw: object,
) -> WorkloadItem:
    it = WorkloadItem(
        id=iid,
        discipline_id=disc,
        teacher_ids=[teacher],
        pairs_per_cycle=n,
        group_ids=list(groups if groups is not None else ([] if subgroups else ["g1"])),
        subgroup_ids=list(subgroups or []),
        format=Format.ONLINE if online else Format.IN_PERSON,
        room_type_id=None if online and not kw.get("teacher_in_room") else room_type,
        **kw,  # type: ignore[arg-type]
    )
    p.items.append(it)
    _reset(p)
    return it


def add_subgroups(p: Problem, group: str, partition: str = "lab", names: tuple[str, ...] = ("a", "b")) -> None:
    for n in names:
        p.subgroups.append(Subgroup(f"{group}{partition}{n}", group, f"п/г {n}", 10, partition))
    _reset(p)


def _reset(p: Problem) -> None:
    """Сбросить кэши индексов после изменения задачи в тесте."""
    for key in list(p.__dict__):
        if key in {
            "course_by_num",
            "group_by_id",
            "subgroup_by_id",
            "teacher_by_id",
            "discipline_by_id",
            "room_by_id",
            "item_by_id",
            "block_of_item",
            "merge_of_item",
            "active_rooms",
        }:
            p.__dict__.pop(key)


reset = _reset
