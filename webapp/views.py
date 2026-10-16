"""Интерфейс диспетчера (этап 6): одна страница React, собранная в frontend/dist (docs/FRONTEND.md)."""

import os

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.staticfiles import finders
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@staff_member_required
@ensure_csrf_cookie  # интерфейс берёт CSRF-токен из cookie для изменяющих запросов к API
def dispatcher_app(request: HttpRequest) -> HttpResponse:
    # с префиксом (STATICFILES_DIRS = [("app", …)]) Django ищет путь через os.sep: на Windows «app\index.html»
    path = finders.find(os.path.join("app", "index.html"))
    if not path:
        return render(request, "app_not_built.html", status=503)
    with open(path, encoding="utf-8") as f:
        return HttpResponse(f.read())


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect x="1" y="2" width="14" height="13" rx="2" '
    'fill="#2f6db5"/><rect x="3" y="6" width="10" height="7" fill="white"/></svg>'
)


def favicon(request: HttpRequest) -> HttpResponse:
    """Иконка вкладки для всего сайта (иначе браузер на каждой странице получает 404 на /favicon.ico)."""
    resp = HttpResponse(FAVICON, content_type="image/svg+xml")
    resp["Cache-Control"] = "public, max-age=604800"
    return resp
