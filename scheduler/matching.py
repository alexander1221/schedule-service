"""Двудольное паросочетание «занятия → аудитории» и поиск нарушения условия Холла.

Нужен в двух местах: быстрые проверки (хватит ли аудиторий синхронному блоку в один
момент) и этап 2 решателя (если в слоте аудиторий не хватает — какое именно
множество аудиторий «узкое»: его решатель превращает в отсечение и пересобирает).

Размеры маленькие (десятки занятий × десятки аудиторий), поэтому — алгоритм Куна.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class MatchResult:
    size: int
    match: list[str | None]  # для каждого занятия — аудитория или None
    #: Если полного паросочетания нет: занятия, которым не хватает аудиторий (U'), и все
    #: аудитории, доступные им (N(U')), причём |N(U')| < |U'| — нарушение условия Холла.
    deficient: list[int]
    neighbourhood: frozenset[str]

    @property
    def perfect(self) -> bool:
        return self.size == len(self.match)


def max_matching(options: Sequence[Sequence[str]]) -> MatchResult:
    owner: dict[str, int] = {}

    def augment(i: int, seen: set[str]) -> bool:
        for r in options[i]:
            if r in seen:
                continue
            seen.add(r)
            if r not in owner or augment(owner[r], seen):
                owner[r] = i
                return True
        return False

    for i in range(len(options)):
        augment(i, set())
    match: list[str | None] = [None] * len(options)
    for r, i in owner.items():
        match[i] = r
    size = sum(1 for x in match if x is not None)
    deficient: list[int] = []
    neighbourhood: frozenset[str] = frozenset()
    if size < len(options):
        # чередующиеся пути от непокрытого занятия: достижимые занятия U' и аудитории N(U')
        start = next(i for i, x in enumerate(match) if x is None)
        left, right = {start}, set()
        stack = [start]
        while stack:
            i = stack.pop()
            for r in options[i]:
                if r not in right:
                    right.add(r)
                    j = owner.get(r)
                    if j is not None and j not in left:
                        left.add(j)
                        stack.append(j)
        deficient, neighbourhood = sorted(left), frozenset(right)
    return MatchResult(size, match, deficient, neighbourhood)
