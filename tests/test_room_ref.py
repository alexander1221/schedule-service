"""Пересборка держит прежние аудитории (Crit.ROOM_REF): этап 2 и валидатор считают одно и то же."""

import pytest

from scheduler.criteria import ROOM_STAGE_CRITS, Crit, WeightProfile
from scheduler.domain import Assignment, Room, Schedule, Week
from scheduler.generator import GenParams, generate
from scheduler.solver import CpSatSolver, SolveParams
from scheduler.solver.rooms import assign_rooms
from scheduler.validator import validate

from .helpers import add_item, tiny


def _strip(s: Schedule) -> list[Assignment]:
    return [Assignment(a.item_id, a.week, a.slot_id, None, a.teacher_remote) for a in s.assignments]


def test_room_ref_is_room_stage_criterion():
    assert Crit.ROOM_REF in ROOM_STAGE_CRITS
    assert WeightProfile().weight(Crit.ROOM_REF) > WeightProfile().weight(Crit.ROOM_PREF)


def test_validator_counts_room_ref_per_week_only_for_same_time():
    p = tiny(days=[1, 2], rooms=3)
    add_item(p, "i1", n=2)  # каждую неделю
    add_item(p, "i2", n=2)
    ref = Schedule([Assignment("i1", Week.BOTH, "1-1", "r1"), Assignment("i2", Week.BOTH, "1-2", "r1")])
    new = Schedule([Assignment("i1", Week.BOTH, "1-1", "r2"), Assignment("i2", Week.BOTH, "2-2", "r3")])
    rep = validate(p, new, reference=ref)
    assert rep.breakdown[Crit.ROOM_REF] == 2  # i1: та же пара, другая аудитория, обе недели; i2 сдвинут — не в счёт
    assert rep.breakdown[Crit.CHANGES] == 1
    assert any("r1" in i.message or "101" in i.message for i in rep.issues if i.crit is Crit.ROOM_REF)
    assert validate(p, new).breakdown[Crit.ROOM_REF] == 0  # без эталона критерия нет


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_room_stage_reproduces_reference_rooms(seed):
    """Те же слоты + эталон → этап 2 ставит каждое занятие в его прежнюю аудиторию (0 по ROOM_REF)."""
    g = generate(GenParams(seed=seed, n_groups=8, n_teachers=16, courses=2, n_pins=0, tightness=0.6))
    p, ref = g.problem, g.planted
    prof = WeightProfile(weights={**WeightProfile().weights, Crit.ROOM_REF: 1000})
    got = Schedule(assign_rooms(p, _strip(ref), prof, reference=ref))
    rep = validate(p, got, prof, reference=ref)
    assert rep.ok, rep.summary()
    assert rep.breakdown[Crit.ROOM_REF] == 0
    assert rep.breakdown[Crit.CHANGES] == 0
    # без эталона аудитории выбираются заново — и обычно расходятся с посаженными
    free = validate(p, Schedule(assign_rooms(p, _strip(ref), prof)), prof, reference=ref)
    assert free.breakdown[Crit.ROOM_REF] > 0


def test_unavailable_reference_room_is_counted_and_replaced():
    """Прежняя аудитория теперь занята «не нами» → занятие в другой, валидатор это и показывает."""
    p = tiny(days=[1], rooms=2)
    add_item(p, "i1", n=2)
    ref = Schedule([Assignment("i1", Week.BOTH, "1-1", "r1")])
    p.rooms[0] = Room("r1", "101", 30, "reg", blocked=["1-1"])
    got = Schedule(assign_rooms(p, _strip(ref), reference=ref))
    assert got.assignments[0].room_id == "r2"
    assert validate(p, got, reference=ref).breakdown[Crit.ROOM_REF] == 2


def test_rebuild_keeps_rooms_end_to_end():
    """Пересборка после одного нового запрета: почти все занятия остаются в своих аудиториях."""
    g = generate(GenParams(seed=4, n_groups=8, n_teachers=16, courses=2, n_pins=0, tightness=0.5))
    p = g.problem
    params = SolveParams(time_limit_s=10, workers=4)
    first = CpSatSolver().solve(p, params).variants[0].schedule
    # преподаватель больше не может в один из своих слотов
    a = next(x for x in first.assignments if p.item_by_id[x.item_id].teacher_ids)
    p.teacher_by_id[p.item_by_id[a.item_id].teacher_ids[0]].prefs[a.slot_id] = -2
    res = CpSatSolver().solve(p, SolveParams(time_limit_s=10, workers=4, reference=first))
    v = res.variants[0]
    rep = validate(p, v.schedule, params.profile, first)
    assert rep.ok
    rooms_total = sum(2 if x.week is Week.BOTH else 1 for x in first.assignments if x.room_id)
    # раньше аудитории раздавались заново (сотни смен на колледже); теперь — единицы
    assert rep.breakdown[Crit.ROOM_REF] <= max(6, rooms_total // 20), rep.breakdown[Crit.ROOM_REF]
    assert v.breakdown[Crit.ROOM_REF] == rep.breakdown[Crit.ROOM_REF]  # решатель отдал разбивку валидатора
