"""БД ↔ ядро: сборка задачи, загрузка синтетики, сохранение и чтение вариантов."""

import datetime as dt

import pytest
from django.core.management import call_command

from scheduler.precheck import has_errors, precheck
from scheduler.serialization import problem_from_dict, problem_to_dict
from scheduler.solver import SolveParams, Status
from scheduler.validator import validate
from timetable import models as m
from timetable.services.problem import ProblemBuildError, build_problem, create_semester
from timetable.services.variants import load_variant, profile_from_preset, run_and_save, save_variant

pytestmark = pytest.mark.django_db


def test_roundtrip_preserves_semantics(synthetic):
    """Задача → БД → задача: посаженное расписание даёт те же штрафы и 0 нарушений."""
    sem, ids, planted, original = synthetic
    built = build_problem(sem)
    assert len(built.items) == len(original.items)
    assert len(built.blocks) == len(original.blocks)
    assert len(built.pins) == len(original.pins)
    r0 = validate(original, planted)
    r1 = validate(built, ids.schedule(planted))
    assert r1.ok, r1.summary()
    assert r1.breakdown == r0.breakdown


def test_build_problem_query_count_is_constant(synthetic, django_assert_max_num_queries):
    sem = synthetic[0]
    with django_assert_max_num_queries(25):
        build_problem(sem)


def test_snapshot_json_roundtrip(synthetic):
    p = build_problem(synthetic[0])
    d = problem_to_dict(p)
    assert problem_to_dict(problem_from_dict(d)) == d


def test_create_semester_defaults():
    sem = create_semester("Осень", dt.date(2026, 9, 1))
    assert sem.pairs.count() == 7
    assert sem.courses.count() == 4
    c1 = sem.courses.get(number=1)
    assert c1.max_per_day == 4 and c1.prefs["1-1"] == 1 and c1.prefs["1-7"] == -1
    with pytest.raises(ProblemBuildError):
        build_problem(m.Semester.objects.create(name="Пустой", start_date=dt.date(2026, 9, 1)))


def test_week_parity():
    sem = m.Semester(name="x", start_date=dt.date(2026, 9, 1), first_week="odd")  # вторник
    assert sem.week_parity(dt.date(2026, 9, 1)) == "odd"
    assert sem.week_parity(dt.date(2026, 9, 6)) == "odd"  # воскресенье той же недели
    assert sem.week_parity(dt.date(2026, 9, 7)) == "even"
    assert sem.week_parity(dt.date(2026, 9, 14)) == "odd"


def test_variant_save_load_and_independence_from_edits(synthetic):
    sem, ids, planted, _ = synthetic
    problem = build_problem(sem)
    sched = ids.schedule(planted)
    v = save_variant(sem, problem, sched, "Посаженное")
    assert v.assignments.count() == len(sched.assignments)
    # правка справочника после сохранения не ломает чтение варианта: снимок самодостаточен
    m.WorkloadItem.objects.filter(semester=sem).first().delete()
    p2, s2 = load_variant(v)
    assert validate(p2, s2).ok


def test_run_and_save_with_solver(synthetic):
    sem = synthetic[0]
    params = SolveParams(time_limit_s=8, workers=2, variants=1, profile=profile_from_preset(None))
    result, saved = run_and_save(sem, params)
    assert result.status in (Status.OPTIMAL, Status.FEASIBLE)
    assert len(saved) == 1
    p, s = load_variant(saved[0])
    rep = validate(p, s)
    assert rep.ok and rep.total == saved[0].total


def test_preset_mapping():
    preset = m.WeightPreset.objects.create(
        name="Тест",
        mode="lex",
        weights={"group_gaps": 99, "unknown": 1},
        pref_cost={"-1": 7},
        lex_levels=[["group_gaps"]],
    )
    prof = profile_from_preset(preset)
    assert prof.weights[next(c for c in prof.weights if c.value == "group_gaps")] == 99
    assert prof.pref_cost[-1] == 7
    assert prof.mode.value == "lex"


def test_commands(capsys):
    call_command("seed_synthetic", "--seed", "5", "--groups", "4", "--teachers", "10", "--name", "S5")
    sem = m.Semester.objects.get(name="S5")
    assert not has_errors(precheck(build_problem(sem)))
    call_command("init_roles")
    from django.contrib.auth.models import Group

    assert Group.objects.filter(name="Диспетчер").get().permissions.filter(codename="change_workloaditem").exists()
    call_command("create_semester", "Весна", "2027-02-09", "--saturday")
    assert m.Semester.objects.get(name="Весна").days == [1, 2, 3, 4, 5, 6]
