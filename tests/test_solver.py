"""Решатель на маленьких ручных кейсах. Каждый результат проверяется валидатором."""

import pytest

from scheduler.criteria import Crit, Mode, WeightProfile
from scheduler.domain import Assignment as A
from scheduler.domain import Merge, Pin, Room, Schedule, SyncBlock, Week
from scheduler.precheck import precheck
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.solver.cpsat import _Builder
from scheduler.validator import validate

from .helpers import add_item, add_subgroups, reset, tiny

FAST = dict(time_limit_s=5, workers=4)


def solve(p, **kw):
    params = SolveParams(**{**FAST, **kw})
    res = CpSatSolver().solve(p, params)
    for v in res.variants:
        rep = validate(p, v.schedule, params.profile, params.reference)
        assert rep.ok, rep.summary()
    return res


def slots_of(res, iid, v=0):
    return sorted((a.slot_id, a.week.value) for a in res.variants[v].schedule.assignments if a.item_id == iid)


def test_places_all_and_avoids_forbidden():
    p = tiny()
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(1, 7)})  # свободна только 7-я пара
    add_item(p, "i1", n=2)
    res = solve(p)
    assert res.status is Status.OPTIMAL
    assert slots_of(res, "i1") == [("1-7", "both")]


def test_two_blinking_pairs_share_one_slot():
    p = tiny(n_groups=2)
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(2, 8)})  # у преподавателя только 1-я пара
    add_item(p, "a", groups=["g1"], n=1)
    add_item(p, "b", groups=["g2"], n=1)
    res = solve(p)
    weeks = {w for (_, w) in slots_of(res, "a") + slots_of(res, "b")}
    assert weeks == {"odd", "even"}


def test_subgroups_go_in_parallel_when_needed():
    p = tiny(n_teachers=2)
    add_subgroups(p, "g1", "lab")
    p.courses[0].prefs.update({f"1-{i}": -2 for i in range(2, 8)})  # группе доступна одна пара
    add_item(p, "a", teacher="t1", subgroups=["g1laba"])
    add_item(p, "b", teacher="t2", subgroups=["g1labb"])
    res = solve(p)
    assert slots_of(res, "a") == slots_of(res, "b") == [("1-1", "both")]


def test_sync_block_in_one_slot():
    p = tiny(n_groups=2, n_teachers=3, rooms=3)
    for n, t in enumerate(("t1", "t2", "t3")):
        add_item(p, f"e{n}", teacher=t, groups=[], disc="d2", headcount=13, n=4)
    p.blocks.append(SyncBlock("b1", "Англ", ["g1", "g2"], ["e0", "e1", "e2"]))
    reset(p)
    res = solve(p)
    assert slots_of(res, "e0") == slots_of(res, "e1") == slots_of(res, "e2")
    rooms = {a.room_id for a in res.variants[0].schedule.assignments if a.slot_id == slots_of(res, "e0")[0][0]}
    assert len(rooms) == 3


def test_merge_used_only_when_necessary():
    p = tiny(n_groups=2, rooms=1, room_cap=50)
    add_item(p, "m1", groups=["g1"])
    add_item(p, "m2", groups=["g2"])
    p.merges.append(Merge("mg", ["m1", "m2"]))
    reset(p)
    res = solve(p)
    assert res.variants[0].breakdown[Crit.MERGE_USED] == 0  # места хватает — не объединяем
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(2, 8)})  # у преподавателя одна пара
    res = solve(p)
    assert res.variants[0].breakdown[Crit.MERGE_USED] == 1
    assert slots_of(res, "m1") == slots_of(res, "m2")


def test_avoids_in_person_to_online_without_gap():
    p = tiny(n_teachers=2)
    p.courses[0].prefs.update({"1-1": -2, "1-2": -2, "1-6": -2, "1-7": -2})  # окно 3..5
    add_item(p, "inp", teacher="t1", n=2)
    add_item(p, "onl", teacher="t2", n=2, online=True, disc="d2")
    p.teachers[0].prefs["1-4"] = -2
    p.teachers[0].prefs["1-5"] = -2  # очная пара может быть только 3-й
    res = solve(p)
    assert slots_of(res, "inp") == [("1-3", "both")]
    assert slots_of(res, "onl") == [("1-5", "both")]  # не 4-я: 45 минут — не успеть домой


def test_pin_respected():
    p = tiny()
    add_item(p, "i1", n=2)
    p.pins.append(Pin("i1", Week.BOTH, "1-6"))
    res = solve(p)
    assert slots_of(res, "i1") == [("1-6", "both")]


