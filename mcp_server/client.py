"""Клиент API сервиса расписания (/api/v1/) — только стандартная библиотека Python.

MCP-сервер работает на компьютере диспетчера и ходит на сервер колледжа с токеном. Ошибки API
превращаются в :class:`ApiError` с понятным текстом по-русски — его увидит агент и диспетчер.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ApiError(Exception):
    """Ошибка обращения к сервису: текст уже годится, чтобы показать человеку."""


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0):
        base = base_url.rstrip("/")
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        self.base, self.token, self.timeout = base, token.strip(), timeout

    def get(self, path: str, **params: Any) -> Any:
        q = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v is not None}
        return self._call("GET", path + ("?" + urllib.parse.urlencode(q) if q else ""))

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._call("POST", path, body or {})

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("PATCH", path, body)

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise ApiError(_explain(e.code, _detail(e))) from None
        except urllib.error.URLError as e:
            raise ApiError(
                f"Сервис расписания недоступен ({self.base}): {e.reason}. Проверьте адрес RASPISANIE_URL "
                "и что компьютер видит сервер колледжа."
            ) from None
        except TimeoutError:
            raise ApiError(f"Сервис расписания не ответил за {self.timeout:.0f} с ({self.base}).") from None


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        d = json.loads(e.read() or b"{}").get("detail", "")
    except (ValueError, AttributeError):
        return ""
    if isinstance(d, list):  # 422: ошибки полей
        return "; ".join(f"{'.'.join(str(x) for x in i.get('loc', [])[1:])}: {i.get('msg', '')}" for i in d)
    return str(d)


def _explain(code: int, detail: str) -> str:
    if code == 401:
        return "Токен не принят (неверный, отозван или истёк). Выпустите новый в админке → «Токены API»."
    if code == 403:
        return detail or "Недостаточно прав."
    if code == 404:
        return "Не найдено — проверьте номер (id)." + (f" ({detail})" if detail and detail != "Не найдено" else "")
    if code == 422:
        return f"Неверные параметры: {detail}"
    return detail or f"Ошибка сервиса: HTTP {code}"
