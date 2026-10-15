"""MCP-сервер по stdio, как его запускает агент, — на установленной версии mcp (1.x или 2.x).

Запуск в окружении с mcp: ``python -m pytest -q tests_mcp`` (CI гоняет обе версии). Без mcp — пропуск.
Вместо сервиса расписания — маленький поддельный API (http.server), логика инструментов против
настоящего API проверяется в tests_web/test_mcp_tools.py.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

mcp = pytest.importorskip("mcp")

import anyio  # noqa: E402 — после importorskip
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "rsp_test-token"
EXPECTED = {
    "semesters",
    "find_teacher",
    "find_in_variant",
    "schedule",
    "teacher_wishes",
    "set_teacher_wish",
    "lessons",
    "pin_lesson",
    "unpin_lesson",
    "check_semester",
    "start_rebuild",
    "run_status",
    "stop_run",
    "variants",
    "compare_variants",
    "check_move",
    "free_rooms",
}


def f(obj, camel, snake):
    """Поле типа протокола: v1 — camelCase, v2 — snake_case."""
    return getattr(obj, snake) if hasattr(obj, snake) else getattr(obj, camel)


class FakeApi(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []

    def log_message(self, *a):  # тише
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        FakeApi.calls.append((method, self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"detail": "Unauthorized"})
        p = self.path
        if p == "/api/v1/me":
            return self._send(
                200,
                {
                    "email": "disp@college.example",
                    "name": "Диспетчер",
                    "version": "0.9.x",
                    "permissions": [
                        "timetable.view_semester",
                        "timetable.change_teacheravailability",
                        "timetable.add_solverun",
                        "timetable.view_schedulevariant",
                    ],
                },
            )
        if p == "/api/v1/semesters":
            return self._send(
                200,
                [
                    {
                        "id": 1,
                        "name": "Осень 2026",
                        "start_date": "2026-09-01",
                        "approved_variant_id": 7,
                        "active_run_id": None,
                    }
                ],
            )
        if p.startswith("/api/v1/semesters/1/teachers/15/prefs") and method == "PATCH":
            return self._send(
                200, {"teacher": {"id": 15, "name": "Иванова А.А."}, "prefs": body["set"], "days": [1, 2, 3, 4, 5]}
            )
        return self._send(404, {"detail": "Не найдено"})

    def do_GET(self):
        self._handle("GET")

    def do_PATCH(self):
        self._handle("PATCH")


@pytest.fixture
def api_url():
    FakeApi.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _env(url):
    return {**os.environ, "RASPISANIE_URL": url, "RASPISANIE_TOKEN": TOKEN, "PYTHONPATH": str(ROOT)}


def session_run(url, body):
    """Открыть сессию с настоящим процессом ``python -m mcp_server`` и выполнить body(session)."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server"], env=_env(url), cwd=str(ROOT))

    async def go():
        async with stdio_client(params) as streams, ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            return await body(s)

    return anyio.run(go)


def text(result):
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def test_tools_listed_with_schema_and_hints(api_url):
    async def body(s):
        return (await s.list_tools()).tools

    tools = {t.name: t for t in session_run(api_url, body)}
    assert set(tools) == EXPECTED
    assert "approve" not in " ".join(tools)  # утверждает только человек
    wish = tools["set_teacher_wish"]
    schema = f(wish, "inputSchema", "input_schema")
    assert set(schema["required"]) == {"semester_id", "teacher_id", "day", "pair", "value"}
    assert schema["properties"]["day"]["type"] == "integer"
    assert "1=Пн" in wish.description
    ann = wish.annotations
    assert f(ann, "readOnlyHint", "read_only_hint") is False
    assert f(tools["semesters"].annotations, "readOnlyHint", "read_only_hint") is True
    assert f(tools["unpin_lesson"].annotations, "destructiveHint", "destructive_hint") is True
    week = f(tools["schedule"], "inputSchema", "input_schema")["properties"]["week"]
    assert set(week.get("enum", [])) == {"odd", "even"}


def test_call_tools_and_errors(api_url):
    async def body(s):
        return (
            await s.call_tool("semesters", {}),
            await s.call_tool(
                "set_teacher_wish", {"semester_id": 1, "teacher_id": 15, "day": 5, "pair": 1, "value": -2}
            ),
            await s.call_tool("run_status", {"run_id": 999}),
            await s.call_tool(
                "set_teacher_wish", {"semester_id": 1, "teacher_id": 15, "day": 9, "pair": 1, "value": 2}
            ),
        )

    sem, wish, missing, bad_day = session_run(api_url, body)
    assert not f(sem, "isError", "is_error")
    assert "Осень 2026" in text(sem) and "утверждённый вариант: 7" in text(sem)
    assert not f(wish, "isError", "is_error")
    assert "Пт 1-я пара — -2 (нельзя)" in text(wish)
    assert ("PATCH", "/api/v1/semesters/1/teachers/15/prefs", f"Bearer {TOKEN}", {"set": {"5-1": -2}}) in FakeApi.calls
    assert f(missing, "isError", "is_error") and "Не найдено" in text(missing)
    assert f(bad_day, "isError", "is_error") and "1–6" in text(bad_day)
    assert not any(c[1].endswith("/prefs") and c[3] == {"set": {"9-1": 2}} for c in FakeApi.calls)  # не дошло до API


def test_prompt(api_url):
    async def body(s):
        names = [p.name for p in (await s.list_prompts()).prompts]
        got = await s.get_prompt("move_request_prompt", {"teacher": "Иванова", "from_slot": "Пт 4", "to_slot": "Чт 6"})
        return names, got

    names, got = session_run(api_url, body)
    assert names == ["move_request_prompt"]
    msg = got.messages[0].content.text
    assert "Иванова" in msg and "start_rebuild" in msg


def test_check_command(api_url):
    ok = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--check"], env=_env(api_url), cwd=ROOT, capture_output=True, text=True
    )
    assert ok.returncode == 0, ok.stderr
    assert f"mcp {mcp_version()}" in ok.stdout and "всё нужное есть" in ok.stdout
    bad = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--check"],
        env={**_env(api_url), "RASPISANIE_TOKEN": "rsp_wrong"},
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0 and "Токен не принят" in bad.stderr


def mcp_version():
    from importlib.metadata import version

    return version("mcp")