def test_double_pair_is_consecutive():
    p = tiny()
    add_item(p, "i1", n=4, double=True)
    res = solve(p)
    s = slots_of(res, "i1")
    pairs = sorted(int(x.split("-")[1]) for x, _ in s)
    assert pairs[1] - pairs[0] == 1


def test_variants_differ_by_k():
    p = tiny(days=[1, 2], n_groups=2, n_teachers=2)
    for g in ("g1", "g2"):
        for n, d in enumerate(("d1", "d2")):
            add_item(p, f"{g}{d}", teacher=f"t{n + 1}", groups=[g], disc=d, n=2)
    res = solve(p, variants=3, min_diff=2)
    assert len(res.variants) == 3
    sets = [{(a.item_id, a.slot_id, a.week) for a in v.schedule.assignments} for v in res.variants]
    for i in range(3):
        for j in range(i):
            assert len(sets[i] - sets[j]) >= 2
    assert res.variants[0].total <= res.variants[1].total


def test_minimal_changes_keeps_reference():
    p = tiny(days=[1, 2], n_groups=2, n_teachers=2)
    for g in ("g1", "g2"):
        add_item(p, f"{g}x", teacher="t1", groups=[g], n=2)
        add_item(p, f"{g}y", teacher="t2", groups=[g], disc="d2", n=2)
    ref = solve(p).variants[0].schedule
    p.teachers[0].prefs.update({"2-7": 1})  # мелкое изменение предпочтений
    res = solve(p, reference=ref)
    assert res.variants[0].breakdown[Crit.CHANGES] == 0


def test_lexicographic_mode():
    p = tiny(n_teachers=2)
    add_item(p, "a", teacher="t1")
    add_item(p, "b", teacher="t2", disc="d2")
    prof = WeightProfile(mode=Mode.LEXICOGRAPHIC)
    res = solve(p, profile=prof)
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    assert res.variants[0].breakdown[Crit.GROUP_GAPS] == 0


def test_infeasible_is_explained_by_precheck():
    p = tiny()
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(4, 8)})  # 3 слота
    add_item(p, "i1", n=8)  # 4 еженедельные пары
    res = solve(p)
    assert res.status is Status.PRECHECK_FAILED
    assert any("Преподаватель 1" in d and "8 пар" in d for d in res.diagnostics)


def test_infeasible_core_names_the_conflict():
    """Precheck пропускает (каждый по отдельности помещается), а вместе — нет."""
    p = tiny(n_groups=2, n_teachers=2)
    for t in p.teachers:
        t.prefs.update({f"1-{i}": -2 for i in range(3, 8)})  # оба свободны на 1-2 парах
    add_item(p, "x", teacher="t1", groups=["g1"], n=2)
    add_item(p, "y", teacher="t2", groups=["g1"], n=2)
    add_item(p, "z", teacher="t1", groups=["g2"], n=2)
    add_item(p, "q", teacher="t2", groups=["g2"], n=2, disc="d2")
    p.pins.append(Pin("x", Week.BOTH, "1-1"))
    p.pins.append(Pin("q", Week.BOTH, "1-2"))
    # x(t1,g1)@1, q(t2,g2)@2 => y(t2,g1) должна быть @2 — занят t2; противоречие
    assert not [f for f in precheck(p) if f.level == "error"]
    res = solve(p)
    assert res.status is Status.INFEASIBLE
    text = "\n".join(res.diagnostics)
    assert "закрепление" in text
    assert len(res.diagnostics) <= 6


@pytest.mark.parametrize(
    "kw",
    [
        dict(n_groups=3, n_teachers=6, courses=1, n_blocks=1, n_merges=0),
        dict(n_groups=2, n_teachers=5, courses=2, n_blocks=0, n_merges=0),
    ],
)
def test_model_objective_matches_validator(kw):
    """Индикаторы модели — нижние оценки, поэтому всегда модель ≥ валидатор;
    на задаче, решённой до доказанного оптимума, — строгое равенство по каждому критерию."""
    from scheduler.generator import GenParams, generate
    from scheduler.solver.rooms import assign_rooms

    g = generate(GenParams(seed=1, n_pins=0, **kw))
    p = g.problem
    prof = WeightProfile()
    b = _Builder(p, prof, None)
    b.build()
    st, s = b.run(SolveParams(time_limit_s=60, workers=8), 0, None)
    assert st.name in ("OPTIMAL", "FEASIBLE")
    sched = Schedule(assign_rooms(p, b.extract(s)))
    rep = validate(p, sched, prof)
    assert rep.ok, rep.summary()
    model = {c: sum(s.value(e) if not isinstance(e, int) else e for e in b.crit.get(c, [])) for c in Crit}
    for c in Crit:
        if c in (Crit.ROOM_CHANGES, Crit.ROOM_PREF):
            continue
        assert model[c] >= rep.breakdown[c], c
        if st.name == "OPTIMAL":
            assert model[c] == rep.breakdown[c], c


