"""Юнит-тесты независимого валидатора: по одному ручному кейсу на правило."""

from scheduler.criteria import Crit
from scheduler.domain import Assignment as A
from scheduler.domain import Merge, Pin, Schedule, SyncBlock, Week
from scheduler.validator import validate

from .helpers import add_item, add_subgroups, reset, tiny

B, OD, E = Week.BOTH, Week.ODD, Week.EVEN


def codes(p, assignments, **kw):
    return {v.code for v in validate(p, Schedule(assignments), **kw).hard}


def test_clean_schedule_ok():
    p = tiny()
    add_item(p, "i1")
    assert codes(p, [A("i1", B, "1-1", "r1")]) == set()


def test_count_mismatch():
    p = tiny()
    add_item(p, "i1", n=3)
    assert "count" in codes(p, [A("i1", B, "1-1", "r1")])
    assert codes(p, [A("i1", B, "1-1", "r1"), A("i1", E, "1-2", "r1")]) == set()


def test_teacher_conflict_and_blink_sharing():
    p = tiny(n_groups=2)
    add_item(p, "i1", groups=["g1"], n=1)
    add_item(p, "i2", groups=["g2"], n=1)
    # обе мигающие в одном слоте, но в разные недели — можно
    assert codes(p, [A("i1", OD, "1-1", "r1"), A("i2", E, "1-1", "r2")]) == set()
    # в одну неделю — накладка у преподавателя
    assert "teacher_conflict" in codes(p, [A("i1", OD, "1-1", "r1"), A("i2", OD, "1-1", "r2")])


def test_teacher_forbidden_slot():
    p = tiny()
    p.teachers[0].prefs["1-1"] = -2
    add_item(p, "i1")
    assert "teacher_forbidden" in codes(p, [A("i1", B, "1-1", "r1")])


def test_subgroups_parallel_ok_but_conflict_with_group():
    p = tiny(n_teachers=3)
    add_subgroups(p, "g1", "lab")
    add_item(p, "a", teacher="t1", subgroups=["g1laba"])
    add_item(p, "b", teacher="t2", subgroups=["g1labb"])
    add_item(p, "w", teacher="t3", groups=["g1"])
    ok = [A("a", B, "1-1", "r1"), A("b", B, "1-1", "r2"), A("w", B, "1-2", "r1")]
    assert codes(p, ok) == set()
    bad = [A("a", B, "1-1", "r1"), A("b", B, "1-2", "r2"), A("w", B, "1-1", "r2")]
    assert "group_conflict" in codes(p, bad)


def test_subgroups_of_different_partitions_conflict():
    p = tiny(n_teachers=2)
    add_subgroups(p, "g1", "lab")
    add_subgroups(p, "g1", "eng")
    add_item(p, "a", teacher="t1", subgroups=["g1laba"])
    add_item(p, "b", teacher="t2", subgroups=["g1enga"])
    assert "group_conflict" in codes(p, [A("a", B, "1-1", "r1"), A("b", B, "1-1", "r2")])


def test_rooms_capacity_type_conflict():
    p = tiny(n_groups=2, n_teachers=2, room_cap=15)
    add_item(p, "i1", groups=["g1"])
    add_item(p, "i2", groups=["g2"], teacher="t2", room_type="lec")
    c = codes(p, [A("i1", B, "1-1", "r1"), A("i2", B, "1-1", "r1")])
    assert {"room_capacity", "room_type", "room_conflict"} <= c


def test_online_needs_no_room():
    p = tiny()
    add_item(p, "i1", online=True)
    assert codes(p, [A("i1", B, "1-1", None)]) == set()


def test_block_items_share_groups_and_must_be_synced():
    p = tiny(n_groups=2, n_teachers=3)
    for n, t in enumerate(("t1", "t2", "t3")):
        add_item(p, f"e{n}", teacher=t, groups=[], disc="d2", headcount=12)
    p.blocks.append(SyncBlock("b1", "Англ", ["g1", "g2"], ["e0", "e1", "e2"]))
    p.rooms.append(type(p.rooms[0])("r3", "103", 30, "reg"))
    reset(p)
    synced = [A(f"e{n}", B, "1-1", f"r{n + 1}") for n in range(3)]
    assert codes(p, synced) == set()
    desync = [*synced[:2], A("e2", B, "1-2", "r3")]
    assert "block_sync" in codes(p, desync)


