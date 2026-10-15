from django.contrib import admin
from django.urls import include, path

from api.routes import api
from scheduler import __version__  # видно, какая сборка установлена
from webapp.views import dispatcher_app, favicon

admin.site.site_header = f"Расписание: диспетчерская · версия {__version__}"
admin.site.site_title = "Расписание"
admin.site.index_title = "Справочники, нагрузка и варианты"
admin.site.site_url = "/app/"  # «Открыть сайт» в шапке админки → интерфейс диспетчера

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("app/", dispatcher_app, name="dispatcher_app"),
    path("favicon.ico", favicon),
    path("", include("portal.urls")),
]
