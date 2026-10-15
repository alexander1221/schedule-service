"""JSON-сериализация задачи и расписания.

Этот же формат — снимок входных данных варианта (JSONB в PostgreSQL), поэтому он
должен быть стабильным: поле добавляется только с значением по умолчанию.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import types
import typing
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .domain import Problem, Schedule

SCHEMA_VERSION = 1
T = TypeVar("T")


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    return obj


def _load(tp: Any, data: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if data is None:
            return None
        return _load(args[0], data)
    if origin is list:
        (arg,) = typing.get_args(tp)
        return [_load(arg, v) for v in data]
    if origin is dict:
        k_tp, v_tp = typing.get_args(tp)
        return {_load(k_tp, k): _load(v_tp, v) for k, v in data.items()}
    if tp is typing.Any or tp is object:
        return data
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data)
    if tp is dt.date:
        return dt.date.fromisoformat(data)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return from_dict(tp, data)
    if tp in (int, str, float, bool):
        return tp(data)
    return data


def from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _load(hints[f.name], data[f.name])
    return cls(**kwargs)


def problem_to_dict(p: Problem) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "problem": to_jsonable(p)}


def problem_from_dict(d: dict[str, Any]) -> Problem:
    _check_version(d)
    return from_dict(Problem, d["problem"])


def schedule_to_dict(s: Schedule) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "schedule": to_jsonable(s)}


def schedule_from_dict(d: dict[str, Any]) -> Schedule:
    _check_version(d)
    return from_dict(Schedule, d["schedule"])


def _check_version(d: dict[str, Any]) -> None:
    v = d.get("schema_version")
    if v != SCHEMA_VERSION:
        raise ValueError(f"Неподдерживаемая версия формата: {v!r}, ожидалась {SCHEMA_VERSION}")


def save_json(obj: dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))  # type: ignore[no-any-return]
