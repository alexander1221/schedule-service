"""Правила 0.3.0 по ответам учебного отдела.

* «Через неделю» (кафедра кино): 1 пара в неделю → 2 пары подряд раз в две недели.
* Английский: блок на весь курс — все подгруппы и преподаватели в одно время.
* Дистант ПиУП с бронью аудитории преподавателю: студенты онлайн, аудитория занята.
* Преподаватель не должен приезжать ради одной пары (мягкий критерий).
"""

from scheduler.criteria import Crit, WeightProfile
from scheduler.domain import Assignment as A
from scheduler.domain import Room, Schedule, SyncBlock, Week, WeekPattern
from scheduler.precheck import precheck
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.validator import validate

from .helpers import add_item, reset, tiny

B, OD, EV = Week.BOTH, Week.ODD, Week.EVEN
ALT = {"week_pattern": WeekPattern.ALT_WEEK}


def solve(p, **kw):
    params = SolveParams(**{"time_limit_s": 5, "workers": 4, **kw})
    res = CpSatSolver().solve(p, params)
    for v in res.variants:
        rep = validate(p, v.schedule, params.profile, params.reference)
        assert rep.ok, rep.summary()
    return res


def codes(p, assignments):
    return {v.code for v in validate(p, Schedule(assignments)).hard}


# ------------------------------------------------------------------ «через неделю»


def test_alt_week_counts():
    p = tiny()
    it = add_item(p, "k", n=2, **ALT)
    assert (it.weekly_count, it.blink_count, it.consecutive) == (0, 2, True)


def test_alt_week_validator():
    p = tiny()
    add_item(p, "k", n=2, **ALT)
    assert codes(p, [A("k", OD, "1-3", "r1"), A("k", OD, "1-4", "r1")]) == set()
    assert "alt_week" in codes(p, [A("k", OD, "1-3", "r1"), A("k", EV, "1-4", "r1")])
    assert "alt_week" in codes(p, [A("k", B, "1-3", "r1")])
    assert "consecutive" in codes(p, [A("k", OD, "1-3", "r1"), A("k", OD, "1-5", "r1")])


def test_double_is_now_checked_by_validator():
    p = tiny()
    add_item(p, "d", n=4, double=True)
    assert "consecutive" in codes(p, [A("d", B, "1-1", "r1"), A("d", B, "1-3", "r1")])
    assert codes(p, [A("d", B, "1-1", "r1"), A("d", B, "1-2", "r1")]) == set()


def test_alt_week_solver_two_pairs_in_a_row_one_week():
    p = tiny(days=[1, 2])
    add_item(p, "k", n=2, **ALT)
    res = solve(p)
    assert res.status is Status.OPTIMAL
    got = res.variants[0].schedule.by_item()["k"]
    assert len(got) == 2
    assert len({a.week for a in got}) == 1 and got[0].week is not B
    pairs = sorted(int(a.slot_id.split("-")[1]) for a in got)
    assert {a.slot_id.split("-")[0] for a in got} == {got[0].slot_id.split("-")[0]}
    assert pairs[1] - pairs[0] == 1


def test_alt_week_two_teachers_share_slots_in_opposite_weeks():
    """Две дисциплины «через неделю» у одной группы, а свободны только две соседние пары —
    решатель обязан развести их по разным неделям."""
    p = tiny(n_teachers=2)
    p.courses[0].prefs.update({f"1-{i}": -2 for i in (1, 2, 5, 6, 7)})  # свободны пары 3–4
    add_item(p, "k1", teacher="t1", n=2, **ALT)
    add_item(p, "k2", teacher="t2", n=2, disc="d2", **ALT)
    res = solve(p)
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    by = res.variants[0].schedule.by_item()
    assert {by["k1"][0].week, by["k2"][0].week} == {OD, EV}


def test_alt_week_precheck():
    p = tiny()
    add_item(p, "k", n=3, **ALT)
    assert any(f.code == "alt_odd" for f in precheck(p))
    p = tiny()
    p.teachers[0].prefs.update({"1-2": -2, "1-4": -2, "1-6": -2})  # нет двух соседних свободных
    add_item(p, "k", n=2, **ALT)
    assert any(f.code == "alt_no_pair" for f in precheck(p))


def test_alt_week_candidate_hint():
    p = tiny(n_groups=2, n_teachers=2)
    add_item(p, "k1", teacher="t1", groups=["g1"], n=2, disc="d3")
    add_item(p, "k2", teacher="t1", groups=["g2"], n=2, disc="d3")
    add_item(p, "x", teacher="t2", groups=["g1"], n=4)
    hints = [f for f in precheck(p) if f.code == "alt_candidate"]
    assert len(hints) == 1 and "Преподаватель 1" in hints[0].message


# ------------------------------------------------------------------ английский: блок на весь курс


