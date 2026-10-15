"""Остановка расчёта по запросу (SolveParams.stop): быстро, найденное сохраняется."""

import threading
import time

from scheduler.generator import GenParams, generate
from scheduler.solver import CpSatSolver, SolveParams, Status
from scheduler.validator import validate


def _problem(**kw):
    return generate(GenParams(seed=2, n_groups=6, n_teachers=14, courses=2, n_blocks=1, n_merges=0, n_pins=0, **kw))


def test_stop_before_start_returns_cancelled():
    g = _problem()
    res = CpSatSolver().solve(g.problem, SolveParams(time_limit_s=30, workers=4, stop=lambda: True))
    assert res.status is Status.CANCELLED
    assert res.stopped and not res.variants
    assert res.diagnostics


def test_stop_after_first_variant_keeps_found():
    g = _problem()
    ev = threading.Event()

    def progress(pr):
        if pr.variant >= 2:
            ev.set()  # первый вариант готов — просим остановиться

    params = SolveParams(time_limit_s=5, workers=4, variants=3, min_diff=3, stop=ev.is_set)
    res = CpSatSolver().solve(g.problem, params, progress)
    assert res.stopped
    assert 1 <= len(res.variants) < 3
    for v in res.variants:
        assert validate(g.problem, v.schedule, params.profile).ok


def test_stop_interrupts_long_search():
    """Лимит 120 с, остановка через 2 с: расчёт заканчивается за секунды, а не за минуты."""
    g = generate(GenParams(seed=1, tightness=0.6))  # масштаб колледжа: оптимум за 2 с не доказать
    ev = threading.Event()
    threading.Timer(2.0, ev.set).start()
    t0 = time.monotonic()
    res = CpSatSolver().solve(g.problem, SolveParams(time_limit_s=120, workers=4, variants=2, stop=ev.is_set))
    assert time.monotonic() - t0 < 30
    assert res.stopped
    assert res.status in (Status.CANCELLED, Status.FEASIBLE, Status.OPTIMAL)
    assert len(res.variants) <= 1
    for v in res.variants:
        assert validate(g.problem, v.schedule).ok
