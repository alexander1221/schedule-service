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
        user = ApiToken.authenticate(token)
        if user is None or not user.is_staff:
            return None
        request.user = user  # права проверяются обычным has_perm
        return user


class StaffSession(SessionAuthIsStaff):
    """Сессия админки; для изменяющих запросов нужен CSRF-токен (cookie csrftoken → X-CSRFToken)."""


def require(request: HttpRequest, *perms: str) -> None:
    missing = [p for p in perms if not request.user.has_perm(p)]
    if missing:
        raise HttpError(403, "Недостаточно прав: " + ", ".join(missing))
