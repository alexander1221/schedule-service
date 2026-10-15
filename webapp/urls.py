from django.contrib import admin
from django.urls import include, path

from scheduler import __version__  # видно, какая сборка установлена

admin.site.site_header = f"Расписание: диспетчерская · версия {__version__}"
admin.site.site_title = "Расписание"
admin.site.index_title = "Справочники, нагрузка и варианты"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("portal.urls")),
]
