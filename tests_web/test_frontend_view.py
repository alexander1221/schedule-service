"""Страница интерфейса диспетчера /app/: вход, собранный и несобранный интерфейс, CSRF-cookie."""

import pytest
from django.contrib.staticfiles import finders

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_cache():
    finders.get_finder.cache_clear()
    yield
    finders.get_finder.cache_clear()


def test_requires_admin_login(client):
    r = client.get("/app/")
    assert r.status_code == 302 and r["Location"].startswith("/admin/login/?next=/app/")


def test_not_built_page(admin_client_ru, settings, no_cache):
    settings.STATICFILES_DIRS = []
    r = admin_client_ru.get("/app/")
    assert r.status_code == 503
    assert "npm run build" in r.content.decode()


def test_built_app_served_with_csrf_cookie(admin_client_ru, settings, tmp_path, no_cache):
    (tmp_path / "index.html").write_text('<div id="root"></div><script src="/static/app/assets/x.js"></script>')
    settings.STATICFILES_DIRS = [("app", tmp_path)]
    r = admin_client_ru.get("/app/?semester=1")
    assert r.status_code == 200 and b'id="root"' in r.content
    assert "csrftoken" in r.cookies


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r["Content-Type"] == "image/svg+xml"