def _english_course(n_groups: int, levels: int, rooms: int):
    p = tiny(n_groups=n_groups, n_teachers=levels + 1, rooms=rooms, room_cap=25)
    total = sum(g.size for g in p.groups)
    ids = []
    for i in range(levels):
        it = add_item(
            p, f"en{i}", teacher=f"t{i + 1}", groups=[], disc="d2", n=2, headcount=-(-total // levels), label=f"L{i}"
        )
        ids.append(it.id)
    p.blocks.append(SyncBlock("en1", "Английский, 1 курс", [g.id for g in p.groups], ids))
    reset(p)
    return p


def test_course_wide_english_block_all_at_once():
    p = _english_course(n_groups=3, levels=4, rooms=4)
    add_item(p, "m", teacher=f"t{5}", groups=["g1"], n=2)
    res = solve(p)
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    by = res.variants[0].schedule.by_item()
    slots = {tuple(sorted((a.slot_id, a.week) for a in by[f"en{i}"])) for i in range(4)}
    assert len(slots) == 1  # все уровни в одно время
    m_slot = by["m"][0].slot_id
    assert all(a.slot_id != m_slot for i in range(4) for a in by[f"en{i}"])


def test_course_wide_block_needs_enough_rooms_at_once():
    p = _english_course(n_groups=3, levels=4, rooms=3)
    errs = {f.code: f.message for f in precheck(p) if f.level == "error"}
    assert "block_rooms" in errs and "4 аудиторий" in errs["block_rooms"]


def test_block_headcount_warning():
    p = _english_course(n_groups=3, levels=4, rooms=4)
    for it in p.items:
        it.headcount = 10
    assert any(f.code == "block_headcount" for f in precheck(p))


# ------------------------------------------------------------------ дистант с бронью аудитории


def test_online_with_room_booking_validator():
    p = tiny(n_groups=2, n_teachers=2, rooms=1)
    add_item(p, "dist", teacher="t1", groups=["g1"], online=True, teacher_in_room=True, room_type="reg")
    add_item(p, "inp", teacher="t2", groups=["g2"])
    assert "no_room" in codes(p, [A("dist", B, "1-1"), A("inp", B, "1-2", "r1")])
    assert "room_conflict" in codes(p, [A("dist", B, "1-1", "r1"), A("inp", B, "1-1", "r1")])
    assert codes(p, [A("dist", B, "1-1", "r1"), A("inp", B, "1-2", "r1")]) == set()


def test_room_booking_capacity_is_one_place():
    p = tiny(rooms=0)
    p.rooms = [Room("cab", "Кабинет", 2, "reg")]
    reset(p)
    add_item(p, "dist", online=True, teacher_in_room=True, room_type="reg")
    assert not [f for f in precheck(p) if f.level == "error"]
    res = solve(p)
    assert res.variants[0].schedule.assignments[0].room_id == "cab"


def test_online_with_booking_is_still_online_for_students():
    """Очно 3-й парой → дистант с бронью 4-й: студентам не успеть домой — штраф как за онлайн."""
    p = tiny(n_teachers=2)
    add_item(p, "inp", teacher="t1")
    add_item(p, "dist", teacher="t2", disc="d2", online=True, teacher_in_room=True, room_type="reg")
    r = validate(p, Schedule([A("inp", B, "1-3", "r1"), A("dist", B, "1-4", "r2")]))
    assert r.ok and r.breakdown[Crit.FMT_IN_TO_ONLINE] == 2


def test_solver_books_room_and_avoids_conflict():
    p = tiny(n_groups=2, n_teachers=2, rooms=1)
    p.courses[0].prefs.update({f"1-{i}": -2 for i in range(3, 8)})
    add_item(p, "dist", teacher="t1", groups=["g1"], online=True, teacher_in_room=True, room_type="reg")
    add_item(p, "inp", teacher="t2", groups=["g2"])
    res = solve(p)
    by = res.variants[0].schedule.by_item()
    assert by["dist"][0].room_id == "r1" and by["inp"][0].room_id == "r1"
    assert by["dist"][0].slot_id != by["inp"][0].slot_id  # одна аудитория — разные пары


def test_room_booking_candidate_hint():
    p = tiny(n_groups=2)
    add_item(p, "dist", groups=["g1"], online=True)
    add_item(p, "inp", groups=["g2"], disc="d2")
    assert any(f.code == "room_booking_candidate" for f in precheck(p))


# ------------------------------------------------------------------ приезд ради одной пары


def test_teacher_min_day_validator():
    p = tiny(days=[1, 2], n_groups=2)
    add_item(p, "a", groups=["g1"])
    add_item(p, "b", groups=["g2"])
    r = validate(p, Schedule([A("a", B, "1-1", "r1"), A("b", B, "2-1", "r1")]))
    assert r.breakdown[Crit.TEACHER_MIN_DAY] == 4  # 2 дня × 2 недели × недобор 1
    r = validate(p, Schedule([A("a", B, "1-1", "r1"), A("b", B, "1-2", "r1")]))
    assert r.breakdown[Crit.TEACHER_MIN_DAY] == 0


def test_teacher_min_day_ignores_online_from_home():
    p = tiny(days=[1, 2], n_groups=2)
    add_item(p, "a", groups=["g1"])
    add_item(p, "b", groups=["g2"], online=True)
    r = validate(p, Schedule([A("a", B, "1-1", "r1"), A("b", B, "1-2")]))
    assert r.breakdown[Crit.TEACHER_MIN_DAY] == 2  # на площадке одна пара; онлайн из дома не в счёт


def test_solver_groups_teacher_pairs_into_one_day():
    p = tiny(days=[1, 2, 3], n_groups=2)
    add_item(p, "a", groups=["g1"])
    add_item(p, "b", groups=["g2"])
    res = solve(p, profile=WeightProfile())
    by = res.variants[0].schedule.by_item()
    assert by["a"][0].slot_id.split("-")[0] == by["b"][0].slot_id.split("-")[0]
    assert res.variants[0].breakdown[Crit.TEACHER_MIN_DAY] == 0