def test_pin_must_be_honoured():
    p = tiny()
    add_item(p, "i1")
    p.pins.append(Pin("i1", B, "1-3"))
    assert "pin" in codes(p, [A("i1", B, "1-1", "r1")])
    assert codes(p, [A("i1", B, "1-3", "r1")]) == set()


def test_merge_allows_shared_teacher_and_room():
    p = tiny(n_groups=2, room_cap=50)
    add_item(p, "m1", groups=["g1"])
    add_item(p, "m2", groups=["g2"])
    p.merges.append(Merge("mg", ["m1", "m2"]))
    reset(p)
    rep = validate(p, Schedule([A("m1", B, "1-1", "r1"), A("m2", B, "1-1", "r1")]))
    assert rep.ok
    assert rep.breakdown[Crit.MERGE_USED] == 1


def test_in_person_to_online_uses_real_times():
    p = tiny(n_teachers=2)
    add_item(p, "inp", teacher="t1")
    add_item(p, "onl", teacher="t2", online=True, disc="d2")
    # 3-я очно (до 14:15) -> 4-я онлайн (15:00): 45 минут — нарушение
    r = validate(p, Schedule([A("inp", B, "1-3", "r1"), A("onl", B, "1-4")]))
    assert r.breakdown[Crit.FMT_IN_TO_ONLINE] == 2  # обе недели
    # 3-я очно -> 5-я онлайн (16:40): 145 минут — нормально (но есть окно)
    r = validate(p, Schedule([A("inp", B, "1-3", "r1"), A("onl", B, "1-5")]))
    assert r.breakdown[Crit.FMT_IN_TO_ONLINE] == 0
    assert r.breakdown[Crit.GROUP_GAPS] == 2


def test_online_to_in_person_and_mixed_day():
    p = tiny(n_teachers=2)
    add_item(p, "onl", teacher="t1", online=True)
    add_item(p, "inp", teacher="t2", disc="d2")
    r = validate(p, Schedule([A("onl", B, "1-1"), A("inp", B, "1-2", "r1")]))
    assert r.breakdown[Crit.FMT_ONLINE_TO_IN] == 2
    assert r.breakdown[Crit.FMT_MIXED_DAY] == 2


def test_course_day_limits_soft_and_hard():
    p = tiny(n_teachers=5)
    p.courses[0].limits.max_per_day = 2
    p.courses[0].limits.min_per_day = 2
    for n in range(3):
        add_item(p, f"i{n}", teacher=f"t{n + 1}", disc=["d1", "d2", "d3"][n])
    full = [A(f"i{n}", B, f"1-{n + 1}", "r1") for n in range(3)]
    r = validate(p, Schedule(full))
    assert r.ok and r.breakdown[Crit.COURSE_MAX_DAY] == 2
    p.courses[0].limits.max_per_day_hard = True
    assert "course_max_day" in codes(p, full)


def test_teacher_preferences_scored_per_week():
    p = tiny()
    p.teachers[0].prefs.update({"1-1": -1, "1-2": 2})
    add_item(p, "i1", n=3)
    r = validate(p, Schedule([A("i1", B, "1-1", "r1"), A("i1", OD, "1-2", "r1")]))
    assert r.breakdown[Crit.TEACHER_PREF] == 10 * 2 - 2  # «-1» дважды, «+2» один раз


def test_discipline_spread_per_cell():
    p = tiny(n_teachers=2)
    add_subgroups(p, "g1", "lab")
    # две подгруппы по очереди на одной дисциплине — у каждой по одной паре: штрафа нет
    add_item(p, "a", teacher="t1", subgroups=["g1laba"])
    add_item(p, "b", teacher="t1", subgroups=["g1labb"])
    r = validate(p, Schedule([A("a", B, "1-1", "r1"), A("b", B, "1-2", "r1")]))
    assert r.breakdown[Crit.DISCIPLINE_SPREAD] == 0


def test_changes_against_reference():
    p = tiny()
    add_item(p, "i1", n=4)
    ref = Schedule([A("i1", B, "1-1", "r1"), A("i1", B, "1-2", "r1")])
    new = Schedule([A("i1", B, "1-1", "r1"), A("i1", B, "1-3", "r1")])
    assert validate(p, new, reference=ref).breakdown[Crit.CHANGES] == 1
