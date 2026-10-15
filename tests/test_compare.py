"""Сравнение расписаний: переносы, смены аудиторий, кого затронуло."""

from scheduler.compare import diff_schedules
from scheduler.domain import Assignment as A
from scheduler.domain import Schedule, Week

from .helpers import add_item, tiny


def _problem():
    p = tiny(days=[1, 2, 3, 4])
    add_item(p, "i1", n=2)
    add_item(p, "i2", n=2)
    return p


def test_identical_schedules_have_no_changes():
    p = _problem()
    s = Schedule([A("i1", Week.BOTH, "1-1", None), A("i2", Week.BOTH, "2-1", None)])
    d = diff_schedules(p, s, p, s)
    assert d.same and d.moved == 0 and d.room_changed == 0


def test_move_and_room_change_are_counted_per_week():
    p = _problem()
    rooms = [r.id for r in p.rooms]
    old = Schedule([A("i1", Week.BOTH, "1-1", rooms[0]), A("i2", Week.BOTH, "2-1", rooms[0])])
    new = Schedule([A("i1", Week.BOTH, "4-6", rooms[0]), A("i2", Week.BOTH, "2-1", rooms[-1])])
    d = diff_schedules(p, old, p, new)
    kinds = {c.item_id: c.kind for c in d.changes}
    assert kinds == {"i1": "перенос", "i2": "аудитория"}
    assert d.moved == 2  # i1 каждую неделю = нечётная + чётная
    assert d.room_changed == 2
    i1 = next(c for c in d.changes if c.item_id == "i1")
    assert [(x.day, x.pair) for x in i1.before] == [(1, 1), (1, 1)]
    assert [(x.day, x.pair) for x in i1.after] == [(4, 6), (4, 6)]
    assert "6 пара" in i1.after[0].label()
    assert d.affected_groups and d.affected_teachers


def test_blinking_pair_moving_one_week_only():
    p = _problem()
    old = Schedule([A("i1", Week.ODD, "1-1", None), A("i1", Week.EVEN, "1-1", None)])
    new = Schedule([A("i1", Week.ODD, "1-1", None), A("i1", Week.EVEN, "3-2", None)])
    d = diff_schedules(p, old, p, new)
    assert d.moved == 1 and d.changes[0].kind == "перенос"


def test_added_and_removed_items():
    p = _problem()
    old = Schedule([A("i1", Week.BOTH, "1-1", None)])
    new = Schedule([A("i2", Week.BOTH, "1-1", None)])
    d = diff_schedules(p, old, p, new)
    assert {c.item_id: c.kind for c in d.changes} == {"i1": "убрано", "i2": "добавлено"}
    assert d.moved == 0
