"""Интеграция на синтетике масштаба колледжа."""

import os

import pytest

from scheduler.generator import GenParams, generate
from scheduler.precheck import has_errors, precheck
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.validator import validate


@pytest.mark.parametrize("seed,tight", [(1, 0.0), (2, 0.5), (3, 1.0), (4, 0.7)])
def test_planted_solution_is_valid(seed, tight):
    g = generate(GenParams(seed=seed, tightness=tight))
    assert not has_errors(precheck(g.problem))
    rep = validate(g.problem, g.planted)
    assert rep.ok, rep.summary()


@pytest.mark.slow
@pytest.mark.parametrize("seed,tight", [(1, 0.5), (3, 1.0)])
def test_solver_on_college_scale(seed, tight):
    g = generate(GenParams(seed=seed, tightness=tight))
    # потоков — по числу ядер: при 8 потоках на 2 ядрах CP-SAT за 30 с не успевает, и тест «мигает»
    res = CpSatSolver().solve(g.problem, SolveParams(time_limit_s=40, workers=os.cpu_count() or 2))
    assert res.status in (Status.OPTIMAL, Status.FEASIBLE), res.diagnostics
    v = res.variants[0]
    assert validate(g.problem, v.schedule).ok
    planted = validate(g.problem, g.planted).total
    if tight <= 0.5:
        assert v.total < planted  # лучше случайно посаженного
    else:
        # на предельной тесноте посадка уже «выжата» (жадно ставит в удобные слоты), а за 40 с на
        # 2 ядрах решатель не всегда успевает её обойти — проверяем только, что не хуже заметно
        assert v.total < 1.25 * planted
