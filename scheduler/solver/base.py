"""Интерфейс решателя: данные -> Problem -> Solver -> SolveResult.

Любой решатель (CP-SAT, собственная эвристика, внешний сервис) реализует
протокол :class:`Solver`. Остальная система — веб, фоновые задачи, экспорт —
знает только об этом интерфейсе и о валидаторе.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..criteria import Crit, WeightProfile
from ..domain import Problem, Schedule
from ..precheck import Finding


class Status(StrEnum):
    OPTIMAL = "optimal"  # доказанный оптимум (для текущей целевой функции)
    FEASIBLE = "feasible"  # допустимое решение найдено, оптимальность не доказана
    INFEASIBLE = "infeasible"  # доказано, что решения нет
    UNKNOWN = "unknown"  # за отведённое время не найдено ничего
    PRECHECK_FAILED = "precheck_failed"  # быстрые проверки нашли противоречие до запуска
    CANCELLED = "cancelled"  # остановлено по запросу раньше, чем нашлось первое расписание


@dataclass
class SolveParams:
    time_limit_s: float = 60.0  # на один вариант
    workers: int = 8
    seed: int = 0
    variants: int = 1
    #: Минимальное число отличающихся назначений между вариантами (K).
    min_diff: int = 10
    profile: WeightProfile = field(default_factory=WeightProfile)
    #: Режим пересборки с минимальными изменениями: текущее расписание.
    reference: Schedule | None = None
    #: При неразрешимости искать минимальный набор противоречий.
    diagnose: bool = True
    diagnose_time_s: float = 30.0
    log_search: bool = False
    #: Запрос остановки: опрашивается примерно раз в 0,5 с из отдельного потока (должен быть
    #: потокобезопасным, например ``threading.Event().is_set``). Найденное к этому моменту
    #: сохраняется: текущий вариант доводится до аудиторий, следующие не строятся.
    stop: Callable[[], bool] | None = None


@dataclass
class Progress:
    variant: int
    stage: str
    elapsed_s: float
    solutions: int
    objective: float | None
    bound: float | None

    @property
    def gap(self) -> float | None:
        if self.objective is None or self.bound is None:
            return None
        denom = max(abs(self.objective), 1.0)
        return abs(self.objective - self.bound) / denom


ProgressCallback = Callable[[Progress], None]


@dataclass
class Variant:
    schedule: Schedule
    status: Status
    objective: float  # значение целевой функции этапа 1 (модель решателя)
    bound: float
    wall_time_s: float
    #: Разбивка штрафа — считается НЕЗАВИСИМЫМ валидатором.
    breakdown: dict[Crit, int] = field(default_factory=dict)
    total: int = 0


@dataclass
class SolveResult:
    status: Status
    variants: list[Variant] = field(default_factory=list)
    precheck: list[Finding] = field(default_factory=list)
    #: Человекочитаемое объяснение неразрешимости.
    diagnostics: list[str] = field(default_factory=list)
    #: Расчёт остановлен по запросу (``SolveParams.stop``) — вариантов может быть меньше заказанных.
    stopped: bool = False


class Solver(Protocol):
    name: str

    def solve(self, problem: Problem, params: SolveParams, progress: ProgressCallback | None = None) -> SolveResult: ...
