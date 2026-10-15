"""Вход в API: токен (для программ) или сессия админки (для будущего интерфейса диспетчера).

В обоих случаях нужен доступ в админку (is_staff), а дальше действуют обычные права ролей Django:
диспетчер может всё, что может в админке. Преподаватели к API доступа не имеют.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from ninja.errors import HttpError
from ninja.security import HttpBearer, SessionAuthIsStaff

from accounts.models import ApiToken


class TokenAuth(HttpBearer):
    """Заголовок ``Authorization: Bearer rsp_…`` (токен выпускается в админке или ``create_api_token``)."""

    def authenticate(self, request: HttpRequest, token: str) -> Any | None:
        tok = ApiToken.lookup(token)
        if tok is None or not tok.user.is_staff:
            return None
        request.user = tok.user  # права проверяются обычным has_perm
        request.api_token = tok  # type: ignore[attr-defined]  # для журнала действий
        return tok.user


class StaffSession(SessionAuthIsStaff):
    """Сессия админки; для изменяющих запросов нужен CSRF-токен (cookie csrftoken → X-CSRFToken)."""


def log_change(request: HttpRequest, obj: Any, message: str, flag: int | None = None) -> None:
    """Запись в журнал действий админки («Недавние действия», история объекта): кто, что, через что."""
    from django.contrib.admin.models import CHANGE, LogEntry

    tok = getattr(request, "api_token", None)
    via = f"через API, токен «{tok.name}» ({tok.prefix}…)" if tok else "через API"
    LogEntry.objects.log_actions(request.user.pk, [obj], flag or CHANGE, f"{message} — {via}", single_object=True)


def require(request: HttpRequest, *perms: str) -> None:
    missing = [p for p in perms if not request.user.has_perm(p)]
    if missing:
        raise HttpError(403, "Недостаточно прав: " + ", ".join(missing))
