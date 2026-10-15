"""Площадка закрывается раньше конца последней пары (ВДНХ — в 21:00, 7-я пара 20:00–21:30)."""

from scheduler.domain import Assignment as A
from scheduler.domain import Schedule, Week
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.validator import validate

from .helpers import add_item, tiny

B = Week.BOTH
CLOSE = 21 * 60


def _site(p):
    for r in p.rooms:
        r.closes_min = CLOSE
    return p


def _only_last_pair(p, teacher="t1"):
    t = next(t for t in p.teachers if t.id == teacher)
    t.prefs.update({f"1-{i}": -2 for i in range(1, 7)})  # свободен только на 7-й паре


def test_allowed_slots_and_validator():
    p = _site(tiny(n_teachers=3))
    strict = add_item(p, "a", teacher="t1")
    early = add_item(p, "club", teacher="t2", disc="d2", may_end_early=True)
    home = add_item(p, "on", teacher="t3", disc="d3", online=True)
    assert "1-7" not in {s.id for s in p.allowed_slots(strict)}
    assert "1-7" in {s.id for s in p.allowed_slots(early)}
    assert "1-7" in {s.id for s in p.allowed_slots(home)}  # онлайн из дома — здание ни при чём
    rep = validate(p, Schedule([A("a", B, "1-7", "r1"), A("club", B, "1-6", "r2"), A("on", B, "1-5", None)]))
    assert {v.code for v in rep.hard} == {"site_closed"}  # по разу на каждую неделю цикла
    rep = validate(p, Schedule([A("a", B, "1-1", "r1"), A("club", B, "1-7", "r2"), A("on", B, "1-5", None)]))
    assert rep.ok, rep.summary()


def test_booking_from_site_is_closed_too():
    """Дистант с бронью: преподаватель на площадке — после закрытия нельзя."""
    p = _site(tiny())
    it = add_item(p, "d", online=True, teacher_in_room=True)
    assert "1-7" not in {s.id for s in p.allowed_slots(it)}


def test_solver_puts_late_only_flagged_items():
    p = _site(tiny(n_teachers=2))
    _only_last_pair(p, "t1")
    _only_last_pair(p, "t2")
    add_item(p, "club", teacher="t1", may_end_early=True)
    res = CpSatSolver().solve(p, SolveParams(time_limit_s=5, workers=2))
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    assert {a.slot_id for a in res.variants[0].schedule.assignments} == {"1-7"}
    add_item(p, "a", teacher="t2", disc="d2")  # обычное очное занятие тоже только на 7-й паре
    res = CpSatSolver().solve(p, SolveParams(time_limit_s=5, workers=2))
    assert res.status not in (Status.OPTIMAL, Status.FEASIBLE)


def test_other_site_without_closing_is_fine():
    p = tiny(n_teachers=1, rooms=2)
    p.rooms[0].closes_min = CLOSE  # ВДНХ
    _only_last_pair(p)
    add_item(p, "a")
    res = CpSatSolver().solve(p, SolveParams(time_limit_s=5, workers=2))
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    assert {a.room_id for a in res.variants[0].schedule.assignments} == {p.rooms[1].id}
