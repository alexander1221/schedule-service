"""Запуск MCP-сервера агента диспетчера: ``python -m mcp_server`` (агент запускает его сам, по stdio).

Настройки — переменные окружения (их задают в конфигурации агента, см. docs/MCP.md):
  RASPISANIE_URL    адрес сайта расписания, например https://raspisanie.college.ru
  RASPISANIE_TOKEN  токен API (админка → «Токены API» или manage.py create_api_token)

Проверка настройки без агента: ``python -m mcp_server --check``.
"""

import argparse
import os
import sys

from .client import ApiClient, ApiError
from .compat import build_server, sdk_major, sdk_version
from .tools import INSTRUCTIONS, make_tools, move_request_prompt


def _client(args: argparse.Namespace) -> ApiClient:
    url = args.url or os.environ.get("RASPISANIE_URL", "")
    token = args.token or os.environ.get("RASPISANIE_TOKEN", "")
    if not url or not token:
        sys.exit("Не заданы RASPISANIE_URL и RASPISANIE_TOKEN (см. docs/MCP.md).")
    return ApiClient(url, token)


def _utf8_output() -> None:
    """Вывод в UTF-8. На Windows, когда вывод уходит в файл или другую программу, Python пишет в кодировке
    системы (cp1251/cp1252) и падает на кириллице и значках (UnicodeEncodeError). В консоли Windows
    вывод и так UTF-8 — там ничего не меняется."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    _utf8_output()  # stdio-протокол библиотека mcp и так ведёт в UTF-8; это — для --check и сообщений об ошибках
    ap = argparse.ArgumentParser(prog="python -m mcp_server", description="MCP-сервер агента диспетчера расписания")
    ap.add_argument("--url", help="адрес сайта (иначе RASPISANIE_URL)")
    ap.add_argument("--token", help="токен API (иначе RASPISANIE_TOKEN)")
    ap.add_argument("--check", action="store_true", help="проверить связь с сервисом и выйти")
    args = ap.parse_args(argv)
    api = _client(args)
    if args.check:
        try:
            me = api.get("/me")
        except ApiError as e:
            sys.exit(f"Связи нет: {e}")
        major = sdk_major()
        print(f"Библиотека mcp {sdk_version()} (v{major}) — поддерживается.")
        print(f"Сервис расписания {me['version']}: вход как {me['name']} <{me['email']}>.")
        can = {p.split(".")[1] for p in me["permissions"]}
        need = {"view_semester", "change_teacheravailability", "add_solverun", "view_schedulevariant"}
        missing = sorted(need - can)
        print("Права: всё нужное есть." if not missing else f"Не хватает прав: {', '.join(missing)}")
        return
    server = build_server(
        "raspisanie",
        INSTRUCTIONS,
        make_tools(api),
        prompts=[(move_request_prompt, "Перенос пары по просьбе преподавателя")],
    )
    server.run("stdio")


if __name__ == "__main__":
    main()
