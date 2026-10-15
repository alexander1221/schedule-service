import datetime as dt

import pytest

from scheduler.domain import PairTime, Semester, TimeGrid, Week, WorkloadItem, weeks_of
from scheduler.generator import GenParams, generate
from scheduler.serialization import problem_from_dict, problem_to_dict, schedule_from_dict, schedule_to_dict

from .helpers import add_subgroups, tiny


def test_default_grid_is_mon_fri_7_pairs():
    g = TimeGrid()
    assert len(g.slots) == 35
    assert g.slots[0].id == "1-1" and g.slots[-1].id == "5-7"
    assert g.slot_by_id["1-4"].start_min == 15 * 60  # 15:00


def test_saturday_toggle_and_no_sunday():
    assert len(TimeGrid(days=[1, 2, 3, 4, 5, 6]).slots) == 42
    with pytest.raises(ValueError, match="Воскресенье"):
        TimeGrid(days=[1, 7])


def test_overlapping_pairs_rejected():
    with pytest.raises(ValueError, match="пересекаются"):
        TimeGrid(pairs=[PairTime(1, "09:00", "10:30"), PairTime(2, "10:00", "11:30")])


def test_semester_first_week_must_be_concrete():
    with pytest.raises(ValueError):
        Semester(first_week=Week.BOTH, start_date=dt.date(2026, 9, 1))


def test_weekly_and_blink_split():
    it = WorkloadItem("x", "d", ["t"], 3)
    assert (it.weekly_count, it.blink_count) == (1, 1)
    assert weeks_of(Week.BOTH) == (Week.ODD, Week.EVEN)


def test_group_cells_cartesian_product_of_partitions():
    p = tiny()
    assert p.group_cells("g1") == [()]
    add_subgroups(p, "g1", "lab")
    add_subgroups(p, "g1", "eng", ("x", "y", "z"))
    cells = p.group_cells("g1")
    assert len(cells) == 6  # 3 x 2
    assert all(len(c) == 2 for c in cells)


def test_problem_and_schedule_json_roundtrip():
    g = generate(GenParams(seed=7, n_groups=6, n_teachers=12, tightness=0.3))
    d = problem_to_dict(g.problem)
    p2 = problem_from_dict(d)
    assert problem_to_dict(p2) == d
    s = schedule_to_dict(g.planted)
    assert schedule_to_dict(schedule_from_dict(s)) == s
