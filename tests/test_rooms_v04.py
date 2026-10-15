"""Аудитории 0.4.0: оборудование, личные предпочтения, «не наши» слоты, бронь в любую аудиторию,
отсечения этапа 2 и паросочетание."""

import pytest

from scheduler.criteria import Crit, WeightProfile
from scheduler.domain import Assignment as A
from scheduler.domain import Room, Schedule, SyncBlock, Week
from scheduler.matching import max_matching
from scheduler.precheck import precheck
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.solver import cpsat as cpsat_mod
from scheduler.solver.rooms import RoomAssignmentError
from scheduler.validator import validate

from .helpers import add_item, reset, tiny

B = Week.BOTH


def solve(p, **kw):
    params = SolveParams(**{"time_limit_s": 5, "workers": 4, **kw})
    res = CpSatSolver().solve(p, params)
    for v in res.variants:
        rep = validate(p, v.schedule, params.profile, params.reference)
        assert rep.ok, rep.summary()
    return res


def rooms_of(res, iid):
    return {a.room_id for a in res.variants[0].schedule.assignments if a.item_id == iid}


def equipped(p, spec):
    """spec: {room_id: [оборудование]} — заменить аудитории задачи."""
    p.rooms = [Room(rid, rid, 30, "reg", equipment=list(eq)) for rid, eq in spec.items()]
    reset(p)
    p.__dict__.pop("_blocked_sets", None)


# ------------------------------------------------------------------ паросочетание


def test_matching_perfect_and_hall_violation():
    assert max_matching([["a", "b"], ["a"]]).perfect
    res = max_matching([["a"], ["a"], ["a", "b"], ["c"]])
    assert not res.perfect and res.size == 3
    # минимальное нарушение Холла: двум занятиям годится только «a»
    assert res.neighbourhood == frozenset({"a"}) and res.deficient == [0, 1]


# ------------------------------------------------------------------ оборудование


def test_teacher_required_equipment_picks_right_room():
    p = tiny()
    equipped(p, {"plain": [], "proj": ["projector"]})
    p.teachers[0].required_equipment = ["projector"]
    add_item(p, "lec")
    res = solve(p)
    assert rooms_of(res, "lec") == {"proj"}


def test_validator_rejects_room_without_equipment():
    p = tiny()
    equipped(p, {"plain": [], "proj": ["projector"]})
    add_item(p, "lec", required_equipment=["projector"])
    codes = {v.code for v in validate(p, Schedule([A("lec", B, "1-1", "plain")])).hard}
    assert "room_equipment" in codes


def test_precheck_explains_missing_equipment():
    p = tiny()
    equipped(p, {"pc1": ["pc"], "pc2": ["pc", "projector"]})
    p.teachers[0].required_equipment = ["pc_rtx"]
    add_item(p, "gfx")
    errs = {f.code: f.message for f in precheck(p) if f.level == "error"}
    assert "room_equipment" in errs and "pc_rtx" in errs["room_equipment"]


def test_equipment_capacity_competition_two_teachers_one_rtx_room():
    """Два преподавателя, которым нужен RTX, а такой класс один — решатель разводит их по времени."""
    p = tiny(n_groups=2, n_teachers=2)
    equipped(p, {"rtx": ["pc", "pc_rtx"], "pc": ["pc"]})
    p.courses[0].prefs.update({f"1-{i}": -2 for i in range(3, 8)})
    for t in p.teachers:
        t.required_equipment = ["pc_rtx"]
    add_item(p, "a", teacher="t1", groups=["g1"])
    add_item(p, "b", teacher="t2", groups=["g2"], disc="d2")
    res = solve(p)
    s = res.variants[0].schedule.by_item()
    assert s["a"][0].slot_id != s["b"][0].slot_id
    assert rooms_of(res, "a") == rooms_of(res, "b") == {"rtx"}


# ------------------------------------------------------------------ предпочтения и занятость


def test_personal_room_preference_is_honoured_when_free():
    p = tiny()
    equipped(p, {"r1": [], "r2": [], "fav": []})
    p.teachers[0].preferred_rooms = ["fav"]
    add_item(p, "x", n=4)
    res = solve(p)
    assert rooms_of(res, "x") == {"fav"}
    assert res.variants[0].breakdown[Crit.ROOM_PREF] == 0


def test_room_pref_counted_by_validator():
    p = tiny()
    equipped(p, {"r1": [], "fav": []})
    p.teachers[0].preferred_rooms = ["fav"]
    add_item(p, "x")
    r = validate(p, Schedule([A("x", B, "1-1", "r1")]))
    assert r.ok and r.breakdown[Crit.ROOM_PREF] == 2  # обе недели


def test_blocked_room_slot_is_hard():
    p = tiny()
    equipped(p, {"only": []})
    p.rooms[0].blocked = [f"1-{i}" for i in range(1, 7)]  # свободна только 7-я пара
    p.__dict__.pop("_blocked_sets", None)
    add_item(p, "x")
    assert "room_blocked" in {v.code for v in validate(p, Schedule([A("x", B, "1-1", "only")])).hard}
    res = solve(p)
    assert res.variants[0].schedule.assignments[0].slot_id == "1-7"