def test_room_packing_weekly_and_blinks():
    """3 аудитории на 5 занятий в одном слоте: еженедельная + 2×2 мигающих делят аудитории по неделям."""
    p = tiny(n_groups=5, n_teachers=5, rooms=0)
    p.rooms = [Room("big", "Б", 40, "reg"), Room("mid", "С", 25, "reg"), Room("mid2", "С2", 25, "reg")]
    for g in p.groups:
        g.size = 20
    p.groups[0].size = 38
    p.courses[0].prefs.update({f"1-{i}": -2 for i in range(2, 8)})
    reset(p)
    add_item(p, "w", teacher="t1", groups=["g1"], n=2)  # 38 человек, еженедельно
    add_item(p, "o1", teacher="t2", groups=["g2"], n=1)
    add_item(p, "e1", teacher="t3", groups=["g3"], n=1)
    add_item(p, "o2", teacher="t4", groups=["g4"], n=1)
    add_item(p, "e2", teacher="t5", groups=["g5"], n=1)
    res = solve(p)
    assert res.status is Status.OPTIMAL


def test_rebuild_from_reference_with_blocks_starts_from_reference():
    """Эталон с синхронным блоком (повтор подсказок по блоку) — пересборка стартует с эталона."""
    from scheduler.generator import GenParams, generate

    g = generate(GenParams(seed=2, n_groups=6, n_teachers=14, courses=2, n_blocks=1, n_merges=0, n_pins=0))
    res = CpSatSolver().solve(g.problem, SolveParams(time_limit_s=6, workers=4, reference=g.planted))
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    v = res.variants[0]
    # старт с нулём отличий; решатель отходит от эталона, только если это окупает штраф за изменение
    with_ref = validate(g.problem, v.schedule, reference=g.planted)
    planted_total = validate(g.problem, g.planted, reference=g.planted).total
    assert with_ref.total <= planted_total


def test_rebuild_keeps_merged_reference():
    """Эталон, где объединение использовано, допустим для модели и пересобирается без отличий."""
    p = tiny(n_groups=2, rooms=1, room_cap=50)
    add_item(p, "m1", groups=["g1"])
    add_item(p, "m2", groups=["g2"])
    p.merges.append(Merge("mg", ["m1", "m2"]))
    p.teachers[0].prefs.update({f"1-{i}": -2 for i in range(3, 8)})
    reset(p)
    ref = Schedule([A("m1", Week.BOTH, "1-2", "r1"), A("m2", Week.BOTH, "1-2", "r1")])
    assert validate(p, ref).ok
    res = solve(p, reference=ref, profile=WeightProfile(weights={Crit.CHANGES: 1000, Crit.MERGE_USED: 1}))
    assert res.variants[0].breakdown[Crit.CHANGES] == 0


@pytest.mark.parametrize("seed", [1, 4])
def test_model_accepts_every_valid_schedule(seed):
    """Обратная сторона защитного контура: если валидатор принимает расписание, модель CP-SAT
    обязана его допускать (иначе решатель «не видит» допустимые варианты)."""
    from ortools.sat.python import cp_model

    from scheduler.generator import GenParams, generate

    g = generate(GenParams(seed=seed, tightness=0.8))
    assert validate(g.problem, g.planted).ok
    b = _Builder(g.problem, WeightProfile(), g.planted)
    b.build()
    s = cp_model.CpSolver()
    s.parameters.fix_variables_to_their_hinted_value = True
    s.parameters.max_time_in_seconds = 30
    s.parameters.num_workers = 4
    assert s.solve(b.m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_model_accepts_its_own_solution_with_merges():
    from ortools.sat.python import cp_model

    p = tiny(days=[1, 2], n_groups=2, rooms=1, room_cap=50)
    add_item(p, "m1", groups=["g1"], n=3)
    add_item(p, "m2", groups=["g2"], n=3)
    add_item(p, "x", groups=["g1"], disc="d2", teacher="t1", n=2)
    p.merges.append(Merge("mg", ["m1", "m2"]))
    p.teachers[0].prefs.update({f"{d}-{i}": -2 for d in (1, 2) for i in range(4, 8)})
    reset(p)
    res = solve(p)
    sched = res.variants[0].schedule
    b = _Builder(p, WeightProfile(), sched)
    b.build()
    s = cp_model.CpSolver()
    s.parameters.fix_variables_to_their_hinted_value = True
    s.parameters.max_time_in_seconds = 10
    assert s.solve(b.m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
