from scheduler.domain import SyncBlock
from scheduler.precheck import precheck

from .helpers import add_item, reset, tiny


def errors(p):
    return {f.code: f.message for f in precheck(p) if f.level == "error"}


def test_teacher_capacity_human_message():
    p = tiny(days=[1, 2, 3])
    for d in (1, 2, 3):
        p.teachers[0].prefs.update({f"{d}-{i}": -2 for i in range(3, 8)})  # 6 слотов в неделю
    add_item(p, "i1", n=8)
    add_item(p, "i2", n=8, disc="d2")
    e = errors(p)
    assert "teacher_capacity" in e
    assert "16 пар" in e["teacher_capacity"] and "по 6 в неделю" in e["teacher_capacity"]


def test_room_too_small():
    p = tiny(room_cap=10)
    add_item(p, "i1")
    assert "room_too_small" in errors(p)


def test_block_without_common_slot():
    p = tiny(n_groups=2, n_teachers=2)
    add_item(p, "e1", teacher="t1", groups=[], headcount=10)
    add_item(p, "e2", teacher="t2", groups=[], headcount=10)
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(1, 5)})
    p.teachers[1].prefs.update({f"1-{i}": -2 for i in range(4, 8)})
    p.blocks.append(SyncBlock("b", "Англ", ["g1", "g2"], ["e1", "e2"]))
    reset(p)
    assert "block_no_slot" in errors(p)


def test_block_same_teacher_twice():
    p = tiny(n_groups=1, n_teachers=1)
    add_item(p, "e1", groups=[], headcount=10)
    add_item(p, "e2", groups=[], headcount=10)
    p.blocks.append(SyncBlock("b", "Англ", ["g1"], ["e1", "e2"]))
    reset(p)
    assert "block_teacher" in errors(p)


def test_clean_problem_has_no_errors():
    p = tiny()
    add_item(p, "i1")
    assert errors(p) == {}