def test_booking_without_room_type_takes_any_room():
    p = tiny(n_groups=2, n_teachers=2, rooms=0)
    p.rooms = [Room("lab", "Лаб", 12, "lec")]  # единственная аудитория — другого типа
    reset(p)
    add_item(p, "dist", groups=["g1"], online=True, teacher_in_room=True, room_type=None)
    assert p.items[0].room_type_id is None
    assert not [f for f in precheck(p) if f.level == "error"]
    res = solve(p)
    assert rooms_of(res, "dist") == {"lab"}


def test_block_rooms_precheck_uses_equipment():
    p = tiny(n_groups=2, n_teachers=3, rooms=0)
    equipped(p, {"a": ["pc"], "b": [], "c": []})
    ids = []
    for i in range(3):
        it = add_item(
            p,
            f"en{i}",
            teacher=f"t{i + 1}",
            groups=[],
            disc="d2",
            headcount=10,
            required_equipment=["pc"] if i < 2 else [],
        )
        ids.append(it.id)
    p.blocks.append(SyncBlock("b", "Англ", ["g1", "g2"], ids))
    reset(p)
    errs = {f.code for f in precheck(p) if f.level == "error"}
    assert "block_rooms" in errs  # двум подгруппам нужен класс с компьютерами, а он один


# ------------------------------------------------------------------ отсечения этапа 2


def test_stage2_cut_loop_resolves(monkeypatch):
    """Если этап 2 не смог назначить аудитории, решатель добавляет отсечение и решает снова."""
    p = tiny(n_groups=2, n_teachers=2)
    equipped(p, {"r1": [], "r2": []})
    add_item(p, "a", teacher="t1", groups=["g1"])
    add_item(p, "b", teacher="t2", groups=["g2"], disc="d2")
    real = cpsat_mod.assign_rooms
    calls = {"n": 0}

    def flaky(problem, assignments, profile=None, reference=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RoomAssignmentError("тест", "1-1", cut=frozenset({"r1"}))
        return real(problem, assignments, profile, reference)

    monkeypatch.setattr(cpsat_mod, "assign_rooms", flaky)
    res = solve(p)
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE)
    assert calls["n"] == 2
    assert res.variants[0].schedule.meta["room_cuts"] == 1


@pytest.mark.parametrize("seed", [1, 2])
def test_model_accepts_planted_with_equipment(seed):
    """Обратный контур на синтетике с оборудованием, «не нашими» слотами и бронью."""
    from ortools.sat.python import cp_model

    from scheduler.generator import GenParams, generate
    from scheduler.solver.cpsat import _Builder

    g = generate(GenParams(seed=seed, tightness=0.8))
    assert validate(g.problem, g.planted).ok
    b = _Builder(g.problem, WeightProfile(), g.planted)
    b.build()
    s = cp_model.CpSolver()
    s.parameters.fix_variables_to_their_hinted_value = True
    s.parameters.max_time_in_seconds = 30
    s.parameters.num_workers = 4
    assert s.solve(b.m) in (cp_model.OPTIMAL, cp_model.FEASIBLE)


def test_capacity_can_be_ignored():
    """«Посещаемость не 100%»: при выключенной вместимости поток двух групп идёт в маленькую аудиторию."""
    from .helpers import tiny as _tiny

    p = _tiny(n_groups=2, n_teachers=1, room_cap=15)
    add_item(p, "s", groups=["g1", "g2"])
    sched = Schedule([A("s", B, "1-1", "r1")])
    assert not p.eligible_rooms([p.item_by_id["s"]])
    assert any(v.code == "room_capacity" for v in validate(p, sched).hard)
    p.policy.check_capacity = False
    assert p.eligible_rooms([p.item_by_id["s"]])
    assert validate(p, sched).ok
    assert not [f for f in precheck(p) if f.level == "error"]
    assert solve(p, variants=1).variants


def test_mixed_format_block_counts_formats_like_validator():
    """Блок английского, где одна подгруппа онлайн, а остальные очно: для группы этот слот и очный,
    и онлайн. Модель должна видеть онлайн-часть (раньше считала блок целиком очным и занижала штраф)."""
    from scheduler.solver.cpsat import _Builder
    from scheduler.solver.rooms import assign_rooms

    p = tiny(n_groups=1, n_teachers=3)
    add_item(p, "en_on", teacher="t1", groups=[], disc="d2", online=True, headcount=10)
    add_item(p, "en_in", teacher="t2", groups=[], disc="d2", headcount=10)
    add_item(p, "x", teacher="t3", groups=["g1"], n=4)
    p.blocks.append(SyncBlock("b", "Англ", ["g1"], ["en_on", "en_in"]))
    reset(p)
    prof = WeightProfile()
    b = _Builder(p, prof, None)
    b.build()
    st, s = b.run(cpsat_mod.SolveParams(time_limit_s=20, workers=4), 0, None)
    assert st.name == "OPTIMAL"
    sched = Schedule(assign_rooms(p, b.extract(s)))
    rep = validate(p, sched, prof)
    assert rep.ok, rep.summary()
    model = {c: sum(s.value(e) if not isinstance(e, int) else e for e in b.crit.get(c, [])) for c in Crit}
    for c in (Crit.FMT_IN_TO_ONLINE, Crit.FMT_ONLINE_TO_IN, Crit.FMT_MIXED_DAY):
        assert model[c] == rep.breakdown[c], (c, model[c], rep.breakdown[c])
    assert rep.breakdown[Crit.FMT_MIXED_DAY] > 0  # смешанный слот виден обоим
