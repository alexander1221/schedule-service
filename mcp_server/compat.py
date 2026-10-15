"""Совместимость с библиотекой ``mcp`` версий 1.x и 2.x — единственное место, где они различаются.

Проверено на mcp 1.30.0 и 2.3.0 (tests_mcp/). Отличия, которые здесь закрыты:

* класс сервера: v1 ``mcp.server.fastmcp.FastMCP``, v2 ``mcp.server.mcpserver.MCPServer``
  (в v2 импорт ``mcp.server.fastmcp`` намеренно падает с ModuleNotFoundError);
* ошибка инструмента: ``…fastmcp.exceptions.ToolError`` / ``…mcpserver.exceptions.ToolError``;
* поля типов протокола: v1 camelCase (``readOnlyHint``), v2 snake_case с camelCase-псевдонимами —
  поэтому аннотации собираются через ``model_validate`` из camelCase, это работает в обеих.

Одинаковы в обеих: ``add_tool(fn, name, title, description, annotations, structured_output)``,
декоратор ``prompt``, ``run("stdio")``. Переход с v1 на v2 — смена строки в requirements-mcp, код не меняется.
"""

import functools
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .client import ApiError
from .tools import ToolSpec


def sdk_version() -> str:
    try:
        return version("mcp")
    except PackageNotFoundError:
        return ""


def sdk_major() -> int:
    v = sdk_version()
    if not v:
        raise RuntimeError(
            "Не установлена библиотека mcp. Установите: pip install --require-hashes -r requirements-mcp-v1-lock.txt "
            "(или v2) — см. docs/MCP.md."
        )
    major = int(v.split(".")[0])
    if major not in (1, 2):
        raise RuntimeError(f"mcp {v}: поддерживаются версии 1.x и 2.x")
    return major


def _sdk() -> tuple[type, type[Exception]]:
    """(класс сервера, класс ToolError) установленной версии."""
    if sdk_major() == 1:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        return FastMCP, ToolError
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError as ToolError2

    return MCPServer, ToolError2


def _annotations(spec: ToolSpec) -> Any:
    from mcp.types import ToolAnnotations

    return ToolAnnotations.model_validate(
        {
            "title": spec.title,
            "readOnlyHint": spec.read_only,
            "destructiveHint": spec.destructive,
            "openWorldHint": False,  # работает только с сервисом колледжа
        }
    )


def _wrap(fn: Callable[..., str], tool_error: type[Exception]) -> Callable[..., str]:
    """ApiError → ToolError библиотеки: агент получит результат «ошибка» с нашим текстом по-русски."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            raise tool_error(str(e)) from None

    return run


def build_server(
    name: str,
    instructions: str,
    tools: list[ToolSpec],
    prompts: list[tuple[Callable[..., str], str]] = (),  # type: ignore[assignment]
) -> Any:
    """MCP-сервер с инструментами и промптами — объект FastMCP (v1) или MCPServer (v2)."""
    server_cls, tool_error = _sdk()
    # WARNING: иначе v1 пишет в журнал агента строку на каждый вызов инструмента
    server = server_cls(name=name, instructions=instructions, log_level="WARNING")
    for spec in tools:
        server.add_tool(
            _wrap(spec.fn, tool_error),
            name=spec.name,
            title=spec.title,
            description=(spec.fn.__doc__ or "").strip(),
            annotations=_annotations(spec),
            structured_output=False,  # только текст: локальным моделям так проще
        )
    for fn, title in prompts:
        server.prompt(name=fn.__name__, title=title, description=(fn.__doc__ or "").strip())(fn)
    return server
