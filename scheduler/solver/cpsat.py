"""Решатель на Google OR-Tools CP-SAT (этап 1 — слоты, этап 2 — аудитории).

Модель
------
«Единица» (unit) — то, что получает слот целиком:
  * отдельный элемент нагрузки;
  * синхронный блок (все его элементы в одних и тех же слотах);
  * объединённое занятие допустимого объединения (альтернатива раздельным элементам).

Переменные единицы u:
  * ``w[u, s]``      — еженедельное вхождение в слоте s (обе недели), Σ = n // 2;
  * ``b[u, s, k]``   — мигающее вхождение в неделю k, Σ = n % 2.
Занятость ``o[u, s, k] = w[u, s] + b[u, s, k]``. Переменные не создаются для слотов
с «не могу» (-2) у преподавателей и групп.

Жёсткие ограничения: число вхождений; преподаватель ≤ 1 занятия в (слот, неделя);
«клетка» студентов группы ≤ 1 (подгруппы разных делений пересекаются);
агрегированные аудитории — для каждого типа и порога вместимости c: занятий
размера ≥ c не больше, чем аудиторий вместимостью ≥ c; закрепления; жёсткие
лимиты в день; сдвоенные пары.

Мягкие критерии — индикаторные переменные (см. criteria.Crit); целевая функция —
взвешенная сумма или лексикографическая цепочка. Итоговая разбивка штрафа для
диспетчера считается НЕЗАВИСИМЫМ валидатором по готовому расписанию.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from ..criteria import ROOM_STAGE_CRITS, Crit, Mode, WeightProfile
from ..domain import (
    CYCLE_WEEKS,
    DAY_NAMES,
    Assignment,
    Format,
    Merge,
    Problem,
    Schedule,
    Slot,
    SyncBlock,
    Week,
    WorkloadItem,
)
from ..precheck import has_errors, precheck
from ..validator import validate
from .base import Progress, ProgressCallback, SolveParams, SolveResult, Status, Variant
from .rooms import RoomAssignmentError, assign_rooms

#: Сколько раз этап 2 может вернуть отсечение этапу 1 для одного варианта.
MAX_ROOM_ROUNDS = 8

# Аннотации OR-Tools для линейных выражений перегружены numpy-типами и дают mypy
# сотни ложных срабатываний; внутри модели выражения типизируем как Any.
Lit = Any
Expr = Any


@dataclass
class Unit:
    id: str
    items: list[WorkloadItem]
    n: int  # пар за цикл
    teachers: list[str]  # с повторами, если блок (повтор ловит precheck)
    whole: set[str]  # группы целиком
    subs: set[str]  # подгруппы
    online: bool
    #: Потребности в аудиториях: множество подходящих аудиторий — по одному на занятие,
    #: которому нужна аудитория (тип, вместимость, оборудование уже учтены).
    rooms: list[frozenset[str]]
    disciplines: set[str]
    heavy: bool
    double: bool
    allowed: list[Slot]
    merge: Merge | None = None  # для объединённой единицы и её членов
    is_merged: bool = False
    block: SyncBlock | None = None
    alt: bool = False  # «через неделю»: все пары в одну неделю цикла, парами подряд
    onsite: bool = True  # преподаватель на площадке (очно или дистант из аудитории)
    #: Есть ли онлайн-часть: у смешанного блока (одна подгруппа онлайн, остальные очно) группа
    #: в этот слот и очно, и онлайн — так считает валидатор, так должна считать и модель.
    any_online: bool = False
    #: Параллельно ``rooms``: занятию можно закончить раньше, к закрытию площадки.
    rooms_late: list[bool] = field(default_factory=list)
    w: dict[str, Lit] = field(default_factory=dict)
    b: dict[tuple[str, Week], Lit] = field(default_factory=dict)
    o: dict[tuple[str, Week], Expr] = field(default_factory=dict)


class CpSatSolver:
    name = "cp-sat"

    def solve(self, problem: Problem, params: SolveParams, progress: ProgressCallback | None = None) -> SolveResult:
        findings = precheck(problem)
        if has_errors(findings):
            return SolveResult(
                Status.PRECHECK_FAILED,
                precheck=findings,
                diagnostics=[f.message for f in findings if f.level == "error"],
            )
        result = SolveResult(Status.UNKNOWN, precheck=findings)
        previous: list[list[tuple[str, str, Week]]] = []
        # отсечения этапа 2 верны для задачи целиком — переходят и в следующие варианты
        cuts: list[frozenset[str]] = []
        nogoods: list[tuple[str, list[tuple[str, Week]]]] = []

        def make_builder() -> _Builder:
            # модель строится заново: уровни лексикографии одного прогона не протекают в следующий
            b = _Builder(problem, params.profile, params.reference)
            b.build()
            for keys in previous:
                b.forbid_close_to(keys, params.min_diff)
            for c in cuts:
                b.add_room_cut(c)
            for sid, present in nogoods:
                b.add_slot_nogood(sid, present)
            return b

        for v in range(params.variants):
            t0 = time.monotonic()
            builder = make_builder()
            st, solver = builder.run(params, v, progress)
            with_rooms: list[Assignment] | None = None
            rounds = 0
            while st in (cp_model.OPTIMAL, cp_model.FEASIBLE) and with_rooms is None:
                try:
                    with_rooms = assign_rooms(problem, builder.extract(solver), params.profile)
                except RoomAssignmentError as e:
                    rounds += 1
                    if rounds > MAX_ROOM_ROUNDS:
                        raise
                    if e.cut is not None:
                        cuts.append(e.cut)
                    elif e.nogood is not None:
                        nogoods.append((e.slot_id, e.nogood))
                    if progress:
                        progress(
                            Progress(v + 1, f"аудитории: отсечение {rounds}", time.monotonic() - t0, 0, None, None)
                        )
                    builder = make_builder()
                    st, solver = builder.run(params, v, progress)
            if with_rooms is not None:
                status = Status.OPTIMAL if st == cp_model.OPTIMAL else Status.FEASIBLE
                sched = Schedule(
                    with_rooms,
                    meta={
                        "solver": self.name,
                        "variant": v + 1,
                        "seed": params.seed,
                        "workers": params.workers,
                        "time_limit_s": params.time_limit_s,
                        "mode": params.profile.mode.value,
                        "weights": {c.value: w for c, w in params.profile.weights.items()},
                        "objective": solver.objective_value,
                        "bound": solver.best_objective_bound,
                        "room_cuts": rounds,
                    },
                )
                rep = validate(problem, sched, params.profile, params.reference)
                if not rep.ok:  # защитный контур: решатель и валидатор расходятся — это баг
                    raise AssertionError("Валидатор нашёл жёсткие нарушения в решении CP-SAT:\n" + rep.summary())
                result.variants.append(
                    Variant(
                        sched,
                        status,
                        solver.objective_value,
                        solver.best_objective_bound,
                        time.monotonic() - t0,
                        dict(rep.breakdown),
                        rep.total,
                    )
                )
                previous.append(builder.true_literals(solver))
                result.status = status if v == 0 else result.status
                continue
            if v == 0:
                if st == cp_model.INFEASIBLE:
                    result.status = Status.INFEASIBLE
                    if params.diagnose:
                        result.diagnostics = diagnose(problem, params)
                else:
                    result.status = Status.UNKNOWN
                    result.diagnostics = [
                        "За отведённое время не найдено ни одного допустимого расписания. "
                        "Увеличьте лимит времени или ослабьте ограничения."
                    ]
            break  # следующий вариант не нашёлся — останавливаемся
        return result


# ====================================================================== модель


class _Builder:
    def __init__(self, p: Problem, prof: WeightProfile, reference: Schedule | None, assumptions: bool = False):
        self.p, self.prof, self.reference = p, prof, reference
        self.m = cp_model.CpModel()
        self.units: list[Unit] = []
        self.unit_of_item: dict[str, Unit] = {}
        self.merge_var: dict[str, Lit] = {}
        self.crit: dict[Crit, list[Expr]] = defaultdict(list)
        self.with_assumptions = assumptions
        #: Наборы аудиторий-отсечения, найденные на этапе 2.
        self.room_cuts: set[frozenset[str]] = set()
        #: литерал-предположение -> человекочитаемое описание (для диагностики)
        self.assumption: dict[int, tuple[Lit, str]] = {}

    # ------------------------------------------------------------ вспомогательное

    def _assume(self, key: str, text: str) -> Lit | None:
        if not self.with_assumptions:
            return None
        lit = self.m.new_bool_var(f"A[{key}]")
        self.assumption[lit.index] = (lit, text)
        return lit

    def _add(self, ct: cp_model.Constraint, lit: Lit | None) -> None:
        if lit is not None:
            ct.only_enforce_if(lit)

    def _or(self, target_name: str, lits: list[Expr], exact: bool) -> Expr:
        """Индикатор OR(lits). exact=False — только нижняя граница (для штрафов)."""
        if not lits:
            return 0
        if len(lits) == 1 and exact:
            return lits[0]
        y = self.m.new_bool_var(target_name)
        for x in lits:
            self.m.add(y >= x)
        if exact:
            self.m.add(y <= sum(lits))
        return y

    def _pos(self, name: str, expr: Expr, ub: int) -> Lit:
        """max(0, expr) снизу: переменная ≥ expr, ≥ 0 (минимизируется целевой)."""
        v = self.m.new_int_var(0, max(ub, 0), name)
        self.m.add(v >= expr)
        return v

    # ------------------------------------------------------------ единицы

    def _make_unit(
        self,
        uid: str,
        items: list[WorkloadItem],
        block: SyncBlock | None = None,
        merge: Merge | None = None,
        merged: bool = False,
    ) -> Unit:
        p = self.p
        whole: set[str] = set()
        subs: set[str] = set()
        for it in items:
            w_, s_ = p.occupancy(it)
            whole |= set(w_)
            subs |= set(s_)
        if merged:
            rooms = [frozenset(r.id for r in p.eligible_rooms(items))] if items[0].needs_room else []
            rooms_late = [p.late_ok(items)] if items[0].needs_room else []
            teachers = list(dict.fromkeys(t for it in items for t in it.teacher_ids))
        else:
            rooms = [frozenset(r.id for r in p.eligible_rooms([it])) for it in items if it.needs_room]
            rooms_late = [it.may_end_early for it in items if it.needs_room]
            teachers = [t for it in items for t in it.teacher_ids]
        allowed = p.allowed_slots(items[0])
        if merged:
            ids = set.intersection(*({s.id for s in p.allowed_slots(it)} for it in items))
            allowed = [s for s in p.slots if s.id in ids]
        return Unit(
            uid,
            items,
            items[0].pairs_per_cycle,
            teachers,
            whole,
            subs,
            all(it.format is Format.ONLINE for it in items),
            rooms,
            {it.discipline_id for it in items},
            any(p.is_heavy(it) for it in items),
            any(it.double for it in items),
            allowed,
            merge,
            merged,
            block,
            alt=any(it.is_alt_week for it in items),
            onsite=any(it.needs_room for it in items),
            any_online=any(it.format is Format.ONLINE for it in items),
            rooms_late=rooms_late,
        )

    @property
    def unit_by_id(self) -> dict[str, Unit]:
        return {u.id: u for u in self.units}

    def _units(self) -> None:
        p = self.p
        for b in p.blocks:
            u = self._make_unit(f"block:{b.id}", [p.item_by_id[i] for i in b.item_ids], block=b)
            self.units.append(u)
            for i in b.item_ids:
                self.unit_of_item[i] = u
        for it in p.items:
            if it.id in p.block_of_item:
                continue
            u = self._make_unit(f"item:{it.id}", [it], merge=p.merge_of_item.get(it.id))
            self.units.append(u)
            self.unit_of_item[it.id] = u
        for mg in p.merges:
            items = [p.item_by_id[i] for i in mg.item_ids]
            self.units.append(self._make_unit(f"merge:{mg.id}", items, merge=mg, merged=True))

    def _name(self, u: Unit) -> str:
        p = self.p
        it = u.items[0]
        if u.block:
            return f"блок «{u.block.name}»"
        groups = sorted(p.group_by_id[g].name for x in u.items for g in p.item_groups(x))
        return f"«{p.discipline_by_id[it.discipline_id].name}» ({', '.join(groups)})"

    # ------------------------------------------------------------ построение

    def build(self) -> None:
        p, m = self.p, self.m
        self._units()
        pinned_merge_members = {pin.item_id for pin in p.pins if pin.item_id in p.merge_of_item}
        for mg in p.merges:
            mv = m.new_bool_var(f"merge[{mg.id}]")
            self.merge_var[mg.id] = mv
            if set(mg.item_ids) & pinned_merge_members:
                m.add(mv == 0)  # закрепление члена объединения = объединять нельзя
            self.crit[Crit.MERGE_USED].append(mv)

        for u in self.units:
            wc, bc = u.items[0].weekly_count, u.items[0].blink_count
            for s in u.allowed:
                if wc:
                    u.w[s.id] = m.new_bool_var(f"w[{u.id},{s.id}]")
                if bc:
                    for k in CYCLE_WEEKS:
                        u.b[(s.id, k)] = m.new_bool_var(f"b[{u.id},{s.id},{k.value}]")
            for s in u.allowed:
                for k in CYCLE_WEEKS:
                    terms = [x for x in (u.w.get(s.id), u.b.get((s.id, k))) if x is not None]
                    if len(terms) == 2:
                        m.add(sum(terms) <= 1)
                    u.o[(s.id, k)] = sum(terms) if len(terms) > 1 else terms[0]
            # количество вхождений (с учётом альтернативы «объединено / раздельно»)
            lit = self._assume(u.id, f"разместить {self._name(u)} — {u.n} пар за цикл")
            if u.is_merged:
                assert u.merge is not None
                active: Expr = self.merge_var[u.merge.id]
            elif u.merge is not None:
                active = 1 - self.merge_var[u.merge.id]
            else:
                active = 1
            if wc:
                self._add(m.add(sum(u.w.values()) == wc * active), lit)
            if bc and u.alt:
                # «через неделю»: выбрать неделю цикла z_k и поставить все пары в неё
                z = {k: m.new_bool_var(f"alt[{u.id},{k.value}]") for k in CYCLE_WEEKS}
                self._add(m.add(sum(z.values()) == active), lit)
                for k in CYCLE_WEEKS:
                    m.add(sum(v for (_, kk), v in u.b.items() if kk is k) == bc * z[k])
                    # парами подряд: каждое вхождение соседствует с другим в тот же день той же недели
                    for sl in u.allowed:
                        nb = [u.b[(x.id, k)] for x in self._neighbours(sl) if (x.id, k) in u.b]
                        m.add(u.b[(sl.id, k)] <= sum(nb))
            elif bc:
                self._add(m.add(sum(u.b.values()) == bc * active), lit)
            # сдвоенная пара: каждое еженедельное вхождение соседствует с другим в тот же день
            if u.double and wc:
                for s in u.allowed:
                    nb = [u.w[x.id] for x in self._neighbours(s) if x.id in u.w]
                    m.add(u.w[s.id] <= sum(nb))

        self._pins()
        self._teachers()
        self._groups()
        self._rooms_aggregate()
        self._reference()

    def _neighbours(self, s: Slot) -> list[Slot]:
        ds = self.p.grid.day_slots(s.day)
        i = ds.index(s)
        return [ds[j] for j in (i - 1, i + 1) if 0 <= j < len(ds)]

    def _occ(self, u: Unit, sid: str, k: Week) -> Expr | None:
        return u.o.get((sid, k))

    def _pins(self) -> None:
        for n, pin in enumerate(self.p.pins):
            u = self.unit_of_item[pin.item_id]
            lit = self._assume(
                f"pin{n}", f"закрепление {self._name(u)} → {self._slot_name(pin.slot_id)} ({pin.week.value})"
            )
            var = u.w.get(pin.slot_id) if pin.week is Week.BOTH else u.b.get((pin.slot_id, pin.week))
            if var is None:  # precheck ловит; на всякий случай — противоречие
                self._add(self.m.add_bool_or([]), lit)
                continue
            self._add(self.m.add(var == 1), lit)

    def _slot_name(self, sid: str) -> str:
        s = self.p.grid.slot_by_id[sid]
        return f"{DAY_NAMES[s.day]} {s.pair} пара"

    # ------------------------------------------------------------ преподаватели

    def _teachers(self) -> None:
        p, m = self.p, self.m
        by_t: dict[str, list[Unit]] = defaultdict(list)
        for u in self.units:
            for t in dict.fromkeys(u.teachers):
                by_t[t].append(u)
        cost = self.prof.pref_cost
        for t, units in by_t.items():
            limit, hard = p.teacher_limit(t)
            t_min = p.policy.teacher_min_per_day
            lit = self._assume(f"tlim:{t}", f"лимит {limit} пар в день у {p.teacher_by_id[t].name}") if hard else None
            for k in CYCLE_WEEKS:
                for d in p.grid.days:
                    occ: list[Expr] = []
                    onsite: list[Expr] = []
                    for s in p.grid.day_slots(d):
                        onsite += [u.o[(s.id, k)] for u in units if u.onsite and (s.id, k) in u.o]
                        terms = [u.teachers.count(t) * u.o[(s.id, k)] for u in units if (s.id, k) in u.o]
                        expr = sum(terms) if terms else 0
                        if terms:
                            m.add(expr <= 1)
                            c = cost.get(p.teacher_pref(t, s.id), 0)
                            if c:
                                self.crit[Crit.TEACHER_PREF].append(c * expr)
                        occ.append(expr)
                    self._gaps(Crit.TEACHER_GAPS, f"t{t}{d}{k}", occ)
                    if limit is not None and any(not isinstance(e, int) for e in occ):
                        if hard:
                            self._add(m.add(sum(occ) <= limit), lit)
                        else:
                            self.crit[Crit.TEACHER_MAX_DAY].append(
                                self._pos(f"tmax[{t},{d},{k}]", sum(occ) - limit, len(occ))
                            )
                    # приезд ради одной пары: на площадке 0 < пар < минимума
                    if t_min is not None and t_min > 1 and onsite:
                        came = self._or(f"tcame[{t},{d},{k.value}]", onsite, exact=False)
                        self.crit[Crit.TEACHER_MIN_DAY].append(
                            self._pos(f"tmin[{t},{d},{k.value}]", t_min * came - sum(onsite), t_min)
                        )

    def _gaps(self, crit: Crit, key: str, busy: list[Expr]) -> None:
        """Окна: свободные пары между первой и последней занятой (нижние границы)."""
        live = [i for i, e in enumerate(busy) if not isinstance(e, int)]
        if len(live) < 2 or live[-1] - live[0] < 2:
            return
        lo, hi = live[0], live[-1]
        m = self.m
        pre: list[Expr] = [0] * len(busy)
        suf: list[Expr] = [0] * len(busy)
        for i in range(lo + 1, hi):
            pv = m.new_bool_var(f"pre[{key},{i}]")
            for j in range(lo, i):
                if not isinstance(busy[j], int):
                    m.add(pv >= busy[j])
            pre[i] = pv
            sv = m.new_bool_var(f"suf[{key},{i}]")
            for j in range(i + 1, hi + 1):
                if not isinstance(busy[j], int):
                    m.add(sv >= busy[j])
            suf[i] = sv
            g = m.new_bool_var(f"gap[{key},{i}]")
            m.add(g >= pre[i] + suf[i] - busy[i] - 1)
            self.crit[crit].append(g)

    # ------------------------------------------------------------ группы

    def _groups(self) -> None:
        p, m = self.p, self.m
        cost = self.prof.pref_cost
        gap_min = p.policy.format_gap_min
        for g in p.groups:
            cells = p.group_cells(g.id)
            touching: list[Unit] = []
            cell_units: dict[tuple[str, ...], list[Unit]] = {c: [] for c in cells}
            for u in self.units:
                hit = False
                for c in cells:
                    if g.id in u.whole or any(s in c for s in u.subs):
                        cell_units[c].append(u)
                        hit = True
                if hit:
                    touching.append(u)
            if not touching:
                continue
            course = p.course_by_num.get(g.course)
            lim = course.limits if course else None
            has_online = any(u.any_online for u in touching)
            heavy_units = [u for u in touching if u.heavy]
            lit_lim = (
                self._assume(f"glim:{g.id}", f"жёсткие лимиты в день у группы {g.name}")
                if lim and (lim.max_per_day_hard or lim.min_per_day_hard or lim.max_consecutive_hard)
                else None
            )
            for k in CYCLE_WEEKS:
                for d in p.grid.days:
                    ds = p.grid.day_slots(d)
                    y: list[Expr] = []
                    y_in: list[Expr] = []
                    y_on: list[Expr] = []
                    hv: list[Expr] = []
                    for s in ds:
                        for c in cells:
                            terms = [x for u in cell_units[c] if (x := self._occ(u, s.id, k)) is not None]
                            if len(terms) > 1:
                                m.add(sum(terms) <= 1)
                        occ_all = [x for u in touching if (x := self._occ(u, s.id, k)) is not None]
                        ys = self._or(f"y[{g.id},{s.id},{k.value}]", occ_all, exact=True)
                        y.append(ys)
                        if not isinstance(ys, int):
                            c = cost.get(p.group_pref(g.id, s.id), 0)
                            if c:
                                self.crit[Crit.GROUP_PREF].append(c * ys)
                        if has_online:
                            y_in.append(
                                self._or(
                                    f"yin[{g.id},{s.id},{k.value}]",
                                    [x for u in touching if not u.online and (x := self._occ(u, s.id, k)) is not None],
                                    exact=False,
                                )
                            )
                            y_on.append(
                                self._or(
                                    f"yon[{g.id},{s.id},{k.value}]",
                                    [x for u in touching if u.any_online and (x := self._occ(u, s.id, k)) is not None],
                                    exact=False,
                                )
                            )
                        if heavy_units:
                            hv.append(
                                self._or(
                                    f"hv[{g.id},{s.id},{k.value}]",
                                    [x for u in heavy_units if (x := self._occ(u, s.id, k)) is not None],
                                    exact=False,
                                )
                            )
                    key = f"{g.id},{d},{k.value}"
                    if all(isinstance(e, int) for e in y):
                        continue
                    self._gaps(Crit.GROUP_GAPS, "g" + key, y)
                    cnt = sum(y)
                    if has_online:
                        self._formats(key, ds, y_in, y_on, gap_min)
                    if lim:
                        self._day_limits(key, y, cnt, lim, lit_lim)
                    if heavy_units:
                        hm = p.policy.heavy_max_consecutive
                        for i in range(len(ds) - hm):
                            win = hv[i : i + hm + 1]
                            if sum(1 for e in win if not isinstance(e, int)) > hm:
                                self.crit[Crit.HEAVY_CONSEC].append(self._pos(f"hc[{key},{i}]", sum(win) - hm, 1))
                    self._discipline_spread(g.id, key, ds, k, cells, cell_units)

    def _formats(self, key: str, ds: list[Slot], y_in: list[Expr], y_on: list[Expr], gap_min: int) -> None:
        m = self.m
        n = len(ds)
        for q in range(n):
            if isinstance(y_on[q], int):
                continue
            early = [
                pi
                for pi in range(n)
                if not isinstance(y_in[pi], int)
                and ds[pi].start_min < ds[q].start_min
                and ds[q].start_min - ds[pi].end_min < gap_min
            ]
            if early:
                v = m.new_bool_var(f"i2o[{key},{q}]")
                for pi in early:
                    m.add(v >= y_in[pi] + y_on[q] - 1)
                self.crit[Crit.FMT_IN_TO_ONLINE].append(v)
        pairs_o2i = [
            (a, b)
            for a in range(n)
            for b in range(n)
            if ds[a].start_min < ds[b].start_min and not isinstance(y_on[a], int) and not isinstance(y_in[b], int)
        ]
        if pairs_o2i:
            v = m.new_bool_var(f"o2i[{key}]")
            for a, b in pairs_o2i:
                m.add(v >= y_on[a] + y_in[b] - 1)
            self.crit[Crit.FMT_ONLINE_TO_IN].append(v)
        live_in = [e for e in y_in if not isinstance(e, int)]
        live_on = [e for e in y_on if not isinstance(e, int)]
        if live_in and live_on:
            v = m.new_bool_var(f"mix[{key}]")
            for a in live_in:
                for b in live_on:
                    m.add(v >= a + b - 1)
            self.crit[Crit.FMT_MIXED_DAY].append(v)

    def _day_limits(self, key: str, y: list[Expr], cnt: Expr, lim: object, lit: Lit | None) -> None:
        from ..domain import DayLimits

        assert isinstance(lim, DayLimits)
        m = self.m
        n = len(y)
        if lim.max_per_day is not None:
            if lim.max_per_day_hard:
                self._add(m.add(cnt <= lim.max_per_day), lit)
            elif n > lim.max_per_day:
                self.crit[Crit.COURSE_MAX_DAY].append(self._pos(f"cmax[{key}]", cnt - lim.max_per_day, n))
        if lim.min_per_day is not None and lim.min_per_day > 1:
            used = self._or(f"used[{key}]", [e for e in y if not isinstance(e, int)], exact=False)
            if lim.min_per_day_hard:
                self._add(m.add(cnt >= lim.min_per_day * used), lit)
            else:
                self.crit[Crit.COURSE_MIN_DAY].append(
                    self._pos(f"cmin[{key}]", lim.min_per_day * used - cnt, lim.min_per_day)
                )
        if lim.max_consecutive is not None:
            mc = lim.max_consecutive
            for i in range(n - mc):
                win = y[i : i + mc + 1]
                if sum(1 for e in win if not isinstance(e, int)) <= mc:
                    continue
                if lim.max_consecutive_hard:
                    self._add(m.add(sum(win) <= mc), lit)
                else:
                    self.crit[Crit.COURSE_MAX_CONSEC].append(self._pos(f"ccons[{key},{i}]", sum(win) - mc, 1))

    def _discipline_spread(
        self,
        gid: str,
        key: str,
        ds: list[Slot],
        k: Week,
        cells: list[tuple[str, ...]],
        cell_units: dict[tuple[str, ...], list[Unit]],
    ) -> None:
        p = self.p
        base = p.policy.discipline_per_day
        discs: set[str] = set().union(*(u.disciplines for c in cells for u in cell_units[c]))
        for dsc in discs:
            allowed = base
            for c in cells:
                if any(u.double or u.alt for u in cell_units[c] if dsc in u.disciplines):
                    allowed = max(allowed, 2)
            exprs = []
            for c in cells:
                us = [u for u in cell_units[c] if dsc in u.disciplines]
                # верхняя оценка числа вхождений в неделю: если не больше allowed — штрафа не бывает
                if sum(u.items[0].weekly_count + u.items[0].blink_count for u in us) <= allowed:
                    continue
                terms = [x for u in us for s in ds if (x := self._occ(u, s.id, k)) is not None]
                if len(terms) > allowed:
                    exprs.append(sum(terms))
            if exprs:
                ex = self.m.new_int_var(0, len(ds), f"disc[{key},{dsc}]")
                for e in exprs:
                    self.m.add(ex >= e - allowed)
                self.crit[Crit.DISCIPLINE_SPREAD].append(ex)

    # ------------------------------------------------------------ аудитории (агрегированно)

    def _rooms_aggregate(self) -> None:
        """Агрегированные аудитории: в каждом (слот, неделя) для «узких» наборов аудиторий S —
        занятий, которым годятся только аудитории из S, не больше |S| (условие Холла).

        Наборы: все различные множества подходящих аудиторий, их попарные объединения, общее
        объединение и наборы-отсечения, найденные на этапе 2 (``room_cuts``). Для вложенных
        множеств (тип + пороги вместимости) это условие точное; при оборудовании «вкось» этап 2
        может найти узкое место — оно возвращается сюда отсечением, и модель решается снова.
        """
        p = self.p
        needs = self._needs()
        base = {R for _, R, _ in needs if R}
        families: dict[tuple[frozenset[str], frozenset[str]], list[frozenset[str]]] = {}
        for s in p.slots:
            av = self._avail(s.id)
            if av not in families:
                fam = {R & a for R in base for a in av} | {S & a for S in self.room_cuts for a in av}
                fam.discard(frozenset())
                lst = sorted(fam, key=len)
                for i, a in enumerate(lst):
                    for b in lst[i + 1 :]:
                        if not (a <= b or b <= a):
                            fam.add(a | b)
                if lst:
                    fam.add(frozenset().union(*lst))
                families[av] = sorted(fam, key=len)
            for S in families[av]:
                self._room_set_constraint(s.id, S, av, needs)

    def _needs(self) -> list[tuple[Unit, frozenset[str], bool]]:
        return [(u, R, late) for u in self.units for R, late in zip(u.rooms, u.rooms_late, strict=True)]

    def _avail(self, sid: str) -> tuple[frozenset[str], frozenset[str]]:
        """Свободные аудитории в слоте: (для обычных занятий, для «можно закончить раньше»)."""
        p = self.p
        return (
            frozenset(r.id for r in p.active_rooms if p.room_free(r, sid)),
            frozenset(r.id for r in p.active_rooms if p.room_free(r, sid, late_ok=True)),
        )

    def _room_set_constraint(
        self,
        sid: str,
        S: frozenset[str],
        av: tuple[frozenset[str], frozenset[str]],
        needs: list[tuple[Unit, frozenset[str], bool]],
    ) -> None:
        """Холл для набора S: занятий, которым в этом слоте годятся только аудитории из S, ≤ |S|."""
        for k in CYCLE_WEEKS:
            coef: dict[int, tuple[int, Expr]] = {}
            for u, R, late in needs:
                if (R & av[1 if late else 0]) <= S and (x := self._occ(u, sid, k)) is not None:
                    key = id(u)
                    c, _ = coef.get(key, (0, x))
                    coef[key] = (c + 1, x)
            if sum(c for c, _ in coef.values()) > len(S):
                self.m.add(sum(c * x for c, x in coef.values()) <= len(S))

    def add_room_cut(self, S: frozenset[str]) -> None:
        """Отсечение с этапа 2: набор аудиторий S оказался узким — ограничить его во всех слотах."""
        self.room_cuts.add(S)
        needs = self._needs()
        for s in self.p.slots:
            av = self._avail(s.id)
            for a in set(av):
                self._room_set_constraint(s.id, S & a, av, needs)

    def add_slot_nogood(self, sid: str, present: list[tuple[str, Week]]) -> None:
        """Запрет конкретного сочетания занятий в слоте (редкий случай: по неделям аудиторий
        хватает, но еженедельное занятие не может занять одну аудиторию обе недели)."""
        lits = []
        for iid, wk in present:
            u = self.unit_of_item.get(iid)
            if u is None:
                continue
            var = u.w.get(sid) if wk is Week.BOTH else u.b.get((sid, wk))
            if var is not None:
                lits.append(var)
        lits = list({v.index: v for v in lits}.values())
        if lits:
            self.m.add(sum(lits) <= len(lits) - 1)

    # ------------------------------------------------------------ пересборка

    def _reference(self) -> None:
        if self.reference is None:
            return
        # Допустимые объединения, которые в эталоне использованы (все элементы набора стоят
        # вместе в каждом вхождении), отображаются на объединённую единицу, а не на члены:
        # иначе фиксация эталона требовала бы двух занятий одного преподавателя в одном слоте.
        by_item: dict[str, set[tuple[Week, str]]] = defaultdict(set)
        for a in self.reference.assignments:
            by_item[a.item_id].add((a.week, a.slot_id))
        merged_unit: dict[str, Unit] = {}
        for mg in self.p.merges:
            pats = [by_item.get(i, set()) for i in mg.item_ids]
            if pats[0] and all(pt == pats[0] for pt in pats):
                mu = self.unit_by_id[f"merge:{mg.id}"]
                for i in mg.item_ids:
                    merged_unit[i] = mu
                self.m.add_hint(self.merge_var[mg.id], 1)
            elif mg.id in self.merge_var:
                self.m.add_hint(self.merge_var[mg.id], 0)
        hinted: set[int] = set()  # одна переменная — одна подсказка (повтор делает модель некорректной)
        for a in self.reference.assignments:
            u = merged_unit.get(a.item_id) or self.unit_of_item.get(a.item_id)
            if u is None:
                self.crit[Crit.CHANGES].append(1)
                continue
            var = u.w.get(a.slot_id) if a.week is Week.BOTH else u.b.get((a.slot_id, a.week))
            if var is None:
                self.crit[Crit.CHANGES].append(1)
            else:
                self.crit[Crit.CHANGES].append(1 - var)
                if var.index not in hinted:
                    hinted.add(var.index)
                    self.m.add_hint(var, 1)

    def forbid_close_to(self, keys: list[tuple[str, str, Week]], k: int) -> None:
        """Вариант должен отличаться от прежнего минимум на k назначений."""
        lits: list[Expr] = []
        for uid, sid, wk in keys:
            u = self.unit_by_id[uid]
            var = u.w.get(sid) if wk is Week.BOTH else u.b.get((sid, wk))
            if var is not None:
                lits.append(var)
        k = max(1, min(k, len(keys)))
        self.m.add(sum(lits) <= len(keys) - k)

    def true_literals(self, solver: cp_model.CpSolver) -> list[tuple[str, str, Week]]:
        out: list[tuple[str, str, Week]] = []
        for u in self.units:
            out += [(u.id, sid, Week.BOTH) for sid, v in u.w.items() if solver.value(v)]
            out += [(u.id, sid, k) for (sid, k), v in u.b.items() if solver.value(v)]
        return out

    # ------------------------------------------------------------ целевая функция и запуск

    def _expr(self, crits: Iterable[Crit]) -> Expr:
        terms = []
        for c in crits:
            w = self.prof.weight(c)
            if w and self.crit.get(c):
                terms.append(w * sum(self.crit[c]))
        return sum(terms) if terms else 0

    def run(
        self, params: SolveParams, variant: int, progress: ProgressCallback | None
    ) -> tuple[Any, cp_model.CpSolver]:
        crits: list[Crit] = [c for c in Crit if c not in ROOM_STAGE_CRITS]
        if self.prof.mode is Mode.LEXICOGRAPHIC:
            levels: list[list[Crit]] = [list(lv) for lv in self.prof.lex_levels if lv]
            in_levels = {c for lv in levels for c in lv}
            rest = [c for c in crits if c not in in_levels]
            if rest:
                levels = [*levels[:-1], [*levels[-1], *rest]] if levels else [rest]
        else:
            levels = [crits]
        budget = params.time_limit_s
        t_start = time.monotonic()
        st, solver = cp_model.UNKNOWN, cp_model.CpSolver()
        # Фаза 0: найти ЛЮБОЕ допустимое расписание без целевой функции — на тесных
        # задачах это в разы быстрее, чем искать первое решение сразу с оптимизацией.
        # Найденное решение становится подсказкой (hint) для оптимизации.
        self.m.clear_objective()

        def phase0(limit: float, fix_hint: bool) -> tuple[Any, cp_model.CpSolver]:
            sol = cp_model.CpSolver()
            sol.parameters.max_time_in_seconds = limit
            sol.parameters.num_workers = params.workers
            sol.parameters.random_seed = params.seed + variant
            sol.parameters.log_search_progress = params.log_search
            # пересборка: сначала пробуем достроить ровно эталон (если он ещё допустим —
            # мгновенно получаем полную подсказку и стартуем с нулём отличий)
            sol.parameters.fix_variables_to_their_hinted_value = fix_hint
            st0 = sol.solve(self.m)
            if st0 == cp_model.MODEL_INVALID:
                raise RuntimeError("Модель CP-SAT некорректна — это ошибка в коде решателя: " + self.m.validate())
            return st0, sol

        fst, feas = cp_model.UNKNOWN, cp_model.CpSolver()
        if self.reference is not None:
            fst, feas = phase0(max(1.0, min(10.0, 0.2 * budget)), fix_hint=True)
        if fst not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            fst, feas = phase0(max(1.0, 0.4 * budget), fix_hint=False)
        if fst == cp_model.INFEASIBLE:
            return fst, feas
        if fst in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            self.m.clear_hints()
            self._hint_from(feas)
            if progress:
                progress(Progress(variant + 1, "первое допустимое", feas.wall_time, 1, None, None))
        for li, lv in enumerate(levels):
            obj = self._expr(lv)
            self.m.clear_objective()
            self.m.minimize(obj)
            remaining = budget - (time.monotonic() - t_start)
            share = remaining / (len(levels) - li)
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = max(share, 1.0)
            solver.parameters.num_workers = params.workers
            solver.parameters.random_seed = params.seed + variant
            solver.parameters.log_search_progress = params.log_search
            stage = f"уровень {li + 1}/{len(levels)}" if len(levels) > 1 else "слоты"
            cb = _Callback(variant, stage, progress)
            st = solver.solve(self.m, cb)
            if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
            if li < len(levels) - 1:
                best = round(solver.objective_value)
                self.m.add(obj <= best)
                self.m.clear_hints()
                self._hint_from(solver)
        return st, solver

    def _hint_from(self, solver: cp_model.CpSolver) -> None:
        """Полная подсказка: значения ВСЕХ переменных найденного решения.

        Неполная подсказка (только w/b) заставляет CP-SAT достраивать индикаторы,
        и на больших задачах он может не успеть; полная — сразу допустимое решение.
        """
        for i in range(len(self.m.proto.variables)):
            v = self.m.get_int_var_from_proto_index(i)
            self.m.add_hint(v, solver.value(v))

    def extract(self, solver: cp_model.CpSolver) -> list[Assignment]:
        out: list[Assignment] = []
        for u in self.units:
            for sid, v in u.w.items():
                if solver.value(v):
                    out += [Assignment(it.id, Week.BOTH, sid) for it in u.items]
            for (sid, k), v in u.b.items():
                if solver.value(v):
                    out += [Assignment(it.id, k, sid) for it in u.items]
        return out


class _Callback(cp_model.CpSolverSolutionCallback):
    def __init__(self, variant: int, stage: str, progress: ProgressCallback | None):
        super().__init__()
        self.variant, self.stage, self.progress = variant, stage, progress
        self.count = 0

    def on_solution_callback(self) -> None:
        self.count += 1
        if self.progress:
            self.progress(
                Progress(
                    self.variant + 1,
                    self.stage,
                    self.wall_time,
                    self.count,
                    self.objective_value,
                    self.best_objective_bound,
                )
            )


# ====================================================================== диагностика


def diagnose(problem: Problem, params: SolveParams) -> list[str]:
    """Минимальный набор противоречащих требований через assumptions CP-SAT."""
    b = _Builder(problem, params.profile, None, assumptions=True)
    b.build()
    lits = [lit for lit, _ in b.assumption.values()]

    def infeasible(core: list[Lit], limit: float) -> bool | None:
        b.m.clear_assumptions()
        b.m.add_assumptions(core)
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = limit
        s.parameters.num_workers = params.workers
        st = s.solve(b.m)
        if st == cp_model.INFEASIBLE:
            core[:] = [b.assumption[i][0] for i in s.sufficient_assumptions_for_infeasibility()] or core
            return True
        if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return False
        return None

    core = list(lits)
    b.m.clear_objective()
    res = infeasible(core, params.diagnose_time_s)
    if res is not True:
        return ["Не удалось за отведённое время выделить причину неразрешимости."]
    # жадная минимизация: убираем по одному, пока противоречие сохраняется
    t_end = time.monotonic() + params.diagnose_time_s
    i = 0
    while i < len(core) and time.monotonic() < t_end:
        trial = core[:i] + core[i + 1 :]
        r = infeasible(trial, max(1.0, min(5.0, t_end - time.monotonic())))
        if r is True:
            core = trial
        else:
            i += 1
    texts = [b.assumption[lit.index][1] for lit in core]
    return ["Эти требования нельзя выполнить одновременно:"] + [f"  • {t}" for t in sorted(texts)]
